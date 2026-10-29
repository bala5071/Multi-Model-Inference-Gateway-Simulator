"""How long pruning old usage events takes, as docs/21-persistence.md quotes.

MEASURED on the machine it runs on: real time, real disk. Builds a usage_events table of
``--rows`` events in a database set up as the server's (WAL, synchronous FULL, migrated),
of which ``--expired`` are older than the retention, then times:

- "batch of N": each ``SqliteUsageStore.prune(before, limit=N)``, as the retention loop runs
  it (one short transaction each); the loop yields to the event loop between batches, so
  the longest batch is the longest a request can wait behind pruning;
- "all at once": a single unbatched delete of the same number of expired events, in a
  fresh copy of the table, for comparison: what one pass would block for without batches.

    uv run python -m benchmarks.usage_retention
    uv run python -m benchmarks.usage_retention --rows 2000000 --expired 200000

The table is built in one transaction and is not part of any timing.
"""

from __future__ import annotations

import argparse
import sqlite3
import tempfile
from collections.abc import Sequence
from pathlib import Path

from benchmarks.calibrate import describe_machine
from benchmarks.usage_store_writes import WriteTimes
from gateway.clock import ClockEpoch, SimClock, WallClock
from gateway.database import open_database
from gateway.ledger import PRUNE_BATCH
from gateway.metrics import percentile
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.state import run_now
from gateway.usage import SqliteUsageStore

DAY_MS = 86_400_000.0
NOW_MS = 1_750_000_000_000.0
APPLICATIONS = 50


def build(path: Path, rows: int, expired: int) -> tuple[sqlite3.Connection, SqliteUsageStore]:
    """``rows`` events, ``expired`` of them 500 days old and the rest spread over 400 days."""
    connection = open_database(path)
    MigrationRunner(connection, MIGRATIONS, ClockEpoch(SimClock())).migrate()
    fresh = rows - expired
    events = (
        (
            f"app-{i % APPLICATIONS}",
            f"req-{i}",
            ("economy", "standard", "premium")[i % 3],
            120,
            40,
            32,
            1e-5,
            8e-6,
            NOW_MS - 500 * DAY_MS + i
            if i < expired
            else NOW_MS - (i - expired) * 400 * DAY_MS / fresh,
        )
        for i in range(rows)
    )
    connection.execute("BEGIN")
    connection.executemany(
        "INSERT INTO usage_events (application_id, request_id, model, prompt_tokens,"
        " cached_tokens, completion_tokens, base_cost_usd, billed_cost_usd, occurred_at_ms)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        events,
    )
    connection.execute("COMMIT")
    return connection, SqliteUsageStore(connection)


def _summary(name: str, times: list[float]) -> WriteTimes:
    return WriteTimes(
        store=name,
        writes=len(times),
        mean_ms=sum(times) / len(times),
        p50_ms=percentile(times, 50),
        p99_ms=percentile(times, 99),
        max_ms=max(times),
    )


def run(rows: int, expired: int, batch: int, directory: Path | None) -> list[WriteTimes]:
    clock = WallClock()
    cutoff = NOW_MS - 400 * DAY_MS
    results = []
    with tempfile.TemporaryDirectory(dir=directory) as folder:
        connection, store = build(Path(folder) / "batched.db", rows, expired)
        times, removed = [], 0
        while True:
            started = clock.now_ms()
            n = run_now(store.prune(cutoff, batch))
            times.append(clock.now_ms() - started)
            removed += n
            if n < batch:
                break
        assert removed == expired, (removed, expired)
        results.append(_summary(f"batch of {batch}", times))
        connection.close()

        connection, store = build(Path(folder) / "unbatched.db", rows, expired)
        started = clock.now_ms()
        assert run_now(store.prune(cutoff)) == expired
        results.append(_summary("all at once", [clock.now_ms() - started]))
        connection.close()
    return results


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Time pruning old usage events.")
    parser.add_argument("--rows", type=int, default=1_000_000)
    parser.add_argument("--expired", type=int, default=100_000)
    parser.add_argument("--batch", type=int, default=PRUNE_BATCH)
    parser.add_argument("--dir", type=Path, help="folder to create the databases in")
    args = parser.parse_args(argv)
    if not 0 < args.expired <= args.rows or args.batch < 1:
        parser.error("need 0 < --expired <= --rows and --batch >= 1")

    machine = describe_machine()
    print("MEASURED on this machine: real time, real disk (SQLite, WAL, synchronous FULL).")
    for key in ("cpu", "logical_cores", "ram_gb", "os", "python"):
        print(f"{key}: {machine[key]}")
    print(f"database folder: {args.dir or tempfile.gettempdir()}")
    print(
        f"table: {args.rows} events, {args.expired} past the retention; percentiles: nearest rank"
    )
    print()
    print(f"{'what':<16} {'n':>6} {'mean ms':>9} {'p50 ms':>9} {'p99 ms':>9} {'max ms':>9}")
    for r in run(args.rows, args.expired, args.batch, args.dir):
        print(
            f"{r.store:<16} {r.writes:>6} {r.mean_ms:>9.3f} {r.p50_ms:>9.3f} "
            f"{r.p99_ms:>9.3f} {r.max_ms:>9.3f}"
        )


if __name__ == "__main__":
    main()
