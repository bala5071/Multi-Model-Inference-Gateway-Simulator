"""What one alert evaluation cycle costs, for 1, 100, 1,000 and 5,000 applications (the plan's
sections 11.7 and 20; docs/45).

MEASURED on the machine it runs on: real time, a SQLite file and a real Postgres, beside the
server (never on a request). Each application has the default rules of note 44 (five enabled)
and one key expiring in 60 days; its rollups are in memory, with one application in ten failing
(its error rate over the threshold) and one in twenty refused for budget, so a cycle has
transitions to make. The cycle is ``AlertEvaluator.cycle`` with the server's own reads
(``gateway.app``'s durable signals and settings, a page at a time; on Postgres in the
evaluator's own pool), fenced as the holder: first the cycle that opens the incidents, then
three steady cycles, each timed, its store calls counted, and its longest page's reads timed.
Usage comes from a ``CostLedger`` on both stores, as in the first measurement.

    GATEWAY_POSTGRES_URL=... uv run python -m benchmarks.alert_evaluation
    GATEWAY_POSTGRES_URL=... uv run python -m benchmarks.alert_evaluation --sizes 1 100

The Postgres rows run in a schema of their own, dropped afterwards (never anything else). The
address comes from ``GATEWAY_POSTGRES_URL`` and is never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg

from benchmarks.calibrate import describe_machine
from gateway.alertrecorder import MINUTE_MS
from gateway.alertrollups import InMemoryAlertRollups
from gateway.alertrules import default_rules
from gateway.app import _DurableSignals, alert_settings, default_platform_settings
from gateway.applications import ApiKeyRecord, application_from_template
from gateway.breaker import Breakers
from gateway.clock import EpochSource, SystemEpoch, WallClock
from gateway.database import open_database
from gateway.evaluator import AlertEvaluator
from gateway.incidents import Fence
from gateway.ledger import CostLedger
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.postgres import URL_ENV, PostgresDatabase, apply_schema, postgres_state
from gateway.routing import load_plans
from gateway.state import StateBackend, sqlite_state

SIZES = (1, 100, 1000, 5000)
STEADY_CYCLES = 3
ORG = "default"
DAY_MS = 86_400_000.0


@dataclass(frozen=True, slots=True)
class CycleTimes:
    store: str
    applications: int
    first_ms: float
    first_calls: int
    steady_ms: tuple[float, ...]
    steady_calls: int
    longest_page_ms: float
    cut_short: int


class _Counting:
    """A store whose coroutine methods count their calls."""

    def __init__(self, inner: Any, counter: list[int]) -> None:
        self._inner = inner
        self._counter = counter

    def __getattr__(self, name: str) -> Any:
        found = getattr(self._inner, name)
        if not asyncio.iscoroutinefunction(found):
            return found

        async def counted(*args: Any, **kwargs: Any) -> Any:
            self._counter[0] += 1
            return await found(*args, **kwargs)

        return counted


async def _seed(state: StateBackend, size: int, epoch: EpochSource) -> None:
    now = epoch.epoch_ms()
    async with state.applications.write_transaction():
        for i in range(size):
            app_id = f"app{i:05d}"
            await state.applications.add_application(
                application_from_template(
                    app_id, f"Bench {i}", load_plans(), "pro", organization_id=ORG,
                    created_at_ms=now,
                )
            )  # fmt: skip
            await state.applications.add_key(
                ApiKeyRecord(f"{i:016x}", app_id, "bench", "0" * 64, now, now + 60 * DAY_MS)
            )
            for rule in default_rules(app_id, now):
                await state.rules.insert_rule(rule)


def _rollups(size: int, now_ms: float) -> InMemoryAlertRollups:
    rollups = InMemoryAlertRollups()
    minute = int(now_ms // MINUTE_MS)
    for i in range(size):
        failed = 10 if i % 10 == 0 else 0
        refused = 2 if i % 20 == 5 else 0
        rollups.absorb(
            {
                (f"app{i:05d}", m): {
                    "requests": 40,
                    "status:5xx": failed,
                    "reject:budget_exhausted": refused,
                }
                for m in range(minute - 70, minute + 5)
            }
        )
    return rollups


class _TimedEvaluator(AlertEvaluator):
    """The server's evaluator, its pages' reads timed (the time box's bound, section 11.7)."""

    longest_page_ms = 0.0

    async def _read_page(self, *args: Any, **kwargs: Any) -> Any:
        started = self._clock.now_ms()
        found = await super()._read_page(*args, **kwargs)
        self.longest_page_ms = max(self.longest_page_ms, self._clock.now_ms() - started)
        return found


async def _measure(name: str, state: StateBackend, size: int) -> CycleTimes:
    clock, epoch = WallClock(), SystemEpoch()
    await _seed(state, size, epoch)
    calls = [0]
    applications = _Counting(state.applications, calls)
    ledger = CostLedger(clock, store=_Counting(state.usage, calls), epoch=epoch)
    settings_of, settings_many = alert_settings(
        _Counting(state.settings, calls), default_platform_settings()
    )
    incidents = _Counting(state.incidents, calls)
    evaluator = _TimedEvaluator(
        incidents=incidents,
        rules=_Counting(state.rules, calls),
        rollups=_Counting(_rollups(size, epoch.epoch_ms()), calls),
        signals=_DurableSignals(
            applications, ledger.usage, settings_of, None, Breakers(clock), epoch,
            usage_many=ledger.usage_many, settings_many=settings_many,
        ),
        clock=clock,
        holder="bench",
        random_bytes=secrets.token_bytes,
    )  # fmt: skip

    async def one() -> tuple[float, int]:
        lease = await state.incidents.attempt_lease("bench")
        calls[0] = 0
        started = clock.now_ms()
        await evaluator.cycle(Fence("bench", lease.epoch), lease.now_ms)
        await incidents.complete_cycle(Fence("bench", lease.epoch))
        return clock.now_ms() - started, calls[0]

    first_ms, first_calls = await one()
    steady: list[float] = []
    steady_calls = 0
    for _ in range(STEADY_CYCLES):
        elapsed, steady_calls = await one()
        steady.append(elapsed)
    return CycleTimes(
        name, size, first_ms, first_calls, tuple(steady), steady_calls,
        evaluator.longest_page_ms, evaluator.cut_short,
    )  # fmt: skip


async def run(sizes: Sequence[int], url: str, env: Mapping[str, str]) -> list[CycleTimes]:
    results = []
    for size in sizes:
        with tempfile.TemporaryDirectory() as folder:
            connection = open_database(Path(folder) / "bench.db")
            MigrationRunner(connection, MIGRATIONS, SystemEpoch()).migrate()
            try:
                results.append(
                    await _measure("sqlite", sqlite_state(connection, SystemEpoch()), size)
                )
            finally:
                connection.close()
        schema = f"gwbench_{secrets.token_hex(8)}"
        admin = await asyncpg.connect(url)
        await admin.execute(f"CREATE SCHEMA {schema}")
        try:
            db = await PostgresDatabase.connect(url, WallClock(), env=env, schema=schema)
            try:
                await apply_schema(db)
                async with db.evaluation():  # the evaluator's own pool, as the server's
                    results.append(await _measure("postgres", postgres_state(db), size))
            finally:
                await db.aclose()
        finally:
            await admin.execute(f"DROP SCHEMA {schema} CASCADE")
            await admin.close()
    return results


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Time one alert evaluation cycle.")
    parser.add_argument("--sizes", type=int, nargs="+", default=list(SIZES))
    args = parser.parse_args(argv)
    if any(size < 1 for size in args.sizes):
        parser.error("--sizes must be at least 1")
    url = os.environ.get(URL_ENV)
    if not url:
        parser.error(f"set {URL_ENV} to the Postgres to measure against")

    machine = describe_machine()
    print("MEASURED on this machine: real time, a SQLite file and a real Postgres.")
    for key in ("cpu", "logical_cores", "ram_gb", "os", "python"):
        print(f"{key}: {machine[key]}")
    print(f"default rules per application: 8 (5 enabled); steady cycles: {STEADY_CYCLES}")
    print()
    print(
        f"{'store':<9} {'apps':>5} {'first ms':>10} {'calls':>7} "
        f"{'steady ms (each)':>28} {'calls':>7} {'page ms':>8} {'cut':>4}"
    )
    found: list[CycleTimes] = asyncio.run(run(args.sizes, url, os.environ))
    for r in found:
        steady = " ".join(f"{t:.1f}" for t in r.steady_ms)
        print(
            f"{r.store:<9} {r.applications:>5} {r.first_ms:>10.1f} {r.first_calls:>7} "
            f"{steady:>28} {r.steady_calls:>7} {r.longest_page_ms:>8.1f} {r.cut_short:>4}"
        )


if __name__ == "__main__":
    main()
