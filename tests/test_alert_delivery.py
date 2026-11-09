"""Delivery (docs/46, the A4 plan's sections 4.3, 5, 7 and 8.1), below HTTP, on SQLite and memory
(the Postgres cases are in ``test_alert_delivery_postgres.py``): the outbox filled in the
transition's transaction; claims that expire and a stale worker's outcome refused; the retry
schedule on the injected clock, six tries, and no retry of a permanent failure; each attempt
signed afresh, with both keys during a rotation; a fresh resolve for every delivery; an
incident's first channels, fixed once; held notifications and their summary; the caps, the
suppressed summary and the cap variables; the instances' ceilings compared; the pool guard; and
the built-in ``channel_delivery_failing``.

Every attempt goes through ``webhook_client`` onto an injected resolver and network. Test values,
not secrets."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from delivery_helpers import (
    OK,
    OTHER_URL,
    T0,
    THIRD_URL,
    World,
    key_of,
    sqlite_world,
)
from gateway.alerts.caps import (
    CAP_VARIABLES,
    LIVE_MS,
    AlertCaps,
    CeilingsWatch,
    InMemoryCeilingsStore,
    SqliteCeilingsStore,
    caps_from,
    ceilings_text,
)
from gateway.alerts.channels import PREVIOUS_KEY_MS
from gateway.alerts.delivery import BACKOFF_MS, CLAIM_MS, MAX_ATTEMPTS
from gateway.alerts.notices import HOUR_MS, NOTIFICATION_ID_PREFIX
from gateway.alerts.outbox import DeliveryState, Outcome, TransactionHeldError
from gateway.alerts.payload import AlertPayload, PayloadRule
from gateway.clock import ClockEpoch, SimClock
from gateway.database import open_database
from gateway.incidents import (
    Change,
    Fence,
    Incident,
    IncidentState,
    InMemoryIncidentStore,
    SqliteIncidentStore,
)
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.vault import Vault
from mock_network import http_response

KEY_RE = r"[0-9a-f]{64}"
REPO = Path(__file__).resolve().parent.parent


def _keep_alive(body: bytes = b"{}") -> bytes:
    """A 200 that leaves the connection open: a pooled client would send the next on it."""
    return b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body)


def _requests(world: World) -> list[tuple[dict[str, str], bytes]]:
    found = []
    for connection in world.network.connections:
        head, _, body = bytes(connection.written).partition(b"\r\n\r\n")
        headers = {}
        for line in head.decode("ascii").split("\r\n")[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        found.append((headers, body))
    return found


def _verifies(key_hex: str, headers: dict[str, str], body: bytes) -> bool:
    expected = hmac.new(
        bytes.fromhex(key_hex),
        headers["x-gateway-timestamp"].encode() + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    return any(
        hmac.compare_digest(expected, v.split("=", 1)[1])
        for v in headers["x-gateway-signature"].split(",")
    )


async def _fire(world: World, channels: list[str], **rule: Any) -> Any:
    """A rule over the budget, evaluated once by holder a: its incident fires."""
    made = await world.rule(channels, **rule)
    world.signals.budget["north-shop"] = 95.0
    assert await world.evaluator("a").step()
    return made


# --- queued in the transition's transaction ------------------------------------------------------


async def test_a_firing_incident_queues_one_delivery_per_channel_and_fixes_its_channels() -> None:
    world = await sqlite_world(responses=[OK, OK])
    a = await world.channel("A")
    b = await world.channel("B", OTHER_URL)
    await world.channel("C", THIRD_URL)  # a default the rule does not name
    await _fire(world, [a, b])
    [incident] = await world.state.incidents.open_incidents()
    assert incident.notify_channels == (a, b)
    assert sorted(await world.deliveries()) == sorted(
        [(a, "queued", None, 0), (b, "queued", None, 0)]
    )
    assert await world.deliver() == 2
    assert sorted(await world.deliveries()) == sorted(
        [(a, "delivered", "delivered", 1), (b, "delivered", "delivered", 1)]
    )
    payloads = [json.loads(body) for _, body in _requests(world)]
    assert {p["kind"] for p in payloads} == {"firing"}
    assert {p["delivery_id"] for p in payloads} == {
        n.id for n in await world.state.incidents.notifications(incident.id)
    }


async def test_a_rolled_back_transition_queues_nothing_and_changes_nothing() -> None:
    """M22: the deliveries are queued inside the change's transaction; if queueing fails, the
    incident is not changed either."""
    world = await sqlite_world()
    a = await world.channel("A")
    await world.rule([a])
    world.signals.budget["north-shop"] = 95.0

    async def broken(deliveries: Any) -> int:
        raise RuntimeError("the outbox is down")

    world.state.outbox.queue = broken  # type: ignore[method-assign]
    assert not await world.evaluator("a").step()  # the cycle failed
    assert await world.state.incidents.open_incidents() == []
    assert world.rows("SELECT count(*) FROM incident_notifications") == [(0,)]
    assert await world.deliveries() == []


async def test_a_transition_made_twice_queues_once() -> None:
    world = await sqlite_world()
    a = await world.channel("A")
    rule = await world.rule([a])
    world.signals.budget["north-shop"] = 95.0
    evaluator = world.evaluator("a")
    view = await world.state.incidents.attempt_lease("a")
    fence = Fence("a", view.epoch)
    from gateway.evaluator import Series, decide
    from gateway.signals import SignalValue

    change = decide(
        Series(rule, "north", "", SignalValue(95.0)),
        None,
        None,
        view.now_ms,
        lambda p: p + "0" * 15 + "1",
    )
    assert change is not None
    prepared, then = await world.notices.prepare(change, rule, view.now_ms)
    assert await world.state.incidents.apply(fence, prepared, then)
    assert not await world.state.incidents.apply(fence, prepared, then)
    assert len(await world.deliveries()) == 1
    assert evaluator.holder == "a"


async def test_two_evaluators_give_one_delivery() -> None:
    world = await sqlite_world(responses=[OK])
    a = await world.channel("A")
    await world.rule([a])
    world.signals.budget["north-shop"] = 95.0
    first, second = world.evaluator("a"), world.evaluator("b")
    assert await first.step()
    assert not await second.step()  # a holds the lease
    assert len(await world.deliveries()) == 1


# --- attempts, retries, health -------------------------------------------------------------------


async def test_a_delivered_notification_resets_the_channels_health() -> None:
    world = await sqlite_world(responses=[OK])
    a = await world.channel("A")
    await _fire(world, [a])
    await world.deliver()
    channel = await world.state.channels.get_channel(a)
    assert channel is not None
    assert (channel.consecutive_failures, channel.last_delivered_at_ms) == (
        0,
        world.epoch.epoch_ms(),
    )
    assert [(r.channel_id, r.code, r.final) for r in world.reports] == [(a, "delivered", True)]


async def test_the_retry_schedule_six_tries_and_no_seventh() -> None:
    """M33's other half: a 5xx is retried, at 30 s, 2 min, 8 min, 30 min and 2 h (jitter up to
    10%), six tries in all; the channel's health counts each failed attempt."""
    world = await sqlite_world(
        responses=[http_response(b"", "503 Service Unavailable", Content_Length="0")] * 7
    )
    a = await world.channel("A")
    await _fire(world, [a])
    worker = world.worker()
    for attempt in range(1, MAX_ATTEMPTS + 1):
        assert await world.deliver(worker) == 1
        [delivery] = await world.state.outbox.channel_deliveries(a, 10)
        assert delivery.attempts == attempt
        if attempt < MAX_ATTEMPTS:
            assert (delivery.state, delivery.last_code) == (DeliveryState.QUEUED, "http_5xx")
            assert delivery.next_attempt_at_ms is not None
            wait = delivery.next_attempt_at_ms - world.epoch.epoch_ms()
            assert BACKOFF_MS[attempt - 1] <= wait <= BACKOFF_MS[attempt - 1] * 1.1
            assert await world.deliver(worker) == 0  # not due yet
            await world.clock.sleep_ms(wait)
        else:
            assert (delivery.state, delivery.last_code) == (DeliveryState.FAILED, "http_5xx")
    await world.clock.sleep_ms(3 * 3600_000.0)
    assert await world.deliver(worker) == 0  # no seventh try
    assert len(world.network.connections) == MAX_ATTEMPTS
    channel = await world.state.channels.get_channel(a)
    assert channel is not None
    assert channel.consecutive_failures == MAX_ATTEMPTS
    assert [r.final for r in world.reports] == [False] * 5 + [True]


@pytest.mark.parametrize(
    ("scripted", "code"),
    [
        (http_response(b"", "400 Bad Request", Content_Length="0"), "http_4xx"),
        (http_response(b"", "404 Not Found", Content_Length="0"), "http_4xx"),
        (http_response(b"", "301 Moved Permanently", Location="https://10.0.0.1/"), "http_3xx"),
    ],
)
async def test_a_permanent_failure_is_not_retried(scripted: bytes, code: str) -> None:
    """M33: any other 4xx, a 3xx: failed after one try."""
    world = await sqlite_world(responses=[scripted, OK])
    a = await world.channel("A")
    await _fire(world, [a])
    await world.deliver()
    assert await world.deliveries() == [(a, "failed", code, 1)]
    await world.clock.sleep_ms(3 * 3600_000.0)
    assert await world.deliver() == 0
    assert len(world.network.connections) == 1


async def test_retry_after_is_the_wait_when_given() -> None:
    world = await sqlite_world(
        responses=[
            http_response(b"", "429 Too Many Requests", Retry_After="90", Content_Length="0"),
            OK,
        ]
    )
    a = await world.channel("A")
    await _fire(world, [a])
    await world.deliver()
    [delivery] = await world.state.outbox.channel_deliveries(a, 10)
    assert delivery.next_attempt_at_ms == world.epoch.epoch_ms() + 90_000.0


async def test_each_attempt_is_signed_afresh_with_its_own_timestamp() -> None:
    """A4-14: a retry carries a new timestamp and a valid signature; the delivery id stays."""
    world = await sqlite_world(responses=[http_response(b"", "500 Oops", Content_Length="0"), OK])
    made = await world.service.create(
        "north",
        name="A",
        url=world_url(),
        is_default=True,
        channel_type="webhook",
        actor=_actor(),
        marker=_Mark(),
    )
    await _fire(world, [made.channel.id])
    worker = world.worker()  # one instance's worker makes both attempts
    await world.deliver(worker)
    await world.clock.sleep_ms(BACKOFF_MS[0] * 1.1)
    await world.deliver(worker)
    (first, body1), (second, body2) = _requests(world)
    assert body1 == body2
    assert first["x-gateway-delivery"] == second["x-gateway-delivery"]
    assert int(second["x-gateway-timestamp"]) - int(first["x-gateway-timestamp"]) >= 30
    assert first["x-gateway-signature"] != second["x-gateway-signature"]
    assert _verifies(key_of(made), first, body1)
    assert _verifies(key_of(made), second, body2)


async def test_every_delivery_resolves_and_checks_again() -> None:
    """M45: a new client for every attempt, so no connection is kept: two deliveries to one
    host by one worker, each answered on a connection left open, resolve and connect twice."""
    world = await sqlite_world(responses=[_keep_alive(), _keep_alive()])
    a = await world.channel("A")
    await _fire(world, [a])
    worker = world.worker()
    await world.deliver(worker)
    rule = await world.state.rules.get_rule("north-shop", "rul_00000000000000a1")
    assert rule is not None
    await world.notices.rule_weakened("north-shop", rule, ["threshold"], deleted=False)
    await world.deliver(worker)
    assert world.resolver.calls.count("hooks.example.com") == 2
    assert len(world.network.connections) == 2


async def test_both_keys_sign_during_a_rotation_and_one_after() -> None:
    """A4-3: the previous key signs for 24 hours (store clock), then not."""
    world = await sqlite_world(responses=[OK, OK])
    made = await world.service.create(
        "north",
        name="A",
        url=world_url(),
        is_default=True,
        channel_type="webhook",
        actor=_actor(),
        marker=_Mark(),
    )
    first_key = key_of(made)
    new_key = await world.service.rotate_key(made.channel.id, actor=_actor(), marker=_Mark())
    rule = await _fire(world, [made.channel.id])
    await world.deliver()
    headers, body = _requests(world)[0]
    assert headers["x-gateway-signature"].count("v1=") == 2
    assert _verifies(new_key, headers, body)
    assert _verifies(first_key, headers, body)
    await world.clock.sleep_ms(PREVIOUS_KEY_MS)
    await world.notices.rule_weakened("north-shop", rule, ["threshold"], deleted=False)
    await world.deliver()
    headers, body = _requests(world)[1]
    assert headers["x-gateway-signature"].count("v1=") == 1
    assert _verifies(new_key, headers, body)
    assert not _verifies(first_key, headers, body)
    # And the next cycle drops the expired row.
    await world.evaluator("a").step()
    assert world.rows(
        "SELECT kind FROM channel_secrets WHERE channel_id = ? ORDER BY kind", made.channel.id
    ) == [("webhook_signing_key",), ("webhook_url",)]


# --- claims ---------------------------------------------------------------------------------------


async def test_a_dead_instances_claim_expires_and_is_taken_again() -> None:
    """A4-1: at least once. An instance that claimed and died: after 60 s another takes it."""
    world = await sqlite_world(responses=[OK])
    a = await world.channel("A")
    await _fire(world, [a])
    now = world.epoch.epoch_ms()
    [_] = await world.state.outbox.claim("clm_dead", 4, now, CLAIM_MS)  # then it died
    assert await world.deliver() == 0  # still claimed
    await world.clock.sleep_ms(CLAIM_MS)
    assert await world.deliver() == 1
    assert await world.deliveries() == [(a, "delivered", "delivered", 2)]


async def test_a_stale_workers_outcome_is_refused() -> None:
    """A4-2: the outcome is a compare-and-set on the claim: once retaken, the first writes
    nothing."""
    world = await sqlite_world()
    a = await world.channel("A")
    await _fire(world, [a])
    outbox = world.state.outbox
    [first] = await outbox.claim("clm_first", 4, world.epoch.epoch_ms(), CLAIM_MS)
    await world.clock.sleep_ms(CLAIM_MS)
    [second] = await outbox.claim("clm_second", 4, world.epoch.epoch_ms(), CLAIM_MS)
    assert first.id == second.id
    done = Outcome(DeliveryState.FAILED, "http_4xx")
    async with outbox.delivery_transaction():
        assert not await outbox.record(first.id, "clm_first", done, world.epoch.epoch_ms())
    ok = Outcome(DeliveryState.DELIVERED, "delivered")
    async with outbox.delivery_transaction():
        assert await outbox.record(second.id, "clm_second", ok, world.epoch.epoch_ms())
    assert await world.deliveries() == [(a, "delivered", "delivered", 2)]


async def test_a_channel_deleted_disabled_or_unreadable_fails_its_delivery() -> None:
    world = await sqlite_world(responses=[OK, OK, OK])
    # Not default: removing them tells nobody, so each has its one delivery.
    a = await world.channel("A", default=False)
    b = await world.channel("B", OTHER_URL, default=False)
    c = await world.channel("C", THIRD_URL, default=False)
    await _fire(world, [a, b, c])
    outbox = world.state.outbox
    worker = world.worker()
    await world.service.delete(a, actor=_actor())  # queued: failed at once (channel_gone)
    await world.service.disable(b, actor=_actor())
    # c's secrets under a master key this worker's vault does not have.
    worker._vault = Vault(bytes(range(100, 132)))
    await world.deliver(worker)
    found = {ch: (state, code) for ch, state, code, _ in await world.deliveries()}
    assert found[a] == ("failed", "channel_gone")
    assert found[b] == ("failed", "channel_disabled")
    assert found[c] == ("failed", "channel_secret_unreadable")
    assert world.network.connections == []
    assert outbox is world.state.outbox


async def test_no_network_call_is_made_inside_a_store_transaction() -> None:
    """The pool rule's guard: entering the HTTP call while this task holds a transaction
    raises, and nothing is sent."""
    world = await sqlite_world(responses=[OK])
    a = await world.channel("A")
    worker = world.worker()
    channel, secrets = await worker.read(a)
    assert channel is not None
    body = AlertPayload("ntf_00000000000000e1", "test", T0, "north").body()
    async with world.state.outbox.delivery_transaction():
        with pytest.raises(TransactionHeldError):
            await worker.attempt(channel, secrets, body, "ntf_00000000000000e1")
    assert world.network.connections == []
    assert (await worker.attempt(channel, secrets, body, "ntf_00000000000000e1")).delivered


# --- an incident's first channels -----------------------------------------------------------------


async def test_later_notifications_go_to_the_incidents_first_channels() -> None:
    """A4-7: the rule's channels changed after it fired; its resolution goes where it fired."""
    world = await sqlite_world(responses=[OK, OK])
    a = await world.channel("A")
    b = await world.channel("B", OTHER_URL)
    rule = await _fire(world, [a])
    [incident] = await world.state.incidents.open_incidents()
    await world.change_rule(rule, channels=[b])  # resolves it, rule_changed
    await world.clock.sleep_ms(30_000.0)
    assert await world.evaluator("a").step()
    found = world.rows("SELECT notify_channels FROM incidents WHERE id = ?", incident.id)
    assert found == [(json.dumps([a], separators=(",", ":")),)]
    kinds = {
        (row[0], json.loads(row[3])["kind"])
        for row in world.rows(
            "SELECT channel_id, state, last_code, payload FROM notification_deliveries"
        )
    }
    assert kinds == {(a, "firing"), (a, "resolved")}


async def test_a_reminder_goes_to_the_first_channels_not_a_new_default() -> None:
    world = await sqlite_world()
    a = await world.channel("A")
    await _fire(world, ["organization defaults"], repeat_every_minutes=60)
    await world.channel("New default", OTHER_URL)
    for _ in range(7):
        await world.clock.sleep_ms(10 * 60_000.0)
        await world.evaluator("a").step()
    kinds = sorted(
        (ch, json.loads(p)["kind"])
        for ch, p in world.rows("SELECT channel_id, payload FROM notification_deliveries")
    )
    assert kinds == [(a, "firing"), (a, "reminder")]


@pytest.mark.parametrize("which", ["memory", "sqlite"])
async def test_the_compare_and_set_update_never_writes_the_first_channels(which: str) -> None:
    """A4-8: set once, by the write that records the first delivered notification; an update
    carrying other ids leaves them."""
    clock = SimClock()
    epoch = ClockEpoch(clock, T0)
    if which == "memory":
        store: Any = InMemoryIncidentStore(epoch)
    else:
        db = open_database(":memory:")
        MigrationRunner(db, MIGRATIONS, epoch).migrate()
        store = SqliteIncidentStore(db, epoch)
    view = await store.attempt_lease("a")
    fence = Fence("a", view.epoch)
    opened = Incident(
        "inc_00000000000000c1",
        "north",
        "north-shop",
        "rul_00000000000000a1",
        1,
        "",
        IncidentState.PENDING,
        "critical",
        T0,
        updated_at_ms=T0,
    )
    assert await store.apply(fence, Change(opened, None, None))
    first = replace(opened, notify_channels=("chn_00000000000000a1",), revision=2)
    assert await store.apply(fence, Change(first, IncidentState.PENDING, 1))
    later = replace(first, notify_channels=("chn_00000000000000b2",), revision=3)
    assert await store.apply(fence, Change(later, IncidentState.PENDING, 2))
    [found] = await store.open_incidents()
    assert (found.revision, found.notify_channels) == (3, ("chn_00000000000000a1",))


# --- quiet hours ---------------------------------------------------------------------------------


async def test_held_notifications_get_no_delivery_and_their_summary_does() -> None:
    world = await sqlite_world()
    a = await world.channel("A")
    # 2026-11-02 10:00 UTC is a Monday; quiet from 09:00 to 11:00.
    await _fire(
        world,
        [a],
        severity="warning",
        quiet_hours={"days": ["mon"], "start": "09:00", "end": "11:00", "utc_offset": "+00:00"},
    )
    assert await world.deliveries() == []
    await world.clock.sleep_ms(61 * 60_000.0)
    await world.evaluator("a").step()
    kinds = [
        json.loads(p)["kind"] for (p,) in world.rows("SELECT payload FROM notification_deliveries")
    ]
    assert kinds == ["quiet_hours_summary"]
    summary = json.loads(world.rows("SELECT payload FROM notification_deliveries")[0][0])
    assert summary["details"]["held"] == 1


# --- the caps and the suppressed summary --------------------------------------------------------


def _notice(world: World, n: int, org: str = "north") -> AlertPayload:
    rule = PayloadRule(
        "rul_00000000000000a1", "Budget", "budget_used", ">=", 80.0, "%", None, 0, "critical"
    )
    return AlertPayload(
        f"{NOTIFICATION_ID_PREFIX}{n:016x}", "firing", world.epoch.epoch_ms(), org, rule=rule
    )


async def test_the_rule_cap_counts_notifications_not_deliveries() -> None:
    """M25: 6 an hour by default, whatever the number of channels; the rest suppressed."""
    world = await sqlite_world()
    a = await world.channel("A")
    b = await world.channel("B", OTHER_URL)
    channels = await world.state.channels.organization_channels("north")
    for n in range(8):
        async with world.state.outbox.delivery_transaction():
            await world.notices.queue(_notice(world, n), channels, rule_id="rul_00000000000000a1")
    states = [(s, c) for _, s, c, _ in await world.deliveries()]
    assert states.count(("queued", None)) == 12  # 6 notifications, two channels each
    assert states.count(("suppressed", "rule_cap")) == 4
    assert world.rows("SELECT scope, sent FROM notification_caps ORDER BY scope") == [
        ("organization", 6),
        ("rule", 6),
    ]
    assert {a, b} == {c.id for c in channels}


async def test_the_organization_cap_suppresses_across_rules() -> None:
    world = await sqlite_world(caps=AlertCaps(per_rule=6, per_organization=4))
    await world.channel("A")
    channels = await world.state.channels.organization_channels("north")
    for n in range(6):
        async with world.state.outbox.delivery_transaction():
            await world.notices.queue(_notice(world, n), channels, rule_id=f"rul_{n:016x}")
    states = [(s, c) for _, s, c, _ in await world.deliveries()]
    assert states == [("queued", None)] * 4 + [("suppressed", "organization_cap")] * 2


async def test_one_summary_an_hour_to_default_channels_only_and_not_counted() -> None:
    """M26: two holders' cycles in the hour after give one summary."""
    world = await sqlite_world(caps=AlertCaps(per_rule=1, per_organization=60))
    a = await world.channel("A")
    await world.channel("Not default", OTHER_URL, default=False)
    channels = [c for c in await world.state.channels.organization_channels("north") if c.id == a]
    for n in range(4):
        async with world.state.outbox.delivery_transaction():
            await world.notices.queue(_notice(world, n), channels, rule_id="rul_00000000000000a1")
    sent_before = world.rows("SELECT scope, sent FROM notification_caps ORDER BY scope")
    await world.clock.sleep_ms(HOUR_MS)
    assert await world.evaluator("a").step()
    await world.clock.sleep_ms(31_000.0)  # a's lease expires; b takes it in the same hour
    await world.clock.sleep_ms(30_000.0)
    assert await world.evaluator("b").step()
    summaries = [
        (ch, json.loads(p))
        for ch, p in world.rows("SELECT channel_id, payload FROM notification_deliveries")
        if json.loads(p)["kind"] == "suppressed_summary"
    ]
    assert [ch for ch, _ in summaries] == [a]
    details = summaries[0][1]["details"]
    assert (details["notifications"], details["rules"]) == (3, 1)
    assert world.rows("SELECT scope, sent FROM notification_caps ORDER BY scope") == sent_before
    assert world.rows("SELECT count(*) FROM suppression_summaries") == [(1,)]


@pytest.mark.parametrize("name", list(CAP_VARIABLES))
def test_each_cap_variable_is_a_whole_number_in_its_range(name: str) -> None:
    """A4-17: each at its bounds, and one past each bound (and 0, negative, empty, not a whole
    number) refusing the start, naming the variable. No cap can be turned off."""
    variable, low, high = CAP_VARIABLES[name]
    assert getattr(caps_from({variable: str(low)}), name) == low
    assert getattr(caps_from({variable: str(high)}), name) == high
    for bad in (str(low - 1), str(high + 1), "0", "-3", "", " ", "2.5", "six", "1e2"):
        with pytest.raises(ValueError, match=variable):
            caps_from({variable: bad})
    # The default: at most a sixth of the maximum (section 21.5), and at least the minimum.
    default = getattr(AlertCaps(), name)
    assert low <= default <= high // 6


# --- the instances' ceilings ----------------------------------------------------------------------


async def test_instances_whose_caps_differ_are_reported_naming_the_caps_never_refused() -> None:
    clock = SimClock()
    store = InMemoryCeilingsStore(clock.now_ms)
    seen: list[list[str]] = []
    one = CeilingsWatch(store, AlertCaps(), "one", on_differ=seen.append)
    two = CeilingsWatch(
        store, AlertCaps(per_rule=7, test_sends_per_channel=9), "two", on_differ=seen.append
    )
    assert await one.check() == []  # alone so far
    assert await two.check() == ["per_rule", "test_sends_per_channel"]
    assert await one.check() == ["per_rule", "test_sends_per_channel"]
    assert seen == [["per_rule", "test_sends_per_channel"]] * 2
    assert await one.check() == ["per_rule", "test_sends_per_channel"]
    assert len(seen) == 2  # once for each new difference


async def test_instances_that_agree_report_nothing() -> None:
    clock = SimClock()
    store = InMemoryCeilingsStore(clock.now_ms)
    seen: list[list[str]] = []
    watches = [CeilingsWatch(store, AlertCaps(), n, on_differ=seen.append) for n in "abc"]
    for _ in range(3):
        for w in watches:
            assert await w.check() == []
    assert seen == []


async def test_a_dead_instances_row_stops_counting() -> None:
    """Only live instances count: a row not written for ``LIVE_MS`` is not compared."""
    clock = SimClock()
    epoch = ClockEpoch(clock, T0)
    db = open_database(":memory:")
    MigrationRunner(db, MIGRATIONS, epoch).migrate()
    store = SqliteCeilingsStore(db, epoch.epoch_ms)
    seen: list[list[str]] = []
    live = CeilingsWatch(store, AlertCaps(), "live", on_differ=seen.append)
    dead = CeilingsWatch(store, AlertCaps(per_organization=120), "dead")
    await dead.check()
    assert await live.check() == ["per_organization"]
    await clock.sleep_ms(LIVE_MS)
    assert await live.check() == []
    assert seen == [["per_organization"]]
    # The row holds the caps' numbers and their digest, nothing else.
    [row] = db.execute("SELECT * FROM instance_ceilings WHERE instance_id = 'live'").fetchall()
    assert row[2] == ceilings_text(AlertCaps())
    assert json.loads(row[2]) == {
        "per_organization": 60,
        "per_rule": 6,
        "test_sends_per_channel": 10,
        "test_sends_per_organization": 30,
        # The email confirmations' caps (docs/48).
        "confirmations_per_address": 3,
        "confirmations_per_channel": 10,
        "confirmations_per_organization": 20,
        "confirmations_per_instance": 200,
    }
    assert len(row) == 4


async def test_the_evaluator_compares_the_ceilings_on_each_lease_attempt() -> None:
    world = await sqlite_world()
    seen: list[list[str]] = []
    store = world.state.ceilings
    other = CeilingsWatch(store, AlertCaps(per_rule=12), "other")
    await other.check()
    evaluator = world.evaluator(
        "a", ceilings=CeilingsWatch(store, AlertCaps(), "a", on_differ=seen.append)
    )
    await evaluator.attempt()
    assert seen == [["per_rule"]]


# --- the built-in channel_delivery_failing -------------------------------------------------------


async def _fail(world: World, channel: str, times: int) -> None:
    for _ in range(times):
        await world.state.channels.record_health(channel, False, world.epoch.epoch_ms())


async def test_the_built_in_fires_at_three_failures_to_every_other_default_channel() -> None:
    """CDF-3: never to the failing channel."""
    world = await sqlite_world(responses=[OK])
    a = await world.channel("A")
    b = await world.channel("B", OTHER_URL)
    await world.channel("Not default", THIRD_URL, default=False)
    await _fail(world, a, 2)
    assert await world.evaluator("a").step()
    assert await world.state.incidents.open_incidents() == []
    await _fail(world, a, 1)
    await world.clock.sleep_ms(30_000.0)
    assert await world.evaluator("a").step()
    [incident] = await world.state.incidents.open_incidents()
    assert (incident.application_id, incident.organization_id, incident.series_key) == (
        "",
        "north",
        a,
    )
    assert incident.rule_id == world.notices.builtin_rule_id("north")  # type: ignore[misc]
    assert incident.severity == "warning"
    [(channel, payload)] = world.rows("SELECT channel_id, payload FROM notification_deliveries")
    assert channel == b
    body = json.loads(payload)
    assert (body["kind"], body["application"], body["rule"]) == (
        "channel_delivery_failing",
        None,
        None,
    )
    assert (body["incident"]["series"], body["details"]["channel"]) == (a, a)


async def test_the_built_in_resolves_when_its_channel_delivers_or_is_deleted() -> None:
    world = await sqlite_world()
    a = await world.channel("A")
    b = await world.channel("B", OTHER_URL)
    c = await world.channel("C", THIRD_URL)
    await _fail(world, a, 3)
    await _fail(world, c, 3)
    assert await world.evaluator("a").step()
    assert len(await world.state.incidents.open_incidents()) == 2
    await world.state.channels.record_health(a, True, world.epoch.epoch_ms())
    await world.service.delete(c, actor=_actor())
    await world.clock.sleep_ms(30_000.0)
    assert await world.evaluator("a").step()
    assert await world.state.incidents.open_incidents() == []
    reasons = sorted(r for (r,) in world.rows("SELECT resolve_reason FROM incidents"))
    assert reasons == ["channel_deleted", "channel_delivered"]
    assert b


async def test_the_built_in_is_per_organization_with_its_own_rule_id() -> None:
    """CDF-4: south's failing channel makes south's incident, under south's rule id; north has
    none."""
    world = await sqlite_world()
    await world.channel("A")
    s = await world.channel("S", OTHER_URL, org="south")
    await _fail(world, s, 3)
    assert await world.evaluator("a").step()
    [incident] = await world.state.incidents.open_incidents()
    rule_id = world.notices.builtin_rule_id
    assert rule_id is not None
    assert (incident.organization_id, incident.rule_id) == ("south", rule_id("south"))
    assert rule_id("south") != rule_id("north")
    assert (
        await world.state.incidents.list_incidents(
            "north", application_id=None, state=None, limit=10, after=None
        )
        == []
    )


# --- the receiver's recipe in note 46 ------------------------------------------------------------


def _recipe() -> dict[str, Any]:
    """Note 46's verification snippet, exactly as written there, run in a namespace of its own."""
    text = (REPO / "docs" / "46-alert-channels.md").read_text(encoding="utf-8")
    block = text.split("<!-- receiver-recipe -->\n```python\n", 1)[1].split("```", 1)[0]
    namespace: dict[str, Any] = {}
    exec(compile(block, "docs/46-alert-channels.md", "exec"), namespace)
    return namespace


async def test_the_receivers_recipe_verifies_a_real_delivery_and_refuses_replays() -> None:
    """Section 21.3: the recipe verifies a real delivery's request, and rejects a stale
    timestamp and a repeated delivery id (and a changed body, and another key)."""
    world = await sqlite_world(responses=[OK])
    made = await world.service.create(
        "north",
        name="A",
        url=world_url(),
        is_default=True,
        channel_type="webhook",
        actor=_actor(),
        marker=_Mark(),
    )
    await _fire(world, [made.channel.id])
    await world.deliver()
    head, _, body = bytes(world.network.connections[0].written).partition(b"\r\n\r\n")
    headers = dict(line.split(": ", 1) for line in head.decode("ascii").split("\r\n")[1:])
    stamp = int(headers["X-Gateway-Timestamp"])
    receiver = _recipe()
    assert receiver["verify"](key_of(made), headers, body, now=stamp + 10)
    assert not receiver["verify"](key_of(made), headers, body, now=stamp + 10)  # a repeat
    fresh = _recipe()
    assert not fresh["verify"](key_of(made), headers, body, now=stamp + 301)  # stale
    assert not fresh["verify"](key_of(made), headers, body + b" ", now=stamp)
    assert not fresh["verify"]("00" * 32, headers, body, now=stamp)
    assert fresh["verify"](key_of(made), headers, body, now=stamp)


# --- small helpers -------------------------------------------------------------------------------


def world_url() -> str:
    from delivery_helpers import URL

    return URL


def _actor() -> Any:
    from delivery_helpers import ACTOR

    return ACTOR["north"]


class _Mark:
    def changed(self) -> None:
        return None
