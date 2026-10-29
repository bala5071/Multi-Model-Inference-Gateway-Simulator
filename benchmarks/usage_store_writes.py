"""How long one usage-event write takes, as docs/19-usage-store.md quotes.

MEASURED on the machine it runs on: real time, real disk. It times
``SqliteUsageStore.append`` (one autocommit INSERT into a write-ahead-logged database)
with SQLite's FULL and NORMAL sync settings, and the in-memory store for comparison.
Each write blocks the event loop for as long as it takes.

    uv run python -m benchmarks.usage_store_writes
    uv run python -m benchmarks.usage_store_writes --writes 5000 --dir D:/somewhere

The database is created in a new temporary folder under ``--dir`` (default: the system
temporary folder) and deleted afterwards, so the disk measured is the one that holds it.
"""

from __future__ import annotations

import argparse
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from benchmarks.calibrate import describe_machine
from gateway.clock import ClockEpoch, SimClock, WallClock
from gateway.database import open_database
from gateway.metrics import percentile
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.state import run_now
from gateway.usage import InMemoryUsageStore, SqliteUsageStore, UsageEvent, UsageStore

# A few writes first, so the file exists and its first pages are allocated.
WARMUP_WRITES = 20


@dataclass(frozen=True, slots=True)
class WriteTimes:
    store: str
    writes: int
    mean_ms: float
    p50_ms: float
    p99_ms: float
    max_ms: float


def _event(i: int) -> UsageEvent:
    return UsageEvent("bench-app", f"req-{i}", "economy", 120, 40, 32, 1e-5, 8e-6, 1.7e12 + i)


def time_writes(name: str, store: UsageStore, writes: int) -> WriteTimes:
    clock = WallClock()
    for i in range(WARMUP_WRITES):
        run_now(store.append(_event(i)))
    times = []
    for i in range(writes):
        started = clock.now_ms()
        run_now(store.append(_event(WARMUP_WRITES + i)))
        times.append(clock.now_ms() - started)
    return WriteTimes(
        store=name,
        writes=writes,
        mean_ms=sum(times) / len(times),
        p50_ms=percentile(times, 50),
        p99_ms=percentile(times, 99),
        max_ms=max(times),
    )


def run(writes: int, directory: Path | None) -> list[WriteTimes]:
    results = [time_writes("in-memory", InMemoryUsageStore(), writes)]
    with tempfile.TemporaryDirectory(dir=directory) as folder:
        for synchronous in ("FULL", "NORMAL"):
            path = Path(folder) / f"usage-{synchronous.lower()}.db"
            connection = open_database(path, synchronous=synchronous)
            MigrationRunner(connection, MIGRATIONS, ClockEpoch(SimClock())).migrate()
            results.append(
                time_writes(f"sqlite WAL {synchronous}", SqliteUsageStore(connection), writes)
            )
            connection.close()
    return results


def _table(results: Sequence[WriteTimes], fmt: Callable[[float], str]) -> list[str]:
    lines = [
        f"{'store':<18} {'writes':>6} {'mean ms':>9} {'p50 ms':>9} {'p99 ms':>9} {'max ms':>9}"
    ]
    for r in results:
        lines.append(
            f"{r.store:<18} {r.writes:>6} {fmt(r.mean_ms):>9} {fmt(r.p50_ms):>9} "
            f"{fmt(r.p99_ms):>9} {fmt(r.max_ms):>9}"
        )
    return lines


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--writes", type=int, default=2000)
    parser.add_argument("--dir", type=Path, help="folder to create the database in")
    args = parser.parse_args(argv)
    if args.writes < 1:
        parser.error("--writes must be at least 1")

    machine = describe_machine()
    print("MEASURED on this machine: real time, real disk. One append = one INSERT + commit.")
    for key in ("cpu", "logical_cores", "ram_gb", "os", "python"):
        print(f"{key}: {machine[key]}")
    print(f"database folder: {args.dir or tempfile.gettempdir()}")
    print(f"warm-up writes per store: {WARMUP_WRITES}; percentiles: nearest rank")
    print()
    for line in _table(run(args.writes, args.dir), lambda v: f"{v:.3f}"):
        print(line)


if __name__ == "__main__":
    main()
