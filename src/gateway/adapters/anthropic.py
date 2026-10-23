"""Anthropic's Messages API, natively: ``POST {base}/v1/messages`` with ``x-api-key`` and
``anthropic-version: 2023-06-01``, streamed as named server-sent events.

See docs/31-chat-and-adapters.md (checked against Anthropic's Messages, streaming and errors
documentation on 2026-10-22).

- System messages go to the top-level ``system``; tool results go back as ``tool_result``
  blocks in a user message; consecutive messages of one role are merged; ``max_tokens`` is
  required (``default_max_tokens`` when the request has none).
- Usage: ``input_tokens`` in ``message_start``, ``output_tokens`` in ``message_delta``.
- A tool call's input arrives as ``input_json_delta`` fragments; the call is emitted at its
  ``content_block_stop``, its input checked to be a JSON object.
- Errors can arrive in the stream (``event: error``); they are mapped like HTTP errors.
- **Thinking blocks are never dropped silently.** ``thinking`` (from the configuration, not
  sent unless set: sending ``{"type": "disabled"}`` to a model that cannot turn thinking off is
  itself refused) asks for thinking off where the model allows it. An answer with thinking
  blocks *and* a tool call needs those blocks sent back with the tool result, which the
  OpenAI-compatible format cannot carry: it is refused as ``tools_not_supported``
  (``thinking_blocks_required``) and the adapter records ``supports_tools: false`` with that
  reason. In an answer without a tool call, thinking blocks are counted in
  ``thinking_blocks_omitted``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
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
    ProviderError,
    ProviderErrorCode,
    Source,
    ToolCall,
    json_object,
)
from gateway.secret import Secret

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 1024
THINKING_BLOCKS = frozenset({"thinking", "redacted_thinking"})
# Error types in the stream, mapped like the HTTP statuses they stand for.
STREAM_ERRORS: Mapping[str, ProviderErrorCode] = {
    "authentication_error": ProviderErrorCode.PROVIDER_AUTH_FAILED,
    "permission_error": ProviderErrorCode.PROVIDER_AUTH_FAILED,
    "rate_limit_error": ProviderErrorCode.UPSTREAM_RATE_LIMITED,
    "overloaded_error": ProviderErrorCode.UPSTREAM_UNAVAILABLE,
    "api_error": ProviderErrorCode.UPSTREAM_UNAVAILABLE,
    "timeout_error": ProviderErrorCode.UPSTREAM_TIMEOUT,
}
STOP_REASONS: Mapping[str, FinishReason] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
}


def anthropic_messages(
    messages: tuple[ChatMessage, ...],
) -> tuple[str | None, list[dict[str, Any]]]:
    """(system prompt, messages) in Anthropic's shape, consecutive roles merged."""
    system = [m.content or "" for m in messages if m.role == "system"]
    out: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            continue
        blocks: list[dict[str, Any]] = []
        if message.role == "tool":
            role = "user"
            blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id,
                    "content": message.content or "",
                }
            )
        else:
            role = message.role
            if message.content:
                blocks.append({"type": "text", "text": message.content})
            for call in message.tool_calls:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": call.parsed_arguments(),
                    }
                )
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})
    return ("\n\n".join(system) if system else None), out


class AnthropicAdapter(HttpAdapter):
    def __init__(
        self,
        *,
        default_max_tokens: int = DEFAULT_MAX_TOKENS,
        thinking: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._default_max_tokens = default_max_tokens
        self._thinking = dict(thinking) if thinking is not None else None

    @property
    def url(self) -> str:
        return self.base_url + "/v1/messages"

    def payload(self, request: ChatRequest) -> dict[str, Any]:
        system, messages = anthropic_messages(request.messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_tokens or self._default_max_tokens,
            "messages": messages,
            "stream": True,
        }
        if system is not None:
            payload["system"] = system
        if request.temperature is not None:
            # Anthropic's range is 0 to 1; the OpenAI format allows up to 2.
            payload["temperature"] = min(request.temperature, 1.0)
        if self._thinking is not None:
            payload["thinking"] = self._thinking
        if request.tools:
            payload["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": dict(t.parameters)}
                for t in request.tools
            ]
            choice = request.tool_choice
            payload["tool_choice"] = (
                {"type": "tool", "name": choice.name}
                if isinstance(choice, NamedTool)
                else {"type": {"auto": "auto", "none": "none", "required": "any"}[choice]}
            )
        return payload

    def _refuse_tools(self) -> ProviderError:
        """This model needs its thinking blocks sent back with tool results: record it, and
        refuse."""
        capabilities = self.capabilities()
        self._capabilities = replace(
            capabilities,
            supports_tools=False,
            sources={**capabilities.sources, "supports_tools": Source.PROBED},
            reasons={**capabilities.reasons, "supports_tools": "thinking_blocks_required"},
        )
        return ProviderError(
            ProviderErrorCode.TOOLS_NOT_SUPPORTED, detail="thinking_blocks_required"
        )

    async def chat(
        self,
        request: ChatRequest,
        *,
        credential: Secret | None = None,
        on_delta: ChatDeltaCallback | None = None,
    ) -> ChatResult:
        payload = self.payload(request)
        headers = {
            **self._headers,
            "anthropic-version": ANTHROPIC_VERSION,
            **credential_headers(credential, "x-api-key"),
        }
        content: list[str] = []
        calls: list[ToolCall] = []
        blocks: dict[int, dict[str, Any]] = {}
        thinking_blocks = 0
        input_tokens: Any = None
        output_tokens: Any = None
        stop_reason: str | None = None
        stopped = False
        async with open_stream(self._client, self.url, payload, headers) as response:
            async for event, data in server_sent_events(response):
                body = parse(data)
                kind = body.get("type", event)
                if kind == "ping":
                    continue
                if kind == "error":
                    error = body.get("error")
                    error_kind = error.get("type") if isinstance(error, dict) else None
                    code = STREAM_ERRORS.get(str(error_kind), ProviderErrorCode.UPSTREAM_MALFORMED)
                    raise ProviderError(code, detail="stream_error")
                if kind == "message_start":
                    message = body.get("message")
                    usage = message.get("usage") if isinstance(message, dict) else None
                    if isinstance(usage, dict):
                        input_tokens = usage.get("input_tokens")
                elif kind == "content_block_start":
                    block = body.get("content_block")
                    index = body.get("index")
                    if not isinstance(block, dict) or not isinstance(index, int):
                        raise malformed("content_block")
                    if block.get("type") in THINKING_BLOCKS:
                        thinking_blocks += 1
                    elif block.get("type") == "tool_use":
                        if thinking_blocks:
                            raise self._refuse_tools()
                        if not isinstance(block.get("id"), str) or not block.get("name"):
                            raise malformed("tool_use")
                        blocks[index] = {"id": block["id"], "name": block["name"], "json": []}
                elif kind == "content_block_delta":
                    delta = body.get("delta")
                    if not isinstance(delta, dict):
                        raise malformed("delta")
                    if delta.get("type") == "text_delta":
                        text = delta.get("text")
                        if isinstance(text, str) and text:
                            content.append(text)
                            if on_delta is not None:
                                on_delta(ChatDelta(text=text))
                    elif delta.get("type") == "input_json_delta":
                        open_block = blocks.get(body.get("index", -1))
                        if open_block is None:
                            raise malformed("input_json_delta")
                        open_block["json"].append(str(delta.get("partial_json", "")))
                elif kind == "content_block_stop":
                    closed = blocks.pop(body.get("index", -1), None)
                    if closed is not None:
                        arguments = "".join(closed["json"]) or "{}"
                        if json_object(arguments) is None:
                            raise malformed("tool_arguments")
                        normalized = json.dumps(json.loads(arguments))
                        call = ToolCall(closed["id"], closed["name"], normalized)
                        calls.append(call)
                        if on_delta is not None:
                            on_delta(ChatDelta(tool_call=call))
                elif kind == "message_delta":
                    delta = body.get("delta")
                    if isinstance(delta, dict) and delta.get("stop_reason"):
                        stop_reason = str(delta["stop_reason"])
                    usage = body.get("usage")
                    if isinstance(usage, dict) and "output_tokens" in usage:
                        output_tokens = usage["output_tokens"]
                elif kind == "message_stop":
                    stopped = True
                    break
        if not stopped:
            raise malformed("ended_early")
        finish = STOP_REASONS.get(stop_reason or "", "stop")
        if calls:
            finish = "tool_calls"
        text = "".join(content)
        return ChatResult(
            text,
            tuple(calls),
            finish,
            self.usage(
                request, input_tokens, output_tokens, text, "".join(c.arguments for c in calls)
            ),
            thinking_blocks_omitted=thinking_blocks,
        )
