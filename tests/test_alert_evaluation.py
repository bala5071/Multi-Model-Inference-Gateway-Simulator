"""The alert evaluator (docs/45): every transition of the plan's section 8 through ``decide``;
one open incident a series, compare-and-set and the fence in every store; a notification
recorded with its change or not at all; the cooldown, reminders, quiet hours and the rule
changed under an incident; the lease on two and three instances, the holder killed and ``for``
continuing, a fenced evaluator writing nothing; the heartbeat in each state, from memory alone;
``/healthz``, the metrics, the snapshot and the events. The endpoints and the leak scan are in
``test_incidents_api.py``.

Tests marked ``postgres`` run against the Postgres of ``docker-compose.postgres.yml``. Test
values, not secrets."""

from __future__ import annotations

import asyncio
import dataclasses
import itertools
import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from gateway.alertrecorder import MINUTE_MS
from gateway.alertrollups import InMemoryAlertRollups
from gateway.alertrules import (
    AlertRule,
    InMemoryRuleStore,
    QuietHours,
    default_rules,
    validate_rule,
)
from gateway.clock import ClockEpoch, SimClock, SystemEpoch
from gateway.continuous import build_continuous_registry
from gateway.database import open_database
from gateway.evaluator import (
    CYCLE_MS,
    NO_DATA_MS,
    AlertEvaluator,
    HeartbeatState,
    Series,
    decide,
    in_quiet_hours,
    quiet_hours_over,
    rule_gone,
)
from gateway.incidents import (
    LEASE_MS,
    Change,
    Fence,
    Incident,
    IncidentState,
    IncidentStore,
    InMemoryIncidentStore,
    NotificationKind,
    ResolveReason,
    SqliteIncidentStore,
)
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.signals import SignalValue
from gateway.state import StateUnavailableError

# 2026-11-02 (a Monday) 10:00 UTC, in ms.
T0 = 1_793_613_600_000.0
RULE_BODY = {
    "name": "Errors",
    "signal": "error_rate",
    "comparison": ">",
    "threshold": 5,
    "window": 5,
    "for_minutes": 5,
    "severity": "critical",
}


def _rule(**change: Any) -> AlertRule:
    spec = validate_rule({**RULE_BODY, **change})
    return AlertRule("rul_00000000000000aa", "app1", spec, 1, None, 0.0, 0.0)


def _ids() -> Callable[[str], str]:
    counter = itertools.count(1)
    return lambda prefix: f"{prefix}{next(counter):016x}"


def _series(rule: AlertRule, value: float | None, why_not: str | None = None) -> Series:
    return Series(rule, "org1", "", SignalValue(value, why_not))


def _apply(change: Change | None, before: Incident | None = None) -> Incident:
    assert change is not None
    if before is not None:
        assert (change.expected_state, change.expected_revision) == (before.state, before.revision)
    return change.incident


def _kinds(change: Change | None) -> list[tuple[str, str]]:
    assert change is not None
    return [(n.kind.value, n.state.value) for n in change.notifications]


# --- decide, one transition at a time -------------------------------------------------------------


def test_ok_to_pending_to_firing_after_for_and_straight_to_firing_without_it() -> None:
    rule, new_id = _rule(), _ids()
    assert decide(_series(rule, 1.0), None, None, T0, new_id) is None  # ok stays ok
    opened = decide(_series(rule, 9.0), None, None, T0, new_id)
    pending = _apply(opened)
    assert (pending.state, pending.pending_since_ms, _kinds(opened)) == ("pending", T0, [])
    assert decide(_series(rule, 9.0), pending, None, T0 + 4 * MINUTE_MS, new_id) is None
    fired = decide(_series(rule, 9.0), pending, None, T0 + 5 * MINUTE_MS, new_id)
    firing = _apply(fired, pending)
    assert (firing.state, firing.fired_at_ms, firing.value) == ("firing", T0 + 5 * MINUTE_MS, 9.0)
    assert _kinds(fired) == [("firing", "recorded")]
    now = _rule(for_minutes=0)
    at_once = decide(_series(now, 9.0), None, None, T0, new_id)
    assert (_apply(at_once).state, at_once.expected_state if at_once else None) == ("firing", None)
    assert _kinds(at_once) == [("firing", "recorded")]


@pytest.mark.parametrize("value", [(1.0, None), (None, "too_few"), (None, "incomplete")])
def test_pending_back_to_ok_when_it_stops_holding_or_cannot_be_judged(
    value: tuple[float | None, str | None],
) -> None:
    rule, new_id = _rule(), _ids()
    pending = _apply(decide(_series(rule, 9.0), None, None, T0, new_id))
    cleared = decide(_series(rule, *value), pending, None, T0 + MINUTE_MS, new_id)
    resolved = _apply(cleared, pending)
    assert (resolved.state, resolved.resolve_reason) == ("resolved", "pending_cleared")
    assert _kinds(cleared) == []  # nothing was sent, nothing to resolve


def _firing(rule: AlertRule, new_id: Callable[[str], str], at: float = T0) -> Incident:
    return _apply(decide(_series(rule, 9.0), None, None, at, new_id))


def test_firing_resolves_after_two_false_evaluations_and_says_so() -> None:
    rule, new_id = _rule(for_minutes=0), _ids()
    firing = _firing(rule, new_id)
    once = decide(_series(rule, 1.0), firing, None, T0 + 30_000, new_id)
    one_false = _apply(once, firing)
    assert (one_false.state, one_false.false_count, _kinds(once)) == ("firing", 1, [])
    # Holding again resets the count.
    back = _apply(decide(_series(rule, 9.0), one_false, None, T0 + 60_000, new_id), one_false)
    assert back.false_count == 0
    first = _apply(decide(_series(rule, 1.0), back, None, T0 + 90_000, new_id), back)
    twice = decide(_series(rule, 1.0), first, None, T0 + 120_000, new_id)
    resolved = _apply(twice, first)
    assert (resolved.state, resolved.resolve_reason) == ("resolved", "cleared")
    assert _kinds(twice) == [("resolved", "recorded")]


@pytest.mark.parametrize("state", ["firing", "acknowledged"])
def test_too_few_while_firing_holds_it_open_then_resolves_it_after_an_hour(state: str) -> None:
    rule, new_id = _rule(for_minutes=0), _ids()
    firing = dataclasses.replace(_firing(rule, new_id), state=IncidentState(state))
    marked = decide(_series(rule, None, "too_few"), firing, None, T0 + MINUTE_MS, new_id)
    held = _apply(marked, firing)
    assert (held.state, held.no_data_since_ms, _kinds(marked)) == (state, T0 + MINUTE_MS, [])
    later = T0 + MINUTE_MS + NO_DATA_MS - 1
    assert decide(_series(rule, None, "incomplete"), held, None, later, new_id) is None
    gone = decide(_series(rule, None, "too_few"), held, None, later + 1, new_id)
    resolved = _apply(gone, held)
    assert (resolved.state, resolved.resolve_reason) == ("resolved", "no_data")
    assert [(n.kind.value, n.reason) for n in gone.notifications] == [("resolved", "no_data")]  # type: ignore[union-attr]


def test_reminders_while_firing_and_not_once_acknowledged() -> None:
    rule, new_id = _rule(for_minutes=0, repeat_every_minutes=60), _ids()
    firing = _firing(rule, new_id)
    assert decide(_series(rule, 9.0), firing, None, T0 + 59 * MINUTE_MS, new_id) is None
    reminded = decide(_series(rule, 9.0), firing, None, T0 + 60 * MINUTE_MS, new_id)
    assert _kinds(reminded) == [("reminder", "recorded")]
    acknowledged = dataclasses.replace(firing, state=IncidentState.ACKNOWLEDGED)
    assert decide(_series(rule, 9.0), acknowledged, None, T0 + 120 * MINUTE_MS, new_id) is None


def test_flapping_inside_the_cooldown_reopens_sends_nothing_then_fires_again_once() -> None:
    """Fired (notified) at T0, resolved at +2 min, holding again at +3 min with a cooldown of
    30 min: the resolved incident is reopened (reopened 1), nothing recorded; when the cooldown
    ends, still firing, one firing-again; a reopened incident resolving inside the cooldown
    records nothing."""
    rule, new_id = _rule(for_minutes=0, cooldown_minutes=30), _ids()
    firing = _firing(rule, new_id)
    one = _apply(decide(_series(rule, 1.0), firing, None, T0 + MINUTE_MS, new_id), firing)
    resolved = _apply(decide(_series(rule, 1.0), one, None, T0 + 2 * MINUTE_MS, new_id), one)
    again = decide(_series(rule, 9.0), None, resolved, T0 + 3 * MINUTE_MS, new_id)
    reopened = _apply(again, resolved)
    assert (reopened.id, reopened.state, reopened.reopened) == (firing.id, "firing", 1)
    assert (reopened.firing_again_due, _kinds(again)) == (True, [])
    still = decide(_series(rule, 9.0), reopened, None, T0 + 29 * MINUTE_MS, new_id)
    assert still is None
    ends = decide(_series(rule, 9.0), reopened, None, T0 + 30 * MINUTE_MS, new_id)
    assert _kinds(ends) == [("firing_again", "recorded")]
    # Reopened, and resolving again inside the cooldown: nothing.
    first = _apply(decide(_series(rule, 1.0), reopened, None, T0 + 4 * MINUTE_MS, new_id), reopened)
    quiet = decide(_series(rule, 1.0), first, None, T0 + 5 * MINUTE_MS, new_id)
    assert (_apply(quiet, first).state, _kinds(quiet)) == ("resolved", [])
    # Outside the cooldown, a new incident and a firing notification.
    late = decide(_series(rule, 9.0), None, resolved, T0 + 31 * MINUTE_MS, new_id)
    assert _apply(late).id != firing.id
    assert _kinds(late) == [("firing", "recorded")]


def test_quiet_hours_hold_warnings_not_critical_and_end_in_one_summary() -> None:
    quiet = {"utc_offset": "+01:00", "start": "10:30", "end": "12:00", "days": ["mon"]}
    # T0 is Monday 10:00 UTC, 11:00 at +01:00: inside.
    assert in_quiet_hours(QuietHours("+01:00", "10:30", "12:00", ("mon",)), T0)
    assert not in_quiet_hours(QuietHours("+01:00", "10:30", "12:00", ("tue",)), T0)
    assert in_quiet_hours(QuietHours("+00:00", "22:00", "11:00", ("sun",)), T0)  # from Sunday
    assert not in_quiet_hours(QuietHours("-05:00", "10:30", "12:00", ("mon",)), T0)
    new_id = _ids()
    warning = _rule(for_minutes=0, severity="warning", quiet_hours=quiet)
    critical = _rule(for_minutes=0, severity="critical", quiet_hours=quiet)
    held = decide(_series(warning, 9.0), None, None, T0, new_id)
    assert _kinds(held) == [("firing", "held")]
    assert _kinds(decide(_series(critical, 9.0), None, None, T0, new_id)) == [
        ("firing", "recorded")
    ]
    incident = _apply(held)
    # Still quiet half an hour later.
    assert quiet_hours_over(incident, warning, 1, T0 + 30 * MINUTE_MS, new_id) is None
    over = quiet_hours_over(incident, warning, 1, T0 + 61 * MINUTE_MS, new_id)
    assert over is not None
    assert over.release_held
    assert _kinds(over) == [("quiet_hours_summary", "recorded")]
    assert quiet_hours_over(incident, warning, 0, T0 + 61 * MINUTE_MS, new_id) is None


@pytest.mark.parametrize(
    ("rule", "reason"),
    [
        (None, "rule_deleted"),
        (dataclasses.replace(_rule(), spec=dataclasses.replace(_rule().spec, enabled=False)),
         "rule_disabled"),
        (dataclasses.replace(_rule(), version=2), "rule_changed"),
    ],
)  # fmt: skip
def test_a_rule_deleted_disabled_or_changed_resolves_its_incident(
    rule: AlertRule | None, reason: str
) -> None:
    new_id = _ids()
    firing = _firing(_rule(for_minutes=0), new_id)
    gone = rule_gone(firing, rule, T0 + MINUTE_MS, new_id)
    done = _apply(gone, firing)
    assert (done.state, done.resolve_reason) == ("resolved", reason)
    assert [(n.kind.value, n.reason) for n in gone.notifications] == [("resolved", reason)]  # type: ignore[union-attr]
    assert rule_gone(firing, _rule(for_minutes=0), T0, new_id) is None


# --- the stores: one open a series, compare-and-set, the fence, atomic notifications -------------


class Epoch:
    def __init__(self, at: float = T0) -> None:
        self.at = at

    def epoch_ms(self) -> float:
        return self.at


def _sqlite(tmp_path: Path, epoch: Epoch) -> SqliteIncidentStore:
    connection = open_database(tmp_path / "gateway.db")
    MigrationRunner(connection, MIGRATIONS, SystemEpoch()).migrate()
    return SqliteIncidentStore(connection, epoch)


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> tuple[IncidentStore, Epoch]:
    epoch = Epoch()
    if request.param == "memory":
        return InMemoryIncidentStore(epoch), epoch
    return _sqlite(tmp_path, epoch), epoch


async def test_one_open_incident_a_series_and_each_change_made_once(
    store: tuple[IncidentStore, Epoch],
) -> None:
    """Two evaluators (or a retried cycle) making the same change: one is made, and its
    notification recorded once."""
    incidents, _ = store
    lease = await incidents.attempt_lease("a")
    fence = Fence("a", lease.epoch)
    rule, ids = _rule(for_minutes=0), _ids()
    first = decide(_series(rule, 9.0), None, None, T0, ids)
    second = decide(_series(rule, 9.0), None, None, T0, ids)  # another evaluator's, own ids
    assert first is not None
    assert second is not None
    assert await incidents.apply(fence, first)
    assert not await incidents.apply(fence, second)  # the index: one open a series
    [opened] = await incidents.open_incidents()
    assert opened.id == first.incident.id
    resolve = decide(_series(rule, 1.0), opened, None, T0 + 30_000, ids)
    assert resolve is not None
    assert await incidents.apply(fence, resolve)
    assert not await incidents.apply(fence, resolve)  # retried: the compare-and-set
    assert [n.kind for n in await incidents.notifications(opened.id)] == [NotificationKind.FIRING]


async def test_a_fenced_evaluator_writes_nothing(store: tuple[IncidentStore, Epoch]) -> None:
    """a holds the lease; it pauses past it; b takes it (a new epoch). a's changes and its
    heartbeat are refused; b's are made."""
    incidents, epoch = store
    a = await incidents.attempt_lease("a")
    assert a.held_by("a")
    assert not (await incidents.attempt_lease("b")).held_by("b")  # held: b waits
    epoch.at += LEASE_MS + 1
    b = await incidents.attempt_lease("b")
    assert (b.held_by("b"), b.epoch) == (True, a.epoch + 1)
    change = decide(_series(_rule(for_minutes=0), 9.0), None, None, epoch.at, _ids())
    assert change is not None
    assert not await incidents.apply(Fence("a", a.epoch), change)
    assert not await incidents.complete_cycle(Fence("a", a.epoch))
    assert await incidents.open_incidents() == []
    assert await incidents.apply(Fence("b", b.epoch), change)
    assert await incidents.complete_cycle(Fence("b", b.epoch))
    renewed = await incidents.attempt_lease("b")
    assert (renewed.epoch, renewed.last_cycle_completed_at_ms, renewed.last_cycle_epoch) == (
        b.epoch,
        epoch.at,
        b.epoch,
    )


async def test_a_change_whose_notification_fails_is_rolled_back_with_it(tmp_path: Path) -> None:
    """The transition and its notification in one transaction: a notification that cannot be
    recorded (its id already taken) takes the transition back with it."""
    incidents = _sqlite(tmp_path, Epoch())
    fence = Fence("a", (await incidents.attempt_lease("a")).epoch)
    first = decide(_series(_rule(for_minutes=0), 9.0), None, None, T0, _ids())
    assert first is not None
    assert await incidents.apply(fence, first)
    [opened] = await incidents.open_incidents()
    resolved = dataclasses.replace(
        opened, state=IncidentState.RESOLVED, resolve_reason=ResolveReason.CLEARED,
        revision=opened.revision + 1,
    )  # fmt: skip
    clash = Change(resolved, opened.state, opened.revision, (first.notifications[0],))
    with pytest.raises(sqlite3.IntegrityError):
        await incidents.apply(fence, clash)
    assert await incidents.open_incidents() == [opened]  # the transition rolled back too
    assert len(await incidents.notifications(opened.id)) == 1


# --- the evaluator, end to end, in memory ---------------------------------------------------------


class Durable:
    """Durable signals the test sets."""

    def __init__(self) -> None:
        self.budget = SignalValue(10.0)
        self.breakers: dict[str, SignalValue] = {}
        self.keys: dict[str, SignalValue] = {}

    async def budget_used(self, application_id: str) -> SignalValue:
        return self.budget

    async def circuit_open(
        self, organization_id: str, application_id: str
    ) -> dict[str, SignalValue]:
        return dict(self.breakers)

    async def key_expires_in(self, application_id: str) -> dict[str, SignalValue]:
        return dict(self.keys)

    async def reservation_expired(self, application_id: str) -> SignalValue:
        return SignalValue(None, "unavailable")


class World:
    def __init__(self, store: IncidentStore | None = None) -> None:
        self.clock = SimClock()
        self.epoch = ClockEpoch(self.clock, T0)
        self.incidents = store if store is not None else InMemoryIncidentStore(self.epoch)
        self.rules = InMemoryRuleStore()
        self.rollups = InMemoryAlertRollups()
        self.durable = Durable()
        self.seq = itertools.count(1)
        self.moves: list[tuple[str, str, str]] = []

    def evaluator(self, holder: str) -> AlertEvaluator:
        def moved(was: HeartbeatState, now: HeartbeatState) -> None:
            self.moves.append((holder, was.value, now.value))

        async def organization_of(application_id: str) -> str | None:
            return "org1"

        return AlertEvaluator(
            incidents=self.incidents,
            rules=self.rules,
            rollups=self.rollups,
            organization_of=organization_of,
            durable=self.durable,
            clock=self.clock,
            holder=holder,
            random_bytes=lambda n: next(self.seq).to_bytes(n, "big"),
            on_heartbeat_change=moved,
        )

    def errors(self, minutes: range, failed: int, requests: int = 40) -> None:
        for minute in minutes:
            self.rollups.absorb({("app1", minute): {"requests": requests, "status:5xx": failed}})

    def minute(self) -> int:
        return int(self.epoch.epoch_ms() // MINUTE_MS)


async def _rule_in(world: World, **change: Any) -> AlertRule:
    rule = _rule(**change)
    await world.rules.insert_rule(rule)
    return rule


async def test_two_evaluators_one_store_one_incident_and_one_notification() -> None:
    world = World()
    await _rule_in(world, for_minutes=0)
    world.errors(range(world.minute() - 120, world.minute() + 10), failed=10)
    a, b = world.evaluator("a"), world.evaluator("b")
    assert await a.step()
    assert not await b.step()  # b does not hold the lease
    # Both run a cycle at the same time with their own fences: only the holder's is made.
    lease = await world.incidents.attempt_lease("a")
    await b.cycle(Fence("b", lease.epoch), lease.now_ms)
    await a.cycle(Fence("a", lease.epoch), lease.now_ms)
    [incident] = await world.incidents.open_incidents()
    notes = await world.incidents.notifications(incident.id)
    assert (incident.state, [n.kind for n in notes]) == ("firing", [NotificationKind.FIRING])


async def test_the_holder_killed_another_takes_over_and_for_continues() -> None:
    """a opens a pending incident (for 5 min) and dies; b, attempting every 10 s, takes the
    lease 30 s later at a new epoch, and the pending incident fires on time under b."""
    world = World()
    await _rule_in(world, for_minutes=5)
    world.errors(range(world.minute() - 120, world.minute() + 30), failed=10)
    a, b = world.evaluator("a"), world.evaluator("b")
    assert await a.step()
    [pending] = await world.incidents.open_incidents()
    assert pending.state == "pending"
    epochs = []
    for _ in range(40):  # a is dead: only b attempts
        await world.clock.sleep_ms(10_000.0)
        await b.step()
        epochs.append((await world.incidents.attempt_lease("b")).epoch)
        [current] = await world.incidents.open_incidents()
        if current.state == "firing":
            break
    assert current.id == pending.id  # the same incident, not a new one
    assert current.fired_at_ms is not None
    assert pending.pending_since_ms is not None
    assert current.fired_at_ms - pending.pending_since_ms < 5 * MINUTE_MS + CYCLE_MS + LEASE_MS
    assert (epochs[0], epochs[-1]) == (1, 2)  # a's until it expired, then b's, a new epoch


async def test_durable_signals_are_judged_with_valkey_down_and_rollup_ones_are_not() -> None:
    world = World()
    budget = _rule()
    budget = dataclasses.replace(
        budget, id="rul_00000000000000bb",
        spec=validate_rule({**RULE_BODY, "signal": "budget_used", "window": None,
                            "threshold": 80, "comparison": ">=", "for_minutes": 0}),
    )  # fmt: skip
    await world.rules.insert_rule(budget)
    errors = await _rule_in(world, for_minutes=0)
    world.durable.budget = SignalValue(95.0)

    class Down(InMemoryAlertRollups):
        async def read(self, application_id: str, minutes: Any) -> Any:
            raise StateUnavailableError("timeout")

    world.rollups = Down()
    assert await world.evaluator("a").step()
    [incident] = await world.incidents.open_incidents()
    assert (incident.rule_id, incident.state) == (budget.id, "firing")
    assert errors.id != incident.rule_id


async def test_a_series_per_provider_and_per_key() -> None:
    world = World()
    circuit = dataclasses.replace(
        _rule(), id="rul_00000000000000cc",
        spec=validate_rule({**RULE_BODY, "signal": "circuit_open", "window": None,
                            "threshold": 2, "comparison": ">=", "for_minutes": 0}),
    )  # fmt: skip
    await world.rules.insert_rule(circuit)
    world.durable.breakers = {
        "prv_00000000000000a1": SignalValue(1.0),
        "prv_00000000000000b2": SignalValue(0.0),
    }
    a = world.evaluator("a")
    await a.step()
    assert await world.incidents.open_incidents() == []  # open, but not for 2 minutes yet
    for _ in range(14):
        await world.clock.sleep_ms(10_000.0)
        await a.step()
    [incident] = await world.incidents.open_incidents()
    assert (incident.series_key, incident.state) == ("prv_00000000000000a1", "firing")


# --- the heartbeat --------------------------------------------------------------------------------


async def _run(world: World, *evaluators: AlertEvaluator, seconds: float) -> None:
    for _ in range(int(seconds // 10)):
        await world.clock.sleep_ms(10_000.0)
        for e in evaluators:
            await e.step()


async def test_ok_on_every_instance_then_stale_when_the_evaluator_dies_with_no_taker() -> None:
    world = World()
    await _rule_in(world)
    a, b, c = (world.evaluator(n) for n in "abc")
    await _run(world, a, b, c, seconds=60)
    assert [e.status().state for e in (a, b, c)] == ["ok"] * 3
    assert [e.status().holder for e in (a, b, c)] == [True, False, False]
    # a dies holding the lease, and nobody takes it over (every attempt reads the row as a
    # left it, the store's time moving on): no cycle completes.
    lease = await world.incidents.attempt_lease("a")

    async def frozen(holder: str) -> Any:
        return dataclasses.replace(lease, now_ms=world.epoch.epoch_ms())

    world.incidents.attempt_lease = frozen  # type: ignore[method-assign]
    # a's last cycle completed at 40 s; it is now 60 s.
    await _run(world, b, c, seconds=40)
    assert [e.status().state for e in (b, c)] == ["ok", "ok"]  # 60 s old
    await _run(world, b, c, seconds=40)
    assert [e.status().state for e in (b, c)] == ["stale", "stale"]  # 100 s old
    assert ("b", "ok", "stale") in world.moves


async def test_stale_when_every_cycle_fails_and_when_the_holder_is_fenced() -> None:
    world = World()
    await _rule_in(world)
    a = world.evaluator("a")

    async def failing(fence: Fence, now_ms: float) -> None:
        raise StateUnavailableError("timeout")

    a.cycle = failing  # type: ignore[method-assign]
    await _run(world, a, seconds=130)
    assert a.status().state == "stale"  # no cycle ever completed, 120 s after the start
    assert a.status().failures >= 4
    # Fenced: b holds the lease at a new epoch but its cycles never complete either.
    world2 = World()
    await _rule_in(world2)
    first, second = world2.evaluator("a"), world2.evaluator("b")
    await _run(world2, first, second, seconds=30)
    assert first.status().state == "ok"
    lease = await world2.incidents.attempt_lease("a")
    assert not await world2.incidents.complete_cycle(Fence("a", lease.epoch + 7))  # wrong epoch
    real = world2.incidents.complete_cycle

    async def fenced(fence: Fence) -> bool:
        return await real(Fence(fence.holder, fence.epoch + 1))  # every renewal fenced out

    world2.incidents.complete_cycle = fenced  # type: ignore[method-assign]
    await _run(world2, first, second, seconds=100)
    assert first.status().state == "stale"


async def test_unknown_when_the_store_cannot_be_read_and_never_ok_meanwhile() -> None:
    world = World()
    await _rule_in(world)
    a = world.evaluator("a")
    await _run(world, a, seconds=30)
    assert a.status().state == "ok"

    async def down(holder: str) -> Any:
        raise StateUnavailableError("timeout")

    world.incidents.attempt_lease = down  # type: ignore[method-assign]
    await _run(world, a, seconds=20)
    assert a.status().state == "ok"  # the last read is under 30 s old
    await _run(world, a, seconds=20)
    status = a.status()
    assert (status.state, status.last_cycle_age_ms) == ("unknown", None)
    assert ("a", "ok", "unknown") in world.moves


# --- /healthz, metrics, the snapshot, and no store call from them ---------------------------------


@asynccontextmanager
async def served(world: World | None) -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    from gateway.app import create_app
    from helpers import application_access

    clock = world.clock if world is not None else SimClock()
    access, _ = application_access(clock, apps={"app1": "pro"})
    registry = build_continuous_registry(clock, seed=7)
    app = create_app(
        registry,
        clock,
        applications=access,
        rules=world.rules if world is not None else None,
        incidents=world.incidents if world is not None else None,
        epoch=world.epoch if world is not None else None,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        yield client, app
    await registry.aclose()


async def test_healthz_says_one_word_readyz_is_unaffected_and_no_store_is_called() -> None:
    world = World()
    calls: list[str] = []
    for name in ("attempt_lease", "complete_cycle", "apply", "open_incidents"):
        real = getattr(world.incidents, name)

        def counted(*args: Any, _real: Any = real, _name: str = name, **kwargs: Any) -> Any:
            calls.append(_name)
            return _real(*args, **kwargs)

        setattr(world.incidents, name, counted)
    async with served(world) as (client, app):
        evaluator = app.state.alert_evaluator
        await evaluator.step()
        calls.clear()
        health = (await client.get("/healthz")).json()
        snapshot = (await client.get("/v1/monitor/snapshot")).json()
        metrics = (await client.get("/metrics")).text
        ready = await client.get("/readyz")
        assert calls == []  # from memory
        # The store goes down: unknown after 30 s; /readyz still ready.

        async def down(holder: str) -> Any:
            raise StateUnavailableError("timeout")

        world.incidents.attempt_lease = down  # type: ignore[method-assign]
        for _ in range(4):
            await world.clock.sleep_ms(10_000.0)
            await evaluator.step()
        unknown = (await client.get("/healthz")).json()
        unknown_snapshot = (await client.get("/v1/monitor/snapshot")).json()["alert_evaluator"]
        unknown_metrics = (await client.get("/metrics")).text
        still_ready = await client.get("/readyz")
        events = [e["type"] for e in (await client.get("/v1/monitor/snapshot?events=50")).json()
                  ["events"]["recent"]]  # fmt: skip
    assert set(health) == {"status", "models", "uptime_ms", "alert_evaluator"}
    assert health["alert_evaluator"] == "ok"
    assert snapshot["alert_evaluator"]["available"] is True
    assert snapshot["alert_evaluator"]["holder"] is True
    assert 'gateway_alert_evaluator_state{state="ok"} 1' in metrics
    assert "gateway_alert_evaluator_last_cycle_age_seconds" in metrics
    assert "gateway_alert_evaluator_holder 1" in metrics
    assert ready.status_code == still_ready.status_code == 200
    assert unknown["alert_evaluator"] == "unknown"
    assert unknown_snapshot["available"] is False
    assert unknown_snapshot["last_cycle_age_ms"] is None  # absent, never 0
    assert "gateway_alert_evaluator_last_cycle_age_seconds" not in unknown_metrics
    assert 'gateway_alert_evaluator_state{state="unknown"} 1' in unknown_metrics
    assert "alert_evaluator_stale" in events


async def test_disabled_without_an_evaluator() -> None:
    async with served(None) as (client, app):
        health = (await client.get("/healthz")).json()
        snapshot = (await client.get("/v1/monitor/snapshot")).json()["alert_evaluator"]
    assert app.state.alert_evaluator is None
    assert health["alert_evaluator"] == "disabled"
    assert (snapshot["state"], snapshot["available"]) == ("disabled", True)


# --- the migration --------------------------------------------------------------------------------


def test_migration_16_adds_the_lease_and_the_incident_tables(tmp_path: Path) -> None:
    connection = open_database(tmp_path / "gateway.db")
    runner = MigrationRunner(connection, MIGRATIONS, SystemEpoch())
    runner.migrate(15)
    assert runner.migrate() == (16,)
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"alert_lease", "incidents", "incident_notifications"} <= tables
    # The lease's row is made by the first attempt to take it.
    assert connection.execute("SELECT count(*) FROM alert_lease").fetchone() == (0,)
    assert runner.rollback(15) == (16,)
    connection.close()


async def test_the_default_rules_are_judged_too(tmp_path: Path) -> None:
    """A default rule set (as A2 stores it) evaluates without error, the budget rule firing."""
    world = World()
    for rule in default_rules("app1", T0):
        await world.rules.insert_rule(rule)
    world.durable.budget = SignalValue(85.0)
    world.durable.keys = {"key_0000000000000001": SignalValue(30.0)}
    assert await world.evaluator("a").step()
    found = {(i.rule_id, i.state) for i in await world.incidents.open_incidents()}
    assert len(found) == 1
    assert next(iter(found))[1] == "firing"


# --- three instances on one Postgres ------------------------------------------------------------


@pytest.mark.postgres
async def test_three_instances_one_lease_the_holder_killed_for_continuing_and_one_heartbeat() -> (
    None
):
    """Three instances' evaluators, each on its own pool of one database, timed on the
    database's clock: one holds the lease; every one reports the same heartbeat from its own
    read; the holder is killed (it stops attempting), another takes the lease at a new epoch
    within its 30 s, and the pending incident fires on time under it; the killed one, waking,
    is fenced: its change and its heartbeat write nothing."""
    from gateway.postgres import PostgresIncidentStore, PostgresRuleStore
    from postgres_helpers import pg_world

    async with pg_world(instances=3) as world:
        now = T0
        await world.set_time(now)
        clock = SimClock()
        rules = PostgresRuleStore(world.instances[0])
        await rules.insert_rule(_rule(for_minutes=5))
        rollups = InMemoryAlertRollups()
        minute = int(now // MINUTE_MS)
        rollups.absorb(
            {
                ("app1", m): {"requests": 40, "status:5xx": 10}
                for m in range(minute - 10, minute + 30)
            }
        )

        async def organization_of(application_id: str) -> str | None:
            return "org1"

        seq = itertools.count(1)
        evaluators = [
            AlertEvaluator(
                incidents=PostgresIncidentStore(db),
                rules=PostgresRuleStore(db),
                rollups=rollups,
                organization_of=organization_of,
                durable=Durable(),
                clock=clock,
                holder=f"instance-{n}",
                random_bytes=lambda k: next(seq).to_bytes(k, "big"),
            )
            for n, db in enumerate(world.instances)
        ]

        async def tick(alive: list[AlertEvaluator]) -> None:
            nonlocal now
            now += 10_000.0
            await world.set_time(now)
            await clock.sleep_ms(10_000.0)
            for e in alive:
                await e.step()

        for e in evaluators:
            await e.step()
        holders = [e.status().holder for e in evaluators]
        assert holders.count(True) == 1
        killed = evaluators[holders.index(True)]
        alive = [e for e in evaluators if e is not killed]
        await tick(evaluators)
        assert [e.status().state for e in evaluators] == ["ok"] * 3
        [pending] = await alive[0].incidents.open_incidents()
        assert pending.state == "pending"
        killed_epoch = (await alive[0].incidents.attempt_lease("observer")).epoch
        for _ in range(40):
            await tick(alive)
            [current] = await alive[0].incidents.open_incidents()
            if current.state == "firing":
                break
        assert current.id == pending.id
        assert any(e.status().holder for e in alive)
        epoch = (await alive[0].incidents.attempt_lease("observer")).epoch
        assert epoch == killed_epoch + 1
        assert [e.status().state for e in alive] == ["ok", "ok"]
        # The killed instance wakes with its old epoch: fenced.
        late = decide(_series(_rule(for_minutes=0), 1.0), current, None, now, _ids())
        assert late is not None
        assert not await killed.incidents.apply(Fence(killed.holder, killed_epoch), late)
        assert not await killed.incidents.complete_cycle(Fence(killed.holder, killed_epoch))
        [still] = await alive[0].incidents.open_incidents()
        assert still == current


@pytest.mark.postgres
async def test_two_instances_each_opening_its_own_incident_for_one_series_store_one() -> None:
    """The partial unique index itself, on Postgres: two instances' stores, each writing its own
    new incident (its own id) for the same rule and series under the lease's fence (the holder's
    cycle retried through another instance's pool), concurrently. The fence lets both through,
    the primary key cannot tell them apart (their ids differ): only the index can refuse one,
    and exactly one is stored, with its one notification."""
    from gateway.postgres import PostgresIncidentStore
    from postgres_helpers import pg_world

    async with pg_world(instances=2) as world:
        await world.set_time(T0)
        first, second = (PostgresIncidentStore(db) for db in world.instances)
        lease = await first.attempt_lease("holder")
        fence = Fence("holder", lease.epoch)
        rule = _rule(for_minutes=0)
        mine = decide(_series(rule, 9.0), None, None, T0, lambda p: p + "a" * 16)
        theirs = decide(_series(rule, 9.0), None, None, T0, lambda p: p + "b" * 16)
        assert mine is not None
        assert theirs is not None
        assert mine.incident.id != theirs.incident.id
        made = await asyncio.gather(first.apply(fence, mine), second.apply(fence, theirs))
        rows = await world.rows("SELECT id, rule_id, series_key, state FROM incidents")
        notes = await world.rows("SELECT incident_id FROM incident_notifications")
    assert sorted(made) == [False, True]
    [(incident_id, rule_id, series_key, state)] = rows
    assert (rule_id, series_key, state) == (rule.id, "", "firing")
    assert notes == [(incident_id,)]


@pytest.mark.postgres
async def test_the_postgres_constraints_refuse_a_state_a_comparison_or_a_severity_not_known() -> (
    None
):
    """The CHECK constraints behind the code's own checks, reached directly."""
    import asyncpg

    from postgres_helpers import pg_world

    async with pg_world() as world:
        incident = decide(_series(_rule(for_minutes=0), 9.0), None, None, T0, _ids())
        assert incident is not None
        from gateway.incidents import INCIDENT_COLUMNS, incident_row

        row = list(incident_row(incident.incident))
        row[INCIDENT_COLUMNS.index("state")] = "smouldering"
        placeholders = ", ".join(f"${n}" for n in range(1, len(row) + 1))
        with pytest.raises(asyncpg.CheckViolationError):
            await world.admin.execute(
                f"INSERT INTO incidents ({', '.join(INCIDENT_COLUMNS)}) VALUES ({placeholders})",
                *row,
            )
        from gateway.alertrules import COLUMNS, default_rules, rule_row

        for column, value in (("comparison", "=="), ("severity", "page")):
            values = list(rule_row(default_rules("app1", T0)[0]))
            values[[c.strip() for c in COLUMNS.split(",")].index(column)] = value
            with pytest.raises(asyncpg.CheckViolationError):
                await world.admin.execute(
                    f"INSERT INTO alert_rules ({COLUMNS}) VALUES"
                    f" ({', '.join(f'${n}' for n in range(1, 20))})",
                    *values,
                )


@pytest.mark.postgres
async def test_two_instances_firing_one_incident_from_the_same_read_make_one_change() -> None:
    """The compare-and-set itself, on Postgres: two instances decide the same pending incident's
    move to firing from the same read, each with its own notification id (so no primary key can
    tell the second from the first), and apply at once under the fence. Only the state and
    revision compared can refuse one: one change, one firing notification."""
    from gateway.postgres import PostgresIncidentStore
    from postgres_helpers import pg_world

    async with pg_world(instances=2) as world:
        await world.set_time(T0)
        first, second = (PostgresIncidentStore(db) for db in world.instances)
        fence = Fence("holder", (await first.attempt_lease("holder")).epoch)
        rule = _rule(for_minutes=5)
        opened = decide(_series(rule, 9.0), None, None, T0, lambda p: p + "c" * 16)
        assert opened is not None
        assert await first.apply(fence, opened)
        [pending] = await second.open_incidents()
        later = T0 + 5 * MINUTE_MS
        await world.set_time(later)
        await first.attempt_lease("holder")  # renewed: the fence holds at the later time
        mine = decide(_series(rule, 9.0), pending, None, later, lambda p: p + "d" * 16)
        theirs = decide(_series(rule, 9.0), pending, None, later, lambda p: p + "e" * 16)
        assert mine is not None
        assert theirs is not None
        made = await asyncio.gather(first.apply(fence, mine), second.apply(fence, theirs))
        notes = await world.rows("SELECT kind FROM incident_notifications")
        [state] = await world.rows("SELECT state, revision FROM incidents")
    assert sorted(made) == [False, True]
    assert notes == [("firing",)]
    assert state == ("firing", pending.revision + 1)
