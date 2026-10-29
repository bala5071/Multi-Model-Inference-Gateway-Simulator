"""Organizations: names, the membership rule, both stores, disabled organizations, and two
organizations that never mix. See docs/26-organizations.md."""

from __future__ import annotations

import random
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from gateway.apikeys import (
    INVALID_KEY_MESSAGE,
    ApiKeyService,
    ApplicationAccess,
    InvalidApiKeyError,
)
from gateway.app import create_app
from gateway.applications import (
    ApplicationStore,
    InMemoryApplicationStore,
    SqliteApplicationStore,
    application_from_template,
)
from gateway.clock import ClockEpoch, SimClock
from gateway.continuous import build_continuous_registry
from gateway.database import open_database
from gateway.ledger import CostLedger
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.organizations import (
    DEFAULT_ORGANIZATION_ID,
    MAX_NAME_LENGTH,
    MembershipDecision,
    MembershipRefusal,
    OrganizationKind,
    membership_decision,
    new_organization,
    normalize_name,
)
from gateway.routing import Plan, PlansConfig
from gateway.schema import RequestResult
from gateway.usage import InMemoryUsageStore, SqliteUsageStore, UsageStore
from helpers import TEST_PEPPER, bearer

# Two plans: a tight budget, so one organization's application can run out while the other's
# does not.
PLANS = PlansConfig(
    plans={
        "tiny": Plan("tiny", ("economy",), 1.0, 30),
        "roomy": Plan("roomy", ("economy",), 1.0, 1_000_000),
    },
    applications={},
)


# --- names ----------------------------------------------------------------------------------


def test_a_name_is_normalized_to_nfc_and_then_compared_exactly() -> None:
    decomposed = "Cafe\u0301"  # "e" and a combining acute accent
    assert normalize_name(decomposed) == "Caf\u00e9"
    assert normalize_name("Acme") != normalize_name("ACME")  # no case folding
    assert normalize_name("x" * MAX_NAME_LENGTH) == "x" * MAX_NAME_LENGTH


@pytest.mark.parametrize(
    ("name", "why"),
    [
        ("", "empty"),
        (" Acme", "whitespace"),
        ("Acme ", "whitespace"),
        ("Acme\u00a0", "whitespace"),  # a no-break space is whitespace too
        ("x" * (MAX_NAME_LENGTH + 1), "at most"),
        ("Ac\tme", "U\\+0009"),  # a control character
        ("Ac\u0000me", "U\\+0000"),
        ("Ac\u200bme", "U\\+200B"),  # zero-width space
        ("Ac\u200dme", "U\\+200D"),  # zero-width joiner
        ("Ac\ufeffme", "U\\+FEFF"),  # zero-width no-break space
        ("Ac\u202eme", "U\\+202E"),  # right-to-left override
        ("Ac\ue000me", "U\\+E000"),  # private use
    ],
)
def test_unacceptable_names_are_refused(name: str, why: str) -> None:
    with pytest.raises(ValueError, match=why):
        normalize_name(name)


def test_look_alike_names_are_not_prevented() -> None:
    # "Acme" in Latin letters, and with a Cyrillic first letter: different names.
    latin, cyrillic = "Acme", "\u0410cme"
    assert normalize_name(latin) != normalize_name(cyrillic)


def test_a_new_organization_has_a_known_kind_and_a_checked_name() -> None:
    made = new_organization("north", "Cafe\u0301 North", "individual", created_at_ms=3.0)
    assert (made.kind, made.name, made.disabled) == (
        OrganizationKind.INDIVIDUAL,
        "Caf\u00e9 North",
        False,
    )
    with pytest.raises(ValueError, match="unknown kind"):
        new_organization("x", "X", "household", created_at_ms=0.0)
    with pytest.raises(ValueError, match="id"):
        new_organization("", "X", "business", created_at_ms=0.0)


# --- who may join ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "members", "decision"),
    [
        (OrganizationKind.INDIVIDUAL, 0, MembershipDecision(True)),
        (
            OrganizationKind.INDIVIDUAL,
            1,
            MembershipDecision(False, MembershipRefusal.INDIVIDUAL_HAS_A_MEMBER),
        ),
        (
            OrganizationKind.INDIVIDUAL,
            2,
            MembershipDecision(False, MembershipRefusal.INDIVIDUAL_HAS_A_MEMBER),
        ),
        (OrganizationKind.BUSINESS, 0, MembershipDecision(True)),
        (OrganizationKind.BUSINESS, 1, MembershipDecision(True)),
        (OrganizationKind.BUSINESS, 2, MembershipDecision(True)),
        (OrganizationKind.BUSINESS, 10_000, MembershipDecision(True)),
    ],
)
def test_an_individual_organization_takes_exactly_one_member(
    kind: OrganizationKind, members: int, decision: MembershipDecision
) -> None:
    assert membership_decision(kind, members) == decision


def test_a_negative_member_count_is_an_error() -> None:
    with pytest.raises(ValueError, match="negative"):
        membership_decision(OrganizationKind.BUSINESS, -1)


# --- the stores -----------------------------------------------------------------------------


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[ApplicationStore]:
    if request.param == "memory":
        yield InMemoryApplicationStore()
        return
    connection = open_database(tmp_path / "gateway.db")
    MigrationRunner(connection, MIGRATIONS, ClockEpoch(SimClock())).migrate()
    yield SqliteApplicationStore(connection)
    connection.close()


async def _two_organizations(store: ApplicationStore, plan: str = "roomy") -> None:
    """North and South, each with an application named Shop."""
    for org_id, name in (("north", "North"), ("south", "South")):
        await store.add_organization(new_organization(org_id, name, "business", created_at_ms=1.0))
        await store.add_application(
            application_from_template(
                f"{org_id}-shop", "Shop", PLANS, plan, organization_id=org_id, created_at_ms=2.0
            )
        )


async def test_every_store_starts_with_the_default_organization(store: ApplicationStore) -> None:
    [default] = await store.list_organizations()
    assert (default.id, default.name, default.kind, default.disabled) == (
        DEFAULT_ORGANIZATION_ID,
        "default",
        OrganizationKind.BUSINESS,
        False,
    )


async def test_organizations_round_trip_and_their_ids_and_names_are_unique(
    store: ApplicationStore,
) -> None:
    north = new_organization("north", "Caf\u00e9", "individual", created_at_ms=5.0)
    await store.add_organization(north)
    assert await store.get_organization("north") == north
    assert await store.find_organization("Cafe\u0301") == north  # found by its NFC form
    assert await store.find_organization("caf\u00e9") is None  # no case folding
    assert [o.id for o in await store.list_organizations()] == [DEFAULT_ORGANIZATION_ID, "north"]
    with pytest.raises(ValueError, match="exists"):
        await store.add_organization(
            new_organization("north", "Other", "business", created_at_ms=0)
        )
    with pytest.raises(ValueError, match="already named"):
        await store.add_organization(
            new_organization("other", "Cafe\u0301", "business", created_at_ms=0)
        )


async def test_an_organization_can_be_disabled_and_enabled(store: ApplicationStore) -> None:
    await store.add_organization(new_organization("north", "North", "business", created_at_ms=0.0))
    await store.set_organization_disabled("north", True)
    assert (await store.get_organization("north")).disabled is True  # type: ignore[union-attr]
    await store.set_organization_disabled("north", False)
    assert (await store.get_organization("north")).disabled is False  # type: ignore[union-attr]
    with pytest.raises(KeyError):
        await store.set_organization_disabled("nobody", True)


async def test_names_are_unique_within_an_organization_not_across(store: ApplicationStore) -> None:
    await _two_organizations(store)
    north = await store.list_applications(organization_id="north")
    south = await store.list_applications(organization_id="south")
    assert [(a.id, a.name) for a in north] == [("north-shop", "Shop")]
    assert [(a.id, a.name) for a in south] == [("south-shop", "Shop")]
    assert await store.list_applications(organization_id=DEFAULT_ORGANIZATION_ID) == []
    assert [a.id for a in await store.list_applications()] == ["north-shop", "south-shop"]
    with pytest.raises(ValueError, match="already has an application named 'Shop'"):
        await store.add_application(
            application_from_template(
                "north-shop-2", "Shop", PLANS, "roomy", organization_id="north", created_at_ms=0
            )
        )
    # Different case is a different name.
    await store.add_application(
        application_from_template(
            "north-shop-3", "SHOP", PLANS, "roomy", organization_id="north", created_at_ms=0
        )
    )


async def test_an_application_needs_an_enabled_organization_and_a_normalized_name(
    store: ApplicationStore,
) -> None:
    async def make(org: str, app_id: str = "x") -> None:
        await store.add_application(
            application_from_template(
                app_id, "X", PLANS, "roomy", organization_id=org, created_at_ms=0
            )
        )

    with pytest.raises(ValueError, match="no organization 'nobody'"):
        await make("nobody")
    await store.add_organization(new_organization("north", "North", "business", created_at_ms=0.0))
    await store.set_organization_disabled("north", True)
    with pytest.raises(ValueError, match="disabled"):
        await make("north")
    await store.set_organization_disabled("north", False)
    await make("north")
    # A name that bypasses application_from_template must already be normalized.
    raw = application_from_template(
        "y", "Y", PLANS, "roomy", organization_id="north", created_at_ms=0
    )
    with pytest.raises(ValueError, match="NFC"):
        await store.add_application(replace(raw, name="Cafe\u0301"))


# --- keys of a disabled organization -------------------------------------------------------


def _keys(store: ApplicationStore, clock: SimClock) -> ApiKeyService:
    return ApiKeyService(
        store, TEST_PEPPER, ClockEpoch(clock), random_bytes=random.Random(3).randbytes
    )


async def test_a_disabled_organizations_keys_stop_working_and_others_do_not(
    store: ApplicationStore,
) -> None:
    await _two_organizations(store)
    keys = _keys(store, SimClock())
    north = (await keys.issue("north-shop", "n")).key
    south = (await keys.issue("south-shop", "s")).key
    await store.set_organization_disabled("north", True)
    with pytest.raises(InvalidApiKeyError) as excinfo:
        await keys.authenticate(north)
    assert str(excinfo.value) == INVALID_KEY_MESSAGE  # the same answer as any bad key
    assert (await keys.authenticate(south)).id == "south-shop"
    # Existing data stays: enabling it again brings the same key back.
    await store.set_organization_disabled("north", False)
    assert (await keys.authenticate(north)).id == "north-shop"


async def test_the_key_lookup_brings_the_application_and_its_organization_state(
    store: ApplicationStore,
) -> None:
    await _two_organizations(store)
    keys = _keys(store, SimClock())
    record = (await keys.issue("north-shop", "n")).record
    found = await store.lookup_key(record.key_id)
    assert found is not None
    assert (found.record, found.application, found.organization_enabled) == (
        record,
        await store.get_application("north-shop"),
        True,
    )
    await store.set_organization_disabled("north", True)
    assert (await store.lookup_key(record.key_id)).organization_enabled is False  # type: ignore[union-attr]
    assert await store.lookup_key("0" * 16) is None


# --- usage and budgets never mix -----------------------------------------------------------


def _result(tokens_in: int, tokens_out: int) -> RequestResult:
    return RequestResult("req", "economy", 0.0, 0.0, 1.0, 2.0, tokens_in, tokens_out, 0.5)


@pytest.fixture(params=["memory", "sqlite"])
def usage_store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[UsageStore]:
    if request.param == "memory":
        yield InMemoryUsageStore()
        return
    connection = open_database(tmp_path / "usage.db")
    MigrationRunner(connection, MIGRATIONS, ClockEpoch(SimClock())).migrate()
    yield SqliteUsageStore(connection)
    connection.close()


async def test_usage_rolls_up_per_organization_and_never_mixes(
    store: ApplicationStore, usage_store: UsageStore
) -> None:
    await _two_organizations(store)
    await store.add_application(
        application_from_template(
            "north-api", "API", PLANS, "tiny", organization_id="north", created_at_ms=0
        )
    )
    clock = SimClock()
    ledger = CostLedger(clock, store=usage_store)
    for app_id, tokens_in, tokens_out in (
        ("north-shop", 10, 5),
        ("north-api", 7, 3),
        ("south-shop", 100, 50),
        ("north-shop", 1, 1),
    ):
        await ledger.record(
            app_id, _result(tokens_in, tokens_out), model="economy", price_multiplier=2
        )

    async def budgets(org: str) -> dict[str, int]:
        apps = await store.list_applications(organization_id=org)
        return {a.id: a.plan.monthly_token_budget for a in apps}

    north = await ledger.organization_usage("north", await budgets("north"))
    south = await ledger.organization_usage("south", await budgets("south"))
    assert [(s.application_id, s.used_tokens, s.budget_tokens) for s in north.applications] == [
        ("north-api", 10, 30),
        ("north-shop", 17, 1_000_000),
    ]
    assert (north.total.requests, north.total.billed_tokens, north.budget_tokens) == (
        3,
        27,
        1_000_030,
    )
    assert [(s.application_id, s.used_tokens) for s in south.applications] == [("south-shop", 150)]
    assert (south.total.requests, south.total.billed_tokens) == (1, 150)
    assert north.by_model["economy"].billed_cost_usd == pytest.approx(3 * 0.5 * 2)
    # Same name, same period: still two applications, two organizations.
    assert (await ledger.usage("north-shop")).total.billed_tokens == 17
    assert (await ledger.usage("south-shop")).total.billed_tokens == 150
    # The store query gives the same totals as the ledger's roll-up.
    start, end = north.period_start_ms, north.period_end_ms
    assert (
        await usage_store.usage_by_model_for(await budgets("north"), start, end) == north.by_model
    )
    assert (
        await usage_store.usage_by_model_for(await budgets("south"), start, end) == south.by_model
    )
    assert await usage_store.usage_by_model_for([], start, end) == {}
    assert (await ledger.organization_usage(DEFAULT_ORGANIZATION_ID, {})).total.requests == 0


async def test_one_organizations_exhausted_budget_does_not_touch_the_others(
    store: ApplicationStore,
) -> None:
    await _two_organizations(store, plan="tiny")
    ledger = CostLedger(SimClock())
    await ledger.record("north-shop", _result(20, 10), model="economy", price_multiplier=1.0)
    with pytest.raises(Exception, match="north-shop"):
        await ledger.check_budget("north-shop", 30)
    await ledger.check_budget("south-shop", 30)


# --- over HTTP ------------------------------------------------------------------------------


@asynccontextmanager
async def _gateway(
    tmp_path: Path,
) -> AsyncIterator[tuple[httpx.AsyncClient, dict[str, str], SqliteApplicationStore]]:
    clock = SimClock()
    epoch = ClockEpoch(clock, origin_ms=1.75e12)
    connection = open_database(tmp_path / "gateway.db")
    MigrationRunner(connection, MIGRATIONS, epoch).migrate()
    store = SqliteApplicationStore(connection)
    await _two_organizations(store, plan="tiny")
    keys = ApiKeyService(store, TEST_PEPPER, epoch, random_bytes=random.Random(9).randbytes)
    issued = {app: (await keys.issue(app, "test")).key for app in ("north-shop", "south-shop")}
    registry = build_continuous_registry(clock, seed=7)
    app = create_app(
        registry,
        clock,
        applications=ApplicationAccess(keys, PLANS),
        usage_store=SqliteUsageStore(connection),
        epoch=epoch,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        yield client, issued, store
    await registry.aclose()
    connection.close()


USAGE_KEYS = {
    "application_id",
    "name",
    "plan",
    "allowed_models",
    "price_multiplier",
    "max_tokens",
    "requests_per_minute",
    "period",
    "budget_tokens",
    "used_tokens",
    "remaining_tokens",
    "total",
    "by_model",
}


async def test_two_organizations_over_http_never_mix(tmp_path: Path) -> None:
    words = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron"
    # Each prompt differs from its first word, so the prefix cache cannot make it cheaper.
    bodies = [{"model": "economy", "prompt": f"{i} {words}", "max_tokens": 5} for i in range(9)]
    async with _gateway(tmp_path) as (client, keys, store):
        north, south = bearer(keys["north-shop"]), bearer(keys["south-shop"])
        served = [
            (await client.post("/v1/generate", json=bodies[i], headers=north)).status_code
            for i in range(3)
        ]
        south_served = (
            await client.post("/v1/generate", json=bodies[8], headers=south)
        ).status_code
        north_usage = (await client.get("/v1/applications/me/usage", headers=north)).json()
        south_usage = (await client.get("/v1/applications/me/usage", headers=south)).json()

        await store.set_organization_disabled("north", True)
        disabled = await client.post("/v1/generate", json=bodies[5], headers=north)
        bad = await client.post("/v1/generate", json=bodies[6], headers=bearer("gwk_nonsense"))
        still = (await client.post("/v1/generate", json=bodies[7], headers=south)).status_code

    # North's Shop runs out of its budget of 30; South's Shop, with the same name, does not.
    assert served == [200, 200, 429]
    assert south_served == 200
    # The usage endpoint keeps its meaning and its shape: one application, no organization.
    assert set(north_usage) == set(south_usage) == USAGE_KEYS
    assert (north_usage["application_id"], north_usage["name"]) == ("north-shop", "Shop")
    assert (south_usage["application_id"], south_usage["name"]) == ("south-shop", "Shop")
    assert north_usage["total"]["requests"] == 2
    assert south_usage["total"]["requests"] == 1
    # A disabled organization's key gets exactly what any bad key gets.
    assert disabled.status_code == bad.status_code == 401
    assert disabled.json() == bad.json()
    assert still == 200
