"""Scripted stand-ins for model providers: an httpx transport that answers each request from a
script, in Ollama's, OpenAI's or Anthropic's stream format, paced on the test's clock.

No network: the adapters under test are given an ``httpx.AsyncClient`` on this transport. Every
request is recorded (URL, headers, JSON body), and every stream records whether it was closed,
so cancellation can be checked. Test values, not secrets.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from gateway.clock import Clock


class PacedStream(httpx.AsyncByteStream):
    """Yields each chunk after its delay on the clock; ``fail_after`` breaks the connection."""

    def __init__(
        self, clock: Clock, chunks: list[tuple[float, bytes]], fail_after: int | None = None
    ) -> None:
        self._clock = clock
        self._chunks = chunks
        self._fail_after = fail_after
        self.sent = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for delay_ms, data in self._chunks:
            if self._fail_after is not None and self.sent >= self._fail_after:
                raise httpx.ReadError("connection reset")
            await self._clock.sleep_ms(delay_ms)
            self.sent += 1
            yield data

    async def aclose(self) -> None:
        self.closed = True


@dataclass
class Reply:
    """One scripted answer: a status, headers and paced chunks, or an exception to raise."""

    status: int = 200
    chunks: list[tuple[float, bytes]] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    raise_error: Exception | None = None
    fail_after: int | None = None


@dataclass
class Recorded:
    url: str
    headers: httpx.Headers
    body: dict[str, Any]


@dataclass
class ScriptedProvider:
    clock: Clock
    replies: list[Reply] = field(default_factory=list)
    requests: list[Recorded] = field(default_factory=list)
    streams: list[PacedStream] = field(default_factory=list)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.requests.append(Recorded(str(request.url), request.headers, body))
        reply = self.replies.pop(0) if self.replies else Reply()
        if reply.raise_error is not None:
            raise reply.raise_error
        stream = PacedStream(self.clock, reply.chunks, reply.fail_after)
        self.streams.append(stream)
        return httpx.Response(reply.status, headers=reply.headers, stream=stream)


# --- the three formats ---------------------------------------------------------------------


def ndjson(*lines: dict[str, Any], gap_ms: float = 10.0) -> list[tuple[float, bytes]]:
    return [(gap_ms, (json.dumps(line) + "\n").encode()) for line in lines]


def sse(*events: dict[str, Any] | str, gap_ms: float = 10.0) -> list[tuple[float, bytes]]:
    out = []
    for event in events:
        if isinstance(event, str):
            out.append((gap_ms, f"data: {event}\n\n".encode()))
        elif "event" in event:  # Anthropic: a named event
            name = event["event"]
            data = {k: v for k, v in event.items() if k != "event"}
            out.append((gap_ms, f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()))
        else:
            out.append((gap_ms, f"data: {json.dumps(event)}\n\n".encode()))
    return out


def ollama_text(*pieces: str, prompt_tokens: int = 7, output_tokens: int | None = None) -> Reply:
    lines: list[dict[str, Any]] = [{"message": {"role": "assistant", "content": p}} for p in pieces]
    done: dict[str, Any] = {"message": {"role": "assistant", "content": ""}, "done": True}
    done["done_reason"] = "stop"
    if output_tokens is not None or pieces:
        done["prompt_eval_count"] = prompt_tokens
        done["eval_count"] = output_tokens if output_tokens is not None else len(pieces)
    return Reply(chunks=ndjson(*lines, done))


def ollama_tools(*calls: tuple[str, dict[str, Any]]) -> Reply:
    message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"function": {"index": i, "name": name, "arguments": args}}
            for i, (name, args) in enumerate(calls)
        ],
    }
    done = {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop"}
    done.update({"prompt_eval_count": 40, "eval_count": 12})
    return Reply(chunks=ndjson({"message": message}, done))


def openai_text(*pieces: str, usage: bool = True) -> Reply:
    def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
        return {
            "id": "chatcmpl-x",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    events: list[dict[str, Any] | str] = [chunk({"role": "assistant", "content": ""})]
    events += [chunk({"content": p}) for p in pieces]
    events.append(chunk({}, "stop"))
    if usage:
        events.append(
            {
                "id": "chatcmpl-x",
                "choices": [],
                "usage": {"prompt_tokens": 9, "completion_tokens": len(pieces), "total_tokens": 0},
            }
        )
    events.append("[DONE]")
    return Reply(chunks=sse(*events))


def openai_tools(*calls: tuple[str, str, list[str]]) -> Reply:
    """Each call as (id, name, argument fragments)."""

    def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
        return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    events: list[dict[str, Any] | str] = [chunk({"role": "assistant", "content": None})]
    for i, (call_id, name, fragments) in enumerate(calls):
        events.append(
            chunk(
                {
                    "tool_calls": [
                        {
                            "index": i,
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": ""},
                        }
                    ]
                }
            )
        )
        for fragment in fragments:
            events.append(
                chunk({"tool_calls": [{"index": i, "function": {"arguments": fragment}}]})
            )
    events.append(chunk({}, "tool_calls"))
    events.append({"choices": [], "usage": {"prompt_tokens": 30, "completion_tokens": 8}})
    events.append("[DONE]")
    return Reply(chunks=sse(*events))


def anthropic_events(*blocks: dict[str, Any], stop: str = "end_turn") -> Reply:
    """Blocks: {"text": ...} | {"tool": (id, name, [fragments])} | {"thinking": True}."""
    events: list[dict[str, Any] | str] = [
        {
            "event": "message_start",
            "type": "message_start",
            "message": {"id": "msg_x", "usage": {"input_tokens": 25, "output_tokens": 1}},
        },
        {"event": "ping", "type": "ping"},
    ]
    for index, block in enumerate(blocks):
        start: dict[str, Any]
        deltas: list[dict[str, Any]]
        if "text" in block:
            start = {"type": "text", "text": ""}
            deltas = [{"type": "text_delta", "text": block["text"]}]
        elif "tool" in block:
            call_id, name, fragments = block["tool"]
            start = {"type": "tool_use", "id": call_id, "name": name, "input": {}}
            deltas = [{"type": "input_json_delta", "partial_json": f} for f in fragments]
        else:
            start = {"type": "thinking", "thinking": ""}
            deltas = [
                {"type": "thinking_delta", "thinking": ""},
                {"type": "signature_delta", "signature": "sig"},
            ]
        events.append(
            {
                "event": "content_block_start",
                "type": "content_block_start",
                "index": index,
                "content_block": start,
            }
        )
        for delta in deltas:
            events.append(
                {
                    "event": "content_block_delta",
                    "type": "content_block_delta",
                    "index": index,
                    "delta": delta,
                }
            )
        events.append({"event": "content_block_stop", "type": "content_block_stop", "index": index})
    events.append(
        {
            "event": "message_delta",
            "type": "message_delta",
            "delta": {"stop_reason": stop, "stop_sequence": None},
            "usage": {"output_tokens": 15},
        }
    )
    events.append({"event": "message_stop", "type": "message_stop"})
    return Reply(chunks=sse(*events))
