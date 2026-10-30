"""What one durable store call costs on SQLite and on Postgres.

MEASURED on the machine it runs on: real time, a real SQLite file and a real Postgres
(docs/36-postgres.md). As docs/36 quotes. Each row is one call, awaited on the event loop, as
the request path makes it:

- "read": ``lookup_key``, the read every keyed request makes (the key, its application and
  whether its organization is enabled; on Postgres also the database's time);
- "write": ``append`` of one usage event, the write every charged request makes;
- "chat, gateway tier": the six store calls of a chat request served by the gateway's own tier
  (after the first request of a period), one after another, as
  ``tests/test_chat_routing.py`` counts them;
- "chat, one provider": the twenty of a chat request served by one organization provider;
- "budget, admit and settle": what a charged request asks of its budget (docs/37): on SQLite
  the ledger's check (in memory after the period's first request) and the usage row; on
  Postgres a reserve and a settle (``PostgresBudgets``);
- "budget, reserve", "budget, settle", "budget, release" (Postgres): each alone;
- "chat, gateway tier, shared" (Postgres): the chat request of the third row with the shared
  budgets, as a shared server makes it: ``lookup_key``, the modes twice, a reserve, a settle.

The Postgres rows run on the request pool, in a schema of their own, dropped afterwards (never
anything else). The address comes from ``GATEWAY_POSTGRES_URL`` and is never printed. The
compose file's test database has no password, which the gateway refuses unless
``GATEWAY_POSTGRES_TEST_NO_PASSWORD=1`` says it is a test database:

    GATEWAY_POSTGRES_URL=postgresql://postgres@127.0.0.1:5433/postgres \\
    GATEWAY_POSTGRES_TEST_NO_PASSWORD=1 uv run python -m benchmarks.durable_calls
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import os
import secrets
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path

import asyncpg

from benchmarks.calibrate import describe_machine
from benchmarks.counter_calls import WARMUP_CALLS, _times
from benchmarks.usage_store_writes import WriteTimes
from gateway.applications import ApiKeyRecord, application_from_template
from gateway.clock import SystemEpoch, WallClock
from gateway.database import open_database
from gateway.ledger import Budgets, CostLedger, Reservation
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.postgres import (
    URL_ENV,
    PostgresBudgets,
    PostgresDatabase,
    apply_schema,
    postgres_state,
)
from gateway.providers import ProviderConfig
from gateway.routing import load_plans
from gateway.schema import RequestResult
from gateway.state import StateBackend, sqlite_state
from gateway.usage import UsageEvent
from gateway.vault import SealedKey

ORG, APP, KEY, PROVIDER = "default", "bench", "0" * 16, "prv_bench"
# Never reached, so every reserve is admitted; a bound and a hold of no particular size.
BUDGET, BOUND, HOLD_MS = 10**15, 300, 180_000.0
RESULT = RequestResult("bench", "economy", 0.0, 0.0, 0.0, 1.0, 10, 5, 0.0)


async def _seed(state: StateBackend) -> None:
    await state.applications.add_application(
        application_from_template(
            APP, "Bench", load_plans(), "pro", organization_id=ORG, created_at_ms=1.0
        )
    )
    await state.applications.add_key(ApiKeyRecord(KEY, APP, "bench", "0" * 64, 1.0))
    await state.providers.add(
        ProviderConfig(
            PROVIDER,
            ORG,
            "openai_compatible",
            "https://bench.invalid",
            "m",
            "economy",
            "primary",
            True,
            True,
            None,
            None,
            {},
            None,
            1.0,
        )
    )
    sealed = SealedKey("k", b"\x00" * 12, b"\x00" * 48, None)
    await state.providers.put_key(PROVIDER, sealed, 1.0)


def _calls(state: StateBackend) -> dict[str, Callable[[], Awaitable[object]]]:
    numbers = itertools.count()

    def event() -> UsageEvent:
        return UsageEvent(APP, f"req-{next(numbers)}", "economy", 10, 0, 5, 0.1, 0.1, 2.0)

    async def modes() -> None:
        await state.modes.organization_mode(ORG)
        await state.modes.application_mode(APP)

    async def candidates() -> None:
        await modes()
        for config in await state.providers.list_for(ORG):
            await state.providers.get_key(config.id)

    async def chat_gateway_tier() -> None:
        await state.applications.lookup_key(KEY)
        await modes()
        await modes()
        await state.usage.append(event())

    async def chat_one_provider() -> None:
        await state.applications.lookup_key(KEY)
        await candidates()
        await candidates()
        await modes()
        await state.providers.get_key(PROVIDER)
        await state.providers.get_key(PROVIDER)
        for _ in range(3):
            await modes()
        await state.usage.append(event())

    return {
        "read": lambda: state.applications.lookup_key(KEY),
        "write": lambda: state.usage.append(event()),
        "chat, gateway tier": chat_gateway_tier,
        "chat, one provider": chat_one_provider,
    }


def _budget_calls(
    state: StateBackend, budgets: Budgets, shared: bool
) -> dict[str, Callable[[], Awaitable[object]]]:
    held: list[Reservation] = []

    async def admit_and_settle() -> None:
        reservation = await budgets.reserve(APP, BUDGET, BOUND, hold_ms=HOLD_MS)
        await budgets.settle(reservation, RESULT, model="economy", price_multiplier=1.0)

    if not shared:
        return {"budget, admit and settle": admit_and_settle}

    async def reserve() -> None:
        held.append(await budgets.reserve(APP, BUDGET, BOUND, hold_ms=HOLD_MS))

    async def settle() -> None:
        await budgets.settle(held.pop(), RESULT, model="economy", price_multiplier=1.0)

    async def release() -> None:
        await budgets.release(await budgets.reserve(APP, BUDGET, BOUND, hold_ms=HOLD_MS))

    async def chat_gateway_tier_shared() -> None:
        await state.applications.lookup_key(KEY)
        for _ in range(2):
            await state.modes.organization_mode(ORG)
            await state.modes.application_mode(APP)
        await admit_and_settle()

    # The settle row settles what the reserve row reserved, one each, warm-up included.
    return {
        "budget, admit and settle": admit_and_settle,
        "budget, reserve": reserve,
        "budget, settle": settle,
        "budget, release": release,
        "chat, gateway tier, shared": chat_gateway_tier_shared,
    }


async def _measure(
    where: str, state: StateBackend, budgets: Budgets, calls: int, *, shared: bool
) -> list[WriteTimes]:
    await _seed(state)
    rows = {**_calls(state), **_budget_calls(state, budgets, shared)}
    return [await _times(f"{name}, {where}", calls, call) for name, call in rows.items()]


async def run(calls: int, url: str, env: Mapping[str, str]) -> list[WriteTimes]:
    results = []
    with tempfile.TemporaryDirectory() as folder:
        connection = open_database(Path(folder) / "bench.db")
        MigrationRunner(connection, MIGRATIONS, SystemEpoch()).migrate()
        try:
            state = sqlite_state(connection, SystemEpoch())
            ledger = CostLedger(WallClock(), store=state.usage, epoch=SystemEpoch())
            results += await _measure("sqlite", state, ledger, calls, shared=False)
        finally:
            connection.close()
    schema = f"gwbench_{secrets.token_hex(8)}"
    admin = await asyncpg.connect(url)
    await admin.execute(f"CREATE SCHEMA {schema}")
    try:
        db = await PostgresDatabase.connect(url, WallClock(), env=env, schema=schema)
        try:
            await apply_schema(db)
            budgets = PostgresBudgets(db, WallClock())
            results += await _measure("postgres", postgres_state(db), budgets, calls, shared=True)
        finally:
            await db.aclose()
    finally:
        await admin.execute(f"DROP SCHEMA {schema} CASCADE")
        await admin.close()
    return results


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Time one durable call, SQLite and Postgres.")
    parser.add_argument("--calls", type=int, default=2000)
    args = parser.parse_args(argv)
    if args.calls < 1:
        parser.error("--calls must be at least 1")
    url = os.environ.get(URL_ENV)
    if not url:
        parser.error(f"set {URL_ENV} to the Postgres to measure against")

    machine = describe_machine()
    print("MEASURED on this machine: real time, a SQLite file and a real Postgres.")
    for key in ("cpu", "logical_cores", "ram_gb", "os", "python"):
        print(f"{key}: {machine[key]}")
    print(f"SQLite folder: {tempfile.gettempdir()}")
    print(f"warm-up per row: {WARMUP_CALLS}; percentiles: nearest rank")
    print()
    print(f"{'what':<38} {'n':>6} {'mean ms':>9} {'p50 ms':>9} {'p99 ms':>9} {'max ms':>9}")
    for r in asyncio.run(run(args.calls, url, os.environ)):
        print(
            f"{r.store:<38} {r.writes:>6} {r.mean_ms:>9.3f} {r.p50_ms:>9.3f} "
            f"{r.p99_ms:>9.3f} {r.max_ms:>9.3f}"
        )


if __name__ == "__main__":
    main()
