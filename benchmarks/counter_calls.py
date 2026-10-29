"""What one counter call costs in this process's memory and in Valkey.

MEASURED on the machine it runs on: real time, a real Valkey (docs/35-valkey-counters.md). As
docs/35 quotes. Each row is one call, awaited on the event loop, as the request path makes it:

- "rate limit": ``RateLimiter.admit`` for an application with a limit no call reaches (the
  window script: trim, count, add);
- "login limiter check": ``FailureWindow.blocked`` (one ``ZCOUNT``);
- "breaker admit": ``BreakerStore.admit`` on a closed breaker (the breaker script);
- "chat worst case, N providers": what one chat request makes at most (docs/35): one rate-limit
  call, then for each of N hosted providers admit, failure, admit (the invalid-tool-JSON
  retry), failure, one after another: 1 + 4N calls. Fresh provider ids each time, so every
  breaker is closed as on a first attempt.

The Valkey rows include the timeout on the injected clock that every call has. The Valkey must
be the operator's (docker-compose.valkey.yml); keys are written under a prefix of their own and
deleted afterwards.

    GATEWAY_VALKEY_URL=redis://127.0.0.1:6380/0 uv run python -m benchmarks.counter_calls
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import os
import secrets
from collections.abc import Awaitable, Callable, Sequence

from redis.asyncio import Redis

from benchmarks.calibrate import describe_machine
from benchmarks.usage_store_writes import WriteTimes
from gateway.auth import LoginLimiter
from gateway.breaker import Breakers, BreakerStore
from gateway.clock import SystemEpoch, WallClock
from gateway.metrics import percentile
from gateway.ratelimit import RateLimiter, RequestRateLimiter
from gateway.valkey import URL_ENV, ValkeyCounters

WARMUP_CALLS = 20
NEVER_REACHED = 10**9


async def _times(name: str, count: int, call: Callable[[], Awaitable[object]]) -> WriteTimes:
    clock = WallClock()
    for _ in range(WARMUP_CALLS):
        await call()
    times = []
    for _ in range(count):
        started = clock.now_ms()
        await call()
        times.append(clock.now_ms() - started)
    return WriteTimes(
        store=name,
        writes=count,
        mean_ms=sum(times) / len(times),
        p50_ms=percentile(times, 50),
        p99_ms=percentile(times, 99),
        max_ms=max(times),
    )


async def run(calls: int, url: str) -> list[WriteTimes]:
    clock, epoch = WallClock(), SystemEpoch()
    # A throwaway secret: it names the keys and lives in this function only.
    secret = secrets.token_bytes(32)
    prefix = f"gwbench:{secrets.token_hex(8)}:"
    shared = ValkeyCounters(
        Redis.from_url(url, decode_responses=True), clock, epoch, secret, prefix=prefix
    )
    own_rate, own_login = RequestRateLimiter(clock), LoginLimiter(clock, secret)
    own_breakers = Breakers(clock)
    rate, window, breakers = (
        shared.rate_limiter(),
        shared.failure_window("login", 5, 900_000.0),
        shared.breakers(),
    )
    key = own_login.key("bench")
    sequence = itertools.count()

    def chat_worst_case(
        limiter: RateLimiter, store: BreakerStore, providers: int
    ) -> Callable[[], Awaitable[object]]:
        async def one_request() -> None:
            await limiter.admit("bench", NEVER_REACHED)
            for _ in range(providers):
                provider = f"prv_bench_{next(sequence)}"
                for _ in range(2):
                    await store.admit("org", provider)
                    await store.failure("org", provider)

        return one_request

    results = [
        await _times("rate limit, memory", calls, lambda: own_rate.admit("bench", NEVER_REACHED)),
        await _times("rate limit, valkey", calls, lambda: rate.admit("bench", NEVER_REACHED)),
        await _times("login check, memory", calls, lambda: own_login.blocked(key)),
        await _times("login check, valkey", calls, lambda: window.blocked(key)),
        await _times("breaker admit, memory", calls, lambda: own_breakers.admit("org", "prv")),
        await _times("breaker admit, valkey", calls, lambda: breakers.admit("org", "prv")),
    ]
    for providers in (1, 3):
        for where, limiter, store in (
            ("memory", own_rate, own_breakers),
            ("valkey", rate, breakers),
        ):
            results.append(
                await _times(
                    f"chat worst, N={providers}, {where}",
                    calls,
                    chat_worst_case(limiter, store, providers),
                )
            )
    async for name in shared.client.scan_iter(match=prefix + "*"):
        await shared.client.delete(name)
    await shared.aclose()
    return results


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Time one counter call, in memory and Valkey.")
    parser.add_argument("--calls", type=int, default=2000)
    args = parser.parse_args(argv)
    if args.calls < 1:
        parser.error("--calls must be at least 1")
    url = os.environ.get(URL_ENV)
    if not url:
        parser.error(f"set {URL_ENV} to the Valkey to measure against")

    machine = describe_machine()
    print("MEASURED on this machine: real time, a real Valkey.")
    for key in ("cpu", "logical_cores", "ram_gb", "os", "python"):
        print(f"{key}: {machine[key]}")
    print(f"warm-up per row: {WARMUP_CALLS}; percentiles: nearest rank")
    print()
    print(f"{'what':<24} {'n':>6} {'mean ms':>9} {'p50 ms':>9} {'p99 ms':>9} {'max ms':>9}")
    for r in asyncio.run(run(args.calls, url)):
        print(
            f"{r.store:<24} {r.writes:>6} {r.mean_ms:>9.3f} {r.p50_ms:>9.3f} "
            f"{r.p99_ms:>9.3f} {r.max_ms:>9.3f}"
        )


if __name__ == "__main__":
    main()
