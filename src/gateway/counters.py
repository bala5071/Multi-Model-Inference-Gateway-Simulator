"""Shared counters: what an instance needs from a counter store that other instances share.

See docs/35-valkey-counters.md and docs/scale-out-plan.md, sections 1.2 and 3.4 to 3.9.

Without shared counters each counter is this process's own (``RequestRateLimiter``,
``LoginLimiter``, ``Breakers``, ``InMemoryConnectionTestCounter``, ``InMemoryAuditCap``), as
before. With them (``gateway.valkey``), every instance that shares the store enforces one
limit, and the store is watched: its clock against the instance's, and its restarts.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from gateway.breaker import BreakerStore, Transition
from gateway.ratelimit import RateLimiter

# Told the store's new run id when the store has restarted (its counters are empty).
OnRestart = Callable[[str], Awaitable[None]]


class CounterWatch(Protocol):
    async def start(self, on_restart: OnRestart) -> None:
        """Check the store before the server serves anything; raise to refuse to start."""
        ...

    async def run(self, on_restart: OnRestart) -> None:
        """Check it again every so often, until cancelled."""
        ...


class SharedCounters(Protocol):
    """The counters ``create_app`` builds its request path on, when they are shared."""

    @property
    def watch(self) -> CounterWatch: ...

    def rate_limiter(self) -> RateLimiter: ...

    def breakers(self, on_transition: Callable[[Transition], None]) -> BreakerStore: ...

    async def aclose(self) -> None: ...
