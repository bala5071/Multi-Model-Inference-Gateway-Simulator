"""Budgets on Postgres (docs/37-budgets-on-postgres.md), on the test Postgres: the budget edge on
two instances (Q1), a reservation released after a failure and a hang-up, an instance that dies
holding one (how it expires and what the customer sees), a settle kept while Postgres is down,
the database's clock deciding the period and the expiry, Valkey's skew measured against it, a
shared server started end to end, and the round trips one request makes.

Marked ``postgres`` (skipped without ``GATEWAY_TEST_POSTGRES_URL``); one also needs Valkey. The
database's time is set by each test. Test values, not secrets and not data.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import logging
import math
import os
import random
from collections.abc import AsyncIterator, Mapping, MutableMapping
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import httpx
import pytest
from benchmarks import request_store_calls
from fastapi import FastAPI
from redis.asyncio import Redis

from gateway.apikeys import ApiKeyService, ApplicationAccess
from gateway.app import RESERVATION_MARGIN_MS, create_app, create_default_app
from gateway.applications import application_from_template
from gateway.audit import AuditLog
from gateway.backend_config import TierBudget
from gateway.clock import Clock, ClockEpoch, SimClock, SystemEpoch, WallClock
from gateway.continuous import build_continuous_registry
from gateway.egress import organization_client
from gateway.ledger import Expired, Reservation
from gateway.modes import Mode
from gateway.overload import OverloadPolicy
from gateway.periods import CalendarMonthsUTC
from gateway.postgres import (
    BUDGET_TABLES,
    PG_BUDGETS,
    PostgresAccountStore,
    PostgresApplicationStore,
    PostgresAuditStore,
    PostgresBudgets,
    PostgresDatabase,
    PostgresModeStore,
    PostgresProviderStore,
    PostgresUsageStore,
    postgres_state,
)
from gateway.profiles import Tier
from gateway.providers import ProviderConfig, ProviderService
from gateway.redaction import Redactor
from gateway.routing import load_plans
from gateway.schema import RequestResult
from gateway.state import StateUnavailableError
from gateway.valkey import ValkeyCounters
from gateway.vault import Vault
from helpers import TEST_PEPPER, TEST_PROFILES, bearer
from mock_network import FakeResolver, ScriptedNetwork
from postgres_helpers import POSTGRES_URL_ENV, TEST_DATABASE_ENV, PgWorld, pg_world
from test_chat_routing import HOSTS, MASTER, TEXT, TLS, P
from valkey_helpers import VALKEY_URL_ENV, ManualEpoch, ScriptedClient

postgres = pytest.mark.postgres
PLANS = load_plans()
NOW = 1.8e12  # the database's time in these tests, far from the instances' own
PROMPT = "one two three four five six seven eight nine ten"
# Test settings for every tier, not any model's real numbers.
BOUNDS = {t.value: TierBudget(context_tokens=4096, reserve_completion_tokens=256) for t in Tier}
TIMEOUT_MS = 600_000.0
HOLD_MS = TIMEOUT_MS + RESERVATION_MARGIN_MS
STATE_BOUND_S = 10.0  # how long a test waits for a state before it reports it never came
# The bound of a generate request for PROMPT with max_tokens 2 (Q2, B4).
BOUND = len(PROMPT.encode()) + 2
PRIMARY = Mode.HOSTED_PRIMARY_WITH_LOCAL_FALLBACK
PROVIDER_KEY = "sk-planted-budget-routing-key-0123456789abcd"  # a test value
# Planted in the shared server's Postgres URL, to be looked for everywhere.
PLANTED = "planted-shared-pg-password-7e2b"


def test_the_budget_tables_are_postgres_only() -> None:
    """Schema version 2: three tables SQLite does not have (the SQLite ledger is unchanged)."""
    created = [
        line.split()[2] for line in PG_BUDGETS.splitlines() if line.startswith("CREATE TABLE")
    ]
    assert tuple(created) == BUDGET_TABLES


def _result(tokens_in: int, tokens_out: int) -> RequestResult:
    return RequestResult("r", "economy", 0.0, 0.0, 0.0, 1.0, tokens_in, tokens_out, 0.0)


async def _application(db: PostgresDatabase, budget: int, app_id: str = "acme") -> str:
    """An application with this budget and its key."""
    store = PostgresApplicationStore(db)
    application = application_from_template(
        app_id, app_id, PLANS, "pro", organization_id="default", created_at_ms=1.0
    )
    plan = dataclasses.replace(application.plan, monthly_token_budget=budget)
    await store.add_application(dataclasses.replace(application, plan=plan))
    keys = ApiKeyService(store, TEST_PEPPER, ClockEpoch(SimClock()), store_clock=db)
    return (await keys.issue(app_id, "main")).key


def _app_on(
    db: PostgresDatabase, clock: Clock, *, timeout_ms: float = TIMEOUT_MS
) -> tuple[FastAPI, PostgresBudgets, Any]:
    state = postgres_state(db)
    keys = ApiKeyService(state.applications, TEST_PEPPER, ClockEpoch(clock), store_clock=db)
    registry = build_continuous_registry(clock, seed=7, profiles=TEST_PROFILES)
    budgets = PostgresBudgets(db, WallClock())
    app = create_app(
        registry,
        clock,
        applications=ApplicationAccess(keys, PLANS),
        usage_store=state.usage,
        epoch=ClockEpoch(clock),
        modes=state.modes,
        startup_applications=[],
        readiness=db.ready,
        overload=OverloadPolicy(request_timeout_ms=timeout_ms),
        budgets=budgets,
        reservation_bounds=BOUNDS,
    )
    return app, budgets, registry


@asynccontextmanager
async def _clients(
    world: PgWorld, clock: SimClock, *, settle_after: int | None = None
) -> AsyncIterator[list[httpx.AsyncClient]]:
    """A client for each instance. With ``settle_after``, no settle is made until that many
    reserves have been answered, on all instances together: the requests are then all at the
    budget at once, as the edge test means, however fast the admitted one is served."""
    built = [_app_on(db, clock) for db in world.instances]
    if settle_after is not None:
        _settle_after_reserves([budgets for _, budgets, _ in built], settle_after)
    clients = [
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw")
        for app, _, _ in built
    ]
    try:
        yield clients
    finally:
        for client in clients:
            await client.aclose()
        for _, _, registry in built:
            await registry.aclose()


def _settle_after_reserves(all_budgets: list[PostgresBudgets], count: int) -> None:
    answered = 0
    reserved = asyncio.Event()
    for budgets in all_budgets:
        reserve, settle = budgets.reserve, budgets.settle

        async def counted(*args: Any, real: Any = reserve, **kwargs: Any) -> Any:
            nonlocal answered
            try:
                return await real(*args, **kwargs)
            finally:
                answered += 1
                if answered == count:
                    reserved.set()

        async def held(*args: Any, real: Any = settle, **kwargs: Any) -> Any:
            await asyncio.wait_for(reserved.wait(), timeout=STATE_BOUND_S)
            return await real(*args, **kwargs)

        budgets.reserve = counted  # type: ignore[method-assign]
        budgets.settle = held  # type: ignore[method-assign]


async def _counter(world: PgWorld, app_id: str = "acme") -> tuple[int, int]:
    [(committed, reserved)] = await world.rows(
        "SELECT committed_tokens, reserved_tokens FROM budget_counters WHERE application_id = $1",
        app_id,
    )
    return int(committed), int(reserved)


async def _until_counter(world: PgWorld, wanted: tuple[int, int]) -> None:
    """Until the counter reads ``wanted`` (a state, polled); the bound reports one never
    reached rather than hanging."""

    async def poll() -> None:
        while await _counter(world) != wanted:
            await WallClock().sleep_ms(10.0)

    await asyncio.wait_for(poll(), timeout=STATE_BOUND_S)


async def _commit_already(world: PgWorld, tokens: int, app_id: str = "acme") -> None:
    """Usage already charged this period: one stored event of ``tokens`` billed tokens."""
    await world.rows(
        "INSERT INTO usage_events (application_id, request_id, model, prompt_tokens,"
        " cached_tokens, completion_tokens, base_cost_usd, billed_cost_usd, occurred_at_ms)"
        " VALUES ($1, 'earlier', 'economy', $2, 0, 0, 0, 0, $3)",
        app_id,
        tokens,
        NOW - 1.0,
    )


# --- the edge (Q1) on two instances -------------------------------------------------------------


async def _at_the_edge(already: int, budget: int) -> tuple[list[httpx.Response], tuple[int, int]]:
    """20 requests split across two instances at once, against a budget with ``already``
    committed; their answers, and the counter afterwards."""
    clock = SimClock()
    async with pg_world(instances=2) as world:
        await world.set_time(NOW)
        key = await _application(world.instances[0], budget)
        if already:
            await _commit_already(world, already)
        # The counter exists before the burst (a period's first reserve seeds it), so every
        # request takes the path of an application already busy this period.
        seeding = PostgresBudgets(world.instances[0], WallClock())
        await seeding.release(await seeding.reserve("acme", budget, 0, hold_ms=HOLD_MS))
        body = {"model": "economy", "prompt": PROMPT, "max_tokens": 2}
        async with _clients(world, clock, settle_after=20) as clients:
            answers = await asyncio.gather(
                *(
                    clients[i % 2].post("/v1/generate", json=body, headers=bearer(key))
                    for i in range(20)
                )
            )
        return list(answers), await _counter(world)


@postgres
async def test_two_instances_admit_exactly_one_request_against_the_last_tokens() -> None:
    """A budget with room for exactly one request's bound: one is admitted across both
    instances, 19 are refused, and what is committed is that one request's billed tokens."""
    answers, (committed, reserved) = await _at_the_edge(0, BOUND)
    admitted = [a for a in answers if a.status_code == 200]
    refused = [a for a in answers if a.status_code == 429]
    assert (len(admitted), len(refused)) == (1, 19), [a.text for a in answers]
    assert {a.json()["error"]["code"] for a in refused} == {"budget_exhausted"}
    usage = admitted[0].json()["usage"]
    assert committed == usage["prompt_tokens"] - usage["cached_tokens"] + usage["completion_tokens"]
    assert reserved == 0


@postgres
async def test_the_last_request_admitted_goes_past_the_budget_by_at_most_its_bound() -> None:
    """One token left: one request is admitted (Q1 compares what is committed and in flight),
    and it can take the budget past by its own bound at most, never by two requests'."""
    budget = 10_000
    answers, (committed, reserved) = await _at_the_edge(budget - 1, budget)
    assert [a.status_code for a in answers].count(200) == 1
    assert budget <= committed <= budget - 1 + BOUND
    assert reserved == 0


# --- release on Postgres --------------------------------------------------------------------------


async def _hang_up(app: FastAPI, key: str, clock: SimClock) -> None:
    raw = json.dumps({"model": "standard", "prompt": PROMPT, "stream": True}).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/generate",
        "raw_path": b"/v1/generate",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"gw"),
            (b"content-type", b"application/json"),
            (b"authorization", f"Bearer {key}".encode()),
        ],
        "client": ("test-client", 1),
        "server": ("gw", 80),
    }
    disconnected = asyncio.Event()
    sent = False
    chunks: list[bytes] = []

    async def receive() -> MutableMapping[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": raw, "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            chunks.append(message["body"])
            if len(chunks) == 2:
                disconnected.set()

    await app(scope, receive, send)
    assert len(chunks) >= 2


@postgres
async def test_a_reservation_is_released_after_a_timeout_and_a_hang_up() -> None:
    clock = SimClock()
    async with pg_world() as world:
        await world.set_time(NOW)
        [db] = world.instances
        key = await _application(db, 10**9)
        timed_out, _, registry = _app_on(db, clock, timeout_ms=1.0)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=timed_out), base_url="http://gw"
        ) as client:
            answer = await client.post(
                "/v1/generate",
                json={"model": "standard", "prompt": PROMPT, "max_tokens": 200},
                headers=bearer(key),
            )
        await registry.aclose()
        assert answer.json()["error"]["code"] == "request_timeout"
        assert await _counter(world) == (0, 0)
        streamed, _, registry = _app_on(db, clock)
        await _hang_up(streamed, key, clock)
        await registry.aclose()
        # Released by the stream as it is cancelled, after the app has answered.
        await _until_counter(world, (0, 0))
        assert await world.rows("SELECT count(*) FROM budget_reservations") == [(0,)]
        assert await world.rows("SELECT count(*) FROM usage_events") == [(0,)]


# --- an instance that dies holding a reservation -------------------------------------------------


@postgres
async def test_a_dead_instances_reservation_expires_and_the_customer_is_told_when() -> None:
    """Instance A reserves the last tokens and dies. Until the reservation expires (the request
    timeout plus the margin, on the database's clock), B refuses with 429 budget_exhausted and a
    Retry-After of that expiry (B3). At the expiry B's sweep releases it, says so, and B
    admits again. A settle from A arriving after that charges the usage once and takes nothing
    off a second time."""
    clock = SimClock()
    async with pg_world(instances=3) as world:
        first, second, third = world.instances
        await world.set_time(NOW)
        key = await _application(second, BOUND)
        held = await PostgresBudgets(first, WallClock()).reserve(
            "acme", BOUND, BOUND, hold_ms=HOLD_MS
        )
        await first.aclose()  # instance A is gone; its reservation is still counted
        app, budgets, registry = _app_on(second, clock)
        body = {"model": "economy", "prompt": PROMPT, "max_tokens": 2}
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gw"
            ) as client:
                refused = await client.post("/v1/generate", json=body, headers=bearer(key))
                assert refused.status_code == 429
                assert refused.json()["error"]["code"] == "budget_exhausted"
                assert refused.json()["error"]["message"] == (
                    f"Application 'acme' has used 0 of its {BOUND} token budget for this "
                    f"period, and {BOUND} tokens are held by requests in progress. Retry in "
                    f"{math.ceil(HOLD_MS / 1000)} seconds, when they have finished or expired."
                )
                assert refused.headers["retry-after"] == str(math.ceil(HOLD_MS / 1000))
                await world.set_time(NOW + HOLD_MS - 1.0)
                assert await budgets.sweep() == []
                later = await client.post("/v1/generate", json=body, headers=bearer(key))
                assert later.headers["retry-after"] == "1"
                await world.set_time(NOW + HOLD_MS)
                assert await budgets.sweep() == [Expired("acme", BOUND)]
                assert await _counter(world) == (0, 0)
                admitted = await client.post("/v1/generate", json=body, headers=bearer(key))
                assert admitted.status_code == 200, admitted.text
        finally:
            await registry.aclose()
        committed, reserved = await _counter(world)
        assert reserved == 0
        late = PostgresBudgets(third, WallClock())
        await late.settle(held, _result(3, 4), model="economy", price_multiplier=1.0)
        assert await _counter(world) == (committed + 7, 0)


# --- a settle the database does not take ------------------------------------------------------


class _DownPool:
    def acquire(self, *, timeout: float | None = None) -> _DownPool:
        return self

    async def __aenter__(self) -> None:
        raise ConnectionRefusedError("the database cannot be reached")

    async def __aexit__(self, *_: object) -> None:
        return None


async def _until_healthy(budgets: PostgresBudgets) -> None:
    async def poll() -> None:
        while not budgets.healthy:
            await WallClock().sleep_ms(10.0)

    await asyncio.wait_for(poll(), timeout=STATE_BOUND_S)


@postgres
async def test_a_settle_postgres_does_not_take_is_kept_and_admits_nothing_new_meanwhile() -> None:
    async with pg_world() as world:
        await world.set_time(NOW)
        [db] = world.instances
        budgets = PostgresBudgets(db, WallClock())
        reservation = await budgets.reserve("acme", 1000, 50, hold_ms=HOLD_MS)
        pool, db.requests = db.requests, _DownPool()
        billed = await budgets.settle(
            reservation, _result(10, 5), model="economy", price_multiplier=1.0
        )
        assert billed == 0.0  # the answer goes out
        assert not budgets.healthy
        with pytest.raises(StateUnavailableError, match="settle_waiting"):
            await budgets.reserve("acme", 1000, 50, hold_ms=HOLD_MS)
        db.requests = pool
        await _until_healthy(budgets)
        assert await _counter(world) == (15, 0)
        assert await world.rows("SELECT count(*) FROM usage_events") == [(1,)]
        await budgets.aclose()


@postgres
async def test_a_settle_tried_again_is_charged_once() -> None:
    """A settle whose answer was lost after it was written, tried again: nothing more."""
    async with pg_world() as world:
        await world.set_time(NOW)
        [db] = world.instances
        budgets = PostgresBudgets(db, WallClock())
        reservation = await budgets.reserve("acme", 1000, 50, hold_ms=HOLD_MS)
        for _ in range(2):
            await budgets.settle(reservation, _result(10, 5), model="economy", price_multiplier=1.0)
        assert await _counter(world) == (15, 0)
        assert await world.rows("SELECT count(*) FROM usage_events") == [(1,)]


@postgres
async def test_a_reserve_postgres_cannot_make_is_refused_and_no_model_sees_the_request() -> None:
    clock = SimClock()
    async with pg_world() as world:
        await world.set_time(NOW)
        [db] = world.instances
        key = await _application(db, 10**9)
        down = PostgresDatabase(_DownPool(), _DownPool(), WallClock())
        state = postgres_state(db)
        keys = ApiKeyService(state.applications, TEST_PEPPER, ClockEpoch(clock), store_clock=db)
        registry = build_continuous_registry(clock, seed=7, profiles=TEST_PROFILES)
        app = create_app(
            registry,
            clock,
            applications=ApplicationAccess(keys, PLANS),
            startup_applications=[],
            overload=OverloadPolicy(request_timeout_ms=TIMEOUT_MS),
            budgets=PostgresBudgets(down, WallClock()),
            reservation_bounds=BOUNDS,
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gw"
            ) as client:
                answer = await client.post(
                    "/v1/generate",
                    json={"model": "economy", "prompt": PROMPT},
                    headers=bearer(key),
                )
                summary = (await client.get("/metrics/summary")).json()
        finally:
            await registry.aclose()
        assert (answer.status_code, answer.json()["error"]["code"]) == (503, "state_unavailable")
        assert all(m["requests"] == 0 for m in summary["models"].values()), summary


async def _until_waiting_for_a_lock(count: int) -> None:
    """Until ``count`` sessions wait for a lock (a state, polled, bounded)."""

    async def poll() -> None:
        probe = await asyncpg.connect(os.environ[POSTGRES_URL_ENV])
        try:
            while (
                await probe.fetchval(
                    "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'"
                    " AND datname = current_database()"
                )
                < count
            ):
                await WallClock().sleep_ms(5.0)
        finally:
            await probe.close()

    await asyncio.wait_for(poll(), timeout=STATE_BOUND_S)


@postgres
async def test_a_settle_racing_another_instances_seed_is_counted_once() -> None:
    """The period's first settle finds no counter and seeds it from the events, its own
    included. Another instance seeding the same counter meanwhile, without that event (not
    committed yet), inserts first: the settle's seed then inserts nothing, and the settle adds
    its tokens to that counter instead."""
    async with pg_world() as world:
        await world.set_time(NOW)
        [db] = world.instances
        start = CalendarMonthsUTC().bounds(NOW)[0]
        other = world.admin
        await other.execute(f"SET search_path TO {world.schema}")
        await other.execute("BEGIN")
        await other.execute("INSERT INTO budget_counters VALUES ('acme', $1, 0, 0)", start)
        settling = asyncio.ensure_future(
            PostgresBudgets(db, WallClock()).settle(
                Reservation("acme", 0, None), _result(10, 5), model="economy", price_multiplier=1.0
            )
        )
        await _until_waiting_for_a_lock(1)
        await other.execute("COMMIT")
        await asyncio.wait_for(settling, timeout=STATE_BOUND_S)
        assert await _counter(world) == (15, 0)


# --- the database's clock decides ---------------------------------------------------------------


@postgres
async def test_the_period_and_the_expiry_are_on_the_databases_clock() -> None:
    async with pg_world(instances=2) as world:
        first, second = world.instances
        await world.set_time(NOW)
        await PostgresBudgets(first, SimClock()).reserve("acme", 1000, 50, hold_ms=HOLD_MS)
        [(period, expiry)] = await world.rows(
            "SELECT period_start_ms, expires_at_ms FROM budget_reservations"
        )
        assert period == CalendarMonthsUTC().bounds(NOW)[0]
        assert expiry == NOW + HOLD_MS
        # The usage the other instance reports is every instance's, on the same period.
        budgets = PostgresBudgets(second, SimClock())
        reservation = Reservation("acme", 50, 1)
        await budgets.settle(reservation, _result(10, 5), model="economy", price_multiplier=1.0)
        usage = await PostgresBudgets(first, SimClock()).usage("acme")
        assert (usage.period_start_ms, usage.total.billed_tokens) == (period, 15)


async def _ignore_restart(_: str) -> None:
    return None


@postgres
async def test_valkeys_skew_is_measured_against_gateway_now_ms() -> None:
    """The instance's epoch against the database's ``gateway_now_ms()``, set by the test: past
    the bound it does not start; inside it, it does. Valkey's own clock is far off and unused."""
    async with pg_world() as world:
        [db] = world.instances
        client = ScriptedClient()
        epoch = ManualEpoch(NOW)
        client.time_ms = NOW + 3_600_000.0

        def counters() -> ValkeyCounters:
            return ValkeyCounters(
                client,  # type: ignore[arg-type]
                WallClock(),
                epoch,
                TEST_PEPPER,
                prefix="gwtest:scripted:",
                reference=db.now_ms,
            )

        await world.set_time(NOW + 1500.0)
        with pytest.raises(StateUnavailableError, match="from Postgres's"):
            await counters().watch.start(_ignore_restart)
        await world.set_time(NOW - 400.0)
        started = counters()
        await started.watch.start(_ignore_restart)
        assert started.healthy
        assert started.watch.skew_ms is not None
        assert abs(started.watch.skew_ms + 400.0) < 100.0


# --- the shared server, started end to end (Postgres and Valkey) ---------------------------------


@postgres
@pytest.mark.valkey
async def test_a_shared_server_starts_serves_and_charges_on_postgres(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """``create_default_app`` with ``GATEWAY_STATE=shared``: the lifespan opens Postgres,
    applies the schema, creates the bootstrap admin and checks Valkey; a request is served and
    charged, and nothing is left reserved. The URL carries a planted password (the test
    database trusts local connections, so any password is accepted, and the start-up guard sees
    one): the leak scan finds it in no log record, no row and no Valkey key or value, and finds
    it where it is planted on purpose."""
    caplog.set_level(logging.DEBUG)
    async with pg_world() as world:
        [db] = world.instances
        key = await _application(db, 10**9)
        parts = urlsplit(os.environ[POSTGRES_URL_ENV])
        planted_url = urlunsplit(
            parts._replace(netloc=f"{parts.username}:{PLANTED}@{parts.hostname}:{parts.port}")
        )
        backends = tmp_path / "backends.yaml"
        tiers = {t.value: {"context_tokens": 4096, "reserve_completion_tokens": 256} for t in Tier}
        backends.write_text(json.dumps({"backend": "simulated", "budgets": tiers}))
        environ: Mapping[str, str] = {
            "GATEWAY_STATE": "shared",
            # asyncpg passes an unknown parameter on as a server setting: this test's schema.
            "GATEWAY_POSTGRES_URL": f"{planted_url}?search_path={world.schema}",
            "GATEWAY_COUNTERS": "valkey",
            "GATEWAY_VALKEY_URL": os.environ[VALKEY_URL_ENV],
            "GATEWAY_BACKENDS": str(backends),
        }
        for name, value in environ.items():
            monkeypatch.setenv(name, value)
        prefix = f"gwtest:{random.SystemRandom().getrandbits(64):016x}:"
        original = ValkeyCounters.__init__

        def prefixed(self: ValkeyCounters, *args: Any, **kwargs: Any) -> None:
            original(self, *args, **{**kwargs, "prefix": prefix})

        monkeypatch.setattr(ValkeyCounters, "__init__", prefixed)
        app = create_default_app()
        try:
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://gw"
                ) as client,
            ):
                assert (await client.get("/readyz")).json() == {"status": "ready"}
                answer = await client.post(
                    "/v1/generate",
                    json={"model": "economy", "prompt": PROMPT, "max_tokens": 2},
                    headers=bearer(key),
                )
                assert answer.status_code == 200, answer.text
            rows = await world.rows("SELECT count(*) FROM usage_events")
            assert rows == [(1,)]
            assert (await _counter(world))[1] == 0
            admins = await world.rows("SELECT count(*) FROM users WHERE username = 'admin'")
            assert admins == [(1,)]
            # The leak scan, then its control: the same scan over a row with the plant in it.
            assert await _leaks(world, prefix, caplog.text) == []
            await world.rows("CREATE TABLE planted_control (v text)")
            await world.rows("INSERT INTO planted_control VALUES ($1)", f"x{PLANTED}x")
            assert await _leaks(world, prefix, caplog.text) == ["planted_control"]
            assert await _leaks(world, prefix, f"logged {PLANTED}") == ["planted_control", "logs"]
        finally:
            valkey = Redis.from_url(os.environ[VALKEY_URL_ENV], decode_responses=True)
            async for name in valkey.scan_iter(match=prefix + "*"):
                await valkey.delete(name)
            await valkey.aclose()


async def _leaks(world: PgWorld, prefix: str, logs: str) -> list[str]:
    """Where the planted password is: each table of the schema (every row as text), the
    Valkey keys and values under ``prefix``, and ``logs``."""
    found = []
    tables = await world.rows(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = $1"
        " ORDER BY table_name",
        world.schema,
    )
    for (table,) in tables:
        rows = await world.rows(f"SELECT t::text FROM {table} AS t")
        if any(PLANTED in str(row[0]) for row in rows):
            found.append(table)
    valkey = Redis.from_url(os.environ[VALKEY_URL_ENV], decode_responses=True)
    try:
        async for name in valkey.scan_iter(match=prefix + "*"):
            kind = await valkey.type(name)
            value = await valkey.dump(name)
            if PLANTED in name or PLANTED.encode() in (value or b"") or PLANTED in kind:
                found.append(f"valkey:{name}")
    finally:
        await valkey.aclose()
    if PLANTED in logs:
        found.append("logs")
    return found


# --- what one request costs in round trips ------------------------------------------------------


@postgres
async def test_the_round_trips_of_a_reserve_and_a_settle() -> None:
    """Counted on the driver: every statement is one round trip, BEGIN and COMMIT included.
    The first reserve of a period seeds its counter; after that, a reserve is seven round trips
    and a settle nine, each counting the reset the pool sends when the connection goes back."""
    async with pg_world() as world:
        await world.set_time(NOW)
        [db] = world.instances
        budgets = PostgresBudgets(db, WallClock())
        sent: list[str] = []
        connection_class = asyncpg.connection.Connection
        originals = {
            name: getattr(connection_class, name)
            for name in ("execute", "fetch", "fetchrow", "fetchval")
        }

        def counting(name: str) -> Any:
            async def call(self: Any, query: str, *args: Any, **kwargs: Any) -> Any:
                # The pool resets a connection given back to it: one more round trip.
                reset = "pg_advisory_unlock_all" in query
                sent.append("RESET" if reset else query.split()[0].upper().rstrip(";"))
                return await originals[name](self, query, *args, **kwargs)

            return call

        try:
            for name in originals:
                setattr(connection_class, name, counting(name))
            await budgets.reserve("acme", 1000, 50, hold_ms=HOLD_MS)  # seeds the counter
            sent.clear()
            reservation = await budgets.reserve("acme", 1000, 50, hold_ms=HOLD_MS)
            reserve = list(sent)
            sent.clear()
            await budgets.settle(reservation, _result(10, 5), model="economy", price_multiplier=1.0)
            settle = list(sent)
            sent.clear()
        finally:
            for name, original in originals.items():
                setattr(connection_class, name, original)
        assert reserve == ["BEGIN", "SET", "SELECT", "UPDATE", "INSERT", "COMMIT", "RESET"]
        assert settle == [
            "BEGIN",
            "SET",
            "SELECT",
            "INSERT",
            "WITH",
            "INSERT",
            "UPDATE",
            "COMMIT",
            "RESET",
        ]


@postgres
async def test_the_store_calls_of_a_chat_request_and_a_generate_request_in_shared_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Which calls a request makes on the shared stores, one after another: the key, the two
    modes once (chat, docs/38), a reserve and a settle. Note 36's ``append`` (and the period's
    totals once a period) are replaced by the reserve and the settle. On real time: the chat
    request reads the modes inside its timed work, where simulated time would jump to the
    timeout while the loop waits on the network."""
    clock = WallClock()
    calls: list[str] = []

    def counted(cls: Any, name: str) -> None:
        original = getattr(cls, name)

        async def call(self: Any, *args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            return await original(self, *args, **kwargs)

        monkeypatch.setattr(cls, name, call)

    async with pg_world() as world:
        await world.set_time(NOW)
        [db] = world.instances
        key = await _application(db, 10**9)
        app, _, registry = _app_on(db, clock)
        state = postgres_state(db)
        for store, names in (
            (type(state.applications), ("lookup_key",)),
            (type(state.modes), ("organization_mode", "application_mode")),
            (type(state.usage), ("append", "usage_by_model")),
            (PostgresBudgets, ("reserve", "settle", "release")),
        ):
            for name in names:
                counted(store, name)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gw"
            ) as client:
                messages = [{"role": "user", "content": PROMPT}]
                for _ in range(2):  # the period's first, and one after
                    calls.clear()
                    chat = await client.post(
                        "/v1/chat/completions",
                        json={"model": "economy", "messages": messages, "max_tokens": 2},
                        headers=bearer(key),
                    )
                    assert chat.status_code == 200, chat.text
                    chatted = list(calls)
                    calls.clear()
                    generated = await client.post(
                        "/v1/generate",
                        json={"model": "economy", "prompt": PROMPT, "max_tokens": 2},
                        headers=bearer(key),
                    )
                    assert generated.status_code == 200, generated.text
                    generate = list(calls)
                    modes = ["organization_mode", "application_mode"]
                    # The settle's usage row is appended inside it.
                    assert chatted == ["lookup_key", "reserve", *modes, "settle", "append"]
                    assert generate == ["lookup_key", "reserve", "settle", "append"]
        finally:
            await registry.aclose()


@postgres
async def test_the_store_calls_of_a_chat_request_an_organization_provider_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The organization-provider path in shared mode, counted on the Postgres stores: every
    public store call and every budget call, one after another, for one chat request that the
    organization's primary provider answers: the modes read once, and again just before the
    hosted attempt (docs/38). Before PR 3c, 21: the modes were read six times."""
    clock = WallClock()  # the modes are read inside the timed work: see the test above
    calls: list[str] = []
    async with pg_world() as world:
        await world.set_time(NOW)
        [db] = world.instances
        key = await _application(db, 10**9)
        state = postgres_state(db)
        resolver, org_net = FakeResolver(dict(HOSTS)), ScriptedNetwork()
        providers = ProviderService(
            state.providers,
            Vault(MASTER),
            AuditLog(state.audit),
            clock,
            SystemEpoch(),
            Redactor(),
            lambda: organization_client(resolver, inner=org_net, ssl_context=TLS),
        )
        config = ProviderConfig(
            P, "default", "openai_compatible", "https://p.provider.test/v1", "model-x",
            "economy", "primary", True, True, None, None, {}, None, 0.0,
        )  # fmt: skip
        await state.providers.add(config)
        await state.providers.put_key(P, providers.vault.seal(PROVIDER_KEY, config.binding), 1.0)
        await state.modes.set_organization_mode("default", PRIMARY, 0.0)
        await state.modes.set_application_mode("acme", PRIMARY, 0.0)
        org_net.responses = [TEXT]
        registry = build_continuous_registry(clock, seed=7, profiles=TEST_PROFILES)
        app = create_app(
            registry,
            clock,
            applications=ApplicationAccess(
                ApiKeyService(state.applications, TEST_PEPPER, SystemEpoch(), store_clock=db),
                PLANS,
            ),
            usage_store=state.usage,
            epoch=SystemEpoch(),
            modes=state.modes,
            providers=providers,
            startup_applications=[],
            overload=OverloadPolicy(request_timeout_ms=TIMEOUT_MS),
            budgets=PostgresBudgets(db, clock),
            reservation_bounds=BOUNDS,
        )
        for cls in (
            PostgresApplicationStore,
            PostgresModeStore,
            PostgresProviderStore,
            PostgresUsageStore,
            PostgresAccountStore,
            PostgresAuditStore,
            PostgresBudgets,
        ):
            for name, method in list(vars(cls).items()):
                if name.startswith("_") or not inspect.iscoroutinefunction(method):
                    continue

                def counted(name: str = name, method: Any = method) -> Any:
                    async def call(self: Any, *args: Any, **kwargs: Any) -> Any:
                        calls.append(name)
                        return await method(self, *args, **kwargs)

                    return call

                monkeypatch.setattr(cls, name, counted())
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gw"
            ) as client:
                answer = await client.post(
                    "/v1/chat/completions",
                    json={"model": "economy", "messages": [{"role": "user", "content": "Hi."}]},
                    headers=bearer(key),
                )
        finally:
            await registry.aclose()
        assert answer.status_code == 200, answer.text
        assert answer.headers["x-provider"] == P
    candidates = ["list_for", "get_key"]
    assert calls == [
        "lookup_key",
        "reserve",
        "organization_mode",
        "application_mode",  # the request's: the candidates and the egress scope use them
        *candidates,  # the handler's, before admission to one
        *candidates,  # the router's
        "get_key",  # skipped if unreadable
        "organization_mode",
        "application_mode",  # again, just before the hosted attempt (docs/33)
        "get_key",  # leased
        "settle",
        "append",  # inside the settle's transaction
    ]
    assert len(calls) - 1 == 13  # store calls; the append is a statement of the settle


@postgres
async def test_the_request_benchmark_counts_each_round_trip_and_leaves_no_schema() -> None:
    """``benchmarks.request_store_calls`` on one request of each case: the same store calls on
    SQLite and on Postgres, and on Postgres every round trip by kind. Per request: the reserve
    and the settle each one transaction (BEGIN, its statement timeout, COMMIT), and every
    connection given back to the pool one reset, the lookup's, each mode's and each provider
    read's included (docs/38)."""
    url = os.environ[POSTGRES_URL_ENV]
    cases = await request_store_calls.run(1, url, TEST_DATABASE_ENV)
    modes = ["organization_mode", "application_mode"]
    providers = ["list_for", "get_key"]
    gateway = ["lookup_key", "reserve", *modes, "settle"]
    provider = ["lookup_key", "reserve", *modes, *providers, *providers, "get_key", *modes]
    provider += ["get_key", "settle"]  # the modes again before the hosted attempt
    assert [(c.name, c.store, c.calls()) for c in cases] == [
        ("gateway tier", "sqlite", gateway),
        ("gateway tier", "postgres", gateway),
        ("org provider", "sqlite", provider),
        ("org provider", "postgres", provider),
    ]
    trips = {c.name: dict(c.requests[0].trips) for c in cases if c.store == "postgres"}
    transactions = {"BEGIN": 2, "SET": 2, "COMMIT": 2}
    assert trips == {
        "gateway tier": {**transactions, "statement": 11, "RESET": 5},
        "org provider": {**transactions, "statement": 19, "RESET": 13},
    }
    admin = await asyncpg.connect(url)
    try:
        left = await admin.fetch("SELECT nspname FROM pg_namespace WHERE nspname LIKE 'gwbench_%'")
    finally:
        await admin.close()
    assert left == []
