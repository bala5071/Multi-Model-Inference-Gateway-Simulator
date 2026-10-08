"""A stand-in for llama-server: an httpx transport that streams OpenAI-style chunks.

Chunks are paced on the test's clock, so a ``SimClock`` gives exact token timestamps.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from gateway.clock import Clock


class PacedStream(httpx.AsyncByteStream):
    """Yields each chunk after waiting its delay on the clock."""

    def __init__(self, clock: Clock, chunks: list[tuple[float, bytes]]) -> None:
        self._clock = clock
        self._chunks = chunks
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for delay_ms, data in self._chunks:
            await self._clock.sleep_ms(delay_ms)
            yield data

    async def aclose(self) -> None:
        self.closed = True


def sse(payload: dict[str, Any] | str) -> bytes:
    data = payload if isinstance(payload, str) else json.dumps(payload)
    return f"data: {data}\n\n".encode()


@dataclass
class MockLlamaServer:
    """What the fake server says, and a record of what it was asked.

    ``down`` lists ports that refuse connections; ``status`` maps ports to an HTTP error.
    """

    clock: Clock
    pieces: tuple[str, ...] = ("Hello", " world", "!")
    prompt_tokens: int = 7
    first_token_delay_ms: float = 50.0
    token_gap_ms: float = 10.0
    report_usage: bool = True
    down: set[int] = field(default_factory=set)
    status: dict[int, int] = field(default_factory=dict)
    # Break the connection after this many pieces, if set.
    drop_after: int | None = None
    requests: list[httpx.Request] = field(default_factory=list)
    bodies: list[dict[str, Any]] = field(default_factory=list)
    streams: list[PacedStream] = field(default_factory=list)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _piece_chunk(self, chat: bool, text: str) -> bytes:
        if chat:
            choice: dict[str, Any] = {"index": 0, "delta": {"content": text}}
        else:
            choice = {"index": 0, "text": text}
        return sse({"choices": [{**choice, "finish_reason": None}]})

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        port = request.url.port or 80
        if port in self.down:
            raise httpx.ConnectError("All connection attempts failed", request=request)
        self.bodies.append(json.loads(request.content))
        if port in self.status:
            return httpx.Response(self.status[port], json={"error": {"message": "Loading model"}})

        chat = request.url.path.endswith("/chat/completions")
        chunks: list[tuple[float, bytes]] = []
        if chat:
            # Real servers open a chat stream with a role-only delta and no content.
            chunks.append((0.0, sse({"choices": [{"index": 0, "delta": {"role": "assistant"}}]})))
        for i, text in enumerate(self.pieces):
            if self.drop_after is not None and i == self.drop_after:
                break
            delay = self.first_token_delay_ms if i == 0 else self.token_gap_ms
            chunks.append((delay, self._piece_chunk(chat, text)))
        stream: PacedStream
        if self.drop_after is not None:
            stream = _DroppingStream(self.clock, chunks, request)
        else:
            # Keep-alive comments and the usage-only final chunk, as llama-server sends them.
            chunks.append((0.0, b": keep-alive\n\n"))
            if self.report_usage:
                usage = {
                    "prompt_tokens": self.prompt_tokens,
                    "completion_tokens": len(self.pieces),
                    "total_tokens": self.prompt_tokens + len(self.pieces),
                }
                chunks.append((0.0, sse({"choices": [], "usage": usage})))
            chunks.append((0.0, sse("[DONE]")))
            stream = PacedStream(self.clock, chunks)
        self.streams.append(stream)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)


class _GroundTruthStream(httpx.AsyncByteStream):
    """One response of ``GroundTruthServer``, timed by its formulas."""

    def __init__(self, server: GroundTruthServer, prompt_tokens: int, tokens: int) -> None:
        self._server = server
        self._prompt_tokens = prompt_tokens
        self._tokens = tokens
        self._counted = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        server = self._server
        server.active += 1
        self._counted = True
        await server.clock.sleep_ms(
            server.overhead_ms + server.prefill_ms_per_token * self._prompt_tokens
        )
        for i in range(self._tokens):
            if i:
                await server.clock.sleep_ms(
                    server.decode_base_ms + server.decode_ms_per_extra_seq * (server.active - 1)
                )
            yield sse({"choices": [{"index": 0, "text": f" t{i}"}]})
        usage = {"prompt_tokens": self._prompt_tokens, "completion_tokens": self._tokens}
        yield sse({"choices": [], "usage": usage})
        yield sse("[DONE]")

    async def aclose(self) -> None:
        if self._counted:
            self._counted = False
            self._server.active -= 1


@dataclass
class GroundTruthServer:
    """A fake model server whose timing follows known parameters exactly.

    Time to first token is ``overhead_ms + prefill_ms_per_token * prompt tokens``; after
    that a token arrives every ``decode_base_ms + decode_ms_per_extra_seq * (others)``,
    where ``others`` is how many other requests are being served at that moment. One
    prompt word is one token and every request produces exactly ``max_tokens`` tokens.
    Calibrating against it must recover those parameters.
    """

    clock: Clock
    prefill_ms_per_token: float
    decode_base_ms: float
    decode_ms_per_extra_seq: float
    overhead_ms: float = 0.0
    active: int = 0
    requests: int = 0

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(404)  # not Ollama, not llama-server: describes as unknown
        self.requests += 1
        body = json.loads(request.content)
        stream = _GroundTruthStream(self, len(body["prompt"].split()), body["max_tokens"])
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)


class _DroppingStream(PacedStream):
    """Sends its chunks, then fails as a broken connection would."""

    def __init__(
        self, clock: Clock, chunks: list[tuple[float, bytes]], request: httpx.Request
    ) -> None:
        super().__init__(clock, chunks)
        self._request = request

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for data in super().__aiter__():
            yield data
        raise httpx.ReadError("connection reset", request=self._request)
