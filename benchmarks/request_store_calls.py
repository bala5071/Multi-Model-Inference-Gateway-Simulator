"""One chat request's store calls and Postgres round trips, counted and timed as it runs.

MEASURED on the machine it runs on: real time, a SQLite file and a real Postgres, in process
(docs/38-request-round-trips.md). Each request goes through the whole app
(``httpx.ASGITransport``): key check, plan, rate limit, budget, router, adapter, settle. No model
and no provider time is in it: the gateway tier is an OpenAI-compatible adapter at a local
destination and the organization provider sits behind the real egress guard, both answered by a
scripted network (``benchmarks.chat_routing``).

- "gateway tier": answered by the gateway's own tier, ``local_only``;
- "org provider": answered by one organization provider, ``hosted_primary_with_local_fallback``.

Each case runs twice, against two kinds of store:

- on SQLite with the process's own ledger, as the default server runs;
- on Postgres with the shared budgets, as a shared server runs (``GATEWAY_STATE=shared``),
  minus Valkey: the counters stay in memory, so only the durable stores are measured.

Per request, every public store call is counted as it is made (a call made inside another,
such as the settle's ``append``, belongs to the outer one), and the time spent inside them is
summed. On Postgres, every round trip is counted by kind and timed: ``BEGIN``, ``SET`` (the
transaction's statement timeout), ``COMMIT``, ``RESET`` (the pool's reset of a connection given
back to it) and the statements themselves. Store time not spent in a round trip is the
client's own work (the pool, asyncpg, the stores).

The Postgres rows run in a schema of their own, dropped afterwards (never anything else). The
address comes from ``GATEWAY_POSTGRES_URL`` and is never printed. The pepper and the provider key
are made here and never printed. The compose file's test database has no password:

    GATEWAY_POSTGRES_URL=postgresql://postgres@127.0.0.1:5433/postgres \\
    GATEWAY_POSTGRES_TEST_NO_PASSWORD=1 uv run python -m benchmarks.request_store_calls
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import inspect
import os
import secrets
import statistics
import tempfile
from collections import Counter
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncpg
import httpx

from benchmarks.calibrate import describe_machine
from benchmarks.chat_routing import OPERATOR, Network, _resolve
from gateway.adapters import OpenAICompatibleAdapter
from gateway.apikeys import ApiKeyService, ApplicationAccess
from gateway.app import create_app
from gateway.applications import application_from_template
from gateway.audit import AuditLog
from gateway.backend_config import TierBudget
from gateway.backends import Backend
from gateway.clock import SystemEpoch, WallClock
from gateway.continuous import build_continuous_registry
from gateway.database import open_database
from gateway.egress import LocalDestinations, operator_client, organization_client
from gateway.ledger import CostLedger
from gateway.metrics import percentile
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.modes import Mode
from gateway.overload import OverloadPolicy
from gateway.postgres import (
    URL_ENV,
    PostgresBudgets,
    PostgresDatabase,
    apply_schema,
    postgres_state,
)
from gateway.profiles import Tier
from gateway.providers import ProviderConfig, ProviderService
from gateway.redaction import Redactor
from gateway.routing import load_plans
from gateway.state import StateBackend, sqlite_state
from gateway.vault import Vault

WARMUP = 20
ORG, APP, PROVIDER = "default", "bench", "prv_00000000000000a1"
CASES = ("gateway tier", "org provider")
STORES = ("sqlite", "postgres")
# Never reached and never limiting: a budget, a rate and a bound of no particular size.
BUDGET, RATE = 10**15, 10**9
BOUNDS = {t.value: TierBudget(context_tokens=4096, reserve_completion_tokens=256) for t in Tier}
TIMEOUT_MS = 600_000.0
KINDS = ("BEGIN", "SET", "statement", "COMMIT", "RESET")


@dataclass
class Request:
    """What one request asked of its stores."""

    calls: list[str] = field(default_factory=list)
    store_ms: float = 0.0
    trips: Counter[str] = field(default_factory=Counter)
    trip_ms: dict[str, float] = field(default_factory=dict)


@dataclass
class Case:
    name: str
    store: str
    request_ms: list[float]
    requests: list[Request]

    def calls(self) -> list[str]:
        """The store calls of a request, the same for every request measured (checked)."""
        sequences = {tuple(r.calls) for r in self.requests}
        if len(sequences) != 1:
            raise RuntimeError(f"{self.name}, {self.store}: the store calls varied: {sequences}")
        return list(next(iter(sequences)))


class Recorder:
    """Counts and times store calls and Postgres round trips into the current request."""

    def __init__(self, clock: WallClock) -> None:
        self._clock = clock
        self._depth = 0
        self.current: Request | None = None

    def store_call(self, name: str, method: Callable[..., Any]) -> Callable[..., Any]:
        recorder = self

        async def call(self: Any, *args: Any, **kwargs: Any) -> Any:
            if recorder.current is None or recorder._depth > 0:
                return await method(self, *args, **kwargs)
            recorder._depth += 1
            started = recorder._clock.now_ms()
            try:
                return await method(self, *args, **kwargs)
            finally:
                recorder._depth -= 1
                if recorder.current is not None:
                    recorder.current.calls.append(name)
                    recorder.current.store_ms += recorder._clock.now_ms() - started

        return call

    def round_trip(self, method: Callable[..., Any]) -> Callable[..., Any]:
        recorder = self

        async def call(self: Any, query: str, *args: Any, **kwargs: Any) -> Any:
            started = recorder._clock.now_ms()
            try:
                return await method(self, query, *args, **kwargs)
            finally:
                if recorder.current is not None:
                    kind = _kind(query)
                    recorder.current.trips[kind] += 1
                    spent = recorder._clock.now_ms() - started
                    trip_ms = recorder.current.trip_ms
                    trip_ms[kind] = trip_ms.get(kind, 0.0) + spent

        return call


def _kind(query: str) -> str:
    if "pg_advisory_unlock_all" in query:  # asyncpg's reset query
        return "RESET"
    word = query.split()[0].upper().rstrip(";")
    if word in ("BEGIN", "COMMIT"):
        return word
    if word == "SET" and "statement_timeout" in query:
        return "SET"
    return "statement"


@contextmanager
def _patched(recorder: Recorder, classes: Sequence[type], postgres: bool) -> Iterator[None]:
    """Every public coroutine method of ``classes`` counted, and on Postgres every round trip;
    put back afterwards."""
    saved: list[tuple[type, str, Any]] = []
    try:
        for cls in classes:
            for name, method in list(vars(cls).items()):
                if name.startswith("_") or not inspect.iscoroutinefunction(method):
                    continue
                saved.append((cls, name, method))
                setattr(cls, name, recorder.store_call(name, method))
        if postgres:
            connection = asyncpg.connection.Connection
            for name in ("execute", "fetch", "fetchrow", "fetchval"):
                original = getattr(connection, name)
                saved.append((connection, name, original))
                setattr(connection, name, recorder.round_trip(original))
        yield
    finally:
        for cls, name, method in reversed(saved):
            setattr(cls, name, method)


async def _seed(state: StateBackend, providers: ProviderService, case: str) -> None:
    """The application and, for "org provider", the provider, its key and the modes."""
    application = application_from_template(
        APP, "Bench", load_plans(), "pro", organization_id=ORG, created_at_ms=1.0
    )
    plan = dataclasses.replace(application.plan, monthly_token_budget=BUDGET)
    await state.applications.add_application(
        dataclasses.replace(application, plan=plan, requests_per_minute=RATE)
    )
    if case == "org provider":
        await state.modes.set_organization_mode(ORG, Mode.HOSTED_PRIMARY_WITH_LOCAL_FALLBACK, 0.0)
        await state.modes.set_application_mode(APP, Mode.HOSTED_PRIMARY_WITH_LOCAL_FALLBACK, 0.0)
        config = ProviderConfig(
            PROVIDER, ORG, "openai_compatible", "https://a.provider.bench/v1", "model-x",
            "economy", "primary", True, True, 1.0, 2.0, {}, None, 0.0,
        )  # fmt: skip
        await state.providers.add(config)
        key = secrets.token_urlsafe(32)
        await state.providers.put_key(PROVIDER, providers.vault.seal(key, config.binding), 0.0)


async def _case(
    case: str,
    store: str,
    state: StateBackend,
    rounds: int,
    *,
    budgets: PostgresBudgets | None,
    store_clock: Any,
) -> Case:
    clock, epoch = WallClock(), SystemEpoch()
    templates = load_plans()
    tls = httpx.create_ssl_context()  # once, as the server does
    providers = ProviderService(
        state.providers,
        Vault(secrets.token_bytes(32)),
        AuditLog(state.audit),
        clock,
        epoch,
        Redactor(),
        lambda: organization_client(_resolve, inner=Network(), ssl_context=tls),
    )
    await _seed(state, providers, case)
    keys = ApiKeyService(
        state.applications, secrets.token_bytes(32), epoch, store_clock=store_clock
    )
    api_key = (await keys.issue(APP, "bench")).key
    local = LocalDestinations.parse("127.0.0.1:8081")
    adapters: dict[str, Backend] = {
        "economy": OpenAICompatibleAdapter(
            name="economy",
            base_url=OPERATOR,
            model="local-model",
            client=operator_client(_resolve, local, inner=Network()),
        )
    }
    registry = build_continuous_registry(clock, seed=7)
    app = create_app(
        registry,
        clock,
        applications=ApplicationAccess(keys, templates),
        usage_store=state.usage,
        epoch=epoch,
        providers=providers,
        chat_adapters=adapters,
        modes=state.modes,
        local_destinations=local,
        startup_applications=[],
        overload=OverloadPolicy(request_timeout_ms=TIMEOUT_MS),
        budgets=budgets,
        reservation_bounds=BOUNDS if budgets is not None else None,
    )
    recorder = Recorder(clock)
    classes: list[type] = [
        type(state.applications),
        type(state.modes),
        type(state.providers),
        type(state.usage),
        type(state.accounts),
        type(state.audit),
        PostgresBudgets if budgets is not None else CostLedger,
    ]
    body = {"model": "economy", "messages": [{"role": "user", "content": "Say hello."}]}
    headers = {"Authorization": f"Bearer {api_key}"}
    expected = PROVIDER if case == "org provider" else "gateway"
    times: list[float] = []
    requests: list[Request] = []
    transport = httpx.ASGITransport(app=app)
    try:
        with _patched(recorder, classes, postgres=budgets is not None):
            async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
                for i in range(WARMUP + rounds):
                    recorder.current = Request()
                    started = clock.now_ms()
                    response = await client.post("/v1/chat/completions", json=body, headers=headers)
                    elapsed = clock.now_ms() - started
                    request, recorder.current = recorder.current, None
                    if response.status_code != 200:
                        raise RuntimeError(f"{case}, {store}: {response.status_code}")
                    if response.headers["x-provider"] != expected:
                        raise RuntimeError(f"{case}: answered by {response.headers['x-provider']}")
                    if i >= WARMUP:
                        times.append(elapsed)
                        requests.append(request)
    finally:
        await registry.aclose()
    return Case(case, store, times, requests)


@asynccontextmanager
async def _sqlite() -> AsyncIterator[tuple[StateBackend, None]]:
    with tempfile.TemporaryDirectory() as folder:
        connection = open_database(Path(folder) / "bench.db")
        try:
            MigrationRunner(connection, MIGRATIONS, SystemEpoch()).migrate()
            yield sqlite_state(connection, SystemEpoch()), None
        finally:
            connection.close()


@asynccontextmanager
async def _postgres(
    url: str, env: Mapping[str, str]
) -> AsyncIterator[tuple[StateBackend, PostgresDatabase]]:
    schema = f"gwbench_{secrets.token_hex(8)}"
    admin = await asyncpg.connect(url)
    await admin.execute(f"CREATE SCHEMA {schema}")
    try:
        db = await PostgresDatabase.connect(url, WallClock(), env=env, schema=schema)
        try:
            await apply_schema(db)
            yield postgres_state(db), db
        finally:
            await db.aclose()
    finally:
        await admin.execute(f"DROP SCHEMA {schema} CASCADE")
        await admin.close()


async def run(rounds: int, url: str, env: Mapping[str, str]) -> list[Case]:
    """Each case on each store, in a database of its own."""
    results = []
    for case in CASES:
        async with _sqlite() as (state, _):
            results.append(
                await _case(case, "sqlite", state, rounds, budgets=None, store_clock=None)
            )
        async with _postgres(url, env) as (state, db):
            budgets = PostgresBudgets(db, WallClock())
            results.append(
                await _case(case, "postgres", state, rounds, budgets=budgets, store_clock=db)
            )
    return results


def _median(values: Sequence[float]) -> float:
    return statistics.median(values)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Count and time one chat request's store calls.")
    parser.add_argument("--rounds", type=int, default=300)
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args(argv)
    if args.rounds < 1 or args.runs < 1:
        parser.error("--rounds and --runs must be at least 1")
    url = os.environ.get(URL_ENV)
    if not url:
        parser.error(f"set {URL_ENV} to the Postgres to measure against")
    machine = describe_machine()
    print("MEASURED on this machine: real time, in process; a SQLite file and a real Postgres.")
    for key in ("cpu", "logical_cores", "ram_gb", "os", "python"):
        print(f"{key}: {machine[key]}")
    print(f"{args.runs} runs of {args.rounds} requests after {WARMUP} warm-up; ms; nearest rank")
    p50s: dict[str, list[float]] = {}
    for number in range(1, args.runs + 1):
        print(f"run {number}")
        cases = asyncio.run(run(args.rounds, url, os.environ))
        for case in cases:
            label = f"{case.name}, {case.store}"
            calls = case.calls()
            store_ms = [r.store_ms for r in case.requests]
            print(
                f"  {label:<24} request p50 {percentile(case.request_ms, 50):>7.3f}"
                f"  p90 {percentile(case.request_ms, 90):>7.3f}"
                f"  store calls {len(calls):>2}  store time p50 {percentile(store_ms, 50):>7.3f}"
            )
            print(f"    calls: {', '.join(calls)}")
            p50s.setdefault(f"{label}: request", []).append(percentile(case.request_ms, 50))
            p50s.setdefault(f"{label}: store time", []).append(percentile(store_ms, 50))
            if case.store != "postgres":
                continue
            trips = [sum(r.trips.values()) for r in case.requests]
            print(f"    round trips per request {_median(trips):.0f}, by kind (count, p50 ms):")
            for kind in KINDS:
                counts = [r.trips[kind] for r in case.requests]
                spent = [r.trip_ms.get(kind, 0.0) for r in case.requests]
                print(f"      {kind:<10} {_median(counts):>3.0f}  {percentile(spent, 50):>7.3f}")
                p50s.setdefault(f"{label}: {kind}", []).append(percentile(spent, 50))
            outside = [r.store_ms - sum(r.trip_ms.values()) for r in case.requests]
            print(f"      {'client':<10} {'':>3}  {percentile(outside, 50):>7.3f}")
            p50s.setdefault(f"{label}: client", []).append(percentile(outside, 50))
    print("p50 across runs (min to max)")
    for name, values in p50s.items():
        print(f"  {name:<40} {min(values):>8.3f} to {max(values):>8.3f}")


if __name__ == "__main__":
    main()
