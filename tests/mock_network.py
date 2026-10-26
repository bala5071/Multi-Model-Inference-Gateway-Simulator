"""A network for the egress guard to connect through, in tests: no sockets.

``ScriptedNetwork`` is an ``httpcore`` network backend. Each connection gets the next scripted
raw HTTP response; it records the address the guard connected to, the TLS host name, and every
byte written (the request, headers included). A fake resolver answers names from a table, and
can change its answer between calls (DNS rebinding). Test values, not secrets.
"""

from __future__ import annotations

import socket
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import httpcore


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


class RecordingStream(httpcore.AsyncMockStream):
    def __init__(self, buffer: list[bytes], connection: Connection) -> None:
        super().__init__(buffer)
        self._connection = connection

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:  # noqa: ASYNC109
        self._connection.written += buffer

    async def start_tls(
        self,
        ssl_context: Any,
        server_hostname: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> httpcore.AsyncNetworkStream:
        self._connection.tls_host = server_hostname
        return self


@dataclass
class ScriptedNetwork(httpcore.AsyncNetworkBackend):
    responses: list[bytes] = field(default_factory=list)
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
        response = self.responses.pop(0)
        return RecordingStream(
            [response[i : i + 512] for i in range(0, len(response), 512)], connection
        )

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
