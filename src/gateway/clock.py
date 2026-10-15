"""Clocks. This is the only module allowed to touch real time (see CLAUDE.md).

All times are in milliseconds, matching the cost model.
"""

from __future__ import annotations

import asyncio
import heapq
import time
from collections.abc import Awaitable, Callable, Sized
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
        while await self._until_idle(ready):
            self._wake_next()

    async def _until_idle(self, ready: Sized) -> bool:
        """Let every runnable task run until it blocks. False if no sleeper is left.

        Tasks that go to sleep meanwhile land in the heap, so the earliest wake-up is
        known before time jumps.
        """
        while True:
            await asyncio.sleep(0)
            # A clock whose remaining sleepers have all been cancelled has nothing to
            # wait for. It must stop yielding at once: while it keeps doing so, the
            # loop never looks idle to another SimClock running on it.
            while self._timers and self._timers[0][2].cancelled():
                heapq.heappop(self._timers)
            if not self._timers:
                return False
            if not len(ready):
                return True

    def _wake_next(self) -> None:
        """Jump to the earliest wake-up and resume its sleeper."""
        wake_ms, _, woken = heapq.heappop(self._timers)
        if woken.cancelled():
            return
        self._now_ms = wake_ms
        woken.set_result(None)


async def _real_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


class PacedSimClock(SimClock):
    """Virtual time that is let through at ``speed`` times real time, for a live demo.

    Like ``SimClock``, time jumps exactly to each wake-up, so everything that is driven by
    sleepers alone happens at the same virtual instants on every run, whatever the machine
    is doing. Unlike it, the clock holds virtual time back to ``speed`` times real time:
    1000 ms at speed 20 take 50 ms. Real delays move when things are shown, never the
    virtual times they are stamped with.

    It does not sleep once per wake-up. A simulation has hundreds of wake-ups in a
    virtual second, and an operating system rounds a short sleep up (to about 15 ms on
    Windows), so that would run many times too slowly. The clock keeps the real time by
    which the current virtual time is due and sleeps only when it is ahead of it by
    ``min_wait_s`` or more; a sleep that overran is made up by not sleeping for a while.
    If the machine cannot keep up at all, the clock does not then rush: it forgets
    everything but the last ``max_behind_s`` of the delay.

    A long gap is taken in slices of ``slice_s`` real seconds, and virtual time moves on
    with each slice. That only matters to something arriving from outside between two
    wake-ups, an HTTP request say: it sees the time it arrived at, and anything it then
    sleeps for is woken on time instead of after the rest of the gap.

    ``wait`` and ``real`` replace the real sleep and the real clock, for tests. With only
    ``wait``, each wait is taken to last exactly as long as was asked. ``speed`` may be
    changed at any time.
    """

    def __init__(
        self,
        speed: float = 1.0,
        *,
        start_ms: float = 0.0,
        slice_s: float = 0.02,
        min_wait_s: float = 0.001,
        max_behind_s: float = 0.25,
        wait: Callable[[float], Awaitable[None]] | None = None,
        real: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(start_ms)
        if slice_s <= 0:
            raise ValueError("slice_s must be positive")
        if min_wait_s < 0 or max_behind_s < 0:
            raise ValueError("min_wait_s and max_behind_s must not be negative")
        self._slice_s = slice_s
        self._min_wait_s = min_wait_s
        self._max_behind_s = max_behind_s
        self._waited_s = 0.0
        self._wait: Callable[[float], Awaitable[None]]
        self._real: Callable[[], float]
        if wait is None:
            self._wait = _real_sleep
            self._real = real if real is not None else time.perf_counter
        elif real is None:
            self._wait = self._counted(wait)
            self._real = self._waited
        else:
            self._wait = wait
            self._real = real
        # The real time at which virtual time may be where it is now; None until it moves.
        self._due_s: float | None = None
        self.speed = speed

    def _waited(self) -> float:
        return self._waited_s

    def _counted(
        self, wait: Callable[[float], Awaitable[None]]
    ) -> Callable[[float], Awaitable[None]]:
        async def counted(seconds: float) -> None:
            await wait(seconds)
            self._waited_s += seconds

        return counted

    @property
    def speed(self) -> float:
        return self._speed

    @speed.setter
    def speed(self, value: float) -> None:
        if value <= 0:
            raise ValueError("speed must be positive")
        self._speed = value

    async def _run_timers(self, ready: Sized) -> None:
        while await self._until_idle(ready):
            gap_ms = self._timers[0][0] - self._now_ms
            if gap_ms <= 0:
                self._wake_next()
                continue
            step_ms = min(gap_ms, self._slice_s * 1000 * self._speed)
            now_s = self._real()
            due_s = self._due_s if self._due_s is not None else now_s
            # Far behind: the machine is slower than the speed asked for, or the clock sat
            # idle. Either way, do not run flat out until the whole delay is made up.
            due_s = max(due_s, now_s - self._max_behind_s) + step_ms / self._speed / 1000
            self._due_s = due_s
            ahead_s = due_s - now_s
            if ahead_s > 0 and ahead_s >= self._min_wait_s:
                await self._wait(ahead_s)
            # Whatever arrived during the wait went to sleep relative to the old time,
            # so no wake-up is earlier than that, but the earliest may be a new one.
            # Never step past it, and land on it exactly, not on a sum of slices.
            while self._timers and self._timers[0][2].cancelled():
                heapq.heappop(self._timers)
            limit_ms = self._timers[0][0] if self._timers else self._now_ms + step_ms
            self._now_ms = min(self._now_ms + step_ms, limit_ms) if step_ms < gap_ms else limit_ms


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
