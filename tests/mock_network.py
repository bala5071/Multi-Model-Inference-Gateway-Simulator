"""A network for the egress guard to connect through, in tests: no sockets.

``ScriptedNetwork`` is an ``httpcore`` network backend. Each connection gets the next scripted
item: a raw HTTP response; an exception raised on connecting (a refused connection, a connect
timeout); ``Breaks`` (part of an answer, then the connection breaks); ``Pauses`` (part of an
answer, a wait for the test, then the rest); ``HANG`` (a server that
accepts and never answers: on a ``SimClock`` the loop goes idle and time moves to the next
timeout); or a ``Gate`` that waits for the test before going on. For the webhook stages (docs/46)
also ``CONNECT_HANGS``, ``TLS_HANGS``, ``TLS_FAILS``, ``WRITE_HANGS`` and ``Trickles`` (an answer
sent a piece at a time on a clock). It records the address the guard connected to, the TLS host
name and context, every byte written (the request, headers included), and how much of the
answer was read.
A fake resolver answers names from a table, and can change its answer between calls (DNS
rebinding). Test values, not secrets.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import httpcore

from gateway.clock import Clock


def http_response(body: bytes, status: str = "200 OK", **headers: str) -> bytes:
    lines = [f"HTTP/1.1 {status}", "Connection: close"]
    lines += [f"{k.replace('_', '-')}: {v}" for k, v in headers.items()]
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def sse_response(chunks: Iterable[tuple[float, bytes]]) -> bytes:
    """A 200 server-sent event response from mock_providers' chunks."""
    return http_response(b"".join(data for _, data in chunks), Content_Type="text/event-stream")


@dataclass
class Connection:
    address: str
    port: int
    tls_host: str | None = None
    written: bytearray = field(default_factory=bytearray)
    tls_context: Any = None
    # Bytes of the answer handed to the client so far.
    read: int = 0


class RecordingStream(httpcore.AsyncMockStream):
    def __init__(self, buffer: list[bytes], connection: Connection) -> None:
        super().__init__(buffer)
        self._connection = connection

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:  # noqa: ASYNC109
        data = await super().read(max_bytes, timeout)
        self._connection.read += len(data)
        return data

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:  # noqa: ASYNC109
        self._connection.written += buffer

    async def start_tls(
        self,
        ssl_context: Any,
        server_hostname: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> httpcore.AsyncNetworkStream:
        self._connection.tls_host = server_hostname
        self._connection.tls_context = ssl_context
        return self


async def _forever() -> None:
    await asyncio.get_running_loop().create_future()  # never set: only cancelled


class TlsHangingStream(RecordingStream):
    async def start_tls(
        self,
        ssl_context: Any,
        server_hostname: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> httpcore.AsyncNetworkStream:
        await _forever()
        return self


class TlsFailingStream(RecordingStream):
    """The handshake fails, as httpcore reports an ``ssl`` error (``ConnectError``), with a
    message that must never be kept."""

    def __init__(self, connection: Connection, message: str) -> None:
        super().__init__([], connection)
        self._message = message

    async def start_tls(
        self,
        ssl_context: Any,
        server_hostname: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> httpcore.AsyncNetworkStream:
        self._connection.tls_context = ssl_context
        raise httpcore.ConnectError(self._message)


class WriteHangingStream(RecordingStream):
    async def write(self, buffer: bytes, timeout: float | None = None) -> None:  # noqa: ASYNC109
        await _forever()


class TricklingStream(RecordingStream):
    """Each piece after ``every_ms`` on ``clock``."""

    def __init__(
        self, pieces: list[bytes], connection: Connection, clock: Clock, every_ms: float
    ) -> None:
        super().__init__(pieces, connection)
        self._clock = clock
        self._every_ms = every_ms

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:  # noqa: ASYNC109
        await self._clock.sleep_ms(self._every_ms)
        return await super().read(max_bytes, timeout)


class HangingStream(RecordingStream):
    """Accepts the request and never answers."""

    def __init__(self, connection: Connection) -> None:
        super().__init__([], connection)

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:  # noqa: ASYNC109
        await asyncio.get_running_loop().create_future()  # never set: only cancelled
        return b""


class BreakingStream(RecordingStream):
    """Sends what it was given, then the connection breaks."""

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:  # noqa: ASYNC109
        data = await super().read(max_bytes, timeout)
        if not data:
            raise httpcore.ReadError("connection reset")
        return data


HANG = object()
CONNECT_HANGS = object()
TLS_HANGS = object()
WRITE_HANGS = object()


@dataclass(frozen=True)
class TlsFails:
    """The handshake fails; ``message`` is what the error says (a test plants a URL in it)."""

    message: str = "certificate verify failed"


@dataclass(frozen=True)
class Trickles:
    """An answer sent a piece at a time, one every ``every_ms`` on ``clock``."""

    pieces: tuple[bytes, ...]
    clock: Clock
    every_ms: float


@dataclass(frozen=True)
class Breaks:
    """A response whose connection breaks after ``data`` (output already sent)."""

    data: bytes


@dataclass
class Pauses:
    """A response that sends ``before`` (output on its way to the client), waits until the test
    resumes it, then sends ``after``. ``paused`` is set once ``before`` has been read whole."""

    before: bytes
    after: bytes
    paused: asyncio.Event = field(default_factory=asyncio.Event)
    resumed: asyncio.Event = field(default_factory=asyncio.Event)


class PausingStream(RecordingStream):
    def __init__(self, item: Pauses, connection: Connection) -> None:
        super().__init__([item.before, item.after], connection)
        self._item = item
        self._reads = 0

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:  # noqa: ASYNC109
        if self._reads == 1:
            # Asked for more: everything in ``before`` has been parsed and passed on.
            self._item.paused.set()
            await self._item.resumed.wait()
        self._reads += 1
        return await super().read(max_bytes, timeout)


@dataclass
class Gate:
    """Waits at the connection until the test opens it, then goes on as ``then``."""

    then: bytes | Exception | object
    reached: asyncio.Event = field(default_factory=asyncio.Event)
    opened: asyncio.Event = field(default_factory=asyncio.Event)


Scripted = bytes | Exception | Gate | Breaks | Pauses | TlsFails | Trickles | object


@dataclass
class ScriptedNetwork(httpcore.AsyncNetworkBackend):
    responses: list[Scripted] = field(default_factory=list)
    connections: list[Connection] = field(default_factory=list)
    # Raised on connecting, when set: a refused connection, a connect timeout.
    failure: Exception | None = None

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        connection = Connection(host, port)
        self.connections.append(connection)
        if self.failure is not None:
            raise self.failure
        if not self.responses:
            raise httpcore.ConnectError("nothing scripted")
        item = self.responses.pop(0)
        if isinstance(item, Gate):
            item.reached.set()
            await item.opened.wait()
            item = item.then
        if isinstance(item, Exception):
            raise item
        if item is CONNECT_HANGS:
            await _forever()
        if item is TLS_HANGS:
            return TlsHangingStream([], connection)
        if item is WRITE_HANGS:
            return WriteHangingStream([], connection)
        if isinstance(item, TlsFails):
            return TlsFailingStream(connection, item.message)
        if isinstance(item, Trickles):
            return TricklingStream(list(item.pieces), connection, item.clock, item.every_ms)
        if item is HANG:
            return HangingStream(connection)
        if isinstance(item, Breaks):
            return BreakingStream([item.data], connection)
        if isinstance(item, Pauses):
            return PausingStream(item, connection)
        assert isinstance(item, bytes)
        return RecordingStream([item[i : i + 512] for i in range(0, len(item), 512)], connection)

    async def sleep(self, seconds: float) -> None:
        return None


@dataclass
class FakeResolver:
    """Answers each name from ``table``; ``then`` gives the answers for later calls (a name
    that resolves differently the second time)."""

    table: dict[str, list[str]]
    then: dict[str, list[str]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    # Names that do not resolve: the system resolver's error for them.
    missing: set[str] = field(default_factory=set)

    async def __call__(self, host: str, port: int) -> list[str]:
        self.calls.append(host)
        if host in self.missing:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        if host in self.then and self.calls.count(host) > 1:
            return list(self.then[host])
        return list(self.table.get(host, []))
