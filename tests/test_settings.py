"""Per-application settings (docs/39): the catalogue, the four levels and the stricter one
winning, judging a stored row, the stores on memory, SQLite and Postgres, versions and the
one-step revert, and the write rules on one instance and on two.

The request path (missing, unreadable and invalid rows, each setting applied, the header) is in
``test_settings_api.py``."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import Any

import pytest

from gateway.applications import (
    Application,
    ApplicationStore,
    InMemoryApplicationStore,
    SqliteApplicationStore,
    application_from_template,
)
from gateway.audit import AuditAction, AuditEntry, AuditEvent, AuditLog, SqliteAuditStore
from gateway.clock import SimClock, SystemEpoch
from gateway.database import open_database
from gateway.management import ManagementRefusal
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.routing import load_plans
from gateway.settings import (
    CATALOGUE,
    SETTINGS_SCHEMA_VERSION,
    VERSIONS_KEPT,
    DocumentCache,
    PlatformSettings,
    Refusal,
    Scope,
    SettingsInvalidError,
    SettingsRow,
    SettingsStore,
    SettingsUnreadableError,
    SettingsVersionConflictError,
    Source,
    SqliteSettingsStore,
    StoredSettings,
    canonical,
    effective_application,
    effective_settings,
    parse_row,
    refusals,
    resolve,
    validate_document,
)
from gateway.settingsservice import SettingsService
from gateway.state import StateUnavailableError

postgres = pytest.mark.postgres
PLANS = load_plans()
TIERS = ("economy", "standard", "premium")
PLATFORM = PlatformSettings(
    tiers=TIERS,
    reserved_model_names=frozenset((*TIERS, "llama-3.2-1b", "llama-3.2-3b", "llama-3.1-8b")),
    generate_timeout_ms=120_000.0,
    chat_first_token_ms=30_000.0,
    chat_total_timeout_ms=120_000.0,
)


def _application(
    plan: str = "enterprise",
    *,
    max_tokens: int | None = None,
    rpm: int | None = None,
    app_id: str = "shop",
    organization_id: str = "default",
) -> Application:
    made = application_from_template(
        app_id, app_id.title(), PLANS, plan, organization_id=organization_id, created_at_ms=1.0
    )
    return replace(made, max_tokens=max_tokens, requests_per_minute=rpm)


# --- the catalogue and validation (no store) ------------------------------------------------------


def test_the_catalogue_is_s1_to_s11_in_the_plans_order() -> None:
    assert list(CATALOGUE) == [
        "allowed_tiers",
        "model_aliases",
        "default_quality",
        "allow_downgrade_default",
        "requests_per_minute",
        "monthly_token_budget",
        "max_tokens",
        "default_max_tokens",
        "generate_timeout_ms",
        "chat_first_token_ms",
        "chat_total_timeout_ms",
    ]


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("allowed_tiers", []),
        ("allowed_tiers", ["economy", "economy"]),
        ("allowed_tiers", "economy"),
        ("allowed_tiers", ["Economy!"]),
        ("model_aliases", {}),
        ("model_aliases", {"UPPER": "economy"}),
        ("model_aliases", {f"a{i}": "economy" for i in range(17)}),
        ("model_aliases", {"fast": 3}),
        ("default_quality", "fastest"),
        ("default_quality", None),
        ("allow_downgrade_default", 1),
        ("requests_per_minute", 0),
        ("requests_per_minute", 1_000_001),
        ("requests_per_minute", 5.0),
        ("requests_per_minute", True),
        ("requests_per_minute", None),
        ("monthly_token_budget", -1),
        ("max_tokens", 0),
        ("default_max_tokens", 0),
        ("generate_timeout_ms", 999),
        ("generate_timeout_ms", 3_600_001),
        ("chat_first_token_ms", 999),
        ("chat_total_timeout_ms", "120000"),
        ("no_such_setting", 1),
    ],
)
def test_a_value_outside_its_type_or_range_is_refused(name: str, value: object) -> None:
    with pytest.raises(SettingsInvalidError) as refused:
        validate_document(Scope.APPLICATION, {name: value})
    assert refused.value.setting == name


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("allowed_tiers", ["economy"]),
        ("model_aliases", {"fast": "economy"}),
        ("default_quality", "best"),
        ("allow_downgrade_default", True),
        ("requests_per_minute", 1),
        ("requests_per_minute", 1_000_000),
        ("monthly_token_budget", 0),
        ("max_tokens", 1),
        ("default_max_tokens", 1),
        ("generate_timeout_ms", 1_000),
        ("chat_first_token_ms", 3_600_000),
        ("chat_total_timeout_ms", 1_000),
    ],
)
def test_a_value_inside_its_range_is_kept(name: str, value: object) -> None:
    assert validate_document(Scope.APPLICATION, {name: value}) == {name: value}


@pytest.mark.parametrize(
    "name",
    ["model_aliases", "default_quality", "allow_downgrade_default", "default_max_tokens"],
)
def test_an_organization_cannot_set_an_application_only_setting(name: str) -> None:
    value = {"model_aliases": {"x": "economy"}, "default_quality": "best"}.get(name, 1)
    if name == "allow_downgrade_default":
        value = True
    with pytest.raises(SettingsInvalidError):
        validate_document(Scope.ORGANIZATION, {name: value})


def test_the_first_token_may_not_wait_longer_than_the_whole_answer() -> None:
    with pytest.raises(SettingsInvalidError):
        validate_document(
            Scope.APPLICATION, {"chat_first_token_ms": 5_000, "chat_total_timeout_ms": 4_000}
        )


def test_documents_are_canonical_text() -> None:
    assert canonical({"max_tokens": 5, "allowed_tiers": ["economy"]}) == (
        '{"allowed_tiers":["economy"],"max_tokens":5}'
    )


# --- the four levels: the stricter wins (no store) ------------------------------------------------


def test_with_nothing_set_every_value_is_todays() -> None:
    app = _application("pro", max_tokens=None, rpm=None)
    e = effective_settings(PLATFORM, app, {}, {})
    assert e.allowed_tiers == ("economy", "standard")
    assert e.model_aliases == {}
    assert e.default_quality is None
    assert e.allow_downgrade_default is False
    assert e.requests_per_minute is None
    assert e.monthly_token_budget == PLANS.plans["pro"].monthly_token_budget
    assert e.max_tokens is None
    assert e.default_max_tokens is None
    assert e.generate_timeout_ms == 120_000.0
    assert (e.chat_first_token_ms, e.chat_total_timeout_ms) == (30_000.0, 120_000.0)
    assert e.explain["monthly_token_budget"].source is Source.PLAN
    assert e.explain["max_tokens"].source is Source.DEFAULT
    assert e.explain["generate_timeout_ms"].source is Source.PLATFORM
    # The request's view of the application is the application as stored.
    assert effective_application(app, e) == app


# (plan copy rpm, organization cap, application value) -> (effective, source, capped_by)
RPM_CASES = [
    (None, None, None, (None, Source.DEFAULT, None)),
    (100, None, None, (100, Source.PLAN, None)),
    (100, 50, None, (50, Source.ORGANIZATION, None)),
    (100, None, 20, (20, Source.APPLICATION, None)),
    (100, 50, 20, (20, Source.APPLICATION, None)),
    (100, 10, 20, (10, Source.APPLICATION, Source.ORGANIZATION)),
    (100, None, 500, (100, Source.APPLICATION, Source.PLAN)),
    (None, 50, 500, (50, Source.APPLICATION, Source.ORGANIZATION)),
    (None, None, 500, (500, Source.APPLICATION, None)),
]


@pytest.mark.parametrize(("plan", "org", "own", "expected"), RPM_CASES)
def test_each_level_lowers_a_lower_is_stricter_value(
    plan: int | None, org: int | None, own: int | None, expected: tuple[Any, ...]
) -> None:
    e = effective_settings(
        PLATFORM,
        _application(rpm=plan),
        {"requests_per_minute": org} if org is not None else {},
        {"requests_per_minute": own} if own is not None else {},
    )
    found = e.explain["requests_per_minute"]
    assert (e.requests_per_minute, found.source, found.capped_by) == expected


def test_a_cap_tightened_after_a_value_was_set_clamps_it_and_keeps_it() -> None:
    """Read time: the organization's later, stricter cap wins; the application's stored value
    is kept (it comes back if the cap is loosened), and the view says what capped it."""
    app = _application("enterprise")
    own = {"monthly_token_budget": 5_000, "allowed_tiers": ["economy", "premium"]}
    org = {"monthly_token_budget": 1_000, "allowed_tiers": ["economy", "standard"]}
    e = effective_settings(PLATFORM, app, org, own)
    assert e.monthly_token_budget == 1_000
    assert e.explain["monthly_token_budget"] == replace(
        e.explain["monthly_token_budget"], source=Source.APPLICATION, capped_by=Source.ORGANIZATION
    )
    assert e.allowed_tiers == ("economy",)
    assert e.explain["allowed_tiers"].capped_by is Source.ORGANIZATION
    loosened = effective_settings(PLATFORM, app, {}, own)
    assert loosened.monthly_token_budget == 5_000
    assert loosened.allowed_tiers == ("economy", "premium")


def test_tiers_keep_the_plans_order_and_never_leave_the_plan() -> None:
    e = effective_settings(
        PLATFORM, _application("pro"), {}, {"allowed_tiers": ["standard", "premium", "economy"]}
    )
    assert e.allowed_tiers == ("economy", "standard")
    assert e.explain["allowed_tiers"].capped_by is Source.PLAN


def test_default_max_tokens_follows_max_tokens() -> None:
    app = _application(max_tokens=100)
    assert effective_settings(PLATFORM, app, {}, {}).default_max_tokens == 100
    assert (
        effective_settings(PLATFORM, app, {}, {"default_max_tokens": 40}).default_max_tokens == 40
    )
    capped = effective_settings(PLATFORM, app, {"max_tokens": 30}, {"default_max_tokens": 40})
    assert (capped.max_tokens, capped.default_max_tokens) == (30, 30)
    # No limit anywhere: none is applied, as today.
    assert effective_settings(PLATFORM, _application(), {}, {}).default_max_tokens is None
    assert (
        effective_settings(
            PLATFORM, _application(), {}, {"default_max_tokens": 7}
        ).default_max_tokens
        == 7
    )


def test_timeouts_never_exceed_the_platform_and_the_first_token_never_the_total() -> None:
    app = _application()
    e = effective_settings(
        PLATFORM, app, {"chat_total_timeout_ms": 10_000}, {"generate_timeout_ms": 500_000}
    )
    assert e.generate_timeout_ms == 120_000.0
    assert e.explain["generate_timeout_ms"].capped_by is Source.PLATFORM
    assert (e.chat_first_token_ms, e.chat_total_timeout_ms) == (10_000.0, 10_000.0)
    none = replace(PLATFORM, generate_timeout_ms=None)
    assert effective_settings(none, app, {}, {}).generate_timeout_ms is None
    assert (
        effective_settings(none, app, {}, {"generate_timeout_ms": 5_000}).generate_timeout_ms
        == 5_000
    )


def test_an_alias_outside_the_allowed_tiers_or_on_a_registry_name_is_not_in_force() -> None:
    e = effective_settings(
        PLATFORM,
        _application("pro"),
        {},
        {"model_aliases": {"fast": "economy", "big": "premium", "economy": "standard"}},
    )
    assert dict(e.model_aliases) == {"fast": "economy"}
    assert set(e.ignored_aliases) == {"big", "economy"}


# --- write-time refusals (no store) ---------------------------------------------------------------


def test_an_application_value_looser_than_a_cap_is_refused_at_each_level() -> None:
    app = _application("pro", max_tokens=100, rpm=60)
    org = {"requests_per_minute": 30, "allowed_tiers": ["economy"], "chat_total_timeout_ms": 9_000}
    cases: list[tuple[dict[str, object], Refusal]] = [
        ({"requests_per_minute": 61}, Refusal("requests_per_minute", Source.PLAN)),
        ({"requests_per_minute": 31}, Refusal("requests_per_minute", Source.ORGANIZATION)),
        ({"max_tokens": 101}, Refusal("max_tokens", Source.PLAN)),
        ({"monthly_token_budget": 10**12}, Refusal("monthly_token_budget", Source.PLAN)),
        ({"allowed_tiers": ["premium"]}, Refusal("allowed_tiers", Source.PLAN)),
        ({"allowed_tiers": ["standard"]}, Refusal("allowed_tiers", Source.ORGANIZATION)),
        ({"generate_timeout_ms": 120_001}, Refusal("generate_timeout_ms", Source.PLATFORM)),
        ({"chat_total_timeout_ms": 9_001}, Refusal("chat_total_timeout_ms", Source.ORGANIZATION)),
        ({"default_max_tokens": 101}, Refusal("default_max_tokens", Source.PLAN)),
        ({"chat_first_token_ms": 9_500}, Refusal("chat_first_token_ms", Source.ORGANIZATION)),
        ({"model_aliases": {"llama-3.2-1b": "economy"}}, Refusal("model_aliases", Source.PLATFORM)),
        ({"model_aliases": {"fast": "standard"}}, Refusal("model_aliases", Source.ORGANIZATION)),
    ]
    for document, refusal in cases:
        checked = validate_document(Scope.APPLICATION, document)
        found = refusals(PLATFORM, Scope.APPLICATION, checked, application=app, organization=org)
        assert found == [refusal], document
    # As strict as every cap, or stricter: nothing to refuse.
    fine = {"requests_per_minute": 30, "max_tokens": 100, "allowed_tiers": ["economy"]}
    assert refusals(PLATFORM, Scope.APPLICATION, fine, application=app, organization=org) == []


def test_an_organization_cap_looser_than_the_platform_is_refused() -> None:
    assert refusals(PLATFORM, Scope.ORGANIZATION, {"generate_timeout_ms": 200_000}) == [
        Refusal("generate_timeout_ms", Source.PLATFORM)
    ]
    assert refusals(PLATFORM, Scope.ORGANIZATION, {"allowed_tiers": ["huge"]}) == [
        Refusal("allowed_tiers", Source.PLATFORM)
    ]
    # An organization cap above one plan is simply not the strictest for that plan's apps.
    assert refusals(PLATFORM, Scope.ORGANIZATION, {"requests_per_minute": 1_000_000}) == []


# --- judging a stored row (no store) --------------------------------------------------------------


def _row(document: str, *, scope: Scope = Scope.APPLICATION, schema: int = 1) -> SettingsRow:
    return SettingsRow(scope, "shop", 3, document, schema, 1.0)


@pytest.mark.parametrize(
    ("document", "schema", "check"),
    [
        ('{"max_tokens":5', SETTINGS_SCHEMA_VERSION, "json"),
        ("[1,2]", SETTINGS_SCHEMA_VERSION, "json"),
        ('{"max_tokens":0}', SETTINGS_SCHEMA_VERSION, "validation"),
        ('{"future_setting":1}', SETTINGS_SCHEMA_VERSION, "validation"),
        ('{"max_tokens":5}', SETTINGS_SCHEMA_VERSION + 1, "schema_version"),
        ('{ "max_tokens": 5 }', SETTINGS_SCHEMA_VERSION, "canonical"),
    ],
)
def test_a_row_that_cannot_be_judged_says_which_check_and_never_the_document(
    document: str, schema: int, check: str
) -> None:
    with pytest.raises(SettingsUnreadableError) as unreadable:
        parse_row(_row(document, schema=schema), Scope.APPLICATION)
    assert unreadable.value.check == check
    assert document not in str(unreadable.value)


def test_a_row_of_the_wrong_scope_cannot_be_judged() -> None:
    with pytest.raises(SettingsUnreadableError) as unreadable:
        parse_row(_row('{"max_tokens":5}', scope=Scope.ORGANIZATION), Scope.APPLICATION)
    assert unreadable.value.check == "scope"
    with pytest.raises(SettingsUnreadableError):
        parse_row(_row('{"default_quality":"best"}', scope=Scope.ORGANIZATION), Scope.ORGANIZATION)


def test_rows_not_read_are_refused_never_taken_for_no_settings() -> None:
    with pytest.raises(SettingsUnreadableError) as unreadable:
        resolve(PLATFORM, _application(), None, DocumentCache())
    assert unreadable.value.check == "not_read"
    assert resolve(PLATFORM, _application(), StoredSettings(None, None), DocumentCache()) == (
        effective_settings(PLATFORM, _application(), {}, {})
    )


def test_the_cache_keeps_parsed_versions_and_judges_a_row_rewritten_in_place() -> None:
    cache = DocumentCache(size=2)
    good = _row('{"max_tokens":5}')
    assert cache.parsed(good, Scope.APPLICATION) == {"max_tokens": 5}
    assert len(cache) == 1
    with pytest.raises(SettingsUnreadableError):
        cache.parsed(replace(good, document='{"max_tokens":0}'), Scope.APPLICATION)
    with pytest.raises(SettingsUnreadableError):
        cache.parsed(replace(good, schema_version=9), Scope.APPLICATION)
    assert len(cache) == 1  # only documents that passed
    for version in range(4, 8):
        cache.parsed(replace(good, version=version), Scope.APPLICATION)
    assert len(cache) == 2


# --- the stores: memory, SQLite and Postgres ------------------------------------------------------


@dataclass
class Stores:
    applications: ApplicationStore
    settings: SettingsStore
    audit: AuditLog
    entries: Callable[[], Awaitable[list[AuditEntry]]]


class ListAuditStore:
    def __init__(self) -> None:
        self.rows: list[AuditEntry] = []

    async def insert(self, event: AuditEvent, at_ms: float) -> None:
        self.rows.append(AuditEntry(len(self.rows) + 1, at_ms, event))

    async def entries(self) -> list[AuditEntry]:
        return list(self.rows)


def _sqlite() -> sqlite3.Connection:
    connection = open_database(":memory:")
    MigrationRunner(connection, MIGRATIONS, SystemEpoch()).migrate()
    return connection


@asynccontextmanager
async def _stores(kind: str) -> AsyncIterator[Stores]:
    if kind == "memory":
        memory = InMemoryApplicationStore()
        listed = ListAuditStore()
        yield Stores(memory, memory.settings, AuditLog(listed), listed.entries)  # type: ignore[arg-type]
    elif kind == "sqlite":
        connection = _sqlite()
        audit = SqliteAuditStore(connection)
        yield Stores(
            SqliteApplicationStore(connection),
            SqliteSettingsStore(connection),
            AuditLog(audit),
            audit.entries,
        )
        connection.close()
    else:
        from gateway.postgres import (
            PostgresApplicationStore,
            PostgresAuditStore,
            PostgresSettingsStore,
        )
        from postgres_helpers import pg_world

        async with pg_world() as world:
            [db] = world.instances
            pg_audit = PostgresAuditStore(db)
            yield Stores(
                PostgresApplicationStore(db),
                PostgresSettingsStore(db),
                AuditLog(pg_audit),
                pg_audit.entries,
            )


@pytest.fixture(params=["memory", "sqlite", pytest.param("postgres", marks=postgres)])
async def stores(request: pytest.FixtureRequest) -> AsyncIterator[Stores]:
    async with _stores(request.param) as found:
        await found.applications.add_application(_application("pro", max_tokens=100, rpm=60))
        yield found


def _service(stores: Stores, platform: PlatformSettings = PLATFORM) -> SettingsService:
    return SettingsService(
        stores.settings, stores.applications, stores.audit, SystemEpoch(), platform
    )


async def _write(store: SettingsStore, expected: int, document: str, **kw: Any) -> int:
    async with store.write_transaction():
        return await store.write(
            Scope.APPLICATION,
            "shop",
            expected_version=expected,
            document=document,
            changed=kw.get("changed", ("max_tokens",)),
            changed_by_user_id=kw.get("by"),
            via=kw.get("via", "cli"),
            at_ms=kw.get("at_ms", 5.0),
        )


async def test_a_scope_never_set_has_no_row(stores: Stores) -> None:
    assert await stores.settings.current(Scope.APPLICATION, "shop") is None
    assert await stores.settings.versions(Scope.APPLICATION, "shop", 10) == []
    found = await stores.applications.lookup_key("0" * 16)
    assert found is None


async def test_each_write_is_a_version_with_who_when_and_the_old_and_new_document(
    stores: Stores,
) -> None:
    store = stores.settings
    assert await _write(store, 0, '{"max_tokens":50}', by="usr_00000000000000aa") == 1
    assert await _write(store, 1, '{"max_tokens":40}', via="revert:1") == 2
    row = await store.current(Scope.APPLICATION, "shop")
    assert row is not None
    assert (row.version, row.document, row.schema_version) == (2, '{"max_tokens":40}', 1)
    newest, oldest = await store.versions(Scope.APPLICATION, "shop", 10)
    assert (oldest.version, oldest.previous_document, oldest.document) == (
        1,
        None,
        '{"max_tokens":50}',
    )
    assert oldest.changed_by_user_id == "usr_00000000000000aa"
    assert (newest.previous_document, newest.document, newest.via) == (
        '{"max_tokens":50}',
        '{"max_tokens":40}',
        "revert:1",
    )
    assert newest.changed_fields == ("max_tokens",)
    assert newest.changed_at_ms > 0
    assert await store.version(Scope.APPLICATION, "shop", 1) == oldest
    assert await store.version(Scope.APPLICATION, "shop", 9) is None


async def test_a_write_made_from_another_version_is_a_conflict_and_writes_nothing(
    stores: Stores,
) -> None:
    store = stores.settings
    await _write(store, 0, '{"max_tokens":50}')
    for stale in (0, 2):
        with pytest.raises(SettingsVersionConflictError) as conflict:
            await _write(store, stale, '{"max_tokens":1}')
        assert conflict.value.current == 1
    row = await store.current(Scope.APPLICATION, "shop")
    assert row is not None
    assert row.document == '{"max_tokens":50}'
    assert len(await store.versions(Scope.APPLICATION, "shop", 10)) == 1


async def test_only_the_latest_versions_are_kept(stores: Stores) -> None:
    store = stores.settings
    for version in range(VERSIONS_KEPT + 3):
        await _write(store, version, canonical({"max_tokens": version + 1}))
    kept = await store.versions(Scope.APPLICATION, "shop", 1_000)
    assert len(kept) == VERSIONS_KEPT
    assert kept[0].version == VERSIONS_KEPT + 3
    assert kept[-1].version == 4


async def test_the_key_lookup_carries_both_rows_in_the_same_call(stores: Stores) -> None:
    from gateway.apikeys import ApiKeyService
    from gateway.clock import ClockEpoch
    from helpers import TEST_PEPPER

    keys = ApiKeyService(stores.applications, TEST_PEPPER, ClockEpoch(SimClock()))
    issued = await keys.issue("shop", "test")
    found = await stores.applications.lookup_key(issued.record.key_id)
    assert found is not None
    assert found.settings == StoredSettings(None, None)
    await _service(stores).set(
        Scope.ORGANIZATION, "default", expected_version=0, values={"max_tokens": 70}
    )
    await _service(stores).set(
        Scope.APPLICATION, "shop", expected_version=0, values={"default_quality": "best"}
    )
    found = await stores.applications.lookup_key(issued.record.key_id)
    assert found is not None
    org, app = found.settings.organization, found.settings.application
    assert org is not None
    assert (org.scope, org.scope_id, org.document) == (
        Scope.ORGANIZATION,
        "default",
        '{"max_tokens":70}',
    )
    assert app is not None
    assert (app.scope, app.scope_id, app.version, app.document) == (
        Scope.APPLICATION,
        "shop",
        1,
        '{"default_quality":"best"}',
    )
    # authenticate hands the rows on with the application.
    authenticated = await keys.authenticate(issued.key)
    assert authenticated.stored_settings == found.settings


# --- the service: set, unset, revert, conflicts, audit --------------------------------------------


async def test_set_unset_and_the_audit_row_with_field_names_only(stores: Stores) -> None:
    service = _service(stores)
    first = await service.set(
        Scope.APPLICATION,
        "shop",
        expected_version=0,
        values={"max_tokens": 50, "default_quality": "cheapest"},
        actor_user_id="usr_00000000000000aa",
    )
    assert (first.version, first.changed) == (1, ("default_quality", "max_tokens"))
    second = await service.set(Scope.APPLICATION, "shop", expected_version=1, unset=["max_tokens"])
    assert (second.version, second.changed, dict(second.document)) == (
        2,
        ("max_tokens",),
        {"default_quality": "cheapest"},
    )
    # Nothing changed: no version.
    same = await service.set(
        Scope.APPLICATION, "shop", expected_version=2, values={"default_quality": "cheapest"}
    )
    assert (same.version, same.changed) == (2, ())
    rows = [e.event for e in await stores.entries()]
    assert [r.action for r in rows] == [AuditAction.SETTINGS_CHANGED] * 2
    assert rows[0].details == {
        "settings_version": 1,
        "changed_fields": ["default_quality", "max_tokens"],
    }
    assert rows[0].actor_user_id == "usr_00000000000000aa"
    assert rows[0].target_id == "shop"
    # Never a value in an audit row.
    assert "cheapest" not in json.dumps([dict(r.details) for r in rows])


async def test_writes_looser_than_a_cap_are_refused_at_each_level_and_write_nothing(
    stores: Stores,
) -> None:
    service = _service(stores)
    await service.set(
        Scope.ORGANIZATION, "default", expected_version=0, values={"requests_per_minute": 30}
    )
    for values, level in (
        ({"requests_per_minute": 61}, "plan"),
        ({"requests_per_minute": 31}, "organization"),
        ({"generate_timeout_ms": 200_000}, "platform"),
    ):
        with pytest.raises(ManagementRefusal) as refused:
            await service.set(Scope.APPLICATION, "shop", expected_version=0, values=values)
        assert refused.value.reason.value == "setting_looser_than_cap"
        assert f"capped by the {level}" in str(refused.value)
    assert await stores.settings.current(Scope.APPLICATION, "shop") is None
    with pytest.raises(ManagementRefusal):
        await service.set(
            Scope.ORGANIZATION,
            "default",
            expected_version=1,
            values={"generate_timeout_ms": 999_999},
        )
    with pytest.raises(SettingsInvalidError):
        await service.set(Scope.APPLICATION, "shop", expected_version=0, values={"max_tokens": 0})
    with pytest.raises(KeyError):
        await service.set(Scope.APPLICATION, "nope", expected_version=0, values={"max_tokens": 1})
    with pytest.raises(KeyError):
        await service.set(Scope.ORGANIZATION, "nope", expected_version=0, values={"max_tokens": 1})


async def test_an_organization_cap_tightened_later_is_always_allowed_and_clamps(
    stores: Stores,
) -> None:
    service = _service(stores)
    await service.set(Scope.APPLICATION, "shop", expected_version=0, values={"max_tokens": 80})
    await service.set(Scope.ORGANIZATION, "default", expected_version=0, values={"max_tokens": 20})
    view = await service.view(Scope.APPLICATION, "shop")
    assert dict(view.document) == {"max_tokens": 80}
    assert view.effective is not None
    assert view.effective.max_tokens == 20
    assert view.effective.explain["max_tokens"].capped_by is Source.ORGANIZATION


async def test_revert_goes_one_step_back_and_a_revert_of_a_revert_goes_back_again(
    stores: Stores,
) -> None:
    service = _service(stores)
    with pytest.raises(ManagementRefusal) as nothing:
        await service.revert(Scope.APPLICATION, "shop", expected_version=0)
    assert nothing.value.reason.value == "no_previous_version"
    await service.set(Scope.APPLICATION, "shop", expected_version=0, values={"max_tokens": 50})
    with pytest.raises(ManagementRefusal) as first:
        await service.revert(Scope.APPLICATION, "shop", expected_version=1)
    assert first.value.reason.value == "no_previous_version"
    await service.set(Scope.APPLICATION, "shop", expected_version=1, values={"max_tokens": 40})
    await service.set(Scope.APPLICATION, "shop", expected_version=2, values={"max_tokens": 30})
    back = await service.revert(Scope.APPLICATION, "shop", expected_version=3)
    assert (back.version, dict(back.document)) == (4, {"max_tokens": 40})
    again = await service.revert(Scope.APPLICATION, "shop", expected_version=4)
    assert (again.version, dict(again.document)) == (5, {"max_tokens": 30})
    newest = (await stores.settings.versions(Scope.APPLICATION, "shop", 1))[0]
    assert newest.via == "revert:3"  # the version whose document it restored
    actions = [e.event.action for e in await stores.entries()]
    assert actions[-2:] == [AuditAction.SETTINGS_REVERTED] * 2
    with pytest.raises(ManagementRefusal) as stale:
        await service.revert(Scope.APPLICATION, "shop", expected_version=4)
    assert stale.value.reason.value == "settings_version_conflict"


async def test_a_revert_now_looser_than_a_cap_is_refused(stores: Stores) -> None:
    service = _service(stores)
    await service.set(Scope.APPLICATION, "shop", expected_version=0, values={"max_tokens": 90})
    await service.set(Scope.APPLICATION, "shop", expected_version=1, values={"max_tokens": 10})
    await service.set(Scope.ORGANIZATION, "default", expected_version=0, values={"max_tokens": 20})
    with pytest.raises(ManagementRefusal) as refused:
        await service.revert(Scope.APPLICATION, "shop", expected_version=2)
    assert refused.value.reason.value == "setting_looser_than_cap"
    row = await stores.settings.current(Scope.APPLICATION, "shop")
    assert row is not None
    assert row.version == 2


async def test_a_change_made_from_a_stale_version_is_refused(stores: Stores) -> None:
    service = _service(stores)
    await service.set(Scope.APPLICATION, "shop", expected_version=0, values={"max_tokens": 50})
    with pytest.raises(ManagementRefusal) as refused:
        await service.set(Scope.APPLICATION, "shop", expected_version=0, values={"max_tokens": 1})
    assert refused.value.reason.value == "settings_version_conflict"
    row = await stores.settings.current(Scope.APPLICATION, "shop")
    assert row is not None
    assert row.document == '{"max_tokens":50}'


async def test_replace_writes_over_a_row_that_cannot_be_read(stores: Stores) -> None:
    service = _service(stores)
    await service.set(Scope.APPLICATION, "shop", expected_version=0, values={"max_tokens": 50})
    await _corrupt(stores, '{"max_tokens":0}')
    with pytest.raises(SettingsUnreadableError):
        await service.set(Scope.APPLICATION, "shop", expected_version=1, values={"max_tokens": 5})
    fixed = await service.set(
        Scope.APPLICATION, "shop", expected_version=1, values={"max_tokens": 5}, replace=True
    )
    assert (fixed.version, dict(fixed.document)) == (2, {"max_tokens": 5})


async def _corrupt(stores: Stores, document: str, *, scope: Scope = Scope.APPLICATION,
                   scope_id: str = "shop", schema_version: int | None = None) -> None:  # fmt: skip
    """Rewrite a stored row in place, as a hand edit or other code would."""
    settings = stores.settings
    if hasattr(settings, "corrupt"):
        changes: dict[str, object] = {"document": document}
        if schema_version is not None:
            changes["schema_version"] = schema_version
        settings.corrupt(scope, scope_id, **changes)
        return
    sql = "UPDATE settings_current SET document = {d}{s} WHERE scope_type = {t} AND scope_id = {i}"
    if isinstance(settings, SqliteSettingsStore):
        connection = settings._connection
        connection.execute(
            sql.format(d="?", s=", schema_version = ?" if schema_version else "", t="?", i="?"),
            (document, *([schema_version] if schema_version else []), scope.value, scope_id),
        )
        return
    db = settings._db  # type: ignore[attr-defined]
    async with db.connection() as c:
        args = (document, *([schema_version] if schema_version else []), scope.value, scope_id)
        n = 1 if not schema_version else 2
        await c.execute(
            sql.format(
                d="$1", s=", schema_version = $2" if schema_version else "",
                t=f"${n + 1}", i=f"${n + 2}",
            ),
            *args,
        )  # fmt: skip


# --- two instances (postgres) ---------------------------------------------------------------------


async def _pg_service(db: Any) -> SettingsService:
    from gateway.postgres import PostgresApplicationStore, PostgresAuditStore, PostgresSettingsStore

    return SettingsService(
        PostgresSettingsStore(db),
        PostgresApplicationStore(db),
        AuditLog(PostgresAuditStore(db)),
        SystemEpoch(),
        PLATFORM,
    )


def _hook(target: Any, name: str, other: Callable[[], Awaitable[object]], done: list[bool]) -> None:
    """After ``target.name``'s first call returns, the other instance writes."""
    real = getattr(target, name)

    async def hooked(*args: Any, **kwargs: Any) -> Any:
        found = await real(*args, **kwargs)
        if not done:
            try:
                await other()
                done.append(True)
            except StateUnavailableError:
                done.append(False)
        return found

    setattr(target, name, hooked)


@postgres
async def test_no_application_value_is_stored_looser_than_a_cap_set_meanwhile() -> None:
    """The cap check runs in the write's transaction, under the write lock: another instance
    tightening the organization's cap after this one read it waits for the lock (here it is
    refused by the short timeout), so a value looser than the cap in force is never stored."""
    from postgres_helpers import pg_world

    async with pg_world(instances=2, lock_timeout_ms=300.0) as world:
        first, second = [await _pg_service(db) for db in world.instances]
        await first._applications.add_application(_application("pro", max_tokens=100))
        await first.set(
            Scope.ORGANIZATION, "default", expected_version=0, values={"max_tokens": 90}
        )
        done: list[bool] = []

        async def stricter() -> object:
            return await second.set(
                Scope.ORGANIZATION, "default", expected_version=1, values={"max_tokens": 10}
            )

        _hook(first.store, "current", stricter, done)
        await first.set(Scope.APPLICATION, "shop", expected_version=0, values={"max_tokens": 80})
        assert done == [False]  # the other instance waited for the lock and timed out
        org = await first.store.current(Scope.ORGANIZATION, "default")
        app = await first.store.current(Scope.APPLICATION, "shop")
        assert org is not None
        assert app is not None
        cap = json.loads(org.document)["max_tokens"]
        assert json.loads(app.document)["max_tokens"] <= cap
        # Once the write is done, the other instance's stricter cap goes in, and clamps.
        await stricter()
        view = await second.view(Scope.APPLICATION, "shop")
        assert view.effective is not None
        assert view.effective.max_tokens == 10


@postgres
async def test_two_instances_writing_the_same_version_give_one_success_and_one_conflict() -> None:
    from postgres_helpers import pg_world

    async with pg_world(instances=2) as world:
        services = [await _pg_service(db) for db in world.instances]
        await services[0]._applications.add_application(_application("pro", max_tokens=100))
        results = await asyncio.gather(
            *(
                services[i % 2].set(
                    Scope.APPLICATION, "shop", expected_version=0, values={"max_tokens": 10 + i}
                )
                for i in range(6)
            ),
            return_exceptions=True,
        )
        ok = [r for r in results if not isinstance(r, BaseException)]
        refused = [r for r in results if isinstance(r, ManagementRefusal)]
        assert len(ok) == 1, results
        assert len(refused) == 5, results
        assert {r.reason.value for r in refused} == {"settings_version_conflict"}
        versions = await services[1].store.versions(Scope.APPLICATION, "shop", 10)
        assert [v.version for v in versions] == [1]


@postgres
async def test_a_change_on_one_instance_is_in_the_others_next_key_lookup() -> None:
    from gateway.apikeys import ApiKeyService
    from gateway.clock import ClockEpoch
    from gateway.postgres import PostgresApplicationStore
    from helpers import TEST_PEPPER
    from postgres_helpers import pg_world

    async with pg_world(instances=2) as world:
        a, b = world.instances
        service = await _pg_service(a)
        await service._applications.add_application(_application("pro"))
        keys_b = ApiKeyService(PostgresApplicationStore(b), TEST_PEPPER, ClockEpoch(SimClock()))
        issued = await keys_b.issue("shop", "test")
        assert (await keys_b.authenticate(issued.key)).stored_settings == StoredSettings(None, None)
        await service.set(Scope.APPLICATION, "shop", expected_version=0, values={"max_tokens": 7})
        seen = (await keys_b.authenticate(issued.key)).stored_settings
        assert seen is not None
        assert seen.application is not None
        assert seen.application.document == '{"max_tokens":7}'


async def test_an_unrelated_change_is_not_blocked_by_a_value_a_cap_now_clamps(
    stores: Stores,
) -> None:
    service = _service(stores)
    await service.set(Scope.APPLICATION, "shop", expected_version=0, values={"max_tokens": 80})
    await service.set(Scope.ORGANIZATION, "default", expected_version=0, values={"max_tokens": 20})
    change = await service.set(
        Scope.APPLICATION, "shop", expected_version=1, values={"default_quality": "best"}
    )
    assert change.changed == ("default_quality",)
    with pytest.raises(ManagementRefusal):
        await service.set(Scope.APPLICATION, "shop", expected_version=2, values={"max_tokens": 70})


# --- the migration and its rollback ---------------------------------------------------------------


def test_migration_14_adds_the_two_tables_and_its_rollback_takes_them_out() -> None:
    connection = open_database(":memory:")
    runner = MigrationRunner(connection, MIGRATIONS, SystemEpoch())
    runner.migrate(13)

    def tables() -> set[str]:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        return {r[0] for r in rows if r[0].startswith("settings_")}

    assert tables() == set()
    assert runner.migrate() == (14,)
    assert tables() == {"settings_current", "settings_versions"}
    assert runner.rollback(13) == (14,)
    assert tables() == set()
    assert runner.migrate() == (14,)
    connection.close()


@postgres
async def test_postgres_schema_3_applies_and_rolls_back() -> None:
    from gateway.postgres import SCHEMA_VERSION, apply_schema, rollback_schema
    from postgres_helpers import pg_world

    async with pg_world() as world:
        [db] = world.instances
        assert SCHEMA_VERSION == 3
        settings = "SELECT count(*) FROM pg_tables WHERE schemaname = $1 AND tablename LIKE $2"
        assert (await world.rows(settings, world.schema, "settings_%")) == [(2,)]
        assert await rollback_schema(db, 2) == (3,)
        assert (await world.rows(settings, world.schema, "settings_%")) == [(0,)]
        assert await world.rows("SELECT max(version) FROM gateway_schema") == [(2,)]
        with pytest.raises(RuntimeError, match="no way back"):
            await rollback_schema(db, 1)
        assert await apply_schema(db) == 3
        assert (await world.rows(settings, world.schema, "settings_%")) == [(2,)]


def test_the_clis_platform_values_are_the_default_servers() -> None:
    from gateway.app import default_platform_settings
    from helpers import default_server

    with default_server() as app:
        assert app.state.settings_platform == default_platform_settings()
