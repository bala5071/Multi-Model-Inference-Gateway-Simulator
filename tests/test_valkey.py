"""The shared counters in Valkey (docs/35-valkey-counters.md).

Tests marked ``valkey`` run against a real Valkey (``docker-compose.valkey.yml``; skipped
without ``GATEWAY_TEST_VALKEY_URL``): the atomic rules on two instances, the shared limit end
to end, the real clock and run id, and a scan of the key space. The rest need no server:
failing closed against an address where nothing listens, the watch against a scripted client,
the settings, and the URL's password kept out of everything.
"""

from __future__ import annotations

import asyncio
import logging
import os

import httpx
import pytest
from benchmarks import counter_calls
from redis.asyncio import Redis

from accounts_helpers import PASSWORD, make_accounts
from gateway.app import create_app
from gateway.audit import (
    AuditAction,
    AuditEntry,
    AuditEvent,
    AuditLog,
    Outcome,
    TargetType,
)
from gateway.auth import (
    LOCK_FAILURES,
    LOCK_WINDOW_MS,
    AccountWindows,
    LoginFailedError,
)
from gateway.breaker import BreakerState, Transition
from gateway.clock import SimClock, SystemEpoch, WallClock
from gateway.continuous import build_continuous_registry
from gateway.counters import OnRestart
from gateway.management import ManagementRefusal
from gateway.state import StateUnavailableError
from gateway.valkey import (
    COUNTERS_ENV,
    MAX_SKEW_ENV,
    URL_ENV,
    ValkeyCounters,
    ValkeyNotSafeError,
    shared_counters_from,
)
from helpers import TEST_PEPPER, application_access, bearer
from valkey_helpers import (
    NOWHERE,
    VALKEY_URL_ENV,
    ManualEpoch,
    OffsetEpoch,
    ScriptedClient,
    counters_on,
    instances,
)

PROMPT = "one two three four five six seven eight nine ten"
ORG, PROVIDER = "north", "prv_00000000000000aa"


class ListAuditStore:
    """Audit entries in a list: what an instance's own database would hold."""

    def __init__(self) -> None:
        self.rows: list[AuditEntry] = []

    async def insert(self, event: AuditEvent, at_ms: float) -> None:
        self.rows.append(AuditEntry(len(self.rows) + 1, at_ms, event))

    async def entries(self) -> list[AuditEntry]:
        return list(self.rows)

    async def prune(self, before_ms: float, limit: int | None = None) -> int:
        return 0


# --- settings ----------------------------------------------------------------------------------


def test_memory_is_the_default_and_valkey_needs_its_url() -> None:
    clock, epoch = WallClock(), SystemEpoch()
    assert shared_counters_from({}, clock, epoch, TEST_PEPPER) is None
    assert shared_counters_from({COUNTERS_ENV: "memory"}, clock, epoch, TEST_PEPPER) is None
    with pytest.raises(ValueError, match="memory or valkey"):
        shared_counters_from({COUNTERS_ENV: "redis"}, clock, epoch, TEST_PEPPER)
    with pytest.raises(ValueError, match=URL_ENV):
        shared_counters_from({COUNTERS_ENV: "valkey"}, clock, epoch, TEST_PEPPER)
    environ = {COUNTERS_ENV: "valkey", URL_ENV: NOWHERE, MAX_SKEW_ENV: "-1"}
    with pytest.raises(ValueError, match="positive"):
        shared_counters_from(environ, clock, epoch, TEST_PEPPER)


async def test_the_url_and_its_password_appear_in_no_error_log_or_repr(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The URL may carry a password: no refusal, log record or repr may hold it. The scan is
    shown to find the password when it is there (the planted control)."""
    caplog.set_level(logging.DEBUG)
    password = "planted-valkey-password-0123456789"
    url = f"redis://:{password}@127.0.0.1:1/0"
    environ = {COUNTERS_ENV: "valkey", URL_ENV: url}
    counters = shared_counters_from(environ, WallClock(), ManualEpoch(), TEST_PEPPER)
    assert counters is not None
    errors: list[str] = []
    try:
        with pytest.raises(StateUnavailableError) as refused:
            await counters.rate_limiter().admit("acme", 10)
        errors.append(repr(refused.value) + str(refused.value))
        with pytest.raises(StateUnavailableError) as not_started:
            await counters.watch.start(_ignore_restart)
        errors.append(repr(not_started.value) + str(not_started.value))
    finally:
        await counters.aclose()
    corpus = {
        "errors": "\n".join(errors),
        "logs": "\n".join(r.getMessage() for r in caplog.records),
        "repr": repr(counters) + repr(counters.watch),
    }

    def holding(texts: dict[str, str]) -> list[str]:
        return [name for name, text in texts.items() if password in text]

    assert holding(corpus) == []
    assert holding({**corpus, "control": f"the url was {url}"}) == ["control"]


async def _ignore_restart(_: str) -> None:
    return None


# --- failing closed: nothing listens -----------------------------------------------------------


async def test_every_counter_refuses_when_valkey_cannot_be_reached() -> None:
    """Missing is forbidden (docs/35, the table): each counter refuses what it would decide,
    and none admits."""
    counters = counters_on(NOWHERE, ManualEpoch(), prefix="gwtest:nowhere:", timeout_ms=300.0)
    moved: list[Transition] = []
    window = counters.failure_window("login", 5, 60_000.0)
    breakers = counters.breakers(moved.append)
    cap = counters.audit_cap(3)
    try:
        with pytest.raises(StateUnavailableError):
            await counters.rate_limiter().admit("acme", 10)
        assert await window.blocked(window.key("alice")) is True
        assert await window.admit(window.key("alice")) is False
        await window.record_failure(window.key("alice"))  # not counted; nothing raised
        await window.clear(window.key("alice"))  # not cleared: only stricter
        with pytest.raises(StateUnavailableError):
            await counters.connection_tests().count(PROVIDER, ORG)
        assert await breakers.admit(ORG, PROVIDER) is False
        assert await breakers.state(ORG, PROVIDER) is BreakerState.OPEN
        await breakers.failure(ORG, PROVIDER)
        assert moved == []
        assert await cap.take(1.75e12) is False  # the row is not written
        assert await cap.collect(1.75e12) is None  # its minute is not over yet
        dropped = await cap.collect(1.75e12 + 60_000.0)
        assert dropped is not None
        assert dropped.dropped == 1  # counted here, written once the minute is over
        with pytest.raises(StateUnavailableError):
            await counters.watch.start(_ignore_restart)
    finally:
        await counters.aclose()


async def test_requests_and_logins_are_refused_while_valkey_is_unreachable() -> None:
    clock = SimClock()
    counters = counters_on(NOWHERE, ManualEpoch(), prefix="gwtest:nowhere:", timeout_ms=300.0)
    access, keys = application_access(clock, apps={"acme": "pro"}, limits={"acme": (None, 10)})
    registry = build_continuous_registry(clock, seed=7)
    app = create_app(registry, clock, applications=access, counters=counters)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
            answer = await client.post(
                "/v1/generate",
                json={"model": "economy", "prompt": PROMPT, "max_tokens": 2},
                headers=bearer(keys["acme"]),
            )
            snapshot = (await client.get("/v1/monitor/snapshot")).json()
        assert answer.status_code == 503
        assert answer.json()["error"]["code"] == "state_unavailable"
        assert "127.0.0.1" not in answer.text
        # Refused before any model saw it.
        assert sum(m["traffic"]["arrived_total"] for m in snapshot["models"].values()) == 0

        login, step_up, creating = (
            counters.failure_window(kind, limit, 60_000.0)
            for kind, limit in (("login", 5), ("step-up", 3), ("user-create", 10))
        )
        accounts = make_accounts(windows=AccountWindows(login, step_up, creating))
        await accounts.service.create_user("alice", PASSWORD, "north")
        with pytest.raises(LoginFailedError):  # the right password: refused, as any refusal
            await accounts.service.login("alice", PASSWORD)
    finally:
        await registry.aclose()
        await counters.aclose()


# --- the watch: restarts and the clock (scripted) ----------------------------------------------


def healthy(counters: ValkeyCounters) -> bool:
    return counters.healthy


def _scripted(client: ScriptedClient, epoch: ManualEpoch, audit: AuditLog) -> ValkeyCounters:
    return ValkeyCounters(
        client,  # type: ignore[arg-type]
        WallClock(),
        epoch,
        TEST_PEPPER,
        prefix="gwtest:scripted:",
        audit=audit,
    )


async def test_a_restart_is_an_event_on_each_instance_and_one_audit_row() -> None:
    shared: dict[str, str] = {}
    epoch = ManualEpoch()
    stores = [ListAuditStore(), ListAuditStore()]
    clients = [ScriptedClient(shared), ScriptedClient(shared)]
    for client in clients:
        client.time_ms = epoch.now_ms
    watches = [
        _scripted(client, epoch, AuditLog(store)).watch
        for client, store in zip(clients, stores, strict=True)
    ]
    seen: list[tuple[int, str]] = []

    def told(i: int) -> OnRestart:
        async def restarted(run_id: str) -> None:
            seen.append((i, run_id))

        return restarted

    for i, watch in enumerate(watches):
        await watch.start(told(i))
        await watch.check(told(i))
    assert seen == []  # the same run id: no restart
    for client in clients:
        client.run_id = "b" * 40
    for i, watch in enumerate(watches):
        await watch.check(told(i))
        await watch.check(told(i))
    assert seen == [(0, "b" * 40), (1, "b" * 40)]  # each instance, once
    rows = [e.event for store in stores for e in store.rows]
    assert [(r.action, r.target_type, dict(r.details)) for r in rows] == [
        (AuditAction.STATE_STORE_RESTARTED, TargetType.STATE_STORE, {"store": "valkey"})
    ]
    assert rows[0].outcome is Outcome.INFO


async def test_a_store_that_could_lose_a_count_refuses_the_start() -> None:
    for setting, value in (("maxmemory-policy", "allkeys-lru"), ("appendonly", "no")):
        client = ScriptedClient()
        client.config[setting] = value
        counters = _scripted(client, ManualEpoch(client.time_ms), AuditLog(ListAuditStore()))
        with pytest.raises(ValkeyNotSafeError):
            await counters.watch.start(_ignore_restart)


async def test_the_skew_bound_refuses_the_start_and_then_every_call() -> None:
    """Section 3.8: an instance more than the bound from Valkey's clock does not start; one that
    drifts past it while running refuses every shared count until a check passes again."""
    client = ScriptedClient()
    epoch = ManualEpoch(client.time_ms + 1500.0)
    counters = _scripted(client, epoch, AuditLog(ListAuditStore()))
    with pytest.raises(StateUnavailableError, match="1500 ms"):
        await counters.watch.start(_ignore_restart)
    epoch.now_ms = client.time_ms - 900.0  # within the bound, on the other side
    await counters.watch.start(_ignore_restart)
    assert healthy(counters)
    epoch.now_ms = client.time_ms + 1001.0
    await counters.watch.check(_ignore_restart)
    assert not healthy(counters)
    with pytest.raises(StateUnavailableError, match="clock_skew"):
        await counters.rate_limiter().admit("acme", 10)
    window = counters.failure_window("login", 5, 60_000.0)
    assert await window.blocked(window.key("alice")) is True
    epoch.now_ms = client.time_ms
    await counters.watch.check(_ignore_restart)
    assert healthy(counters)


# --- on a real Valkey: one limit for every instance --------------------------------------------

valkey = pytest.mark.valkey


@valkey
async def test_two_instances_serve_one_rate_limit_between_them() -> None:
    """Two gateways, each with its own applications and workers, one Valkey: an application
    limited to 10 requests a minute gets 10 in all, however they are spread."""
    clock = SimClock()
    async with instances(ManualEpoch()) as (a, b):
        apps = []
        for counters in (a, b):
            # The same seed on both: the application's key is the same on each instance.
            access, keys = application_access(
                clock, apps={"acme": "pro"}, limits={"acme": (None, 10)}
            )
            registry = build_continuous_registry(clock, seed=7)
            apps.append(
                (create_app(registry, clock, applications=access, counters=counters), registry)
            )
        clients = [
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw")
            for app, _ in apps
        ]
        body = {"model": "economy", "prompt": PROMPT, "max_tokens": 2}
        try:
            answers = await asyncio.gather(
                *(
                    clients[i % 2].post("/v1/generate", json=body, headers=bearer(keys["acme"]))
                    for i in range(30)
                )
            )
        finally:
            for client in clients:
                await client.aclose()
            for _, registry in apps:
                await registry.aclose()
    statuses = sorted(a.status_code for a in answers)
    assert statuses.count(200) == 10, statuses
    assert statuses.count(429) == 20
    assert {a.json()["error"]["code"] for a in answers if a.status_code == 429} == {"rate_limited"}


@valkey
async def test_the_last_place_in_a_window_is_taken_once() -> None:
    """The user-creation cap: 30 attempts at once on two instances, a cap of 10: exactly 10."""
    async with instances(ManualEpoch()) as (a, b):
        windows = [c.failure_window("user-create", 10, 3_600_000.0) for c in (a, b)]
        taken = await asyncio.gather(*(windows[i % 2].admit("session") for i in range(30)))
    assert taken.count(True) == 10


@valkey
async def test_every_window_counts_or_none_does() -> None:
    """Test-connection: two windows (2 a provider, 3 an organization). A test refused by either
    window is counted in neither."""
    async with instances(ManualEpoch()) as (a, b):
        tests = [c.connection_tests(2, 3) for c in (a, b)]
        await tests[0].count("prv_a", ORG)
        await tests[1].count("prv_a", ORG)
        with pytest.raises(ManagementRefusal, match="2 tests of a provider"):
            await tests[0].count("prv_a", ORG)  # the provider is full; the organization is not
        await tests[1].count("prv_b", ORG)  # so the organization still has its third
        with pytest.raises(ManagementRefusal, match="3 tests of an organization"):
            await tests[0].count("prv_b", ORG)
        provider_b = await a.client.zcard(a.name("tests", "prv_b"))
        organization = await a.client.zcard(a.name("org-tests", ORG))
    assert (provider_b, organization) == (1, 3)


@valkey
async def test_login_failures_count_on_every_instance() -> None:
    async with instances(ManualEpoch()) as (a, b):
        logins = [c.failure_window("login", LOCK_FAILURES, LOCK_WINDOW_MS) for c in (a, b)]
        key = logins[0].key("ghost")
        assert key == logins[1].key("ghost")
        for i in range(LOCK_FAILURES):
            assert not await logins[i % 2].blocked(key)
            await logins[i % 2].record_failure(key)
        assert await logins[0].blocked(key)
        assert await logins[1].blocked(key)
        await logins[1].clear(key)
        assert not await logins[0].blocked(key)


@valkey
async def test_a_half_open_breaker_lets_one_probe_through_across_instances() -> None:
    epoch = ManualEpoch()
    async with instances(epoch) as (a, b):
        moved: list[Transition] = []
        breakers = [a.breakers(moved.append), b.breakers(moved.append)]
        for i in range(5):
            assert await breakers[i % 2].admit(ORG, PROVIDER)
            await breakers[i % 2].failure(ORG, PROVIDER)
        assert not await breakers[1].admit(ORG, PROVIDER)  # opened by both together
        assert await breakers[1].state(ORG, PROVIDER) is BreakerState.OPEN
        epoch.now_ms += 30_000.0
        probes = await asyncio.gather(*(breakers[i % 2].admit(ORG, PROVIDER) for i in range(20)))
        assert probes.count(True) == 1
        epoch.now_ms += 120_000.0  # the probe's instance never reported: its slot frees itself
        assert await breakers[0].admit(ORG, PROVIDER)
        await breakers[0].success(ORG, PROVIDER)
        assert await breakers[1].admit(ORG, PROVIDER)
        await breakers[1].success(ORG, PROVIDER)
        assert await breakers[0].state(ORG, PROVIDER) is BreakerState.CLOSED
    assert [m.state for m in moved] == [
        BreakerState.OPEN,
        BreakerState.HALF_OPEN,
        BreakerState.CLOSED,
    ]


@valkey
async def test_dropped_audit_rows_are_written_once_across_instances() -> None:
    epoch = ManualEpoch()
    async with instances(epoch) as (a, b):
        stores = [ListAuditStore(), ListAuditStore()]
        logs = [AuditLog(s, cap=c.audit_cap(3)) for s, c in zip(stores, (a, b), strict=True)]
        unknown = AuditEvent(AuditAction.LOGIN_FAILED, Outcome.FAILURE, TargetType.UNKNOWN_USER)
        written = await asyncio.gather(
            *(logs[i % 2].write(unknown, epoch.now_ms) for i in range(10))
        )
        later = epoch.now_ms + 60_000.0
        flushed = await asyncio.gather(*(log.flush_dropped(later) for log in logs * 3))
    assert written.count(True) == 3
    assert sorted(flushed) == [0, 0, 0, 0, 0, 7]
    dropped = [
        e.event for s in stores for e in s.rows if e.event.action is AuditAction.ROWS_DROPPED
    ]
    assert [d.details["dropped"] for d in dropped] == [7]


@valkey
async def test_the_watch_reads_valkeys_run_id_and_clock() -> None:
    async with instances(OffsetEpoch(), count=1) as (a,):
        await a.watch.start(_ignore_restart)
        first = a.watch.run_id
        await a.watch.check(_ignore_restart)
        assert a.watch.run_id == first is not None
        assert a.watch.skew_ms is not None
        assert abs(a.watch.skew_ms) < 1000.0


@valkey
async def test_an_instance_past_the_skew_bound_does_not_start() -> None:
    async with instances(OffsetEpoch(2500.0), count=1) as (a,):
        with pytest.raises(StateUnavailableError, match="bound"):
            await a.watch.start(_ignore_restart)
        with pytest.raises(StateUnavailableError, match="clock_skew"):
            await a.rate_limiter().admit("acme", 10)


@valkey
async def test_valkey_holds_no_identifier_and_the_scan_would_see_one() -> None:
    """Every name is an HMAC and every value a count or a time: no username, session digest,
    application or provider id appears in the key space (docs/35). A key planted with one is
    found by the same scan."""
    epoch = ManualEpoch()
    secrets = {
        "username": "alice-the-username",
        "session digest": "f" * 64,
        "application": "acme-application",
        "provider": PROVIDER,
        "organization": "north-organization",
    }
    async with instances(epoch) as (a, b):
        login = a.failure_window("login", 5, 60_000.0)
        await login.record_failure(login.key(secrets["username"]))
        await b.failure_window("step-up", 3, 60_000.0).record_failure(secrets["session digest"])
        await a.failure_window("user-create", 10, 60_000.0).admit(secrets["session digest"])
        await b.rate_limiter().admit(secrets["application"], 10)
        await a.connection_tests().count(secrets["provider"], secrets["organization"])
        await b.breakers().failure(secrets["organization"], secrets["provider"])
        await AuditLog(ListAuditStore(), cap=a.audit_cap(1)).write(
            AuditEvent(AuditAction.LOGIN_FAILED, Outcome.FAILURE, TargetType.UNKNOWN_USER),
            epoch.now_ms,
        )

        async def space() -> str:
            """Every key under the prefix, with everything it holds."""
            texts = []
            async for key in a.client.scan_iter(match=a.prefix + "*"):
                kind = str(await a.client.type(key))
                value: object
                if kind == "zset":
                    value = await a.client.zrange(key, 0, -1, withscores=True)
                elif kind == "hash":
                    value = await a.client.hgetall(key)
                elif kind == "set":
                    value = await a.client.smembers(key)
                else:
                    value = await a.client.get(key)
                texts.append(f"{key!s} {value!s}")
            return "\n".join(texts)

        clean = await space()
        await a.client.set(a.prefix + "planted:" + secrets["provider"], "1")
        planted = await space()
    assert len(clean.splitlines()) >= 7
    assert [n for n, s in secrets.items() if s in clean] == []
    assert [n for n, s in secrets.items() if s in planted] == ["provider"]


def test_the_counter_benchmark_needs_a_valkey_to_measure_against(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(URL_ENV, raising=False)
    with pytest.raises(SystemExit):
        counter_calls.main(["--calls", "1"])
    assert URL_ENV in capsys.readouterr().err


@valkey
async def test_the_counter_benchmark_times_each_call_and_leaves_no_key() -> None:
    url = os.environ[VALKEY_URL_ENV]
    rows = await counter_calls.run(1, url)
    assert [r.store for r in rows] == [
        "rate limit, memory",
        "rate limit, valkey",
        "login check, memory",
        "login check, valkey",
        "breaker admit, memory",
        "breaker admit, valkey",
    ]
    client = Redis.from_url(url, decode_responses=True)
    try:
        assert [k async for k in client.scan_iter(match="gwbench:*")] == []
    finally:
        await client.aclose()
