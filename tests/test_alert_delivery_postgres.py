"""Delivery on Postgres (and Valkey) (docs/46): each constraint reached by a test of its own, not
by an earlier key (``SKIP LOCKED`` from two workers on one row, the unique (notification,
channel) from two instances' notices with distinct delivery ids, the unique (organization, hour)
from two holders' summaries); two evaluators on two instances give one delivery; the stale
worker refused and the claim taken again; an incident's first channels set once; the instances'
ceilings compared, a dead one's row expiring; the pool rule with the evaluator's pool of 3 and
four webhooks that never answer; the test-send counter in Valkey; and the leak scan of every
Postgres row and Valkey key.

The tests run against the compose services (``postgres``, ``valkey``). Every attempt goes
through ``webhook_client`` onto an injected resolver and network. Test values, not secrets."""

from __future__ import annotations

import asyncio
import json
import random
from dataclasses import replace
from typing import Any

import httpx
import pytest

from delivery_helpers import (
    ACTOR,
    OK,
    OTHER_URL,
    T0,
    URL,
    FrozenClock,
    World,
    build,
    key_of,
    seed,
)
from gateway.alerts.caps import LIVE_MS, AlertCaps, CeilingsWatch
from gateway.alerts.delivery import CLAIM_MS
from gateway.alerts.outbox import DeliveryState, Outcome
from gateway.alerts.payload import AlertPayload
from gateway.apikeys import ApiKeyService, ApplicationAccess
from gateway.app import create_app
from gateway.clock import SystemEpoch, WallClock
from gateway.continuous import build_continuous_registry
from gateway.incidents import Change, Fence, Incident, IncidentState
from gateway.routing import load_plans
from helpers import TEST_PEPPER, TEST_PROFILES
from mock_network import HANG, ScriptedNetwork

pytestmark = pytest.mark.postgres


async def _world(
    db: Any,
    *,
    caps: AlertCaps | None = None,
    responses: list[Any] | None = None,
    seeded: bool = True,
) -> World:
    from gateway.postgres import postgres_state

    state = postgres_state(db)
    if seeded:
        await seed(state, T0)
    return build(state, WallClock(), SystemEpoch(), caps=caps, responses=responses)


async def _fire(world: World, channels: list[str], holder: str = "a") -> bool:
    if not await world.state.rules.get_rule("north-shop", "rul_00000000000000a1"):
        await world.rule(channels)
    world.signals.budget["north-shop"] = 95.0
    return await world.evaluator(holder).step()


async def test_skip_locked_a_claimed_row_is_skipped_not_waited_for() -> None:
    """Two workers, one row: while the first's claim is uncommitted, the second's claim returns
    at once with nothing (``SKIP LOCKED``), never waiting for the first."""
    from postgres_helpers import pg_world

    async with pg_world(instances=2) as pg:
        one = await _world(pg.instances[0])
        two = await _world(pg.instances[1], seeded=False)
        channel = await one.channel("A")
        await _fire(one, [channel])
        first, second = one.state.outbox, two.state.outbox
        now = await one.state.clock.now_ms()
        async with first.delivery_transaction():
            [claimed] = await first.claim("clm_one", 4, now, CLAIM_MS)
            async with second.delivery_transaction():
                found = await asyncio.wait_for(second.claim("clm_two", 4, now, CLAIM_MS), 2.0)
            assert found == []
        assert claimed.claimed_by == "clm_one"


async def test_two_instances_notices_with_distinct_delivery_ids_queue_once() -> None:
    """The unique (notification, channel), reached by two instances' rows of different ids."""
    from postgres_helpers import pg_world

    async with pg_world(instances=2) as pg:
        one = await _world(pg.instances[0])
        two = await _world(pg.instances[1], seeded=False)
        await one.channel("A")
        channels = await one.state.channels.organization_channels("north")
        payload = AlertPayload("ntf_00000000000000c1", "firing", T0, "north")

        async def queue(world: World) -> int:
            async with world.state.outbox.delivery_transaction():
                return await world.notices.queue(payload, channels, counted=False)

        counted = await asyncio.gather(queue(one), queue(two))
        assert sorted(counted) == [0, 1]
        assert await pg.rows(
            "SELECT count(*), count(DISTINCT id) FROM notification_deliveries"
        ) == [(1, 1)]


async def test_two_holders_summaries_of_one_hour_give_one() -> None:
    """The unique (organization, hour), reached by two holders' cycles at the same time."""
    from postgres_helpers import pg_world

    async with pg_world(instances=2) as pg:
        caps = AlertCaps(per_rule=1)
        one = await _world(pg.instances[0], caps=caps)
        two = await _world(pg.instances[1], caps=caps, seeded=False)
        await one.channel("A")
        channels = await one.state.channels.organization_channels("north")
        hour = int(T0 // 3_600_000)
        for n in range(3):
            async with one.state.outbox.delivery_transaction():
                await one.notices.queue(
                    AlertPayload(f"ntf_{n:016x}", "firing", T0, "north"),
                    channels,
                    rule_id="rul_x",
                    now_ms=T0,
                )
        later = (hour + 1) * 3_600_000.0 + 1.0
        queued = await asyncio.gather(
            one.notices.summaries(hour, later), two.notices.summaries(hour, later)
        )
        assert sorted(queued) == [0, 1]
        kinds = [
            json.loads(p)["kind"]
            for (p,) in await pg.rows("SELECT payload FROM notification_deliveries")
        ]
        assert kinds.count("suppressed_summary") == 1


async def test_two_evaluators_on_two_instances_give_one_delivery() -> None:
    from postgres_helpers import pg_world

    async with pg_world(instances=2) as pg:
        one = await _world(pg.instances[0], responses=[OK])
        two = await _world(pg.instances[1], seeded=False)
        channel = await one.channel("A")
        await one.rule([channel])
        for world in (one, two):
            world.signals.budget["north-shop"] = 95.0
        done = await asyncio.gather(one.evaluator("a").step(), two.evaluator("b").step())
        assert sorted(done) == [False, True]
        assert await pg.rows("SELECT count(*) FROM notification_deliveries") == [(1,)]
        assert await one.deliver() == 1
        assert await pg.rows("SELECT state, last_code, attempts FROM notification_deliveries") == [
            ("delivered", "delivered", 1)
        ]
        [(failures, delivered)] = await pg.rows(
            "SELECT consecutive_failures, last_delivered_at_ms FROM alert_channels"
        )
        assert failures == 0
        assert delivered is not None


async def test_a_stale_workers_outcome_is_refused_and_the_claim_taken_again() -> None:
    from postgres_helpers import pg_world

    async with pg_world(instances=2) as pg:
        one = await _world(pg.instances[0])
        two = await _world(pg.instances[1], seeded=False)
        channel = await one.channel("A")
        await pg.set_time(T0)
        await _fire(one, [channel])
        async with one.state.outbox.delivery_transaction():
            [first] = await one.state.outbox.claim("clm_one", 4, T0, CLAIM_MS)
        await pg.set_time(T0 + CLAIM_MS)
        async with two.state.outbox.delivery_transaction():
            [again] = await two.state.outbox.claim("clm_two", 4, T0 + CLAIM_MS, CLAIM_MS)
        assert (first.id, again.attempts) == (again.id, 2)
        async with one.state.outbox.delivery_transaction():
            assert not await one.state.outbox.record(
                first.id, "clm_one", Outcome(DeliveryState.FAILED, "http_4xx"), T0 + CLAIM_MS
            )
        async with two.state.outbox.delivery_transaction():
            assert await two.state.outbox.record(
                again.id, "clm_two", Outcome(DeliveryState.DELIVERED, "delivered"), T0 + CLAIM_MS
            )
        assert await pg.rows("SELECT state, claimed_by FROM notification_deliveries") == [
            ("delivered", None)
        ]


async def test_the_update_never_writes_the_first_channels_on_postgres() -> None:
    """A4-8 on Postgres."""
    from gateway.postgres import PostgresIncidentStore
    from postgres_helpers import pg_world

    async with pg_world() as pg:
        store = PostgresIncidentStore(pg.instances[0])
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
        assert await pg.rows("SELECT revision, notify_channels FROM incidents") == [
            (3, '["chn_00000000000000a1"]')
        ]


async def test_instances_ceilings_are_compared_live_only_on_postgres() -> None:
    from postgres_helpers import pg_world

    async with pg_world(instances=3) as pg:
        from gateway.postgres import PostgresCeilingsStore

        stores = [PostgresCeilingsStore(db) for db in pg.instances]
        seen: list[list[str]] = []
        await pg.set_time(T0)
        one = CeilingsWatch(stores[0], AlertCaps(), "one", on_differ=seen.append)
        same = CeilingsWatch(stores[1], AlertCaps(), "same", on_differ=seen.append)
        assert await one.check() == []
        assert await same.check() == []
        assert seen == []  # agreeing: nothing
        other = CeilingsWatch(stores[2], AlertCaps(per_organization=90), "other")
        await other.check()
        assert await one.check() == ["per_organization"]
        assert seen == [["per_organization"]]
        await pg.set_time(T0 + LIVE_MS)  # other stops reporting: no longer live
        assert await one.check() == []
        [(count,)] = await pg.rows("SELECT count(*) FROM instance_ceilings")
        assert count == 3  # kept for an hour, unread
        await pg.set_time(None)


async def test_four_hanging_webhooks_hold_no_connection_of_the_evaluators_pool() -> None:
    """The pool rule (decided 2026-11-08): delivery shares the evaluator's pool of 3. Four
    deliveries hang at their receivers on one instance; meanwhile that instance's evaluator
    completes a cycle, a second worker over the same database and pool claims and records a
    fifth delivery, and chat and generate requests are answered."""
    from gateway.postgres import postgres_state
    from postgres_helpers import pg_world

    async with pg_world(evaluator_pool_size=3) as pg:
        db = pg.instances[0]
        world = await _world(db)
        channels = [await world.channel(f"C{n}", u) for n, u in enumerate((URL, OTHER_URL))]
        await world.rule(channels)
        world.signals.budget["north-shop"] = 95.0
        async with db.evaluation():
            assert await world.evaluator("a").step()
            outbox = world.state.outbox
            async with outbox.delivery_transaction():
                for n in range(1):  # with the firing's two: four to hang
                    await world.notices.queue(
                        AlertPayload(f"ntf_{n + 10:016x}", "test", T0, "north"),
                        await world.state.channels.organization_channels("north"),
                        counted=False,
                    )
            hanging = ScriptedNetwork([HANG] * 4)
            frozen = FrozenClock(WallClock())
            stuck = world.worker(clock=frozen, network=hanging)
            assert await stuck.step() == 4
            for _ in range(500):
                if len(hanging.connections) == 4:
                    break
                await WallClock().sleep_ms(10.0)
            assert len(hanging.connections) == 4
            # The fifth, queued now, through a second worker on the same pool.
            target = await world.state.channels.get_channel(channels[0])
            assert target is not None
            async with outbox.delivery_transaction():
                await world.notices.queue(
                    AlertPayload("ntf_00000000000000ff", "test", T0, "north"),
                    [target],
                    counted=False,
                )
            fifth = world.worker(network=ScriptedNetwork([OK]))
            assert await asyncio.wait_for(world.deliver(fifth), 10.0) == 1
            world.signals.budget["north-shop"] = 0.0
            world.evaluators["a"]._last_cycle_local_ms = None
            assert await asyncio.wait_for(world.evaluator("a").step(), 10.0)
        # Chat and generate requests, on the request pool, answered meanwhile.
        state = postgres_state(db)
        keys = ApiKeyService(
            state.applications,
            TEST_PEPPER,
            SystemEpoch(),
            store_clock=db,
            random_bytes=random.Random(4).randbytes,
        )
        key = (await keys.issue("north-shop", "chat")).key
        registry = build_continuous_registry(WallClock(), seed=7, profiles=TEST_PROFILES)
        app = create_app(
            registry,
            WallClock(),
            applications=ApplicationAccess(keys, load_plans()),
            epoch=SystemEpoch(),
            startup_applications=[],
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
            answers = await asyncio.wait_for(
                asyncio.gather(
                    client.post(
                        "/v1/chat/completions",
                        headers={"Authorization": f"Bearer {key}"},
                        json={"model": "economy", "messages": [{"role": "user", "content": "Hi."}]},
                    ),
                    client.post(
                        "/v1/generate",
                        headers={"Authorization": f"Bearer {key}"},
                        json={"model": "economy", "prompt": "hello", "max_tokens": 1},
                    ),
                ),
                10.0,
            )
        await registry.aclose()
        assert [a.status_code for a in answers] == [200, 200]
        assert stuck.running == 4  # still hanging, all the while
        for task in list(stuck._running):
            task.cancel()
        await asyncio.gather(*list(stuck._running), return_exceptions=True)


@pytest.mark.valkey
async def test_test_sends_are_counted_in_valkey_in_keys_of_their_own() -> None:
    from gateway.management import ManagementRefusal
    from valkey_helpers import ManualEpoch, instances

    async with instances(ManualEpoch(T0)) as (one, two):
        counters = [one.channel_tests(2, 3), two.channel_tests(2, 3)]
        await counters[0].count("chn_a", "north")
        await counters[1].count("chn_a", "north")
        with pytest.raises(ManagementRefusal) as refused:
            await counters[0].count("chn_a", "north")
        assert refused.value.reason.value == "test_send_limited"
        await counters[1].count("chn_b", "north")
        with pytest.raises(ManagementRefusal):
            await counters[0].count("chn_c", "north")  # the organization's 3
        names = [k async for k in one.client.scan_iter(match=one.prefix + "*")]
        assert names
        assert all(":channel-tests:" in n or ":org-channel-tests:" in n for n in names)
        assert not any("chn_" in n or "north" in n for n in names)


@pytest.mark.valkey
async def test_the_channel_secrets_leak_into_no_postgres_row_and_no_valkey_key() -> None:
    """Section 15's scan of the shared stores: a channel made, rotated, tested and delivered to
    on Postgres, its test sends counted in Valkey; every row of every table and every Valkey key
    and value scanned for the URL's tokens and the signing keys; the control finds each."""
    from gateway.alerts.delivery import TestSends
    from postgres_helpers import pg_world
    from valkey_helpers import ManualEpoch, instances

    token, query = "planted-pg-path-7a1c", "planted-pg-query-2e9b"
    url = f"https://hooks.example.com/hook/{token}?token={query}"
    async with pg_world() as pg, instances(ManualEpoch(T0), count=1) as (counters,):
        world = await _world(pg.instances[0], responses=[OK, OK, OK])
        made = await world.service.create(
            "north",
            name="A",
            url=url,
            is_default=True,
            channel_type="webhook",
            actor=ACTOR["north"],
            marker=_Mark(),
        )
        new_key = await world.service.rotate_key(
            made.channel.id, actor=ACTOR["north"], marker=_Mark()
        )
        sends = TestSends(
            world.worker(),
            counters.channel_tests(10, 30),
            world.service.audit,
            world.epoch,
            random.Random(5).randbytes,
        )
        assert await sends.send("north", made.channel.id, ACTOR["north"]) == "delivered"
        await _fire(world, [made.channel.id])
        await world.deliver()
        tables = [
            t
            for (t,) in await pg.rows(
                "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
            )
        ]
        rows = []
        for table in tables:
            for row in await pg.rows(f"SELECT * FROM {table}"):
                rows.append(repr([v.hex() if isinstance(v, bytes) else v for v in row]))
        valkey = []
        async for name in counters.client.scan_iter(match=counters.prefix + "*"):
            kind = await counters.client.type(name)
            value = (
                await counters.client.zrange(name, 0, -1)
                if kind == "zset"
                else await counters.client.get(name)
                if kind == "string"
                else ""
            )
            valkey.append(f"{name} {value!r}")
        planted = {
            "path token": token,
            "query token": query,
            "key": key_of(made),
            "new key": new_key,
        }
        for where, text in (("postgres", "\n".join(rows)), ("valkey", "\n".join(valkey))):
            assert sorted(n for n, v in planted.items() if v in text) == [], where
        assert len(world.network.connections) == 2
        control = url + key_of(made) + new_key
        assert sorted(n for n, v in planted.items() if v in control) == sorted(planted)


class _Mark:
    def changed(self) -> None:
        return None
