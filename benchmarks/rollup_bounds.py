"""How many keys and how much memory the recorder's hour keys take, and what one flush costs
(docs/57, console-ia-v2-plan 15.6, condition 1).

MEASURED on the machine it runs on. Each application gets one answered request at minute 30 of
each of 27 consecutive hours (one more than the 26 kept), recorded by the gateway's own
``AlertRecorder``, ``--organizations`` organizations sharing the applications:

- in memory (always): the recorder writes into ``InMemoryAlertRollups``; the keys held after the
  27 hours, by kind, the oldest hour still held, and the rollups' memory (``tracemalloc``, the
  rollups' own allocations: what the recorder's sink grew by);
- ``--valkey`` (needs ``GATEWAY_VALKEY_URL``): the same requests flushed hour by hour through
  ``ValkeyAlertRollups``; the keys by kind (``SCAN`` of this run's prefix), their memory
  (``MEMORY USAGE`` of every key), and each kind's ``PTTL`` range. Every key of the run is written
  within seconds, so all 27 hours are present here: in a running gateway an hour key's last
  write is in its hour (or one flush later), so at most 27 hours of a subject are alive, and the
  count below is that bound. Then one flush, timed: the minute keys alone (what a flush sent
  before docs/57) against the minute keys and the hour and organization keys (what it sends
  now), for ``--flush-applications`` applications with ``--flush-requests`` requests each.

Keys are written under a prefix of this run's own and deleted at the end (never the database).

    uv run python -m benchmarks.rollup_bounds
    GATEWAY_VALKEY_URL=redis://127.0.0.1:6380/0 uv run python -m benchmarks.rollup_bounds --valkey
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import tracemalloc
from collections import Counter
from collections.abc import Sequence

from redis.asyncio import Redis

from benchmarks.calibrate import describe_machine
from benchmarks.usage_store_writes import WriteTimes
from gateway.alertrecorder import APP_HOUR, HOUR_MS, MINUTE_MS, ORG_HOUR, ORG_MINUTE, AlertRecorder
from gateway.alertrollups import KEEP_HOURS, InMemoryAlertRollups
from gateway.clock import SystemEpoch, WallClock
from gateway.metrics import percentile
from gateway.valkey import URL_ENV, ValkeyAlertRollups, ValkeyCounters

HOURS = KEEP_HOURS + 1
FLUSH_RUNS = 50
T0 = 1_793_613_600_000.0  # an hour's start


class Epoch:
    def __init__(self) -> None:
        self.at = T0

    def epoch_ms(self) -> float:
        return self.at


def _record(recorder: AlertRecorder, applications: int, organizations: int) -> None:
    for i in range(applications):
        recorder.record(
            f"app{i:05d}",
            kind="generate",
            status=200,
            ttft_ms=42.0,
            total_ms=380.0,
            organization_id=f"org{i % organizations:03d}",
        )


def in_memory(applications: int, organizations: int) -> dict[str, object]:
    epoch = Epoch()
    tracemalloc.start()
    before = tracemalloc.take_snapshot()
    rollups = InMemoryAlertRollups()
    recorder = AlertRecorder(epoch, sink=rollups)
    for hour in range(HOURS):
        epoch.at = T0 + hour * HOUR_MS + 30 * MINUTE_MS
        _record(recorder, applications, organizations)
    after = tracemalloc.take_snapshot()
    tracemalloc.stop()
    grown = sum(s.size_diff for s in after.compare_to(before, "filename"))
    kinds = Counter(key[0] for key in rollups._extras)  # the store's own keys, read in place
    first_hour = int(T0 // HOUR_MS)
    oldest = min(key[2] for key in rollups._extras if key[0] == APP_HOUR) - first_hour
    return {
        "app-hour keys": kinds[APP_HOUR],
        "org-hour keys": kinds[ORG_HOUR],
        "org-minute keys": kinds[ORG_MINUTE],
        "minute keys": rollups.size(),
        "oldest hour held (of 0 to 26)": oldest,
        "memory MiB": round(grown / 2**20, 1),
    }


async def _scan(client: Redis, match: str) -> list[str]:
    return [name async for name in client.scan_iter(match=match, count=1000)]


async def on_valkey(
    url: str, applications: int, organizations: int, flush_apps: int, flush_requests: int
) -> tuple[dict[str, dict[str, object]], list[WriteTimes]]:
    clock = WallClock()
    # A throwaway secret: it names the keys and lives in this function only.
    secret = secrets.token_bytes(32)
    prefix = f"gwbench:{secrets.token_hex(8)}:"
    # One call adds a whole hour of 5,000 applications at once, far more than a 10 s flush holds:
    # a longer timeout than the server's, so the measurement is not cut short.
    counters = ValkeyCounters(
        Redis.from_url(url, decode_responses=True),
        clock,
        SystemEpoch(),
        secret,
        prefix=prefix,
        timeout_ms=30_000,
    )
    rollups = ValkeyAlertRollups(counters)
    epoch = Epoch()
    recorder = AlertRecorder(epoch)
    try:
        for hour in range(HOURS):
            epoch.at = T0 + hour * HOUR_MS + 30 * MINUTE_MS
            _record(recorder, applications, organizations)
            await rollups.add(f"bounds:{hour}", recorder.take())
            await rollups.add_extras(f"bounds:{hour}/extras", recorder.take_extras())
        found: dict[str, dict[str, object]] = {}
        for kind in ("rollup", f"rollup-{APP_HOUR}", f"rollup-{ORG_HOUR}", f"rollup-{ORG_MINUTE}"):
            names = await _scan(counters.client, f"{prefix}{kind}:*")
            async with counters.client.pipeline(transaction=False) as pipe:
                for name in names:
                    pipe.memory_usage(name)
                    pipe.pttl(name)
                answers = await pipe.execute()
            sizes, ttls = answers[0::2], answers[1::2]
            found[kind] = {
                "keys": len(names),
                "memory MiB": round(sum(int(s) for s in sizes) / 2**20, 1),
                "bytes a key": round(sum(int(s) for s in sizes) / max(len(names), 1)),
                "pttl h min": round(min(ttls) / HOUR_MS, 2) if ttls else None,
                "pttl h max": round(max(ttls) / HOUR_MS, 2) if ttls else None,
            }
        times = await _flush_times(rollups, clock, flush_apps, flush_requests)
    finally:
        async for name in counters.client.scan_iter(match=prefix + "*", count=1000):
            await counters.client.delete(name)
        await counters.aclose()
    return found, times


async def _flush_times(
    rollups: ValkeyAlertRollups, clock: WallClock, applications: int, requests: int
) -> list[WriteTimes]:
    epoch = Epoch()
    epoch.at = T0 + 500 * HOUR_MS
    recorder = AlertRecorder(epoch)
    minutes_only: list[float] = []
    with_extras: list[float] = []
    for run in range(FLUSH_RUNS):
        for _ in range(requests):
            _record(recorder, applications, max(1, applications // 10))
        deltas, extras = recorder.take(), recorder.take_extras()
        started = clock.now_ms()
        await rollups.add(f"flush:{run}:a", deltas)
        minutes_only.append(clock.now_ms() - started)
        started = clock.now_ms()
        await rollups.add(f"flush:{run}:b", deltas)
        await rollups.add_extras(f"flush:{run}:b/extras", extras)
        with_extras.append(clock.now_ms() - started)

    def summary(name: str, times: list[float]) -> WriteTimes:
        return WriteTimes(name, len(times), sum(times) / len(times), percentile(times, 50),
                          percentile(times, 99), max(times))  # fmt: skip

    return [
        summary("minutes only (before)", minutes_only),
        summary("with hours (now)", with_extras),
    ]


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="The recorder's hour keys: keys, memory, flush.")
    parser.add_argument("--applications", type=int, default=5000)
    parser.add_argument("--organizations", type=int, default=50)
    parser.add_argument("--valkey", action="store_true")
    parser.add_argument("--flush-applications", type=int, default=100)
    parser.add_argument("--flush-requests", type=int, default=10)
    args = parser.parse_args(argv)
    if min(args.applications, args.organizations, args.flush_applications) < 1:
        parser.error("counts must be at least 1")
    url = os.environ.get(URL_ENV) if args.valkey else None
    if args.valkey and not url:
        parser.error(f"set {URL_ENV} to the Valkey to measure against")

    machine = describe_machine()
    print("MEASURED on this machine: real time, the gateway's recorder and rollups.")
    for key in ("cpu", "logical_cores", "ram_gb", "os", "python"):
        print(f"{key}: {machine[key]}")
    print(f"{args.applications} applications, {args.organizations} organizations, {HOURS} hours,")
    print("one answered request per application per hour")
    print()
    print("in memory:")
    for name, value in in_memory(args.applications, args.organizations).items():
        print(f"  {name:<32} {value}")
    if url is None:
        return
    found, times = asyncio.run(
        on_valkey(url, args.applications, args.organizations, args.flush_applications,
                  args.flush_requests)
    )  # fmt: skip
    print()
    print("valkey:")
    for kind, values in found.items():
        print(f"  {kind:<20} " + "  ".join(f"{k} {v}" for k, v in values.items()))
    print()
    print(
        f"one flush: {args.flush_applications} applications x {args.flush_requests} requests, "
        f"{FLUSH_RUNS} flushes"
    )
    print(f"  {'what':<24} {'n':>4} {'mean ms':>9} {'p50 ms':>9} {'p99 ms':>9} {'max ms':>9}")
    for r in times:
        print(
            f"  {r.store:<24} {r.writes:>4} {r.mean_ms:>9.3f} {r.p50_ms:>9.3f} "
            f"{r.p99_ms:>9.3f} {r.max_ms:>9.3f}"
        )


if __name__ == "__main__":
    main()
