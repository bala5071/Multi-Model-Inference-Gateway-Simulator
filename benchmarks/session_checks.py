"""What checking a console session costs, and what writing last_seen_at adds, as docs/27
quotes.

MEASURED on the machine it runs on: real time, real disk. On a SQLite database set up as the
server's (WAL, synchronous FULL):

- "check, no write": ``AccountService.authenticate`` within a minute of the last write, as
  most requests are: a session read and a user read.
- "check + write": the same when the minute is up, so ``last_seen_at`` is written (at most
  once a minute per session).
- "the write alone": that one ``UPDATE``.

    uv run python -m benchmarks.session_checks
    uv run python -m benchmarks.session_checks --checks 5000 --dir D:/somewhere

The user, its password and the session are made here and never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import secrets
import tempfile
from collections.abc import Sequence
from pathlib import Path

from benchmarks.calibrate import describe_machine
from benchmarks.key_checks import WARMUP_CHECKS, _times
from benchmarks.usage_store_writes import WriteTimes
from gateway.applications import SqliteApplicationStore
from gateway.audit import AuditLog
from gateway.auth import LAST_SEEN_WRITE_MS, AccountService
from gateway.clock import SystemEpoch, WallClock
from gateway.database import open_database
from gateway.metrics import percentile
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.passwords import CHEAP, InlineHashRunner, PasswordHasher, Passwords
from gateway.users import AccountStore


def run(checks: int, directory: Path | None) -> list[WriteTimes]:
    epoch = SystemEpoch()
    with tempfile.TemporaryDirectory(dir=directory, ignore_cleanup_errors=True) as folder:
        connection = open_database(Path(folder) / "sessions.db")
        MigrationRunner(connection, MIGRATIONS, epoch).migrate()
        store = AccountStore(connection)
        # The hashing cost does not matter here: only the session check is timed.
        service = AccountService(
            store,
            AuditLog(connection),
            Passwords(PasswordHasher(CHEAP), InlineHashRunner()),
            organizations=SqliteApplicationStore(connection),
            clock=WallClock(),
            epoch=epoch,
            limiter_secret=secrets.token_bytes(32),
        )
        password = secrets.token_urlsafe(18)
        service.create_user("bench", password, "default")
        login = asyncio.run(service.login("bench", password))
        id_hash = login.session.id_hash
        results = [
            _times("check, no write", checks, lambda: service.authenticate(login.session_id))
        ]

        def stale() -> None:
            # Untimed: put the last write more than a minute back, so the check writes.
            connection.execute(
                "UPDATE sessions SET last_seen_at_ms = ? WHERE id_hash = ?",
                (epoch.epoch_ms() - LAST_SEEN_WRITE_MS - 1.0, id_hash),
            )

        clock = WallClock()
        times = []
        for i in range(WARMUP_CHECKS + checks):
            stale()
            started = clock.now_ms()
            service.authenticate(login.session_id)
            if i >= WARMUP_CHECKS:
                times.append(clock.now_ms() - started)
        results.append(
            WriteTimes(
                "check + write",
                checks,
                sum(times) / len(times),
                percentile(times, 50),
                percentile(times, 99),
                max(times),
            )
        )
        stamps = itertools.count(epoch.epoch_ms())
        results.append(
            _times("the write alone", checks, lambda: store.touch_session(id_hash, next(stamps)))
        )
        connection.close()
    return results


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Time console session checks.")
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
    print(f"{'what':<18} {'n':>6} {'mean ms':>9} {'p50 ms':>9} {'p99 ms':>9} {'max ms':>9}")
    for r in run(args.checks, args.dir):
        print(
            f"{r.store:<18} {r.writes:>6} {r.mean_ms:>9.3f} {r.p50_ms:>9.3f} "
            f"{r.p99_ms:>9.3f} {r.max_ms:>9.3f}"
        )


if __name__ == "__main__":
    main()
