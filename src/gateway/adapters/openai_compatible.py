"""Any endpoint in the OpenAI chat-completions format: ``POST {base_url}/chat/completions``,
server-sent events ending in ``data: [DONE]``.

See docs/31-chat-and-adapters.md. ``base_url`` includes the version path (for example
``https://api.example.com/v1``); ``model``, fixed headers and the connect timeout come from the
configuration, and the key from the caller of each request (``Authorization: Bearer``).

Tool calls arrive in fragments: the first carries the call's ``index``, ``id`` and name, later
ones more of its ``arguments`` text. Each call is put together by index and emitted once, when
the answer finishes, with arguments checked to be a JSON object.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from gateway.adapters.base import HttpAdapter
from gateway.adapters.http import credential_headers, malformed, open_stream, server_sent_events
from gateway.adapters.http import sse_object as parse
from gateway.chat import (
    ChatDelta,
    ChatDeltaCallback,
    ChatMessage,
    ChatRequest,
    ChatResult,
    FinishReason,
    NamedTool,
    ToolCall,
    json_object,
)
from gateway.secret import Secret


def openai_messages(messages: tuple[ChatMessage, ...]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        entry: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.tool_calls:
            entry["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": c.arguments},
                }
                for c in message.tool_calls
            ]
        if message.tool_call_id is not None:
            entry["tool_call_id"] = message.tool_call_id
        out.append(entry)
    return out


@dataclass
class _Fragments:
    id: str = ""
    name: str = ""
    arguments: list[str] = field(default_factory=list)


class OpenAICompatibleAdapter(HttpAdapter):
    @property
    def url(self) -> str:
        return self.base_url + "/chat/completions"

    def payload(self, request: ChatRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": openai_messages(request.messages),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": dict(t.parameters),
                    },
                }
                for t in request.tools
            ]
            choice = request.tool_choice
            payload["tool_choice"] = (
                {"type": "function", "function": {"name": choice.name}}
                if isinstance(choice, NamedTool)
                else choice
            )
        return payload

    async def chat(
        self,
        request: ChatRequest,
        *,
        credential: Secret | None = None,
        on_delta: ChatDeltaCallback | None = None,
    ) -> ChatResult:
        payload = self.payload(request)
        headers = {**self._headers, **credential_headers(credential, "bearer")}
        content: list[str] = []
        fragments: dict[int, _Fragments] = {}
        finish_reason: str | None = None
        usage: dict[str, Any] = {}
        done = False
        async with open_stream(self._client, self.url, payload, headers) as response:
            async for _, data in server_sent_events(response):
                if data.strip() == "[DONE]":
                    done = True
                    break
                chunk = parse(data)
                if "error" in chunk:
                    raise malformed("stream_error")
                if isinstance(chunk.get("usage"), dict):
                    reported = chunk["usage"]
                    # Some servers attach an all-zero usage to every chunk and the real one
                    # only to the last.
                    if reported.get("completion_tokens") or reported.get("prompt_tokens"):
                        usage = reported
                choices = chunk.get("choices")
                if not isinstance(choices, list):
                    raise malformed("choices")
                if not choices:
                    continue  # the usage chunk
                choice = choices[0]
                if not isinstance(choice, dict):
                    raise malformed("choices")
                delta = choice.get("delta") or {}
                if not isinstance(delta, dict):
                    raise malformed("delta")
                text = delta.get("content")
                if isinstance(text, str) and text:
                    content.append(text)
                    if on_delta is not None:
                        on_delta(ChatDelta(text=text))
                for raw in delta.get("tool_calls") or []:
                    if not isinstance(raw, dict) or not isinstance(raw.get("index"), int):
                        raise malformed("tool_call")
                    part = fragments.setdefault(raw["index"], _Fragments())
                    part.id = raw.get("id") or part.id
                    function = raw.get("function") or {}
                    if not isinstance(function, dict):
                        raise malformed("tool_call")
                    part.name = function.get("name") or part.name
                    if isinstance(function.get("arguments"), str):
                        part.arguments.append(function["arguments"])
                if choice.get("finish_reason"):
                    finish_reason = str(choice["finish_reason"])
        if not done and finish_reason is None:
            raise malformed("ended_early")
        calls = []
        for index in sorted(fragments):
            part = fragments[index]
            arguments = "".join(part.arguments) or "{}"
            if not part.name or json_object(arguments) is None:
                raise malformed("tool_arguments")
            call = ToolCall(part.id or self.new_tool_call_id(), part.name, arguments)
            calls.append(call)
            if on_delta is not None:
                on_delta(ChatDelta(tool_call=call))
        finish: FinishReason = (
            "tool_calls" if calls else "length" if finish_reason == "length" else "stop"
        )
        text = "".join(content)
        return ChatResult(
            text,
            tuple(calls),
            finish,
            self.usage(
                request,
                usage.get("prompt_tokens"),
                usage.get("completion_tokens"),
                text,
                "".join(c.arguments for c in calls),
            ),
        )
