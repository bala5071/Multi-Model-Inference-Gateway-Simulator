"""Building blocks for the Valkey counter tests: counters over a real Valkey (the tests marked
``valkey``) with a key prefix of their own, over an address where nothing listens, or over a
scripted client; and epochs the test controls. Test values, not secrets."""

from __future__ import annotations

import os
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from redis.asyncio import Redis

from gateway.clock import SystemEpoch, WallClock
from gateway.valkey import ValkeyCounters
from helpers import TEST_PEPPER

VALKEY_URL_ENV = "GATEWAY_TEST_VALKEY_URL"
# Nothing listens on port 1: every call there fails (refused, or out of time).
NOWHERE = "redis://127.0.0.1:1/0"


class ManualEpoch:
    """Calendar time the test sets."""

    def __init__(self, now_ms: float = 1.75e12) -> None:
        self.now_ms = now_ms

    def epoch_ms(self) -> float:
        return self.now_ms


class OffsetEpoch:
    """The system's calendar time, moved by ``offset_ms``: an instance whose clock is off."""

    def __init__(self, offset_ms: float = 0.0) -> None:
        self.offset_ms = offset_ms

    def epoch_ms(self) -> float:
        return SystemEpoch().epoch_ms() + self.offset_ms


def counters_on(
    url: str,
    epoch: Any,
    *,
    prefix: str,
    seed: int = 1,
    timeout_ms: float = 500.0,
    max_skew_ms: float = 1000.0,
) -> ValkeyCounters:
    return ValkeyCounters(
        Redis.from_url(url, decode_responses=True),
        WallClock(),
        epoch,
        TEST_PEPPER,
        prefix=prefix,
        timeout_ms=timeout_ms,
        max_skew_ms=max_skew_ms,
        random_bytes=random.Random(seed).randbytes,
    )


@asynccontextmanager
async def instances(
    epoch: Any, count: int = 2, **kwargs: Any
) -> AsyncIterator[list[ValkeyCounters]]:
    """``count`` instances' counters on the test Valkey, under one prefix of their own, which is
    deleted afterwards (never the whole database)."""
    url = os.environ[VALKEY_URL_ENV]
    prefix = f"gwtest:{random.SystemRandom().getrandbits(64):016x}:"
    found = [counters_on(url, epoch, prefix=prefix, seed=i + 1, **kwargs) for i in range(count)]
    try:
        yield found
    finally:
        client = found[0].client
        async for key in client.scan_iter(match=prefix + "*"):
            await client.delete(key)
        for counters in found:
            await counters.aclose()


class ScriptedClient:
    """Stands in for ``redis.asyncio.Redis`` where only the watch is tested: ``TIME``, ``INFO``,
    ``CONFIG GET`` and ``SET NX``, answering what the test says. ``shared`` is the key space
    two scripted clients share."""

    def __init__(self, shared: dict[str, str] | None = None) -> None:
        self.run_id = "a" * 40
        self.time_ms = 1.75e12
        self.config = {"maxmemory-policy": "noeviction", "appendonly": "yes"}
        self.shared = shared if shared is not None else {}

    def register_script(self, script: str) -> Callable[..., Awaitable[Any]]:
        async def never(**_: Any) -> Any:
            raise AssertionError("no script runs in these tests")

        return never

    async def time(self) -> tuple[int, int]:
        whole, part = divmod(self.time_ms, 1000.0)
        return int(whole), int(part * 1000)

    async def info(self, section: str) -> dict[str, Any]:
        return {"run_id": self.run_id}

    async def config_get(self, name: str) -> dict[str, str]:
        return {name: self.config[name]}

    async def set(self, name: str, value: str, *, nx: bool, px: int) -> bool:
        if nx and name in self.shared:
            return False
        self.shared[name] = value
        return True

    async def aclose(self) -> None:
        return None
