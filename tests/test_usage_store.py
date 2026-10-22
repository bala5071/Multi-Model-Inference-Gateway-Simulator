"""Usage stores, the epoch sources, and the ledger as a facade over a store."""

from __future__ import annotations

import inspect
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from gateway.clock import ClockEpoch, SimClock, SystemEpoch
from gateway.database import BUSY_TIMEOUT_MS, open_database
from gateway.ledger import CostLedger
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.periods import FixedPeriods
from gateway.schema import RequestResult
from gateway.usage import (
    InMemoryUsageStore,
    ModelUsage,
    SqliteUsageStore,
    UsageEvent,
    UsageStore,
)


def _migrated(path: Path | str) -> sqlite3.Connection:
    connection = open_database(path)
    MigrationRunner(connection, MIGRATIONS, ClockEpoch(SimClock())).migrate()
    return connection


def _event(
    app: str = "a1",
    request: str = "req-1",
    at: float = 0.0,
    *,
    model: str = "economy",
    prompt: int = 10,
    cached: int = 0,
    completion: int = 5,
) -> UsageEvent:
    return UsageEvent(app, request, model, prompt, cached, completion, 0.25, 0.5, at)


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[UsageStore]:
    if request.param == "memory":
        yield InMemoryUsageStore()
    else:
        connection = _migrated(tmp_path / "usage.db")
        yield SqliteUsageStore(connection)
        connection.close()


def test_append_returns_increasing_unique_keys(store: UsageStore) -> None:
    seqs = [store.append(_event()) for _ in range(3)]
    assert seqs == sorted(set(seqs))
    # The same response id three times is three rows: req-N is not the key.
    assert [s.event.request_id for s in store.events()] == ["req-1"] * 3
    assert [s.seq for s in store.events()] == seqs


def test_events_round_trip_exactly(store: UsageStore) -> None:
    event = UsageEvent("a1", "req-7", "premium", 123, 23, 45, 0.1 + 0.2, 0.3 * 0.8, 1.5e12)
    seq = store.append(event)
    [stored] = store.events()
    assert (stored.seq, stored.event) == (seq, event)


def test_usage_by_model_sums_one_application_in_a_half_open_range(store: UsageStore) -> None:
    store.append(_event("a1", at=99.0))  # before
    store.append(_event("a1", at=100.0, model="premium", prompt=10, cached=4, completion=5))
    store.append(_event("a1", at=150.0, prompt=20, completion=1))
    store.append(_event("a2", at=150.0))  # another application
    store.append(_event("a1", at=200.0))  # the end is excluded

    by_model = store.usage_by_model("a1", 100.0, 200.0)

    assert set(by_model) == {"premium", "economy"}
    assert by_model["premium"] == ModelUsage(1, 10, 4, 5, 0.25, 0.5)
    assert by_model["economy"] == ModelUsage(1, 20, 0, 1, 0.25, 0.5)
    assert store.usage_by_model("nobody", 0.0, 1e15) == {}
    assert [s.event.application_id for s in store.events("a2")] == ["a2"]


def test_store_methods_are_synchronous() -> None:
    # Awaiting a store, or moving it onto a thread, would let a SimClock's time jump.
    for cls in (InMemoryUsageStore, SqliteUsageStore):
        for name in ("append", "usage_by_model", "events"):
            assert not inspect.iscoroutinefunction(getattr(cls, name)), (cls, name)


def test_in_memory_store_is_bounded() -> None:
    store = InMemoryUsageStore(max_events=2)
    for i in range(5):
        store.append(_event(request=f"req-{i}"))
    assert [(s.seq, s.event.request_id) for s in store.events()] == [(4, "req-3"), (5, "req-4")]
    with pytest.raises(ValueError, match="max_events"):
        InMemoryUsageStore(max_events=0)


# --- SQLite ---------------------------------------------------------------------------------


def test_a_restarted_process_gets_new_keys_and_keeps_the_old_rows(tmp_path: Path) -> None:
    path = tmp_path / "usage.db"
    first = _migrated(path)
    # Each process numbers its responses from req-1.
    a = SqliteUsageStore(first).append(_event(request="req-1"))
    b = SqliteUsageStore(first).append(_event(request="req-2"))
    first.close()

    second = _migrated(path)
    store = SqliteUsageStore(second)
    c = store.append(_event(request="req-1"))

    assert a < b < c
    assert [(s.seq, s.event.request_id) for s in store.events()] == [
        (a, "req-1"),
        (b, "req-2"),
        (c, "req-1"),
    ]
    second.close()


def test_keys_are_never_reused_even_after_the_newest_row_is_deleted(tmp_path: Path) -> None:
    connection = _migrated(tmp_path / "usage.db")
    store = SqliteUsageStore(connection)
    store.append(_event())
    newest = store.append(_event())
    connection.execute("DELETE FROM usage_events WHERE seq = ?", (newest,))
    assert store.append(_event()) == newest + 1
    connection.close()


def test_open_database_uses_wal_a_busy_timeout_and_full_sync(tmp_path: Path) -> None:
    connection = open_database(tmp_path / "g.db")
    assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    assert connection.execute("PRAGMA busy_timeout").fetchone() == (BUSY_TIMEOUT_MS,)
    assert connection.execute("PRAGMA synchronous").fetchone() == (2,)  # FULL
    assert connection.isolation_level is None
    connection.close()
    normal = open_database(tmp_path / "g.db", synchronous="NORMAL")
    assert normal.execute("PRAGMA synchronous").fetchone() == (1,)
    normal.close()
    with pytest.raises(ValueError, match="synchronous"):
        open_database(tmp_path / "g.db", synchronous="OFF")


def test_sqlite_store_needs_the_migrations() -> None:
    with pytest.raises(RuntimeError, match="run the migrations"):
        SqliteUsageStore(open_database(":memory:"))


# --- epoch sources ------------------------------------------------------------------------


async def test_clock_epoch_follows_the_clock_from_its_origin() -> None:
    clock = SimClock(start_ms=5.0)
    epoch = ClockEpoch(clock, origin_ms=1_700_000_000_000.0)
    assert epoch.epoch_ms() == 1_700_000_000_005.0
    await clock.sleep_ms(10.0)
    assert epoch.epoch_ms() == 1_700_000_000_015.0


def test_system_epoch_is_calendar_time() -> None:
    # After 2023-11-14 and before 2100: a Unix-epoch reading, not a perf_counter one.
    assert 1_700_000_000_000 < SystemEpoch().epoch_ms() < 4_102_444_800_000


# --- the ledger over a store --------------------------------------------------------------


def _result(request_id: str, tokens_in: int, tokens_out: int, cost: float) -> RequestResult:
    return RequestResult(request_id, "m", 0.0, 0.0, 1.0, 2.0, tokens_in, tokens_out, cost)


def test_the_ledger_stores_one_event_per_charge(store: UsageStore) -> None:
    clock = SimClock(start_ms=10.0)
    ledger = CostLedger(clock, store=store, epoch=ClockEpoch(clock, origin_ms=1e12))

    ledger.record("t1", _result("req-1", 10, 5, 0.5), model="economy", price_multiplier=0.8)
    ledger.record("t2", _result("req-2", 7, 3, 0.25), model="premium", price_multiplier=1.0)

    assert [s.event for s in store.events()] == [
        UsageEvent("t1", "req-1", "economy", 10, 0, 5, 0.5, 0.5 * 0.8, 1e12 + 10.0),
        UsageEvent("t2", "req-2", "premium", 7, 0, 3, 0.25, 0.25, 1e12 + 10.0),
    ]


def test_a_new_ledger_on_the_same_database_sees_the_stored_usage(tmp_path: Path) -> None:
    path = tmp_path / "usage.db"
    clock = SimClock()
    epoch = ClockEpoch(clock, origin_ms=1e12)
    first = _migrated(path)
    ledger = CostLedger(clock, store=SqliteUsageStore(first), epoch=epoch)
    for i in range(3):
        ledger.record("t1", _result(f"req-{i}", 10, 5, 0.1), model="economy", price_multiplier=1.0)
    before = ledger.usage("t1")
    first.close()

    second = _migrated(path)
    restarted = CostLedger(clock, store=SqliteUsageStore(second), epoch=epoch)

    assert restarted.usage("t1") == before
    assert restarted.usage("t1").total.billed_tokens == 45
    second.close()


def test_stored_usage_from_an_earlier_period_does_not_count(tmp_path: Path) -> None:
    connection = _migrated(tmp_path / "usage.db")
    store = SqliteUsageStore(connection)
    store.append(_event("t1", at=999.0))  # the period before
    clock = SimClock(start_ms=1500.0)
    ledger = CostLedger(clock, periods=FixedPeriods(0.0, 1000.0), store=store)
    usage = ledger.usage("t1")
    assert (usage.period_start_ms, usage.total) == (1000.0, ModelUsage())
    connection.close()
