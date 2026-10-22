"""Usage in SQLite in the default server: calendar-month budget periods, restarts,
retention. See docs/21-persistence.md."""

from __future__ import annotations

import asyncio
import datetime
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from gateway.apikeys import ApiKeyService, ApplicationAccess
from gateway.app import (
    DEFAULT_USAGE_RETENTION_DAYS,
    create_app,
    create_default_app,
    usage_retention_ms,
)
from gateway.applications import SqliteApplicationStore, seed_demo_applications
from gateway.clock import ClockEpoch, SimClock
from gateway.continuous import build_continuous_registry
from gateway.database import open_database
from gateway.ledger import PRUNE_EVERY_MS, CostLedger
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.periods import CalendarMonthsUTC, FixedPeriods
from gateway.routing import DemoApplication, Plan, PlansConfig
from gateway.schema import RequestResult
from gateway.usage import InMemoryUsageStore, SqliteUsageStore, UsageEvent
from helpers import TEST_PEPPER, bearer, default_server_key

DAY_MS = 86_400_000.0
WORDS = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi"
UTC = datetime.UTC


def ms(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> float:
    """Epoch milliseconds of a UTC date and time."""
    return datetime.datetime(year, month, day, hour, minute, second, tzinfo=UTC).timestamp() * 1000


# --- calendar months --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("at", "start", "end", "days"),
    [
        ((1970, 1, 15), (1970, 1, 1), (1970, 2, 1), 31),
        ((2024, 2, 29, 23, 59, 59), (2024, 2, 1), (2024, 3, 1), 29),  # leap year
        ((2023, 2, 14), (2023, 2, 1), (2023, 3, 1), 28),
        ((2100, 2, 10), (2100, 2, 1), (2100, 3, 1), 28),  # divisible by 100: not a leap year
        ((2000, 2, 10), (2000, 2, 1), (2000, 3, 1), 29),  # divisible by 400: a leap year
        ((2024, 12, 31, 23, 59, 59), (2024, 12, 1), (2025, 1, 1), 31),  # December to January
        ((2025, 1, 1), (2025, 1, 1), (2025, 2, 1), 31),  # the boundary is in the new month
        ((2024, 4, 30, 12), (2024, 4, 1), (2024, 5, 1), 30),
    ],
)
def test_a_period_is_the_calendar_month_in_utc(
    at: tuple[int, ...], start: tuple[int, ...], end: tuple[int, ...], days: int
) -> None:
    bounds = CalendarMonthsUTC().bounds(ms(*at))
    assert bounds == (ms(*start), ms(*end))
    assert bounds[1] - bounds[0] == days * DAY_MS


def test_the_instants_either_side_of_midnight_on_the_first() -> None:
    months = CalendarMonthsUTC()
    boundary = ms(2025, 1, 1)
    # 0.001 ms is about four times the float resolution at this magnitude.
    for before in (boundary - 1.0, boundary - 0.001):
        assert months.bounds(before) == (ms(2024, 12, 1), boundary), before
    assert months.bounds(boundary)[0] == boundary


def test_fixed_periods_count_from_their_start() -> None:
    periods = FixedPeriods(500.0, 1000.0)
    assert periods.bounds(500.0) == (500.0, 1500.0)
    assert periods.bounds(1499.9) == (500.0, 1500.0)
    assert periods.bounds(1500.0) == (1500.0, 2500.0)
    assert periods.bounds(0.0) == (-500.0, 500.0)


# --- the ledger across a month boundary ----------------------------------------------------


def _result(tokens_in: int = 10, tokens_out: int = 5) -> RequestResult:
    return RequestResult("req-1", "economy", 0.0, 0.0, 1.0, 2.0, tokens_in, tokens_out, 0.1)


@pytest.mark.parametrize(
    "last_second",
    [(2024, 2, 29, 23, 59, 59), (2024, 12, 31, 23, 59, 59), (2023, 2, 28, 23, 59, 59)],
    ids=["leap-february", "december", "february"],
)
async def test_usage_starts_again_on_the_first_of_the_month(last_second: tuple[int, ...]) -> None:
    clock = SimClock()
    epoch = ClockEpoch(clock, origin_ms=ms(*last_second))
    ledger = CostLedger(clock, epoch=epoch)
    ledger.record("a", _result(), model="economy", price_multiplier=1.0)
    old = ledger.usage("a")
    assert old.total.billed_tokens == 15
    # The budget would reset in exactly one second.
    with pytest.raises(Exception) as excinfo:  # noqa: PT011
        ledger.check_budget("a", 15)
    assert getattr(excinfo.value, "retry_after_ms", None) == 1000.0

    await clock.sleep_ms(1000.0)  # midnight UTC on the 1st
    new = ledger.usage("a")
    assert new.period_start_ms == old.period_end_ms == epoch.epoch_ms()
    assert new.total.billed_tokens == 0
    ledger.check_budget("a", 15)


# --- persistent gateways --------------------------------------------------------------------


@dataclass
class Persistent:
    client: httpx.AsyncClient
    clock: SimClock
    store: SqliteUsageStore


TIGHT = PlansConfig(
    plans={"tiny": Plan("tiny", ("economy", "premium"), 1.0, 30)},
    applications={"small": DemoApplication("Small", "tiny")},
)


@asynccontextmanager
async def _persistent(
    path: Path, origin_ms: float, key_seed: int = 0
) -> AsyncIterator[tuple[Persistent, ApiKeyService]]:
    """A gateway whose applications, keys and usage are all in the SQLite file ``path``, as
    the default server's are, on simulated time starting at ``origin_ms``."""
    clock = SimClock()
    epoch = ClockEpoch(clock, origin_ms=origin_ms)
    connection = open_database(path)
    MigrationRunner(connection, MIGRATIONS, epoch).migrate()
    store = SqliteApplicationStore(connection)
    seed_demo_applications(store, TIGHT, created_at_ms=epoch.epoch_ms())
    keys = ApiKeyService(store, TEST_PEPPER, epoch, random_bytes=random.Random(key_seed).randbytes)
    usage = SqliteUsageStore(connection)
    registry = build_continuous_registry(clock, seed=7)
    app = create_app(
        registry, clock, applications=ApplicationAccess(keys, TIGHT), usage_store=usage, epoch=epoch
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        yield Persistent(client, clock, usage), keys
    await registry.aclose()
    connection.close()


async def _generate(gw: Persistent, key: str, prompt: str, **body: Any) -> httpx.Response:
    body = {"model": "economy", "prompt": prompt, "max_tokens": 5, **body}
    return await gw.client.post("/v1/generate", json=body, headers=bearer(key))


async def test_usage_and_budgets_survive_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "gateway.db"
    origin = ms(2025, 3, 10)
    async with _persistent(path, origin) as (gw, keys):
        key = keys.issue("small", "k").key
        # Fifteen-word prompts that differ from their first word: each bills at least 15.
        first = [(await _generate(gw, key, f"{i} {WORDS}")).status_code for i in range(3)]
        used = (await gw.client.get("/v1/applications/me/usage", headers=bearer(key))).json()

    # A new process: new clock, new connection, same file, same month.
    async with _persistent(path, origin + 60_000.0) as (gw, _):
        after = (await gw.client.get("/v1/applications/me/usage", headers=bearer(key))).json()
        refused = await _generate(gw, key, "another prompt entirely")

    assert first == [200, 200, 429]  # budget 30: checked at 0, then at 30 or more
    assert after["total"] == used["total"]
    assert after["used_tokens"] >= 30
    assert refused.status_code == 429
    assert refused.json()["error"]["code"] == "budget_exhausted"


async def test_the_default_server_stores_usage_in_its_database() -> None:
    key = default_server_key("acme")
    body = {"model": "economy", "prompt": "hello there", "max_tokens": 1}
    for _ in range(2):  # two processes, one database file
        transport = httpx.ASGITransport(app=create_default_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
            assert (
                await client.post("/v1/generate", json=body, headers=bearer(key))
            ).status_code == 200
            usage = (await client.get("/v1/applications/me/usage", headers=bearer(key))).json()
    assert usage["total"]["requests"] == 2
    start = datetime.datetime.fromtimestamp(usage["period"]["start_ms"] / 1000, tz=UTC)
    assert (start.day, start.hour, start.minute, start.second) == (1, 0, 0, 0)


async def test_a_request_that_straddles_the_month_is_charged_to_the_month_it_finishes_in(
    tmp_path: Path,
) -> None:
    january = ms(2025, 1, 1)
    # Half a second before midnight on 31 December.
    async with _persistent(tmp_path / "gateway.db", january - 500.0) as (gw, keys):
        key = keys.issue("small", "k").key
        long = await _generate(gw, key, "a long answer please", model="premium", max_tokens=200)
        finished_ms = january - 500.0 + gw.clock.now_ms()
        usage = (await gw.client.get("/v1/applications/me/usage", headers=bearer(key))).json()
        events = gw.store.events("small")
        december = gw.store.usage_by_model("small", ms(2024, 12, 1), january)

    # Admitted in December (checked against December's budget), finished in January.
    assert long.status_code == 200
    assert finished_ms > january
    # Charged when it finished: the event is dated January and counts against January.
    [event] = events
    assert event.event.occurred_at_ms >= january
    assert usage["period"]["start_ms"] == january
    assert usage["total"]["requests"] == 1
    assert december == {}


async def test_concurrent_requests_all_pass_the_check_and_overshoot_together(
    tmp_path: Path,
) -> None:
    async with _persistent(tmp_path / "gateway.db", ms(2025, 6, 1)) as (gw, keys):
        key = keys.issue("small", "k").key
        answers = await asyncio.gather(
            *(_generate(gw, key, f"{i} distinct prompt words here") for i in range(4))
        )
        used = (await gw.client.get("/v1/applications/me/usage", headers=bearer(key))).json()
    # The soft budget, unchanged by persistence: checked on arrival, charged on finish.
    assert [a.status_code for a in answers] == [200] * 4
    assert used["used_tokens"] > 30


# --- retention ------------------------------------------------------------------------------


def _event(at_ms: float, app: str = "a") -> UsageEvent:
    return UsageEvent(app, "req-x", "economy", 10, 0, 5, 0.1, 0.1, at_ms)


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
async def test_events_older_than_the_retention_are_pruned_at_most_hourly(
    tmp_path: Path, store_kind: str
) -> None:
    now = ms(2025, 5, 20)
    clock = SimClock()
    if store_kind == "memory":
        store: InMemoryUsageStore | SqliteUsageStore = InMemoryUsageStore()
    else:
        connection = open_database(tmp_path / "gateway.db")
        MigrationRunner(connection, MIGRATIONS, ClockEpoch(clock)).migrate()
        store = SqliteUsageStore(connection)
    for at in (now - 100 * DAY_MS, now - 50 * DAY_MS, now - 10 * DAY_MS):
        store.append(_event(at))
    ledger = CostLedger(clock, store=store, epoch=ClockEpoch(clock, now), retention_ms=40 * DAY_MS)

    ledger.record("a", _result(), model="economy", price_multiplier=1.0)  # prunes: first time
    assert [round((now - e.event.occurred_at_ms) / DAY_MS) for e in store.events()] == [10, 0]

    store.append(_event(now - 90 * DAY_MS))
    await clock.sleep_ms(PRUNE_EVERY_MS - 1.0)
    ledger.record("a", _result(), model="economy", price_multiplier=1.0)
    assert len(store.events()) == 4  # not due yet
    await clock.sleep_ms(1.0)
    ledger.record("a", _result(), model="economy", price_multiplier=1.0)
    assert len(store.events()) == 4  # the 90-day-old one went, the new charge arrived
    assert min(e.event.occurred_at_ms for e in store.events()) >= now - 40 * DAY_MS


def test_the_current_period_is_kept_whatever_the_retention() -> None:
    clock = SimClock()
    now = ms(2025, 5, 31, 12)
    store = InMemoryUsageStore()
    store.append(_event(ms(2025, 5, 1)))  # thirty days ago, but this month
    store.append(_event(ms(2025, 4, 30)))  # last month
    ledger = CostLedger(clock, store=store, epoch=ClockEpoch(clock, now), retention_ms=1.0)
    assert ledger.prune() == 1
    assert [e.event.occurred_at_ms for e in store.events()] == [ms(2025, 5, 1)]
    assert ledger.usage("a").total.requests == 1


def test_without_a_retention_nothing_is_pruned() -> None:
    store = InMemoryUsageStore()
    store.append(_event(0.0))
    assert CostLedger(SimClock(), store=store).prune() == 0
    assert len(store.events()) == 1


def test_retention_is_configured_in_days() -> None:
    assert usage_retention_ms({}) == DEFAULT_USAGE_RETENTION_DAYS * DAY_MS
    assert usage_retention_ms({"GATEWAY_USAGE_RETENTION_DAYS": "90"}) == 90 * DAY_MS
    for bad in ("0", "-3", "nan"):
        with pytest.raises(ValueError, match="RETENTION_DAYS"):
            usage_retention_ms({"GATEWAY_USAGE_RETENTION_DAYS": bad})
    with pytest.raises(ValueError, match="retention_ms"):
        CostLedger(SimClock(), retention_ms=0.0)
