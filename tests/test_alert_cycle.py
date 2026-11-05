"""The alert evaluator's cycle and what it costs (the plan's section 11.7, docs/45): each bulk
read answers what its one-at-a-time read does, on memory, SQLite, Postgres and Valkey; the
server's page read gives each application the values its one-application reads give, and a
cycle through it makes the same incidents as one through those; a cycle's store calls grow with
its pages, not its applications; the time box, the fair resume and "always one application";
and a slow cycle delaying no chat request, on Postgres (its own pool) and on SQLite (the event
loop given back between applications).

Tests marked ``postgres`` and ``valkey`` run against the compose services. Test values, not
secrets."""

from __future__ import annotations

import asyncio
import dataclasses
import itertools
import os
import random
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from gateway.alertrecorder import MINUTE_MS
from gateway.alertrollups import AlertRollups, InMemoryAlertRollups
from gateway.alertrules import (
    AlertRule,
    InMemoryRuleStore,
    RuleStore,
    default_rules,
    validate_rule,
)
from gateway.apikeys import ApiKeyService, ApplicationAccess
from gateway.app import _DurableSignals, alert_settings, create_app, default_platform_settings
from gateway.applications import (
    ApiKeyRecord,
    ApplicationStore,
    InMemoryApplicationStore,
    application_from_template,
)
from gateway.audit import AuditLog
from gateway.breaker import Breakers
from gateway.clock import Clock, ClockEpoch, SimClock, SystemEpoch, WallClock
from gateway.continuous import build_continuous_registry
from gateway.database import open_database
from gateway.evaluator import CYCLE_BUDGET_MS, AlertEvaluator, PerApplicationSignals
from gateway.incidents import (
    Change,
    Fence,
    Incident,
    IncidentState,
    IncidentStore,
    InMemoryIncidentStore,
    ResolveReason,
)
from gateway.ledger import CostLedger
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.providers import ProviderConfig, ProviderService
from gateway.redaction import Redactor
from gateway.routing import load_plans
from gateway.settings import InMemorySettingsStore, Scope, SettingsStore, canonical
from gateway.signals import SignalValue
from gateway.state import StateBackend, sqlite_state
from gateway.usage import InMemoryUsageStore, UsageEvent, UsageStore
from gateway.vault import Vault
from helpers import TEST_PEPPER, TEST_PROFILES

# 2026-11-02 (a Monday) 10:00 UTC, in ms.
T0 = 1_793_613_600_000.0
DAY_MS = 86_400_000.0
PLANS = load_plans()
MASTER = bytes(range(32))
ORG = "default"


def _app_id(i: int) -> str:
    return f"app{i:04d}"


def _sqlite_state(tmp_path: Path, epoch: Any) -> StateBackend:
    connection = open_database(tmp_path / "gateway.db")
    MigrationRunner(connection, MIGRATIONS, SystemEpoch()).migrate()
    return sqlite_state(connection, epoch)


async def _seed_applications(applications: ApplicationStore, count: int, now: float) -> None:
    for i in range(count):
        app_id = _app_id(i)
        await applications.add_application(
            application_from_template(
                app_id, f"App {i}", PLANS, "pro", organization_id=ORG, created_at_ms=now
            )
        )
        # Keys: none for every third application, one or two for the others, one revoked.
        for k in range(i % 3):
            await applications.add_key(
                ApiKeyRecord(
                    f"{i:012x}{k:04x}", app_id, "k", "0" * 64, now,
                    now + (i + k + 1) * DAY_MS, revoked=(i % 7 == 0 and k == 1),
                )
            )  # fmt: skip


async def _seed_usage(usage: UsageStore, count: int, now: float) -> None:
    for i in range(count):
        for n in range(i % 4):
            await usage.append(
                UsageEvent(
                    _app_id(i), f"req-{i}-{n}", ("economy", "premium")[n % 2], 100 + n, n,
                    10 * i + n, 0.1 * n, 0.3 * (n + 1), now - 1000.0 * (n + 1),
                )
            )  # fmt: skip
    # One before the period: in no total.
    await usage.append(UsageEvent(_app_id(1), "req-old", "economy", 9, 0, 9, 1.0, 1.0, 0.0))


async def _seed_settings(settings: SettingsStore, now: float) -> None:
    for scope, scope_id, document in (
        (Scope.ORGANIZATION, ORG, {"monthly_token_budget": 5000}),
        (Scope.APPLICATION, _app_id(2), {"monthly_token_budget": 40}),
        (Scope.APPLICATION, _app_id(4), {"allowed_providers": ["prv_00000000000000a1"]}),
    ):
        await settings.write(
            scope, scope_id, expected_version=0, document=canonical(document),
            changed=list(document), changed_by_user_id=None, via="test", at_ms=now,
        )  # fmt: skip
    # A row that cannot be judged: its application's values are "unavailable".
    await settings.write(
        Scope.APPLICATION, _app_id(5), expected_version=0, document="{not json",
        changed=["monthly_token_budget"], changed_by_user_id=None, via="test", at_ms=now,
    )  # fmt: skip


def _resolved(
    n: int, rule_id: str, series_key: str, resolved_at: float, *, app: str = "app0000"
) -> Incident:
    return Incident(
        f"inc_{n:016x}", ORG, app, rule_id, 1, series_key, IncidentState.RESOLVED, "warning",
        resolved_at - 60_000.0, resolved_at_ms=resolved_at, resolve_reason=ResolveReason.CLEARED,
        last_notified_at_ms=resolved_at - 30_000.0,
    )  # fmt: skip


async def _seed_resolved(incidents: IncidentStore) -> list[tuple[str, str]]:
    """Resolved incidents of several series, two of them twice; returns the series asked."""
    lease = await incidents.attempt_lease("seed")
    fence = Fence("seed", lease.epoch)
    made = [
        _resolved(1, "rul_00000000000000a1", "", T0 - 50 * MINUTE_MS),
        _resolved(2, "rul_00000000000000a1", "", T0 - 5 * MINUTE_MS),  # the later of the two
        _resolved(3, "rul_00000000000000a2", "key1", T0 - 3 * MINUTE_MS),
        _resolved(4, "rul_00000000000000a2", "key2", T0 - 3 * MINUTE_MS),
        _resolved(5, "rul_00000000000000a2", "key2", T0 - 3 * MINUTE_MS),  # a tie: the larger id
        _resolved(6, "rul_00000000000000a3", "", T0 - 500 * MINUTE_MS),  # before the window
        _resolved(7, "rul_00000000000000a4", "", T0 - MINUTE_MS),  # a series not asked
    ]
    for incident in made:
        assert await incidents.apply(fence, Change(incident, None, None))
    return [
        ("rul_00000000000000a1", ""),
        ("rul_00000000000000a2", "key1"),
        ("rul_00000000000000a2", "key2"),
        ("rul_00000000000000a3", ""),
        ("rul_00000000000000a9", ""),  # none at all
    ]


@dataclass
class Stores:
    applications: ApplicationStore
    settings: SettingsStore
    usage: UsageStore
    incidents: IncidentStore


async def _bulk_equals_one_at_a_time(stores: Stores, clock: Clock) -> None:
    count = 12
    await _seed_applications(stores.applications, count, T0)
    await _seed_usage(stores.usage, count, T0)
    await _seed_settings(stores.settings, T0)
    asked = [_app_id(i) for i in range(count)] + ["app-gone"]
    one = {a: await stores.applications.get_application(a) for a in asked}
    assert await stores.applications.get_applications(asked) == {
        a: found for a, found in one.items() if found is not None
    }
    assert await stores.applications.get_applications([]) == {}
    assert await stores.applications.keys_of(asked) == {
        a: await stores.applications.list_keys(a) for a in asked
    }
    for scope, ids in ((Scope.ORGANIZATION, [ORG, "org-none"]), (Scope.APPLICATION, asked)):
        rows = {i: await stores.settings.current(scope, i) for i in ids}
        assert await stores.settings.current_many(scope, ids) == {
            i: row for i, row in rows.items() if row is not None
        }
    start, end = T0 - DAY_MS, T0 + DAY_MS
    assert await stores.usage.usage_by_application(asked, start, end) == {
        a: await stores.usage.usage_by_model(a, start, end) for a in asked
    }
    ledger = CostLedger(clock, store=stores.usage, epoch=ClockEpoch(clock, T0))
    await ledger.usage(_app_id(3))  # one account held already, the rest read at once
    assert await ledger.usage_many(asked) == {a: await ledger.usage(a) for a in asked}
    series = await _seed_resolved(stores.incidents)
    since = T0 - 60 * MINUTE_MS
    expected = {}
    for rule_id, key in series:
        found = await stores.incidents.recent_resolved(rule_id, key)
        if found is not None and found.resolved_at_ms is not None and found.resolved_at_ms >= since:
            expected[(rule_id, key)] = found
    assert await stores.incidents.recent_resolved_many(series, since) == expected
    assert set(expected) == {
        ("rul_00000000000000a1", ""),
        ("rul_00000000000000a2", "key1"),
        ("rul_00000000000000a2", "key2"),
    }
    assert expected[("rul_00000000000000a2", "key2")].id == f"inc_{5:016x}"


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_each_bulk_read_answers_what_its_one_at_a_time_read_does(
    kind: str, tmp_path: Path
) -> None:
    clock = SimClock()
    epoch = ClockEpoch(clock, T0)
    if kind == "memory":
        stores = Stores(
            InMemoryApplicationStore(),
            InMemorySettingsStore(),
            InMemoryUsageStore(),
            InMemoryIncidentStore(epoch),
        )
    else:
        state = _sqlite_state(tmp_path, epoch)
        stores = Stores(state.applications, state.settings, state.usage, state.incidents)
    await _bulk_equals_one_at_a_time(stores, clock)


@pytest.mark.postgres
async def test_each_postgres_bulk_read_answers_what_its_one_at_a_time_read_does() -> None:
    from gateway.postgres import PostgresBudgets, postgres_state
    from postgres_helpers import pg_world

    async with pg_world() as world:
        await world.set_time(T0)
        state = postgres_state(world.instances[0])
        await _bulk_equals_one_at_a_time(
            Stores(state.applications, state.settings, state.usage, state.incidents), WallClock()
        )
        budgets = PostgresBudgets(world.instances[0], WallClock())
        asked = [_app_id(i) for i in range(12)] + ["app-gone"]
        assert await budgets.usage_many(asked) == {a: await budgets.usage(a) for a in asked}


async def _rollups_bulk_equals_one_at_a_time(rollups: AlertRollups) -> None:
    await rollups.add(
        "b1",
        {
            ("app1", 600): {"requests": 2, "status:5xx": 1},
            ("app1", 601): {"requests": 3},
            ("app2", 601): {"requests": 7},
        },
    )
    await rollups.mark_incomplete([("app2", 600)])
    wanted = [("app1", range(599, 603)), ("app2", range(600, 602)), ("app3", range(600, 601))]
    one = [await rollups.read(a, minutes) for a, minutes in wanted]
    assert await rollups.read_many(wanted) == one
    assert one[1][0].incomplete


async def test_the_rollups_read_many_answers_what_read_does_in_memory() -> None:
    await _rollups_bulk_equals_one_at_a_time(InMemoryAlertRollups())


@pytest.mark.valkey
async def test_the_valkey_rollups_read_many_answers_what_read_does_in_one_pipeline() -> None:
    from valkey_helpers import VALKEY_URL_ENV, ManualEpoch, counters_on

    prefix = f"gwtest:{random.SystemRandom().getrandbits(64):016x}:"
    counters = counters_on(os.environ[VALKEY_URL_ENV], ManualEpoch(), prefix=prefix)
    try:
        await _rollups_bulk_equals_one_at_a_time(counters.alert_rollups())
    finally:
        async for key in counters.client.scan_iter(match=prefix + "*"):
            await counters.client.delete(key)
        await counters.aclose()


# --- the server's page read, and a cycle through it -----------------------------------------------


@dataclass
class Server:
    """The server's reads (``gateway.app``'s) on one SQLite state."""

    state: StateBackend
    clock: SimClock
    breakers: Breakers
    signals: _DurableSignals


def _server(tmp_path: Path, clock: SimClock) -> Server:
    epoch = ClockEpoch(clock, T0)
    state = _sqlite_state(tmp_path, epoch)
    ledger = CostLedger(clock, store=state.usage, epoch=epoch)
    settings_of, settings_many = alert_settings(state.settings, default_platform_settings())
    providers = ProviderService(
        state.providers, Vault(MASTER), AuditLog(state.audit), clock, epoch, Redactor(),
        lambda: httpx.AsyncClient(),  # never called: nothing here connects
    )  # fmt: skip
    breakers = Breakers(clock, failure_threshold=1)
    signals = _DurableSignals(
        state.applications, ledger.usage, settings_of, providers, breakers, epoch,
        usage_many=ledger.usage_many, settings_many=settings_many,
    )  # fmt: skip
    return Server(state, clock, breakers, signals)


async def _seed_server(server: Server, count: int) -> None:
    state = server.state
    await _seed_applications(state.applications, count, T0)
    await _seed_usage(state.usage, count, T0)
    await _seed_settings(state.settings, T0)
    for n, enabled in ((1, True), (2, True), (3, False)):
        await state.providers.add(
            ProviderConfig(
                f"prv_00000000000000a{n}", ORG, "openai_compatible", f"https://p{n}.example/v1",
                "m", "economy", "primary", enabled, True, None, None, {}, None, T0,
            )
        )  # fmt: skip
    await server.breakers.failure(ORG, "prv_00000000000000a2")  # open
    for i in range(count):
        for rule in default_rules(_app_id(i), T0):
            await state.rules.insert_rule(
                dataclasses.replace(rule, spec=dataclasses.replace(rule.spec, enabled=True))
            )


async def test_the_servers_page_read_gives_each_application_its_one_at_a_time_values(
    tmp_path: Path,
) -> None:
    server = _server(tmp_path, SimClock())
    count = 9
    await _seed_server(server, count)
    every = frozenset({"budget_used", "circuit_open", "key_expires_in", "reservation_expired"})
    wanted = {_app_id(i): every for i in range(count)} | {"app-gone": every}
    wanted[_app_id(1)] = frozenset({"key_expires_in"})  # only what its rules use
    page = await server.signals.read_many(wanted)
    assert set(page) == set(wanted)
    assert page["app-gone"].organization_id is None
    one = PerApplicationSignals(_organization_of(server.state.applications), server.signals)
    assert dict(await one.read_many(wanted)) == dict(page)
    # What the values are: a budget from the organization's row and one of its own; a row that
    # cannot be judged is "unavailable"; the open breaker, the closed one, the disabled one not
    # at all, and only the allowed one where the application limits them.
    assert {p: v.value for p, v in page[_app_id(0)].circuit_open.items()} == {
        "prv_00000000000000a1": 0.0,
        "prv_00000000000000a2": 1.0,
    }
    assert set(page[_app_id(4)].circuit_open) == {"prv_00000000000000a1"}
    assert page[_app_id(5)].budget_used.why_not == "unavailable"
    assert page[_app_id(2)].budget_used.value is not None
    assert page[_app_id(1)].budget_used.why_not == "unavailable"  # not asked
    assert len(page[_app_id(2)].key_expires_in) == 2


def _organization_of(applications: ApplicationStore) -> Callable[[str], Any]:
    async def organization_of(application_id: str) -> str | None:
        found = await applications.get_application(application_id)
        return found.organization_id if found is not None else None

    return organization_of


def _rollups_for(count: int, now_ms: float) -> InMemoryAlertRollups:
    rollups = InMemoryAlertRollups()
    minute = int(now_ms // MINUTE_MS)
    for i in range(count):
        rollups.absorb(
            {
                (_app_id(i), m): {
                    "requests": 40,
                    "status:5xx": 10 if m < minute + i % 4 else 0,
                    "reject:budget_exhausted": 2 if i % 4 == 1 else 0,
                }
                for m in range(minute - 70, minute + 70)
            }
        )
    return rollups


def _evaluator(
    server: Server, rollups: AlertRollups, *, bulk: bool, holder: str = "a"
) -> AlertEvaluator:
    seq = itertools.count(1)
    random_bytes = lambda n: next(seq).to_bytes(n, "big")  # noqa: E731
    if bulk:
        return AlertEvaluator(
            incidents=server.state.incidents, rules=server.state.rules, rollups=rollups,
            signals=server.signals, clock=server.clock, holder=holder,
            random_bytes=random_bytes,
        )  # fmt: skip
    return AlertEvaluator(
        incidents=server.state.incidents, rules=server.state.rules, rollups=rollups,
        organization_of=_organization_of(server.state.applications), durable=server.signals,
        clock=server.clock, holder=holder, random_bytes=random_bytes,
    )  # fmt: skip


async def _history(server: Server, evaluator: AlertEvaluator, cycles: int) -> list[Any]:
    """The incidents and notifications after each of ``cycles`` cycles, 30 s apart."""
    found: list[Any] = []
    for _ in range(cycles):
        assert await evaluator.step()
        incidents = await server.state.incidents.list_incidents(
            ORG, application_id=None, state=None, limit=1000, after=None
        )
        notes = [await server.state.incidents.notifications(i.id) for i in incidents]
        found.append((incidents, notes))
        await server.clock.sleep_ms(30_000.0)
    return found


async def test_a_cycle_through_the_page_read_makes_what_one_through_one_at_a_time_reads_does(
    tmp_path: Path,
) -> None:
    """The same applications, rules, rollups and stores, evaluated for 24 cycles through the
    server's page read and through its one-application reads: the same incidents in the same
    states, with the same notifications, after every cycle."""
    histories = []
    for bulk in (True, False):
        server = _server(tmp_path / str(bulk), SimClock())
        await _seed_server(server, 30)
        rollups = _rollups_for(30, T0)
        histories.append(await _history(server, _evaluator(server, rollups, bulk=bulk), 24))
    bulk_history, one_history = histories
    assert bulk_history == one_history
    states = {i.state for i in bulk_history[-1][0]}
    assert {IncidentState.FIRING, IncidentState.RESOLVED} <= states  # transitions were made


class _CountingStore:
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


async def _steady_calls(tmp_path: Path, count: int) -> int:
    clock = SimClock()
    server = _server(tmp_path, clock)
    await _seed_server(server, count)
    calls = [0]
    state = server.state
    epoch = ClockEpoch(clock, T0)
    ledger = CostLedger(clock, store=_CountingStore(state.usage, calls), epoch=epoch)
    settings_of, settings_many = alert_settings(
        _CountingStore(state.settings, calls), default_platform_settings()
    )
    applications = _CountingStore(state.applications, calls)
    signals = _DurableSignals(
        applications, ledger.usage, settings_of, None, server.breakers, epoch,
        usage_many=ledger.usage_many, settings_many=settings_many,
    )  # fmt: skip
    incidents = _CountingStore(state.incidents, calls)
    evaluator = AlertEvaluator(
        incidents=incidents, rules=_CountingStore(state.rules, calls),
        rollups=_CountingStore(_rollups_for(count, T0), calls), signals=signals, clock=clock,
        holder="a", random_bytes=random.Random(1).randbytes,
    )  # fmt: skip
    lease = await state.incidents.attempt_lease("a")
    await evaluator.cycle(Fence("a", lease.epoch), lease.now_ms)  # opens what it opens
    calls[0] = 0
    await evaluator.cycle(Fence("a", lease.epoch), lease.now_ms)
    return calls[0]


async def test_a_cycles_store_calls_grow_with_its_pages_not_its_applications(
    tmp_path: Path,
) -> None:
    """Steady cycles (nothing new to open) of 1, 200 and 400 applications with every default
    rule on: about the same calls a page, whatever the page holds."""
    one, page, two_pages = [await _steady_calls(tmp_path / str(n), n) for n in (1, 200, 400)]
    assert one <= 12
    # A page's reads, and the rules listed 200 at a time (8 rules an application).
    assert page <= one + 12
    assert two_pages - page <= page - one + 12
    assert two_pages < 60


async def test_a_series_firing_again_inside_its_cooldown_reopens_its_incident_through_a_cycle(
    tmp_path: Path,
) -> None:
    """The last resolved incident of a series, read with the page's other series (only inside
    the page's longest cooldown): an error rule fires, resolves, and fires again 10 minutes
    later, inside its 30-minute cooldown: the same incident reopens, and nothing is notified
    until the cooldown ends."""
    clock = SimClock()
    server = _server(tmp_path, clock)
    await _seed_applications(server.state.applications, 2, T0)
    spec = validate_rule(
        {"name": "Errors", "signal": "error_rate", "comparison": ">", "threshold": 5,
         "window": 5, "for_minutes": 0, "severity": "critical", "cooldown_minutes": 30}
    )  # fmt: skip
    rule = default_rules(_app_id(0), T0)[0]
    await server.state.rules.insert_rule(dataclasses.replace(rule, spec=spec))
    minute = int(T0 // MINUTE_MS)
    rollups = InMemoryAlertRollups()
    for m in range(minute - 10, minute + 40):
        failing = m < minute + 1 or minute + 12 <= m
        rollups.absorb({(_app_id(0), m): {"requests": 40, "status:5xx": 10 if failing else 0}})
    evaluator = _evaluator(server, rollups, bulk=True)
    seen = []
    for _ in range(40):
        assert await evaluator.step()
        [incident] = await server.state.incidents.list_incidents(
            ORG, application_id=None, state=None, limit=10, after=None
        )
        seen.append((incident.state, incident.reopened))
        await clock.sleep_ms(30_000.0)
    states = [state for state, _ in seen]
    assert states[0] == IncidentState.FIRING
    assert IncidentState.RESOLVED in states
    assert seen[-1] == (IncidentState.FIRING, 1)  # the same incident, reopened once
    notes = await server.state.incidents.notifications(incident.id)
    assert [n.kind.value for n in notes] == ["firing", "resolved"]


# --- the time box, the fair resume, always one ----------------------------------------------------


class _SecondAnApplication(SimClock):
    """Each application's work takes a second: the cycle's yield between two applications costs
    one simulated second here (and nothing else does)."""

    async def sleep_ms(self, duration_ms: float) -> None:
        await super().sleep_ms(duration_ms if duration_ms > 0 else 1000.0)


async def _boxed(count: int, *, slow_open: float = 0.0) -> tuple[AlertEvaluator, list[set[str]]]:
    """``count`` applications, each with one rule that fires at once, evaluated for three cycles
    on a clock where each application's work takes a second; which applications have an open
    incident after each cycle. ``slow_open``: the open incidents' read takes that long."""
    clock = _SecondAnApplication(T0)
    incidents = InMemoryIncidentStore(ClockEpoch(clock, 0.0))
    rules = InMemoryRuleStore()
    rule = default_rules("x", T0)[0]
    spec = validate_rule(
        {"name": "Load", "signal": "requests", "comparison": ">=", "threshold": 1, "window": 5,
         "for_minutes": 0, "severity": "critical"}
    )  # fmt: skip
    for i in range(count):
        await rules.insert_rule(
            dataclasses.replace(rule, id=f"rul_{i:016x}", application_id=_app_id(i), spec=spec)
        )
    rollups = _rollups_for(count, T0)

    class Slow:
        def __getattr__(self, name: str) -> Any:
            return getattr(incidents, name)

        async def open_incidents(self) -> list[Incident]:
            if slow_open:
                await SimClock.sleep_ms(clock, slow_open)
            return await incidents.open_incidents()

    async def organization_of(application_id: str) -> str | None:
        return ORG

    class Durable:  # no durable rule here: never asked
        async def budget_used(self, application_id: str) -> SignalValue:
            raise AssertionError

        async def circuit_open(self, organization_id: str, application_id: str) -> Any:
            raise AssertionError

        async def key_expires_in(self, application_id: str) -> Any:
            raise AssertionError

        async def reservation_expired(self, application_id: str) -> SignalValue:
            raise AssertionError

    seq = itertools.count(1)
    evaluator = AlertEvaluator(
        incidents=Slow(), rules=rules, rollups=rollups, organization_of=organization_of,
        durable=Durable(), clock=clock, holder="a",
        random_bytes=lambda n: next(seq).to_bytes(n, "big"),
    )  # fmt: skip
    opened: list[set[str]] = []
    for _ in range(3):
        lease = await incidents.attempt_lease("a")
        started = clock.now_ms()
        await evaluator.cycle(Fence("a", lease.epoch), T0)
        # A cycle starts no application past its time box: it ends within one application of it.
        assert clock.now_ms() - started <= slow_open + CYCLE_BUDGET_MS + 1000.0
        opened.append({i.application_id for i in await incidents.open_incidents()})
    return evaluator, opened


async def test_the_time_box_cuts_a_cycle_at_20_s_and_the_next_resumes_after_it() -> None:
    """50 applications, a second each: the first cycle reaches the first 20 and stops; the
    second starts with the 21st; the third reaches the last 10 and wraps round. Every
    application is evaluated within three cycles."""
    evaluator, opened = await _boxed(50)
    assert opened[0] == {_app_id(i) for i in range(20)}
    assert opened[1] == {_app_id(i) for i in range(40)}
    assert opened[2] == {_app_id(i) for i in range(50)}
    assert evaluator.cut_short == 3


async def test_a_cycle_under_its_time_box_is_not_cut_and_starts_from_the_first() -> None:
    evaluator, opened = await _boxed(15)
    assert opened == [{_app_id(i) for i in range(15)}] * 3
    assert evaluator.cut_short == 0


async def test_a_cycle_whose_time_box_is_spent_before_any_application_still_does_one() -> None:
    """The open incidents' read takes 25 s, longer than the time box: each cycle still
    evaluates one application, and the next resumes after it."""
    _, opened = await _boxed(5, slow_open=25_000.0)
    assert opened == [{_app_id(0)}, {_app_id(0), _app_id(1)}, {_app_id(i) for i in range(3)}]


# --- a slow cycle delays no chat request ----------------------------------------------------------


async def test_a_chat_request_is_served_while_a_cycle_runs_on_sqlite(tmp_path: Path) -> None:
    """The server on SQLite, whose calls never suspend: a cycle over 300 applications started
    first, then a chat request. The request's key is checked while the cycle is still running
    (the cycle gives the event loop back between applications), and it is answered."""
    clock = SimClock()
    epoch = ClockEpoch(clock, T0)
    state = _sqlite_state(tmp_path, epoch)
    await _seed_applications(state.applications, 300, T0)
    for i in range(300):
        for rule in default_rules(_app_id(i), T0):
            await state.rules.insert_rule(rule)
    keys = ApiKeyService(
        state.applications, TEST_PEPPER, epoch, random_bytes=random.Random(3).randbytes
    )
    key = (await keys.issue(_app_id(1), "chat")).key
    registry = build_continuous_registry(clock, seed=7, profiles=TEST_PROFILES)
    app = create_app(
        registry, clock, applications=ApplicationAccess(keys, PLANS), epoch=epoch,
        usage_store=state.usage, settings=state.settings, rules=state.rules,
        incidents=state.incidents,
    )  # fmt: skip
    evaluator: AlertEvaluator = app.state.alert_evaluator
    started = asyncio.Event()
    seen: list[bool] = []
    listing = state.rules.list_enabled_rules
    lookup = state.applications.lookup_key

    async def list_enabled(limit: int, after: str | None = None) -> list[AlertRule]:
        started.set()
        return await listing(limit, after)

    async def looked_up(key_id: str) -> Any:
        seen.append(cycle.done())
        return await lookup(key_id)

    state.rules.list_enabled_rules = list_enabled  # type: ignore[method-assign]
    state.applications.lookup_key = looked_up  # type: ignore[method-assign]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
        cycle = asyncio.create_task(evaluator.step())
        await started.wait()
        answer = await client.post(
            "/v1/generate",
            json={"model": "economy", "prompt": "hello", "max_tokens": 1},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert await cycle
    await registry.aclose()
    assert answer.status_code == 200
    assert seen == [False]  # the key checked while the cycle ran


@pytest.mark.postgres
async def test_a_chat_request_is_served_while_the_evaluators_pool_is_held_on_postgres() -> None:
    """The server on Postgres with one request connection and one evaluator connection: the
    evaluator's cycle is held in a 3 s read in its own pool, and a chat request made meanwhile
    is answered before that read ends."""
    from gateway.postgres import postgres_state
    from postgres_helpers import pg_world

    async with pg_world(request_pool_size=1, evaluator_pool_size=1) as world:
        db = world.instances[0]
        state = postgres_state(db)
        clock = WallClock()
        await _seed_applications(state.applications, 3, T0)
        for rule in default_rules(_app_id(1), T0):
            await state.rules.insert_rule(rule)
        keys = ApiKeyService(
            state.applications, TEST_PEPPER, SystemEpoch(), store_clock=db,
            random_bytes=random.Random(4).randbytes,
        )  # fmt: skip
        key = (await keys.issue(_app_id(1), "chat")).key
        slow = _SlowRules(state.rules, db)
        registry = build_continuous_registry(clock, seed=7, profiles=TEST_PROFILES)
        app = create_app(
            registry, clock, applications=ApplicationAccess(keys, PLANS), epoch=SystemEpoch(),
            rules=slow, incidents=state.incidents, startup_applications=[],
            evaluator_context=db.evaluation,
        )  # fmt: skip
        transport = httpx.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=transport, base_url="http://gw") as client,
        ):
            await slow.started.wait()  # the evaluator's task, in its read
            answer = await client.post(
                "/v1/generate",
                json={"model": "economy", "prompt": "hello", "max_tokens": 1},
                headers={"Authorization": f"Bearer {key}"},
            )
            answered_during_the_read = not slow.finished
        await registry.aclose()
    assert answer.status_code == 200
    assert answered_during_the_read


class _SlowRules:
    """The rule store, its first listing held 3 s in a read of the task's own pool."""

    def __init__(self, inner: RuleStore, db: Any) -> None:
        self._inner = inner
        self._db = db
        self.started = asyncio.Event()
        self.finished = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def list_enabled_rules(self, limit: int, after: str | None = None) -> list[AlertRule]:
        if not self.started.is_set():
            self.started.set()
            async with self._db.connection() as connection:
                await connection.execute("SELECT pg_sleep(3)")
            self.finished = True
        return await self._inner.list_enabled_rules(limit, after)
