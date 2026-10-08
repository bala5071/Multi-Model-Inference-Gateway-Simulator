"""Overload protection: request timeouts and SLO-aware downgrade with hysteresis.

Queue limits live in the workers; they raise ``QueueFullError`` from here.
See docs/10-overload.md.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TypeVar

from gateway.clock import Clock
from gateway.metrics import percentile

T = TypeVar("T")


class QueueFullError(Exception):
    """A model's queue is at its limit; the request was not accepted."""

    def __init__(self, model: str, waiting: int, limit: int, retry_after_ms: float) -> None:
        self.model = model
        self.waiting = waiting
        self.limit = limit
        self.retry_after_ms = retry_after_ms
        super().__init__(
            f"The queue for model {model!r} is full ({waiting} waiting, limit {limit})."
        )


class RequestTimeoutError(Exception):
    def __init__(self, timeout_ms: float) -> None:
        self.timeout_ms = timeout_ms
        super().__init__(f"The request did not finish within {timeout_ms:g} ms.")


async def run_with_timeout(clock: Clock, work: Awaitable[T], timeout_ms: float) -> T:
    """Await ``work``, cancelling it if it takes longer than ``timeout_ms`` on ``clock``.

    On timeout the work is cancelled and has finished unwinding before
    ``RequestTimeoutError`` is raised. If both finish at the same instant, the work wins.
    """
    task = asyncio.ensure_future(work)
    timer = asyncio.ensure_future(clock.sleep_ms(timeout_ms))
    try:
        await asyncio.wait({task, timer}, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        task.cancel()
        timer.cancel()
        raise
    if task.done():
        timer.cancel()
        return task.result()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    raise RequestTimeoutError(timeout_ms)


@dataclass(frozen=True, slots=True)
class SloPolicy:
    """When a model counts as degraded, and when it counts as recovered.

    A model becomes degraded when the p99 total latency of the requests it finished in the
    last ``window_ms`` is above its SLO. It recovers when that p99 falls below
    ``recover_ratio`` times the SLO, or when the window is empty. Between the two
    thresholds the state does not change, and no change happens within ``min_hold_ms`` of
    the previous one. Fewer than ``min_samples`` requests in the window are never enough
    to judge either way.
    """

    # Model id -> SLO on p99 total latency. Models not listed are never degraded.
    p99_ms: Mapping[str, float]
    window_ms: float = 60_000.0
    min_samples: int = 20
    recover_ratio: float = 0.7
    min_hold_ms: float = 30_000.0

    def __post_init__(self) -> None:
        if any(slo <= 0 for slo in self.p99_ms.values()):
            raise ValueError("every SLO must be positive")
        if self.window_ms <= 0:
            raise ValueError("window_ms must be positive")
        if self.min_samples < 1:
            raise ValueError("min_samples must be at least 1")
        if not 0 < self.recover_ratio <= 1:
            raise ValueError("recover_ratio must be in (0, 1]")
        if self.min_hold_ms < 0:
            raise ValueError("min_hold_ms must not be negative")


@dataclass(frozen=True, slots=True)
class OverloadPolicy:
    # Longest a request may take from arrival to completion; None for no limit.
    request_timeout_ms: float | None = None
    # None disables SLO-aware downgrade.
    slo: SloPolicy | None = None

    def __post_init__(self) -> None:
        if self.request_timeout_ms is not None and self.request_timeout_ms <= 0:
            raise ValueError("request_timeout_ms must be positive")


@dataclass(frozen=True, slots=True)
class Transition:
    at_ms: float
    model: str
    degraded: bool
    # The window's p99 that caused the change; None if the window was empty.
    p99_ms: float | None


@dataclass(frozen=True, slots=True)
class ModelHealth:
    degraded: bool
    slo_p99_ms: float | None
    recent_p99_ms: float | None
    samples: int


@dataclass(slots=True)
class _ModelState:
    # (finish time, total latency) of recent requests, oldest first.
    window: deque[tuple[float, float]] = field(default_factory=deque)
    degraded: bool = False
    changed_ms: float | None = None


class SloMonitor:
    """Tracks each model's recent p99 and whether it is currently degraded."""

    def __init__(self, policy: SloPolicy, clock: Clock) -> None:
        self._policy = policy
        self._clock = clock
        self._states: dict[str, _ModelState] = {}
        self._transitions: list[Transition] = []

    @property
    def transitions(self) -> tuple[Transition, ...]:
        """Every state change so far, in order."""
        return tuple(self._transitions)

    def record(self, model: str, latency_ms: float) -> None:
        """Note that a request for ``model`` just finished after ``latency_ms``."""
        state = self._states.setdefault(model, _ModelState())
        state.window.append((self._clock.now_ms(), latency_ms))
        self._evaluate(model, state)

    def is_degraded(self, model: str) -> bool:
        state = self._states.setdefault(model, _ModelState())
        self._evaluate(model, state)
        return state.degraded

    def health(self, model: str) -> ModelHealth:
        state = self._states.setdefault(model, _ModelState())
        self._evaluate(model, state)
        return ModelHealth(
            degraded=state.degraded,
            slo_p99_ms=self._policy.p99_ms.get(model),
            recent_p99_ms=_p99(state),
            samples=len(state.window),
        )

    def _evaluate(self, model: str, state: _ModelState) -> None:
        now_ms = self._clock.now_ms()
        while state.window and state.window[0][0] <= now_ms - self._policy.window_ms:
            state.window.popleft()

        slo_ms = self._policy.p99_ms.get(model)
        if slo_ms is None:
            return
        if state.changed_ms is not None and now_ms - state.changed_ms < self._policy.min_hold_ms:
            return

        p99_ms = _p99(state)
        enough = len(state.window) >= self._policy.min_samples
        if state.degraded:
            recovered = p99_ms is None or (enough and p99_ms < slo_ms * self._policy.recover_ratio)
            if not recovered:
                return
        elif not (enough and p99_ms is not None and p99_ms > slo_ms):
            return

        state.degraded = not state.degraded
        state.changed_ms = now_ms
        self._transitions.append(Transition(now_ms, model, state.degraded, p99_ms))


def _p99(state: _ModelState) -> float | None:
    if not state.window:
        return None
    return percentile([latency for _, latency in state.window], 99)


def pick_downgrade(models: Sequence[str], chosen: str, is_degraded: Callable[[str], bool]) -> str:
    """The model to use in place of ``chosen``, given ``models`` ordered cheapest first.

    Steps down one model at a time while the current pick is degraded and a cheaper one
    exists. Returns ``chosen`` itself if it is healthy or already the cheapest.
    """
    index = models.index(chosen)
    while index > 0 and is_degraded(models[index]):
        index -= 1
    return models[index]
