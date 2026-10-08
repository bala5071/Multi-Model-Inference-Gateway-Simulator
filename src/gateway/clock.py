"""Clocks. This is the only module allowed to touch real time (see CLAUDE.md).

All times are in milliseconds, matching the cost model.
"""

from __future__ import annotations

import asyncio
import heapq
import time
from collections.abc import Sized
from typing import Protocol


class Clock(Protocol):
    def now_ms(self) -> float: ...

    async def sleep_ms(self, duration_ms: float) -> None: ...


def _check_duration(duration_ms: float) -> None:
    if duration_ms < 0:
        raise ValueError("duration_ms must not be negative")


class WallClock:
    """Real time, for the server."""

    def now_ms(self) -> float:
        # perf_counter, not monotonic: on Windows monotonic only ticks every ~16 ms, which
        # gave consecutive tokens identical timestamps.
        return time.perf_counter() * 1000

    async def sleep_ms(self, duration_ms: float) -> None:
        _check_duration(duration_ms)
        await asyncio.sleep(duration_ms / 1000)


class SimClock:
    """Virtual time, for tests and CI. Sleeping costs no real time.

    Time only moves when every task on the event loop is blocked. The clock then jumps to
    the earliest pending wake-up and resumes that one sleeper. Sleepers due at the same
    instant resume in the order they went to sleep. Callers need no driver: awaiting
    ``sleep_ms`` is enough.

    Several SimClocks may be used on one event loop one after another, but only one may
    have live sleepers at a time: two clocks both waiting for the loop to go idle would
    each keep it busy for the other.
    """

    def __init__(self, start_ms: float = 0.0) -> None:
        self._now_ms = start_ms
        self._timers: list[tuple[float, int, asyncio.Future[None]]] = []
        self._next_seq = 0
        self._pump: asyncio.Task[None] | None = None

    def now_ms(self) -> float:
        return self._now_ms

    async def sleep_ms(self, duration_ms: float) -> None:
        _check_duration(duration_ms)
        loop = asyncio.get_running_loop()
        ready = _ready_queue(loop)
        woken: asyncio.Future[None] = loop.create_future()
        heapq.heappush(self._timers, (self._now_ms + duration_ms, self._next_seq, woken))
        self._next_seq += 1
        if self._pump is None or self._pump.done():
            self._pump = loop.create_task(self._run_timers(ready))
        await woken

    async def _run_timers(self, ready: Sized) -> None:
        while self._timers:
            # Let every runnable task run until it blocks. Tasks that go to sleep while
            # we wait land in the heap, so the earliest wake-up is known before we jump.
            while True:
                await asyncio.sleep(0)
                # A clock whose remaining sleepers have all been cancelled has nothing to
                # wait for. It must stop yielding at once: while it keeps doing so, the
                # loop never looks idle to another SimClock running on it.
                while self._timers and self._timers[0][2].cancelled():
                    heapq.heappop(self._timers)
                if not self._timers:
                    return
                if not len(ready):
                    break
            wake_ms, _, woken = heapq.heappop(self._timers)
            if woken.cancelled():
                continue
            self._now_ms = wake_ms
            woken.set_result(None)


def _ready_queue(loop: asyncio.AbstractEventLoop) -> Sized:
    # There is no public way to ask asyncio whether any task is runnable. The standard
    # loops keep runnable callbacks in ``_ready``; fail loudly on a loop that does not,
    # rather than advance time on a guess.
    ready = getattr(loop, "_ready", None)
    if not isinstance(ready, Sized):
        raise RuntimeError(
            f"SimClock needs a standard asyncio event loop, got {type(loop).__name__}"
        )
    return ready
