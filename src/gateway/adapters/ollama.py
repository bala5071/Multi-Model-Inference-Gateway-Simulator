"""Ollama's own chat API: ``POST {base}/api/chat``, a newline-delimited JSON stream.

See docs/31-chat-and-adapters.md. Operator-configured only (docs/provider-plan.md, section 4):
an organization never supplies an Ollama URL.

Ollama's tool calls have **no id** and their arguments are a JSON **object**; when streaming it
sends each call whole, in one chunk. So the adapter gives each call an id of its own
(``call_`` and 24 random hex characters). On the next turn, a ``tool`` message is matched to
its call by that id within the assistant message before it, which gives the call's position
and name; the results go to Ollama in the calls' order, each with its ``tool_name``, so the
same tool called twice in one turn gets each result back in its own place. Usage is
``prompt_eval_count`` and ``eval_count`` on the final (``done``) line.
"""

from __future__ import annotations

import json
from typing import Any

from gateway.adapters.base import HttpAdapter
from gateway.adapters.http import malformed, ndjson, open_stream
from gateway.chat import (
    ChatDelta,
    ChatDeltaCallback,
    ChatInputError,
    ChatMessage,
    ChatRequest,
    ChatResult,
    FinishReason,
    NamedTool,
    ProviderError,
    ProviderErrorCode,
    ToolCall,
    json_object,
)
from gateway.secret import Secret


def ollama_messages(messages: tuple[ChatMessage, ...]) -> list[dict[str, Any]]:
    """The conversation in Ollama's shape. Tool results are put in the order of the calls they
    answer, each named; a result that answers no call of the assistant message before it is a
    ``ChatInputError``."""
    out: list[dict[str, Any]] = []
    calls: dict[str, tuple[int, str]] = {}  # id -> (position, name), of the last assistant
    results: list[tuple[int, dict[str, Any]]] = []

    def flush() -> None:
        out.extend(message for _, message in sorted(results, key=lambda r: r[0]))
        results.clear()

    for index, message in enumerate(messages):
        if message.role == "tool":
            found = calls.get(message.tool_call_id or "")
            if found is None:
                raise ChatInputError(
                    "This tool message answers no tool call of the assistant message before it.",
                    param=f"messages[{index}].tool_call_id",
                )
            position, name = found
            results.append(
                (position, {"role": "tool", "tool_name": name, "content": message.content or ""})
            )
            continue
        flush()
        if message.role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": message.content or ""}
            if message.tool_calls:
                entry["tool_calls"] = [
                    {"function": {"name": c.name, "arguments": c.parsed_arguments()}}
                    for c in message.tool_calls
                ]
            calls = {c.id: (i, c.name) for i, c in enumerate(message.tool_calls)}
            out.append(entry)
        else:
            out.append({"role": message.role, "content": message.content or ""})
    flush()
    return out


class OllamaAdapter(HttpAdapter):
    @property
    def url(self) -> str:
        return self.base_url + "/api/chat"

    def payload(self, request: ChatRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": ollama_messages(request.messages),
            "stream": True,
        }
        options: dict[str, Any] = {}
        if request.temperature is not None:
            options["temperature"] = request.temperature
        if request.max_tokens is not None:
            options["num_predict"] = request.max_tokens
        if options:
            payload["options"] = options
        if request.tools and request.tool_choice != "none":
            if request.tool_choice == "required" or isinstance(request.tool_choice, NamedTool):
                # Ollama cannot be told to call a tool.
                raise ProviderError(
                    ProviderErrorCode.TOOLS_NOT_SUPPORTED, detail="tool_choice_not_supported"
                )
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
        return payload

    def tool_call(self, raw: Any) -> ToolCall:
        function = raw.get("function") if isinstance(raw, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        arguments = function.get("arguments") if isinstance(function, dict) else None
        if not isinstance(name, str) or not name:
            raise malformed("tool_call")
        if isinstance(arguments, str):
            parsed = json_object(arguments)
        else:
            parsed = arguments if isinstance(arguments, dict) else None
        if parsed is None:
            raise malformed("tool_arguments")
        return ToolCall(self.new_tool_call_id(), name, json.dumps(parsed))

    async def chat(
        self,
        request: ChatRequest,
        *,
        credential: Secret | None = None,
        on_delta: ChatDeltaCallback | None = None,
    ) -> ChatResult:
        payload = self.payload(request)
        content: list[str] = []
        calls: list[ToolCall] = []
        final: dict[str, Any] | None = None
        async with open_stream(self._client, self.url, payload, self._headers) as response:
            async for line in ndjson(response):
                if "error" in line:
                    raise ProviderError(
                        ProviderErrorCode.UPSTREAM_UNAVAILABLE, detail="stream_error"
                    )
                message = line.get("message")
                if isinstance(message, dict):
                    text = message.get("content")
                    if isinstance(text, str) and text:
                        content.append(text)
                        if on_delta is not None:
                            on_delta(ChatDelta(text=text))
                    raw_calls = message.get("tool_calls")
                    if raw_calls is not None:
                        if not isinstance(raw_calls, list):
                            raise malformed("tool_calls")
                        for raw in raw_calls:
                            call = self.tool_call(raw)
                            calls.append(call)
                            if on_delta is not None:
                                on_delta(ChatDelta(tool_call=call))
                if line.get("done") is True:
                    final = line
                    break
        if final is None:
            raise malformed("ended_early")
        finish: FinishReason = (
            "tool_calls" if calls else "length" if final.get("done_reason") == "length" else "stop"
        )
        text = "".join(content)
        return ChatResult(
            text,
            tuple(calls),
            finish,
            self.usage(
                request,
                final.get("prompt_eval_count"),
                final.get("eval_count"),
                text,
                "".join(c.arguments for c in calls),
            ),
        )
