"""The recorder's hour and organization keys (docs/57): what each request adds, how long each
kind is kept in memory and in Valkey, the flusher sending them once and marking them lost, the
two instances adding up, and the paged organization read the Clients pages use.

Test values, not secrets."""

from __future__ import annotations

import os
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from gateway.alertrecorder import (
    APP_HOUR,
    HOUR_MS,
    MINUTE_MS,
    ORG_HOUR,
    ORG_MINUTE,
    AlertRecorder,
)
from gateway.alertrollups import KEEP_HOURS, KEEP_MINUTES, InMemoryAlertRollups, RollupFlusher
from gateway.applications import (
    InMemoryApplicationStore,
    SqliteApplicationStore,
    application_from_template,
)
from gateway.clock import SimClock, WallClock
from gateway.database import open_database
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.organizations import new_organization
from gateway.routing import load_plans
from gateway.state import StateUnavailableError
from valkey_helpers import VALKEY_URL_ENV, ManualEpoch, counters_on

T0 = 1_793_613_600_000.0  # an hour's start
H0 = int(T0 // HOUR_MS)
M0 = int(T0 // MINUTE_MS)


class Epoch:
    def __init__(self, at: float = T0) -> None:
        self.at = at

    def epoch_ms(self) -> float:
        return self.at


def _one(recorder: AlertRecorder, application: str = "app1", organization: str | None = "org1",
         status: int = 200) -> None:  # fmt: skip
    recorder.record(application, kind="generate", status=status, total_ms=10.0,
                    organization_id=organization)  # fmt: skip


# --- what one request adds -----------------------------------------------------------------------


def test_a_request_adds_to_its_minute_its_hour_and_its_organizations_minute_and_hour() -> None:
    epoch = Epoch(T0 + 90 * MINUTE_MS)  # minute 90: hour 1
    recorder = AlertRecorder(epoch)
    _one(recorder)
    minutes, extras = recorder.take(), recorder.take_extras()
    [(minute_key, counts)] = minutes.items()
    assert minute_key == ("app1", M0 + 90)
    assert extras == {
        (APP_HOUR, "app1", H0 + 1): counts,
        (ORG_MINUTE, "org1", M0 + 90): counts,
        (ORG_HOUR, "org1", H0 + 1): counts,
    }
    # The same fixed names: never an id, a prompt or a key in a field.
    assert all(":" in name or name == "requests" for name in counts)


def test_without_an_organization_only_the_applications_hour_is_added() -> None:
    recorder = AlertRecorder(Epoch())
    _one(recorder, organization=None)
    assert set(recorder.take_extras()) == {(APP_HOUR, "app1", H0)}


def test_counts_of_one_request_are_never_shared_between_keys() -> None:
    """Each key holds its own counts: adding to one never moves another."""
    rollups = InMemoryAlertRollups()
    recorder = AlertRecorder(Epoch(), sink=rollups)
    _one(recorder)
    _one(recorder, application="app2")
    assert rollups._extras[(APP_HOUR, "app1", H0)]["requests"] == 1
    assert rollups._extras[(ORG_HOUR, "org1", H0)]["requests"] == 2


# --- kept in memory (15.6, condition 1) ----------------------------------------------------------


async def test_in_memory_hours_are_kept_26_hours_and_minutes_120() -> None:
    epoch = Epoch()
    rollups = InMemoryAlertRollups()
    recorder = AlertRecorder(epoch, sink=rollups)
    for hour in range(KEEP_HOURS + 4):
        epoch.at = T0 + hour * HOUR_MS + 30 * MINUTE_MS
        _one(recorder)
    newest = H0 + KEEP_HOURS + 3
    hours = sorted(k[2] for k in rollups._extras if k[0] == APP_HOUR)
    assert hours == list(range(newest - KEEP_HOURS + 1, newest + 1))
    assert len(hours) == KEEP_HOURS
    org_minutes = [k[2] for k in rollups._extras if k[0] == ORG_MINUTE]
    assert all(m > (newest * 60 + 30) - KEEP_MINUTES for m in org_minutes)
    # The bound: per subject, 26 hour keys and the minutes of the last 120 minutes, whatever
    # the traffic.
    assert rollups.size_extras() == KEEP_HOURS * 2 + len(org_minutes)
    rows, since = await rollups.read_extras(APP_HOUR, [("app1", [newest - KEEP_HOURS, newest])])
    assert since is None  # in memory the process's start says when counting began
    assert [r.counts.get("requests", 0) for r in rows[0]] == [0, 1]


async def test_in_memory_read_marks_and_forgets_an_incomplete_hour() -> None:
    rollups = InMemoryAlertRollups()
    recorder = AlertRecorder(Epoch(), sink=rollups)
    _one(recorder)
    await rollups.mark_incomplete_extras([(APP_HOUR, "app1", H0)])
    [[row]], _ = await rollups.read_extras(APP_HOUR, [("app1", [H0])])
    assert row.incomplete
    assert row.counts["requests"] == 1


# --- the flusher -------------------------------------------------------------------------------


class FlakyRollups(InMemoryAlertRollups):
    def __init__(self) -> None:
        super().__init__()
        self.down = False

    async def add(self, batch: str, deltas: Any) -> None:
        if self.down:
            raise StateUnavailableError("timeout")
        await super().add(batch, deltas)

    async def add_extras(self, batch: str, extras: Any) -> None:
        if self.down:
            raise StateUnavailableError("timeout")
        await super().add_extras(batch, extras)

    async def mark_incomplete_extras(self, keys: Any) -> None:
        if self.down:
            raise StateUnavailableError("timeout")
        await super().mark_incomplete_extras(keys)


async def test_the_flusher_sends_the_hours_once_and_marks_the_lost_ones() -> None:
    clock, epoch = SimClock(), Epoch()
    recorder = AlertRecorder(epoch)
    rollups = FlakyRollups()
    flusher = RollupFlusher(recorder, rollups, clock, "i1")
    _one(recorder)
    assert await flusher.flush()
    assert await flusher.flush()  # nothing new: nothing added twice
    [[row]], _ = await rollups.read_extras(ORG_HOUR, [("org1", [H0])])
    assert row.counts["requests"] == 1
    # Down past the ten minutes kept: dropped, and the hour marked once Valkey answers.
    _one(recorder)
    rollups.down = True
    assert not await flusher.flush()
    await clock.sleep_ms(10 * MINUTE_MS)
    assert not await flusher.flush()
    rollups.down = False
    _one(recorder)
    assert await flusher.flush()
    [[lost]], _ = await rollups.read_extras(ORG_HOUR, [("org1", [H0])])
    assert lost.incomplete
    assert lost.counts["requests"] == 2  # the first and the last; the dropped one is not there


# --- Valkey ------------------------------------------------------------------------------------


@asynccontextmanager
async def valkey(count: int = 1) -> AsyncIterator[list[Any]]:
    url = os.environ[VALKEY_URL_ENV]
    prefix = f"gwtest:{random.SystemRandom().getrandbits(64):016x}:"
    found = [counters_on(url, ManualEpoch(), prefix=prefix, seed=i + 1) for i in range(count)]
    try:
        yield found
    finally:
        client = found[0].client
        async for key in client.scan_iter(match=prefix + "*"):
            await client.delete(key)
        for counters in found:
            await counters.aclose()


@pytest.mark.valkey
async def test_valkey_hours_round_trip_once_a_batch_and_expire_after_26_hours() -> None:
    recorder = AlertRecorder(Epoch(T0 + 30 * MINUTE_MS))
    _one(recorder)
    _one(recorder, application="app2", status=500)
    extras = recorder.take_extras()
    async with valkey() as [counters]:
        rollups = counters.alert_rollups()
        await rollups.add_extras("i1:1", extras)
        await rollups.add_extras("i1:1", extras)  # sent again: nothing added
        [[shop], [blog]], since = await rollups.read_extras(
            APP_HOUR, [("app1", [H0]), ("app2", [H0])]
        )
        [[org]], _ = await rollups.read_extras(ORG_HOUR, [("org1", [H0])])
        [[org_minute]], _ = await rollups.read_extras(ORG_MINUTE, [("org1", [M0 + 30])])
        hour_ttl = await counters.client.pttl(counters.rollup_name(f"rollup-{APP_HOUR}",
                                                                   f"app1\x00{H0}"))  # fmt: skip
        minute_ttl = await counters.client.pttl(
            counters.rollup_name(f"rollup-{ORG_MINUTE}", f"org1\x00{M0 + 30}")
        )
    assert shop.counts["requests"] == 1
    assert blog.counts["status:5xx"] == 1
    assert org.counts["requests"] == 2
    assert org_minute.counts["requests"] == 2
    assert since == M0 + 30  # the first organization minute counted
    assert 25 * HOUR_MS < hour_ttl <= 26 * HOUR_MS
    assert 0 < minute_ttl <= 2 * HOUR_MS


@pytest.mark.valkey
async def test_valkey_hour_keys_are_gone_once_their_time_is_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The expiry itself, on a real Valkey: the hours' keep shortened to 300 ms, every hour key
    is gone within a few seconds while the minute keys (2 hours) stay."""
    monkeypatch.setattr("gateway.valkey.HOURS_KEEP_MS", 300)
    recorder = AlertRecorder(Epoch(T0 + 30 * MINUTE_MS))
    _one(recorder)
    async with valkey() as [counters]:
        rollups = counters.alert_rollups()
        await rollups.add_extras("i1:1", recorder.take_extras())
        hour = counters.rollup_name(f"rollup-{APP_HOUR}", f"app1\x00{H0}")
        minute = counters.rollup_name(f"rollup-{ORG_MINUTE}", f"org1\x00{M0 + 30}")
        assert await counters.client.exists(hour) == 1
        clock = WallClock()
        deadline = clock.now_ms() + 5_000
        while await counters.client.exists(hour) and clock.now_ms() < deadline:
            await clock.sleep_ms(50)
        assert await counters.client.exists(hour) == 0
        assert await counters.client.exists(minute) == 1


@pytest.mark.valkey
async def test_valkey_hours_of_two_instances_equal_one_instances_in_memory() -> None:
    memory = InMemoryAlertRollups()
    epoch = Epoch()
    one = AlertRecorder(epoch, sink=memory)
    first, second = AlertRecorder(epoch), AlertRecorder(epoch)
    for i in range(40):
        epoch.at = T0 + (i % 3) * HOUR_MS + i * 1000
        app, org = f"app{i % 4}", f"org{i % 2}"
        _one(one, app, org, 200 if i % 5 else 500)
        _one(first if i % 2 else second, app, org, 200 if i % 5 else 500)
    async with valkey(2) as (a, b):
        await a.alert_rollups().add_extras("i1:1", first.take_extras())
        await b.alert_rollups().add_extras("i2:1", second.take_extras())
        wanted = [(f"org{i}", list(range(H0, H0 + 3))) for i in range(2)]
        shared, _ = await a.alert_rollups().read_extras(ORG_HOUR, wanted)
    local, _ = await memory.read_extras(ORG_HOUR, wanted)
    assert [[r.counts for r in rows] for rows in shared] == [
        [r.counts for r in rows] for rows in local
    ]


@pytest.mark.valkey
async def test_valkey_a_restart_marks_the_hours_before_it_incomplete() -> None:
    recorder = AlertRecorder(Epoch(T0 + 30 * MINUTE_MS))
    _one(recorder)
    async with valkey() as [counters]:
        rollups = counters.alert_rollups()
        await rollups.add_extras("i1:1", recorder.take_extras())
        await rollups.note_restart(M0 + 90)
        [[before, after]], _ = await rollups.read_extras(APP_HOUR, [("app1", [H0, H0 + 2])])
        await rollups.mark_incomplete_extras([(ORG_HOUR, "org1", H0 + 5)])
        [[marked]], _ = await rollups.read_extras(ORG_HOUR, [("org1", [H0 + 5])])
    assert before.incomplete
    assert not after.incomplete
    assert marked.incomplete


# --- the paged organization read -----------------------------------------------------------------


def _sqlite_store(tmp_path: Any) -> SqliteApplicationStore:
    connection = open_database(tmp_path / "orgs.db")
    MigrationRunner(connection, MIGRATIONS, Epoch()).migrate()
    return SqliteApplicationStore(connection)


async def _check_paging(store: Any) -> None:
    for name in ("delta", "alpha", "charlie", "bravo"):
        await store.add_organization(
            new_organization(name, name.title(), "business", created_at_ms=0)
        )
    for i, org in enumerate(("bravo", "bravo", "delta")):
        await store.add_application(
            application_from_template(
                f"app{i}", f"App {i}", load_plans(), "free", organization_id=org, created_at_ms=0
            )
        )
    first = await store.page_organizations(None, 2)
    rest = await store.page_organizations(first[-1][0].id, 10)
    found = [(o.id, n) for o, n in first + rest]
    # The default organization is in every store from the start.
    assert found == [("alpha", 0), ("bravo", 2), ("charlie", 0), ("default", 0), ("delta", 1)]
    assert [o.id for o, _ in await store.page_organizations("b", 10)] == [
        "bravo",
        "charlie",
        "default",
        "delta",
    ]


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_organizations_come_a_page_at_a_time_by_id_with_their_application_counts(
    kind: str, tmp_path: Any
) -> None:
    await _check_paging(InMemoryApplicationStore() if kind == "memory" else _sqlite_store(tmp_path))


@pytest.mark.postgres
async def test_organizations_come_a_page_at_a_time_on_postgres() -> None:
    from gateway.postgres import PostgresApplicationStore
    from postgres_helpers import pg_world

    async with pg_world() as world:
        await _check_paging(PostgresApplicationStore(world.instances[0]))
