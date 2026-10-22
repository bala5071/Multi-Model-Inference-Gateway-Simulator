"""What a key check costs per request, and what writing last_used_at would add.

MEASURED on the machine it runs on: real time, real disk. As docs/20-applications-api-keys.md
quotes. On a SQLite database set up as the server's (WAL, synchronous FULL):

- "check, batched": ``ApiKeyService.authenticate`` as the server runs it. Two indexed reads
  and one HMAC; last_used_at is only noted in memory (the flush interval is not reached).
- "check + write per request": the same, plus writing last_used_at for that key at once:
  what the server would pay if it did not batch.
- "flush of N keys": one batched write of N keys' last_used_at, which the server does at
  most once a minute.

    uv run python -m benchmarks.key_checks
    uv run python -m benchmarks.key_checks --checks 5000 --dir D:/somewhere

Each check blocks the event loop for as long as it takes.
"""

from __future__ import annotations

import argparse
import itertools
import secrets
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from benchmarks.calibrate import describe_machine
from benchmarks.usage_store_writes import WriteTimes
from gateway.apikeys import ApiKeyService
from gateway.applications import SqliteApplicationStore, seed_demo_applications
from gateway.clock import SystemEpoch, WallClock
from gateway.database import open_database
from gateway.metrics import percentile
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.routing import load_plans

WARMUP_CHECKS = 20
FLUSH_SIZES = (1, 100, 1000)


def _times(name: str, count: int, step: Callable[[], object]) -> WriteTimes:
    clock = WallClock()
    for _ in range(WARMUP_CHECKS):
        step()
    times = []
    for _ in range(count):
        started = clock.now_ms()
        step()
        times.append(clock.now_ms() - started)
    return WriteTimes(
        store=name,
        writes=count,
        mean_ms=sum(times) / len(times),
        p50_ms=percentile(times, 50),
        p99_ms=percentile(times, 99),
        max_ms=max(times),
    )


def run(checks: int, directory: Path | None) -> list[WriteTimes]:
    epoch = SystemEpoch()
    results = []
    with tempfile.TemporaryDirectory(dir=directory) as folder:
        connection = open_database(Path(folder) / "keys.db")
        MigrationRunner(connection, MIGRATIONS, epoch).migrate()
        store = SqliteApplicationStore(connection)
        seed_demo_applications(store, load_plans(), created_at_ms=epoch.epoch_ms())
        # A throwaway pepper and keys: they live in this function and are never printed.
        keys = ApiKeyService(store, secrets.token_bytes(32), epoch, last_used_flush_ms=float("inf"))
        issued = keys.issue("acme", "benchmark")
        many = [keys.issue("globex", f"k{i}").record.key_id for i in range(max(FLUSH_SIZES))]

        results.append(_times("check, batched", checks, lambda: keys.authenticate(issued.key)))
        # Strictly increasing times, so every write really changes the row: the system clock
        # can return the same reading twice, and an unchanged time is not written.
        stamps = itertools.count(epoch.epoch_ms())

        def check_and_write() -> None:
            keys.authenticate(issued.key)
            store.record_last_used({issued.record.key_id: next(stamps)})

        def flush(batch: list[str]) -> Callable[[], None]:
            return lambda: store.record_last_used(dict.fromkeys(batch, next(stamps)))

        results.append(_times("check + write per request", checks, check_and_write))
        for size in FLUSH_SIZES:
            results.append(
                _times(f"flush of {size} keys", max(1, checks // 20), flush(many[:size]))
            )
        connection.close()
    return results


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Time API key checks and last_used_at writes.")
    parser.add_argument("--checks", type=int, default=2000)
    parser.add_argument("--dir", type=Path, help="folder to create the database in")
    args = parser.parse_args(argv)
    if args.checks < 1:
        parser.error("--checks must be at least 1")

    machine = describe_machine()
    print("MEASURED on this machine: real time, real disk (SQLite, WAL, synchronous FULL).")
    for key in ("cpu", "logical_cores", "ram_gb", "os", "python"):
        print(f"{key}: {machine[key]}")
    print(f"database folder: {args.dir or tempfile.gettempdir()}")
    print(f"warm-up per row: {WARMUP_CHECKS}; percentiles: nearest rank")
    print()
    print(f"{'what':<26} {'n':>6} {'mean ms':>9} {'p50 ms':>9} {'p99 ms':>9} {'max ms':>9}")
    for r in run(args.checks, args.dir):
        print(
            f"{r.store:<26} {r.writes:>6} {r.mean_ms:>9.3f} {r.p50_ms:>9.3f} "
            f"{r.p99_ms:>9.3f} {r.max_ms:>9.3f}"
        )


if __name__ == "__main__":
    main()
