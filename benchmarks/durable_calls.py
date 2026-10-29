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
- "chat, one provider": the twenty of a chat request served by one organization provider.

The Postgres rows run on the request pool, in a schema of their own, dropped afterwards (never
anything else). The address comes from ``GATEWAY_POSTGRES_URL`` and is never printed.

    GATEWAY_POSTGRES_URL=postgresql://postgres@127.0.0.1:5433/postgres \\
        uv run python -m benchmarks.durable_calls
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import os
import secrets
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

import asyncpg

from benchmarks.calibrate import describe_machine
from benchmarks.counter_calls import WARMUP_CALLS, _times
from benchmarks.usage_store_writes import WriteTimes
from gateway.applications import ApiKeyRecord, application_from_template
from gateway.clock import SystemEpoch, WallClock
from gateway.database import open_database
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.postgres import URL_ENV, PostgresDatabase, apply_schema, postgres_state
from gateway.providers import ProviderConfig
from gateway.routing import load_plans
from gateway.state import StateBackend, sqlite_state
from gateway.usage import UsageEvent
from gateway.vault import SealedKey

ORG, APP, KEY, PROVIDER = "default", "bench", "0" * 16, "prv_bench"


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


async def _measure(where: str, state: StateBackend, calls: int) -> list[WriteTimes]:
    await _seed(state)
    return [await _times(f"{name}, {where}", calls, call) for name, call in _calls(state).items()]


async def run(calls: int, url: str) -> list[WriteTimes]:
    results = []
    with tempfile.TemporaryDirectory() as folder:
        connection = open_database(Path(folder) / "bench.db")
        MigrationRunner(connection, MIGRATIONS, SystemEpoch()).migrate()
        try:
            results += await _measure("sqlite", sqlite_state(connection, SystemEpoch()), calls)
        finally:
            connection.close()
    schema = f"gwbench_{secrets.token_hex(8)}"
    admin = await asyncpg.connect(url)
    await admin.execute(f"CREATE SCHEMA {schema}")
    try:
        db = await PostgresDatabase.connect(url, WallClock(), schema=schema)
        try:
            await apply_schema(db)
            results += await _measure("postgres", postgres_state(db), calls)
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
    print(f"{'what':<30} {'n':>6} {'mean ms':>9} {'p50 ms':>9} {'p99 ms':>9} {'max ms':>9}")
    for r in asyncio.run(run(args.calls, url)):
        print(
            f"{r.store:<30} {r.writes:>6} {r.mean_ms:>9.3f} {r.p50_ms:>9.3f} "
            f"{r.p99_ms:>9.3f} {r.max_ms:>9.3f}"
        )


if __name__ == "__main__":
    main()
