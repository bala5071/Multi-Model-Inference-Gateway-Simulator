"""The circuit breakers' reports, sent off the request path (docs/41, scale-out PR 2b).

``admit`` stays on the path: it decides whether a provider is tried. The reports (``success``,
``failure``, ``release``) of an attempt a closed breaker admitted go into a bounded queue of
this instance's, sent in the order they were made by one sender; a request no longer waits for
them. **A half-open probe's report is sent at once, on the path, never queued**, after any
report of the same breaker still queued, so a probe slot is never held by a report waiting in,
or dropped from, the queue.

A report that does not fit, that the shutdown flush has no time left for, or that the counters
cannot take (Valkey unavailable), is dropped and counted by reason. A dropped report never
admits anything: it can make a breaker open late (a failure lost) or early (a success lost, so
the run of failures is not reset), refusing a provider for at most its open time. Nothing
decides anything from the counts: monitoring is told them.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from enum import StrEnum

from gateway.breaker import BreakerStore
from gateway.clock import Clock
from gateway.overload import RequestTimeoutError, run_with_timeout

# How many reports wait at most: far more than the attempts of the requests one instance has
# in flight, so a drop means the sender has stalled (Valkey slow), not ordinary traffic.
DEFAULT_QUEUE_SIZE = 1024
# How long the shutdown flush may take before what is left is dropped and counted.
DEFAULT_FLUSH_MS = 2_000.0


class Report(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    RELEASE = "release"


class DropReason(StrEnum):
    QUEUE_FULL = "queue_full"
    FLUSH_TIMEOUT = "flush_timeout"
    UNAVAILABLE = "unavailable"


Item = tuple[Report, str, str]


class BreakerReports:
    """Sends ``store``'s reports: probes at once, the others through the queue."""

    def __init__(
        self,
        store: BreakerStore,
        clock: Clock,
        *,
        size: int = DEFAULT_QUEUE_SIZE,
        on_drop: Callable[[DropReason], None] | None = None,
    ) -> None:
        if size < 1:
            raise ValueError("the report queue needs room for at least one report")
        self._store = store
        self._clock = clock
        self._size = size
        self._on_drop = on_drop
        self._queue: deque[Item] = deque()
        self._sender: asyncio.Task[None] | None = None
        # The report the sender is writing now: counted if the shutdown flush cuts it off.
        self._sending: Item | None = None
        self._closed = False
        self.dropped: dict[DropReason, int] = dict.fromkeys(DropReason, 0)

    @property
    def queued(self) -> int:
        return len(self._queue)

    def _drop(self, reason: DropReason, count: int = 1) -> None:
        self.dropped[reason] += count
        if self._on_drop is not None:
            for _ in range(count):
                self._on_drop(reason)

    async def _send(self, item: Item) -> None:
        report, organization_id, provider_id = item
        written = await getattr(self._store, report.value)(organization_id, provider_id)
        if not written:
            self._drop(DropReason.UNAVAILABLE)

    def enqueue(self, report: Report, organization_id: str, provider_id: str) -> None:
        """A closed breaker's report, sent after the request without it waiting."""
        if self._closed or len(self._queue) >= self._size:
            self._drop(DropReason.QUEUE_FULL)
            return
        self._queue.append((report, organization_id, provider_id))
        if self._sender is None or self._sender.done():
            self._sender = asyncio.ensure_future(self._drain())

    async def send_now(self, report: Report, organization_id: str, provider_id: str) -> None:
        """A half-open probe's report, on the path. The same breaker's reports still queued go
        first, in their order, so the breaker sees every report in the order it was made."""
        key = (organization_id, provider_id)
        earlier = [item for item in self._queue if item[1:] == key]
        if earlier:
            self._queue = deque(item for item in self._queue if item[1:] != key)
            for item in earlier:
                await self._send(item)
        await self._send((report, organization_id, provider_id))

    async def _drain(self) -> None:
        while self._queue:
            self._sending = self._queue.popleft()
            await self._send(self._sending)
            self._sending = None

    async def drained(self) -> None:
        """Wait until every queued report is sent (tests; the flush at shutdown)."""
        while self._sender is not None and not self._sender.done():
            await self._sender

    async def _send_all(self) -> None:
        if self._sender is not None and not self._sender.done():
            await self._sender
        await self._drain()

    async def flush(self, limit_ms: float = DEFAULT_FLUSH_MS) -> int:
        """At shutdown, before the counters' client closes: send what is queued within
        ``limit_ms`` on the injected clock, then drop and count the rest, a report cut off
        while being written included. Nothing is queued afterwards. Returns how many were
        dropped at the flush."""
        self._closed = True
        before = self.dropped[DropReason.FLUSH_TIMEOUT]
        try:
            await run_with_timeout(self._clock, self._send_all(), limit_ms)
        except RequestTimeoutError:
            if self._sender is not None and not self._sender.done():
                self._sender.cancel()
                await asyncio.gather(self._sender, return_exceptions=True)
            left = len(self._queue) + (1 if self._sending is not None else 0)
            self._queue.clear()
            self._sending = None
            if left:
                self._drop(DropReason.FLUSH_TIMEOUT, left)
        return self.dropped[DropReason.FLUSH_TIMEOUT] - before
