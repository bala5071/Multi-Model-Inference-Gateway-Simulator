"""Circuit breakers and the slow-provider warning, per (organization, provider), in memory.

See docs/33-fallback-modes.md.

- **closed** to **open** after ``failure_threshold`` consecutive failures of the fallback
  trigger kinds (a connection error, a 5xx, a missed first-token budget, invalid tool JSON).
- **open** for ``open_ms``, doubling (up to ``max_open_ms``) each time a probe fails; while
  open the provider is skipped (``circuit_open``).
- **half-open** when the time has passed: one request goes through; the others still skip.
- **closed** again after ``close_after`` consecutive successes (the hysteresis); a failure
  while half-open opens it again, for twice as long.

The slow-provider warning: ``slow_after`` consecutive missed first-token budgets set
``slow_since_ms``; an answer within budget clears it.

Time is the injected clock's. Each transition is a ``circuit_breaker`` event on the
``EventBus``. Nothing here reads monitoring: the router owns the breakers.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from gateway.clock import Clock

FAILURE_THRESHOLD = 5
OPEN_MS = 30_000.0
MAX_OPEN_MS = 5 * 60_000.0
CLOSE_AFTER = 2
SLOW_AFTER = 3


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True, slots=True)
class Transition:
    organization_id: str
    provider_id: str
    state: BreakerState
    open_ms: float | None


@dataclass(frozen=True, slots=True)
class SlowWarning:
    slow_since_ms: float | None
    consecutive_misses: int


class _Breaker:
    __slots__ = (
        "failures",
        "misses",
        "open_ms",
        "open_until_ms",
        "probing",
        "slow_since_ms",
        "state",
        "successes",
    )

    def __init__(self) -> None:
        self.state = BreakerState.CLOSED
        self.failures = 0
        self.successes = 0
        self.open_until_ms = 0.0
        self.open_ms = 0.0
        self.probing = False
        self.misses = 0
        self.slow_since_ms: float | None = None


class BreakerStore(Protocol):
    """The circuit breakers' state per (organization, provider), as the router uses it
    (docs/34-state-interfaces.md). The slow-provider warning is not part of it: it stays with
    each instance (docs/scale-out-plan.md, decision 11)."""

    async def admit(self, organization_id: str, provider_id: str) -> bool: ...

    async def release(self, organization_id: str, provider_id: str) -> None: ...

    async def success(self, organization_id: str, provider_id: str) -> None: ...

    async def failure(self, organization_id: str, provider_id: str) -> None: ...

    async def state(self, organization_id: str, provider_id: str) -> BreakerState: ...


class Breakers:
    def __init__(
        self,
        clock: Clock,
        on_transition: Callable[[Transition], None] | None = None,
        *,
        failure_threshold: int = FAILURE_THRESHOLD,
        open_ms: float = OPEN_MS,
        max_open_ms: float = MAX_OPEN_MS,
        close_after: int = CLOSE_AFTER,
        slow_after: int = SLOW_AFTER,
    ) -> None:
        self._clock = clock
        self._on_transition = on_transition
        self._failure_threshold = failure_threshold
        self._open_ms = open_ms
        self._max_open_ms = max_open_ms
        self._close_after = close_after
        self._slow_after = slow_after
        self._breakers: dict[tuple[str, str], _Breaker] = {}

    def _get(self, organization_id: str, provider_id: str) -> _Breaker:
        return self._breakers.setdefault((organization_id, provider_id), _Breaker())

    def _move(self, key: tuple[str, str], breaker: _Breaker, state: BreakerState) -> None:
        breaker.state = state
        if self._on_transition is not None:
            self._on_transition(
                Transition(
                    key[0], key[1], state, breaker.open_ms if state is BreakerState.OPEN else None
                )
            )

    def _open(self, key: tuple[str, str], breaker: _Breaker, open_ms: float) -> None:
        breaker.open_ms = min(open_ms, self._max_open_ms)
        breaker.open_until_ms = self._clock.now_ms() + breaker.open_ms
        breaker.failures = 0
        breaker.successes = 0
        breaker.probing = False
        self._move(key, breaker, BreakerState.OPEN)

    async def admit(self, organization_id: str, provider_id: str) -> bool:
        """Whether a request may try this provider now. When an open breaker's time has
        passed it turns half-open and admits exactly one request until that one reports."""
        key = (organization_id, provider_id)
        breaker = self._get(*key)
        if breaker.state is BreakerState.OPEN:
            if self._clock.now_ms() < breaker.open_until_ms:
                return False
            self._move(key, breaker, BreakerState.HALF_OPEN)
        if breaker.state is BreakerState.HALF_OPEN:
            if breaker.probing:
                return False
            breaker.probing = True
        return True

    async def release(self, organization_id: str, provider_id: str) -> None:
        """An admitted request ended without a verdict (cancelled, or a non-trigger failure):
        let the next one probe."""
        self._get(organization_id, provider_id).probing = False

    async def success(self, organization_id: str, provider_id: str) -> None:
        key = (organization_id, provider_id)
        breaker = self._get(*key)
        breaker.failures = 0
        breaker.probing = False
        if breaker.state is BreakerState.HALF_OPEN:
            breaker.successes += 1
            if breaker.successes >= self._close_after:
                breaker.successes = 0
                breaker.open_ms = 0.0
                self._move(key, breaker, BreakerState.CLOSED)

    async def failure(self, organization_id: str, provider_id: str) -> None:
        key = (organization_id, provider_id)
        breaker = self._get(*key)
        breaker.probing = False
        if breaker.state is BreakerState.HALF_OPEN:
            self._open(key, breaker, breaker.open_ms * 2 if breaker.open_ms else self._open_ms)
            return
        breaker.failures += 1
        if breaker.failures >= self._failure_threshold:
            self._open(key, breaker, self._open_ms)

    async def state(self, organization_id: str, provider_id: str) -> BreakerState:
        return self._get(organization_id, provider_id).state

    def first_token(self, organization_id: str, provider_id: str, *, missed: bool) -> None:
        """A hosted answer's first token came within its budget, or did not."""
        breaker = self._get(organization_id, provider_id)
        if not missed:
            breaker.misses = 0
            breaker.slow_since_ms = None
            return
        breaker.misses += 1
        if breaker.misses >= self._slow_after and breaker.slow_since_ms is None:
            breaker.slow_since_ms = self._clock.now_ms()

    def slow(self, organization_id: str, provider_id: str) -> SlowWarning:
        breaker = self._breakers.get((organization_id, provider_id))
        if breaker is None:
            return SlowWarning(None, 0)
        return SlowWarning(breaker.slow_since_ms, breaker.misses)
