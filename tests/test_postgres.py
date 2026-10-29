"""The durable stores on Postgres (docs/36-postgres.md).

Tests marked ``postgres`` run against a real Postgres (``docker-compose.postgres.yml``; skipped
without ``GATEWAY_TEST_POSTGRES_URL``), each in a schema of its own: the stores against SQLite's,
the atomic rules on two instances, the database's clock, the write lock and its timeout, a slow
management write beside chat requests, the migration both ways, and a leak scan. The rest need
no server: the two schemas side by side, the setting that refuses shared state, an address where
nothing listens, and every endpoint while the database is down.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import re
import sqlite3
import sys
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
from benchmarks import durable_calls

from accounts_helpers import PASSWORD, cookie, with_csrf
from gateway.apikeys import ApiKeyService, ApplicationAccess, InvalidApiKeyError
from gateway.app import create_app, create_default_app
from gateway.applications import ApiKeyRecord, KeyLookup, application_from_template
from gateway.audit import AuditAction, AuditEvent, AuditLog, Outcome, TargetType
from gateway.auth import LOCK_MS, AccountAccess, AccountService, LoginFailedError
from gateway.clock import ClockEpoch, SimClock, SystemEpoch, WallClock
from gateway.continuous import build_continuous_registry
from gateway.database import open_database
from gateway.localonly import LocalOnlyPolicy
from gateway.management import KeyLimits, ManagementService
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.modes import Mode
from gateway.organizations import new_organization
from gateway.permissions import Assignment, Role, ScopeType
from gateway.pg_migrate import (
    MigrationRefused,
    _postgres_rows,
    _sqlite_rows,
    columns_of,
    migrate_to_postgres,
    migrate_to_sqlite,
)
from gateway.postgres import (
    PG_BASELINE,
    TABLES,
    PostgresApplicationStore,
    PostgresDatabase,
    postgres_state,
)
from gateway.providers import ProviderConfig
from gateway.routing import load_plans
from gateway.state import (
    STATE_ENV,
    SharedStateRefusedError,
    StateBackend,
    StateUnavailableError,
    sqlite_state,
)
from gateway.usage import UsageEvent
from gateway.users import Bootstrap, Session, User
from gateway.vault import SealedKey
from helpers import TEST_PEPPER, application_access, bearer
from postgres_helpers import POSTGRES_URL_ENV, PgWorld, pg_accounts, pg_world

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from manage import main as manage

postgres = pytest.mark.postgres
PLANS = load_plans()
NOW = 1.8e12  # the database's time in these tests, far from the instance's own
PROMPT = "one two three four five six seven eight nine ten"
# Nothing listens on port 1; the password is planted, to be looked for everywhere.
PLANTED = "planted-pg-password-4f1c9a"
NOWHERE = f"postgresql://gateway:{PLANTED}@127.0.0.1:1/gateway"


# --- the schemas, side by side (no server) ------------------------------------------------------


def _postgres_columns() -> dict[str, list[str]]:
    """Each table's columns, read from the baseline's text."""
    found: dict[str, list[str]] = {}
    for match in re.finditer(r"CREATE TABLE (\w+) \((.*?)\);\n", PG_BASELINE, re.S):
        depth, part, parts = 0, "", []
        for char in match.group(2):
            depth += {"(": 1, ")": -1}.get(char, 0)
            if char == "," and depth == 0:
                parts.append(part)
                part = ""
            else:
                part += char
        parts.append(part)
        names = [p.split()[0] for p in parts if p.split()]
        found[match.group(1)] = [
            n for n in names if n not in ("CHECK", "UNIQUE", "PRIMARY", "FOREIGN", "CONSTRAINT")
        ]
    return found


def test_the_postgres_schema_has_sqlites_tables_and_columns() -> None:
    """The baseline is SQLite migrations 1 to 13: the same tables and columns, in the same order,
    plus ``ord`` where the SQLite stores order by rowid."""
    connection = open_database(":memory:")
    MigrationRunner(connection, MIGRATIONS, SystemEpoch()).migrate()
    sqlite_tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            " AND name != 'schema_migrations'"
        )
    }
    pg = _postgres_columns()
    assert set(pg) == sqlite_tables == set(TABLES)
    for table in TABLES:
        assert [c for c in pg[table] if c != "ord"] == list(columns_of(connection, table)), table
    with_ord = {t for t in TABLES if "ord" in pg[t]}
    assert with_ord == {
        "organizations",
        "applications",
        "api_keys",
        "sessions",
        "role_assignments",
        "app_roles",
    }
    connection.close()


# --- the setting (no server) ----------------------------------------------------------------------


def test_sqlite_is_the_default_and_shared_state_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Until budgets are shared (PR 3b), two instances must not each spend one budget."""
    monkeypatch.setenv("GATEWAY_KEY_PEPPER", TEST_PEPPER.decode())
    monkeypatch.setenv("GATEWAY_DATABASE", str(tmp_path / "gateway.db"))
    for value in ("shared",):
        monkeypatch.setenv(STATE_ENV, value)
        with pytest.raises(SharedStateRefusedError, match="budgets"):
            create_default_app()
        monkeypatch.setenv("GATEWAY_SIMULATED_LIVE", "1")
        with pytest.raises(SharedStateRefusedError):
            create_default_app()
        monkeypatch.delenv("GATEWAY_SIMULATED_LIVE")
    monkeypatch.setenv(STATE_ENV, "postgres")
    with pytest.raises(ValueError, match="sqlite or shared"):
        create_default_app()
    assert not (tmp_path / "gateway.db").exists()  # refused before anything was opened


# --- an address where nothing listens (no server) -------------------------------------------------


async def test_an_unreachable_postgres_is_refused_without_its_address(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    with pytest.raises(StateUnavailableError) as raised:
        await PostgresDatabase.connect(NOWHERE, WallClock())
    seen = [str(raised.value), repr(raised.value), caplog.text]
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is not None
    for text in seen:
        assert PLANTED not in text, text
        assert "127.0.0.1" not in text, text
    # The control: the same scan finds the password where it is.
    logging.getLogger("gateway.test").warning("planted %s", NOWHERE)
    assert PLANTED in caplog.text


def test_the_migration_commands_never_print_the_address(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "gateway.db"
    env = {"GATEWAY_DATABASE": str(database), POSTGRES_URL_ENV.replace("TEST_", ""): NOWHERE}
    connection = open_database(database)
    MigrationRunner(connection, MIGRATIONS, SystemEpoch()).migrate()
    connection.close()
    assert manage(["migrate-to-postgres", "--dry-run"], env) == 1
    assert manage(["migrate-to-sqlite", "--to", str(tmp_path / "back.db")], env) == 1
    out = capsys.readouterr()
    assert "could not be reached" in out.err
    assert PLANTED not in out.out + out.err
    assert "127.0.0.1" not in out.out + out.err
    assert not (tmp_path / "back.db").exists()
    assert manage(["migrate-to-postgres"], {"GATEWAY_DATABASE": str(database)}) == 1
    assert "GATEWAY_POSTGRES_URL" in capsys.readouterr().err


# --- every endpoint while Postgres is down (no server) --------------------------------------------


class _DownPool:
    """A pool whose database cannot be reached: every connection is refused."""

    def acquire(self, *, timeout: float | None = None) -> _DownPool:
        return self

    async def __aenter__(self) -> None:
        raise ConnectionRefusedError("the database cannot be reached")

    async def __aexit__(self, *_: object) -> None:
        return None

    async def close(self) -> None:
        return None


def _app_on(
    db: PostgresDatabase, clock: SimClock, startup: list[Any]
) -> tuple[Any, ApiKeyService, Any]:
    state = postgres_state(db)
    keys = ApiKeyService(state.applications, TEST_PEPPER, ClockEpoch(clock), store_clock=db)
    registry = build_continuous_registry(clock, seed=7)
    app = create_app(
        registry,
        clock,
        applications=ApplicationAccess(keys, PLANS),
        accounts=AccountAccess(pg_accounts(db)),
        usage_store=state.usage,
        epoch=ClockEpoch(clock),
        modes=state.modes,
        local_only=LocalOnlyPolicy(),
        startup_applications=startup,
        readiness=db.ready,
    )
    return app, keys, registry


# Each endpoint, what it is sent, and its answer while the database is down.
SESSION = "s" * 43
DOWN_TABLE: list[tuple[str, str, dict[str, Any], int, str | None]] = [
    (
        "POST",
        "/v1/generate",
        {"json": {"model": "economy", "prompt": PROMPT}},
        503,
        "state_unavailable",
    ),
    (
        "POST",
        "/v1/chat/completions",
        {"json": {"model": "economy", "messages": [{"role": "user", "content": PROMPT}]}},
        503,
        "state_unavailable",
    ),
    ("GET", "/v1/auth/me", {"headers": cookie(SESSION)}, 503, "state_unavailable"),
    ("GET", "/v1/auth/sessions", {"headers": cookie(SESSION)}, 503, "state_unavailable"),
    (
        "POST",
        "/v1/auth/logout",
        {"headers": with_csrf(SESSION, "c" * 43)},
        503,
        "state_unavailable",
    ),
    (
        "POST",
        "/v1/auth/step-up",
        {"headers": with_csrf(SESSION, "c" * 43), "json": {"password": PASSWORD}},
        503,
        "state_unavailable",
    ),
    (
        "GET",
        "/v1/admin/orgs/default/applications",
        {"headers": cookie(SESSION)},
        503,
        "state_unavailable",
    ),
    (
        "POST",
        "/v1/admin/orgs/default/applications",
        {"headers": with_csrf(SESSION, "c" * 43), "json": {"name": "Shop", "plan": "pro"}},
        503,
        "state_unavailable",
    ),
    (
        "POST",
        "/v1/auth/login",
        {"json": {"username": "alice", "password": PASSWORD}},
        401,
        "invalid_credentials",
    ),
    ("GET", "/healthz", {}, 200, None),
    ("GET", "/readyz", {}, 503, None),
    ("GET", "/v1/models", {}, 200, None),
]


async def test_every_endpoint_while_postgres_is_down_refuses_or_has_no_state() -> None:
    """Missing is forbidden: nothing that needs the database is admitted. A key, a session or a
    management request: 503, the same whatever the key or session was; a login: refused like
    any other refusal; liveness up, readiness down."""
    clock = SimClock()
    _, issued = application_access(clock, apps={"acme": "pro"})
    db = PostgresDatabase(_DownPool(), _DownPool(), WallClock())
    app, _, registry = _app_on(db, clock, [])
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            for method, path, kwargs, status, code in DOWN_TABLE:
                headers = dict(kwargs.get("headers", {}))
                if path in ("/v1/generate", "/v1/chat/completions"):
                    headers |= bearer(issued["acme"])
                answer = await client.request(
                    method, path, json=kwargs.get("json"), headers=headers
                )
                assert answer.status_code == status, (path, answer.text)
                if code is not None:
                    assert answer.json()["error"]["code"] == code, path
                if status == 503:
                    assert answer.headers.get("cache-control") == "no-store", path
                assert "127.0.0.1" not in answer.text
                assert "refused" not in answer.text.lower()
            # Missing is forbidden, whatever the key: a made-up one gets the same answer.
            made_up = await client.post(
                "/v1/generate",
                json={"model": "economy", "prompt": PROMPT},
                headers=bearer("gwk_" + "0" * 16 + "_" + "x" * 43),
            )
            real = await client.post(
                "/v1/generate",
                json={"model": "economy", "prompt": PROMPT},
                headers=bearer(issued["acme"]),
            )
            assert (made_up.status_code, made_up.content) == (real.status_code, real.content)
            assert (await client.get("/readyz")).json() == {"status": "not_ready"}
    finally:
        await registry.aclose()


# --- the stores against SQLite's (postgres) -------------------------------------------------------


class _Counting:
    """A store whose calls are noted by name (``Store.method``)."""

    def __init__(self, target: Any, called: set[str]) -> None:
        self._target, self._called = target, called

    def __getattr__(self, name: str) -> Any:
        found = getattr(self._target, name)
        if callable(found) and not name.startswith("_"):
            self._called.add(f"{type(self._target).__name__}.{name}")
        return found


async def _outcome(call: Awaitable[Any]) -> Any:
    try:
        return ("ok", await call)
    except (ValueError, KeyError) as exc:
        return (type(exc).__name__, str(exc))


def _same_clock(found: Any) -> Any:
    """A key lookup without the time it was read at: SQLite has no clock of its own."""
    if isinstance(found, KeyLookup):
        return dataclasses.replace(found, now_ms=None)
    return found


async def _scenario(s: StateBackend) -> list[Any]:
    """One call or more of every store method, in an order that gives each something to do;
    returns what each read answered."""
    out: list[Any] = []
    a, acc, roles, audit, p, m, u = (
        s.applications,
        s.accounts,
        s.roles,
        s.audit,
        s.providers,
        s.modes,
        s.usage,
    )
    # Organizations, and a transaction rolled back.
    async with a.write_transaction():
        await a.add_organization(new_organization("north", "North", "business", created_at_ms=1.0))
        await a.add_organization(new_organization("solo", "Solo", "individual", created_at_ms=2.0))

    async def rolled_back() -> None:
        async with a.write_transaction():
            await a.add_organization(new_organization("gone", "Gone", "business", created_at_ms=3))
            raise RuntimeError("roll back")

    with pytest.raises(RuntimeError):
        await rolled_back()
    out.append(await a.get_organization("gone"))
    out.append(
        await _outcome(
            a.add_organization(new_organization("n2", "North", "business", created_at_ms=4.0))
        )
    )
    out += [await a.get_organization("north"), await a.find_organization("Solo")]
    await a.set_organization_disabled("solo", True)
    out += [await a.list_organizations()]
    # Applications and keys.
    for i, name in enumerate(["Shop", "Bank", "Cafe"]):
        await a.add_application(
            application_from_template(
                f"app{i}", name, PLANS, "pro", organization_id="north", created_at_ms=10.0 + i
            )
        )
    out.append(
        await _outcome(
            a.add_application(
                application_from_template(
                    "app9", "Shop", PLANS, "pro", organization_id="north", created_at_ms=19.0
                )
            )
        )
    )
    out += [
        await a.get_application("app1"),
        await a.list_applications(),
        await a.list_applications("north"),
        await a.page_applications("north", None, 2),
        await a.page_applications("north", "app1", 2),
    ]
    for i in range(3):
        await a.add_key(
            ApiKeyRecord(f"key{i}", "app0", f"label {i}", f"hmac{i}", 20.0 + i, 99.0 if i else None)
        )
    out.append(await _outcome(a.add_key(ApiKeyRecord("key0", "app1", "again", "h", 29.0))))
    out += [
        await a.get_key("key1"),
        _same_clock(await a.lookup_key("key0")),
        await a.lookup_key("nope"),
        await a.page_keys("app0", None, 2),
        await a.page_keys("app0", "key1", 5),
    ]
    await a.set_key_expiry("key1", 500.0)
    await a.revoke_key("key2")
    await a.record_last_used({"key0": 30.0, "key1": 31.0})
    out.append(await a.list_keys("app0"))
    # Usage.
    seqs = [
        await u.append(UsageEvent("app0", f"req{i}", "economy", 10, 2, 5, 0.25, 0.5, 100.0 + i))
        for i in range(3)
    ]
    await u.append(UsageEvent("app1", "req9", "premium", 7, 0, 3, 0.1 + 0.2, 0.3 * 0.8, 105.0))
    out += [
        seqs,
        await u.usage_by_model("app0", 0.0, 102.0),
        await u.usage_by_model_for(["app0", "app1"], 0.0, 1e3),
        await u.events("app1"),
        await u.prune(102.0, limit=1),
        await u.prune(102.0),
        await u.events(),
    ]
    # Users, the lockout count, sessions and step-up.
    users = [User(f"usr_{i:016d}", f"user{i}", f"hash{i}", "north", 40.0 + i) for i in range(3)]
    for user in users:
        await acc.add_user(user)
    out.append(await _outcome(acc.add_user(User("usr_x", "user0", "h", "north", 49.0))))
    out += [
        await acc.get_user(users[0].id),
        await acc.find_user("user1"),
        await acc.page_users("north", None, 2),
        await acc.page_users("north", users[1].id, 2),
        await acc.count_members("north"),
    ]
    await acc.set_login_state(users[0].id, 2, 50.0, None)
    for _ in range(3):
        out.append(
            await acc.count_login_failure(
                users[1].id, NOW, window_ms=1000.0, lock_after=3, lock_ms=60_000.0
            )
        )
    await acc.set_password(users[2].id, "hash b", must_change=True, bootstrap=Bootstrap.UNUSED)
    await acc.set_password_hash(users[2].id, "hash c")
    await acc.set_bootstrap(users[2].id, Bootstrap.USED)
    await acc.set_disabled(users[0].id, True)
    out.append([await acc.get_user(user.id) for user in users])
    for i in range(3):
        await acc.add_session(
            Session(
                f"sid{i}",
                f"handle{i}",
                users[i % 2].id,
                f"csrf{i}",
                60.0 + i,
                60.0 + i,
                NOW + 1000.0 * (i + 1),
            )
        )
    await acc.touch_session("sid0", 70.0)
    await acc.set_step_up("sid0", NOW + 500.0)
    out += [
        await acc.get_session("sid0"),
        await acc.claim_step_up("sid0", NOW),
        await acc.claim_step_up("sid0", NOW),
        await acc.restore_step_up("sid0", NOW + 500.0),
    ]
    await acc.set_step_up("sid2", NOW - 1.0)
    out += [
        await acc.claim_step_up("sid2", NOW),
        await acc.clear_step_up(users[0].id),
        await acc.list_sessions(users[0].id),
        await acc.revoke_session("sid1"),
        await acc.revoke_session("sid1"),
        await acc.revoke_sessions(users[0].id, keep_id_hash="sid0"),
        await acc.prune_sessions(NOW + 500.0, 1e15),
        await acc.list_sessions(users[0].id),
    ]
    # Roles.
    org_admin = Assignment(Role.ORG_ADMIN.value, ScopeType.ORGANIZATION, "north")
    platform = Assignment(Role.PLATFORM_ADMIN.value, ScopeType.PLATFORM, "*")
    await roles.grant(users[1].id, org_admin, None, 80.0)
    await roles.grant(users[0].id, org_admin, users[1].id, 81.0)
    await roles.define_app_role("app0", "support", 82.0)
    support = Assignment("support", ScopeType.APPLICATION, "app0")
    await roles.grant(users[2].id, support, users[1].id, 83.0)
    out += [
        await _outcome(roles.grant(users[1].id, org_admin, None, 84.0)),
        await _outcome(roles.define_app_role("app0", "support", 85.0)),
        await roles.assignments_for(users[2].id),
        await roles.enabled_holders(org_admin),
        await roles.platform_admin_exists(),
    ]
    await roles.grant(users[1].id, platform, None, 86.0)
    out += [
        await roles.platform_admin_exists(),
        await roles.revoke(users[2].id, support),
        await roles.revoke(users[2].id, support),
        await roles.app_roles("app0"),
    ]
    # Audit.
    for i in range(3):
        await audit.insert(
            AuditEvent(
                AuditAction.LOGIN,
                Outcome.SUCCESS,
                TargetType.USER,
                users[i].id,
                users[1].id,
                details={"failures": i},
            ),
            90.0 + i,
        )
    out += [await audit.entries(), await audit.prune(92.0, limit=1), await audit.entries()]
    # Providers and their keys.
    config = ProviderConfig(
        "prv_1", "north", "openai_compatible", "https://api.example.test", "m1", "economy",
        "primary", True, True, 1.0, 2.0, {"tools": {"supported": True, "source": "declared"}},
        None, 95.0,
    )  # fmt: skip
    await p.add(config)
    await p.add(dataclasses.replace(config, id="prv_2", model="m2", role="fallback"))
    out += [
        await _outcome(p.add(dataclasses.replace(config, id="prv_3"))),
        await p.get("prv_1"),
        await p.list_for("north"),
    ]
    await p.replace(dataclasses.replace(config, enabled=False, first_token_budget_ms=900.0))
    await p.put_key("prv_1", SealedKey("k1", b"\x00" * 12, b"\x01sealed\xff", "abcd"), 96.0)
    await p.put_key("prv_2", SealedKey("k1", b"\x02" * 12, b"other", None), 97.0)
    await p.put_key("prv_1", SealedKey("k2", b"\x03" * 12, b"again", "wxyz"), 98.0)
    out += [await p.get_key("prv_1"), await p.all_keys(), await p.delete_key("prv_2")]
    out += [await p.delete_key("prv_2")]
    await p.delete("prv_2")
    out += [await p.list_all()]
    # Modes.
    out += [await m.organization_mode("north"), await m.application_mode("app0")]
    async with m.write_transaction():
        await m.set_organization_mode("north", Mode.LOCAL_WITH_HOSTED_FALLBACK, 99.0)
        await m.set_application_mode("app0", Mode.LOCAL_ONLY, 99.5)
    await m.set_application_mode("app0", Mode.LOCAL_WITH_HOSTED_FALLBACK, 99.7)
    out += [await m.organization_mode("north"), await m.application_mode("app0")]
    return out


def _protocol_methods() -> set[str]:
    from gateway.applications import ApplicationStore
    from gateway.audit import AuditStore
    from gateway.modes import ModeStore
    from gateway.providers import ProviderStore
    from gateway.roles import RoleStore
    from gateway.usage import UsageStore
    from gateway.users import AccountStore

    names = {
        ApplicationStore: "PostgresApplicationStore",
        AccountStore: "PostgresAccountStore",
        RoleStore: "PostgresRoleStore",
        AuditStore: "PostgresAuditStore",
        ProviderStore: "PostgresProviderStore",
        ModeStore: "PostgresModeStore",
        UsageStore: "PostgresUsageStore",
    }
    return {
        f"{store}.{name}"
        for protocol, store in names.items()
        for name, value in vars(protocol).items()
        if callable(value) and not name.startswith("_")
    }


def _sqlite_world() -> tuple[sqlite3.Connection, StateBackend]:
    connection = open_database(":memory:")
    MigrationRunner(connection, MIGRATIONS, ClockEpoch(SimClock())).migrate()
    return connection, sqlite_state(connection, ClockEpoch(SimClock()))


@postgres
async def test_every_store_method_answers_as_sqlites_and_leaves_the_same_rows() -> None:
    connection, lite = _sqlite_world()
    expected = await _scenario(lite)
    async with pg_world() as world:
        await world.set_time(NOW)
        [db] = world.instances
        called: set[str] = set()
        state = postgres_state(db)
        stores: dict[str, Any] = {
            f.name: _Counting(getattr(state, f.name), called) if f.name != "clock" else db
            for f in dataclasses.fields(StateBackend)
        }
        counted = StateBackend(**stores)
        found = await _scenario(counted)
        for i, (want, got) in enumerate(zip(expected, found, strict=True)):
            assert got == want, f"read {i}"
        missing = _protocol_methods() - called
        assert not missing, sorted(missing)
        # The lookup brings the database's time with the key (section 3.8).
        lookup = await state.applications.lookup_key("key0")
        assert lookup is not None
        assert lookup.now_ms == NOW
        async with db.management.acquire() as pg:
            for table in TABLES:
                columns = columns_of(connection, table)
                rows = await _postgres_rows(pg, table, columns)
                assert rows == _sqlite_rows(connection, table), table
    connection.close()


async def _ready_user(service: AccountService, username: str) -> User:
    """A user who may step up: made at the CLI, then with no password change pending."""
    user = await service.create_user(username, PASSWORD, "default")
    await service.store.set_password(
        user.id, user.password_hash, must_change=False, bootstrap=Bootstrap.NONE
    )
    return user


# --- the database's clock (postgres) --------------------------------------------------------------


@postgres
async def test_the_lockout_and_the_step_up_window_are_on_the_databases_clock() -> None:
    """The instance's own epoch is real time; the database says NOW. Every window is NOW's."""
    async with pg_world() as world:
        await world.set_time(NOW)
        [db] = world.instances
        service = pg_accounts(db)
        await _ready_user(service, "alice")
        for _ in range(5):
            with pytest.raises(LoginFailedError):
                await service.login("alice", "not the password at all")
        alice = await service.store.find_user("alice")
        assert alice is not None
        assert alice.locked_until_ms == NOW + LOCK_MS
        with pytest.raises(LoginFailedError):  # still locked on the database's clock
            await service.login("alice", PASSWORD)
        await world.set_time(NOW + LOCK_MS + 1.0)
        # The login limiter is a counter on the instance's own clock (note 35), not a window in
        # Postgres: emptied here, so that only the lockout decides.
        await service.limiter.clear(service.limiter.key("alice"))
        result = await service.login("alice", PASSWORD)
        context = await service.authenticate(result.session_id)
        assert context.now_ms == NOW + LOCK_MS + 1.0
        until = await service.step_up(context, PASSWORD)
        assert until == NOW + LOCK_MS + 1.0 + 5 * 60_000.0
        # Claimed on the database's time, whatever the instance passes.
        id_hash = context.session.id_hash
        await world.set_time(until)
        assert await service.store.claim_step_up(id_hash, 0.0) is None
        await service.store.set_step_up(id_hash, until)
        await world.set_time(until - 1.0)
        assert await service.store.claim_step_up(id_hash, 1e15) == until


@postgres
async def test_a_keys_expiry_is_decided_on_the_databases_clock() -> None:
    async with pg_world() as world:
        await world.set_time(NOW)
        [db] = world.instances
        store = PostgresApplicationStore(db)
        await store.add_application(
            application_from_template(
                "shop", "Shop", PLANS, "pro", organization_id="default", created_at_ms=1.0
            )
        )
        keys = ApiKeyService(store, TEST_PEPPER, SystemEpoch(), store_clock=db)
        issued = await keys.issue("shop", "main", expires_at_ms=NOW + 60_000.0)
        assert (await keys.authenticate(issued.key)).id == "shop"
        await world.set_time(NOW + 60_000.0)
        with pytest.raises(InvalidApiKeyError):
            await keys.authenticate(issued.key)


# --- the atomic rules on two instances (postgres) -------------------------------------------------


@asynccontextmanager
async def _row_held(world: PgWorld, table: str, key: str, value: str) -> AsyncIterator[None]:
    """A row locked by another session until the block ends, so that the statements sent in it
    all start before any can finish."""
    await world.admin.execute("BEGIN")
    await world.admin.execute(
        f"SELECT 1 FROM {world.schema}.{table} WHERE {key} = $1 FOR UPDATE", value
    )
    try:
        yield
    finally:
        await world.admin.execute("COMMIT")


async def _until_waiting(world: PgWorld, count: int) -> None:
    """Until ``count`` other sessions are waiting for a lock (a state, polled)."""
    probe = await asyncpg.connect(os.environ[POSTGRES_URL_ENV])
    try:
        while (
            await probe.fetchval(
                "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'"
                " AND datname = current_database()"
            )
            < count
        ):
            await WallClock().sleep_ms(5.0)
    finally:
        await probe.close()


@postgres
async def test_wrong_passwords_on_two_instances_each_count() -> None:
    """Q3 on Postgres: four wrong passwords at once, two on each instance, all started before any
    is counted: four failures, not one."""
    async with pg_world(instances=2) as world:
        first, second = (pg_accounts(db, seed=i + 1) for i, db in enumerate(world.instances))
        alice = await first.create_user("alice", PASSWORD, "default")
        async with _row_held(world, "users", "id", alice.id):
            logins = [
                asyncio.create_task(service.login("alice", f"wrong password {i}"))
                for i, service in enumerate((first, second, first, second))
            ]
            await _until_waiting(world, 4)
        results = await asyncio.gather(*logins, return_exceptions=True)
        assert all(isinstance(r, LoginFailedError) for r in results), results
        counted = await second.store.find_user("alice")
        assert counted is not None
        assert (counted.failed_logins, counted.locked_until_ms) == (4, None)
        with pytest.raises(LoginFailedError):
            await second.login("alice", "the fifth wrong password")
        locked = await first.store.find_user("alice")
        assert locked is not None
        assert locked.locked_until_ms is not None


@postgres
async def test_a_step_up_window_is_claimed_once_across_instances() -> None:
    async with pg_world(instances=2) as world:
        await world.set_time(NOW)
        first, second = (pg_accounts(db, seed=i + 1) for i, db in enumerate(world.instances))
        await _ready_user(first, "alice")
        context = await first.authenticate((await first.login("alice", PASSWORD)).session_id)
        until = await first.step_up(context, PASSWORD)
        id_hash = context.session.id_hash
        async with _row_held(world, "sessions", "id_hash", id_hash):
            claims = [
                asyncio.create_task(service.store.claim_step_up(id_hash, NOW))
                for service in (first, second, first, second)
            ]
            await _until_waiting(world, 4)
        assert sorted(await asyncio.gather(*claims), key=str) == [None, None, None, until]


async def _management(db: PostgresDatabase) -> ManagementService:
    keys = ApiKeyService(PostgresApplicationStore(db), TEST_PEPPER, SystemEpoch(), store_clock=db)
    return ManagementService(
        keys,
        PLANS,
        AuditLog(postgres_state(db).audit),
        SystemEpoch(),
        limits=KeyLimits(max_active=3),
    )


@postgres
async def test_the_management_rules_hold_across_two_instances() -> None:
    """The last org_admin, an individual organization's one member, and the active-key cap: each
    asked of both instances at once, each decided once (section 3.6)."""
    async with pg_world(instances=2) as world:
        first, second = (pg_accounts(db, seed=i + 1) for i, db in enumerate(world.instances))
        await PostgresApplicationStore(world.instances[0]).add_organization(
            new_organization("solo", "Solo", "individual", created_at_ms=1.0)
        )
        for name in ("alice", "bob"):
            await first.create_user(name, PASSWORD, "default")
            await first.grant_role(name, "org_admin", "organization", "default")
        revoked = await asyncio.gather(
            first.revoke_role("alice", "org_admin", "organization", "default"),
            second.revoke_role("bob", "org_admin", "organization", "default"),
            return_exceptions=True,
        )
        assert sum(r is None for r in revoked) == 1, revoked
        joined = await asyncio.gather(
            first.create_user("carol", PASSWORD, "solo"),
            second.create_user("dave", PASSWORD, "solo"),
            return_exceptions=True,
        )
        assert sum(isinstance(r, User) for r in joined) == 1, joined
        managers = [await _management(db) for db in world.instances]
        await managers[0].create_application("default", "Shop", "pro", application_id="shop")
        issued = await asyncio.gather(
            *(managers[i % 2].issue_key("shop", f"key {i}", expires_in_days=30) for i in range(8)),
            return_exceptions=True,
        )
        assert sum(not isinstance(r, BaseException) for r in issued) == 3, issued
        assert len(await PostgresApplicationStore(world.instances[1]).list_keys("shop")) == 3


# --- the write lock: its timeout, and chat beside a slow management write (postgres) -------------


@asynccontextmanager
async def _slow_management_write(db: PostgresDatabase) -> AsyncIterator[asyncio.Event]:
    """A management write held inside the lock until the event is set."""
    inside, release = asyncio.Event(), asyncio.Event()

    async def hold() -> None:
        async with db.write_transaction():
            await PostgresApplicationStore(db).add_organization(
                new_organization("slow", "Slow", "business", created_at_ms=1.0)
            )
            inside.set()
            await release.wait()

    task = asyncio.create_task(hold())
    await inside.wait()
    try:
        yield release
    finally:
        release.set()
        await task


@postgres
async def test_the_write_lock_times_out_closed_and_writes_nothing() -> None:
    async with pg_world(instances=2, lock_timeout_ms=200.0) as world:
        first, second = world.instances
        async with _slow_management_write(first):
            with pytest.raises(StateUnavailableError, match="lock_timeout"):
                async with second.write_transaction():
                    await PostgresApplicationStore(second).add_organization(
                        new_organization("late", "Late", "business", created_at_ms=2.0)
                    )
        assert await world.rows("SELECT id FROM organizations WHERE id IN ('late', 'slow')") == [
            ("slow",)
        ]


@postgres
async def test_chat_on_two_instances_is_served_while_a_management_write_holds_the_lock() -> None:
    """Section 3.6: a management write holds the lock, and every management connection of both
    instances is checked out; chat requests on both instances are admitted, served and charged
    meanwhile. Waits on states; the bound only reports which requests had not finished."""
    clock = SimClock()
    async with pg_world(instances=2, management_pool_size=1) as world:
        first, second = world.instances
        store = PostgresApplicationStore(first)
        await store.add_application(
            application_from_template(
                "acme", "Acme", PLANS, "pro", organization_id="default", created_at_ms=1.0
            )
        )
        issuer = ApiKeyService(store, TEST_PEPPER, SystemEpoch(), store_clock=first)
        key = (await issuer.issue("acme", "main")).key
        startup = await store.list_applications()
        built = [_app_on(db, clock, startup) for db in world.instances]
        clients = [
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw")
            for app, _, _ in built
        ]
        body = {"model": "economy", "prompt": PROMPT, "max_tokens": 2}
        try:
            async with _slow_management_write(first):
                held = await second.management.acquire()  # first's one is in the write
                try:
                    requests = [
                        asyncio.create_task(
                            clients[i % 2].post("/v1/generate", json=body, headers=bearer(key))
                        )
                        for i in range(6)
                    ]
                    _, pending = await asyncio.wait(requests, timeout=10.0)
                    assert not pending, f"{len(pending)} of 6 requests had not finished"
                    statuses = [t.result().status_code for t in requests]
                    assert statuses == [200] * 6, [t.result().text for t in requests]
                    # Charged meanwhile, and the write still held (not committed).
                    assert await world.rows("SELECT count(*) FROM usage_events") == [(6,)]
                    assert await world.rows("SELECT id FROM organizations WHERE id = 'slow'") == []
                finally:
                    await second.management.release(held)
        finally:
            for client in clients:
                await client.aclose()
            for _, _, registry in built:
                await registry.aclose()


@postgres
async def test_a_closed_pool_is_answered_as_postgres_down() -> None:
    """The real driver's errors, not a stand-in's: a pool closed under the app."""
    clock = SimClock()
    _, issued = application_access(clock, apps={"acme": "pro"})
    async with pg_world() as world:
        [db] = world.instances
        app, _, registry = _app_on(db, clock, [])
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
                assert (await c.get("/readyz")).json() == {"status": "ready"}
                await db.requests.close()
                answer = await c.post(
                    "/v1/generate",
                    json={"model": "economy", "prompt": PROMPT},
                    headers=bearer(issued["acme"]),
                )
                assert (answer.status_code, answer.json()["error"]["code"]) == (
                    503,
                    "state_unavailable",
                )
                assert (await c.get("/readyz")).status_code == 503
        finally:
            await registry.aclose()


# --- the migration, both ways (postgres) ----------------------------------------------------------


def _populated(path: Path) -> sqlite3.Connection:
    """A SQLite database at the latest migration with every table filled."""
    connection = open_database(path)
    MigrationRunner(connection, MIGRATIONS, ClockEpoch(SimClock())).migrate()
    return connection


async def _fill(connection: sqlite3.Connection) -> None:
    await _scenario(sqlite_state(connection, ClockEpoch(SimClock())))
    for table in TABLES:
        assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] > 0, table


def _tables(connection: sqlite3.Connection) -> dict[str, list[tuple[Any, ...]]]:
    return {t: _sqlite_rows(connection, t) for t in TABLES}


@postgres
async def test_sqlite_to_postgres_and_back_gives_every_row_back(tmp_path: Path) -> None:
    source = _populated(tmp_path / "gateway.db")
    await _fill(source)
    original = _tables(source)
    async with pg_world() as world:
        [db] = world.instances
        dry = await migrate_to_postgres(source, db, dry_run=True)
        assert await world.rows("SELECT count(*) FROM usage_events") == [(0,)]
        report = await migrate_to_postgres(source, db, dry_run=False)
        assert [(t.table, t.rows, t.sha256) for t in dry.tables] == [
            (t.table, t.rows, t.sha256) for t in report.tables
        ]
        # A second copy into the same database is refused, with nothing changed.
        with pytest.raises(MigrationRefused, match="not new"):
            await migrate_to_postgres(source, db, dry_run=False)
        # The stores read what was copied, and new rows continue the sequences.
        state = postgres_state(db)
        assert [o.id for o in await state.applications.list_organizations()] == [
            row[0] for row in original["organizations"]
        ]
        seq = await state.usage.append(
            UsageEvent("app0", "after", "economy", 1, 0, 1, 0.1, 0.1, 200.0)
        )
        assert seq > max(row[0] for row in original["usage_events"])
        await state.usage.prune(seq + 1)  # back to what was copied
        # Back: a dry run writes no file; then a new file with every row.
        back = tmp_path / "back.db"
        await migrate_to_sqlite(db, back, dry_run=True)
        assert not back.exists()
        returned = await migrate_to_sqlite(db, back, dry_run=False)
        with pytest.raises(MigrationRefused, match="never overwrites"):
            await migrate_to_sqlite(db, back, dry_run=False)
    copy = open_database(back)
    try:
        assert _tables(copy) == original
        assert [t.sha256 for t in returned.tables] == [t.sha256 for t in report.tables]
    finally:
        copy.close()
        source.close()


@postgres
async def test_a_copy_that_differs_is_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _populated(tmp_path / "gateway.db")
    await _fill(source)
    import gateway.pg_migrate as pg_migrate

    real = pg_migrate._postgres_rows

    async def one_row_short(connection: Any, table: str, columns: Any) -> list[tuple[Any, ...]]:
        rows = await real(connection, table, columns)
        return rows[:-1] if table == "audit_log" else rows

    monkeypatch.setattr(pg_migrate, "_postgres_rows", one_row_short)
    async with pg_world() as world:
        [db] = world.instances
        with pytest.raises(MigrationRefused, match="audit_log differs"):
            await migrate_to_postgres(source, db, dry_run=False)
        assert await world.rows("SELECT count(*) FROM users") == [(0,)]
        back = tmp_path / "back.db"
        monkeypatch.setattr(pg_migrate, "_postgres_rows", real)
        monkeypatch.setattr(pg_migrate, "_sqlite_rows", lambda c, t: [])
        with pytest.raises(MigrationRefused, match="differs"):
            await migrate_to_sqlite(db, back, dry_run=False)
        assert not back.exists()
    source.close()


@postgres
def test_the_cli_migrates_both_ways(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = _populated(tmp_path / "gateway.db")
    asyncio.run(_fill(source))
    original = _tables(source)
    source.close()

    async def schema() -> str:
        admin = await asyncpg.connect(os.environ[POSTGRES_URL_ENV])
        name = "gwtest_cli_" + os.urandom(6).hex()
        await admin.execute(f"CREATE SCHEMA {name}")
        await admin.close()
        return name

    async def drop(name: str) -> None:
        admin = await asyncpg.connect(os.environ[POSTGRES_URL_ENV])
        await admin.execute(f"DROP SCHEMA {name} CASCADE")
        await admin.close()

    name = asyncio.run(schema())
    try:
        url = f"{os.environ[POSTGRES_URL_ENV]}?search_path={name}"
        env = {"GATEWAY_DATABASE": str(tmp_path / "gateway.db"), "GATEWAY_POSTGRES_URL": url}
        assert manage(["migrate-to-postgres", "--dry-run"], env) == 0
        assert "dry run" in capsys.readouterr().err
        assert manage(["migrate-to-postgres"], env) == 0
        assert manage(["migrate-to-postgres"], env) == 1
        assert "not new" in capsys.readouterr().err
        assert manage(["migrate-to-sqlite", "--to", str(tmp_path / "back.db")], env) == 0
        out = capsys.readouterr()
        assert url not in out.out + out.err
    finally:
        asyncio.run(drop(name))
    copy = open_database(tmp_path / "back.db")
    assert _tables(copy) == original
    copy.close()


# --- a leak scan (postgres) -----------------------------------------------------------------------


@postgres
async def test_the_password_in_the_address_is_in_no_log_error_or_row(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A password planted in the URL (the test server trusts local connections, so it is sent
    and ignored): it appears in no log record, error, repr or stored row after the stores and
    the lock have been used and have failed. The control: planted in a row, the scan finds it."""
    caplog.set_level(logging.DEBUG)
    base = os.environ[POSTGRES_URL_ENV]
    planted_url = base.replace("://postgres@", f"://postgres:{PLANTED}@")
    assert PLANTED in planted_url
    async with pg_world() as world:
        db = await PostgresDatabase.connect(
            planted_url, WallClock(), schema=world.schema, lock_timeout_ms=100.0
        )
        texts = [repr(db)]
        try:
            state = postgres_state(db)
            service = pg_accounts(db)
            await service.create_user("alice", PASSWORD, "default")
            with pytest.raises(LoginFailedError):
                await service.login("alice", "not the password at all")
            try:
                await state.applications.add_organization(
                    new_organization("default", "default", "business", created_at_ms=1.0)
                )
            except ValueError as exc:
                texts.append(str(exc))
            async with _slow_management_write(world.instances[0]):
                try:
                    async with db.write_transaction():
                        pass
                except StateUnavailableError as exc:
                    texts += [str(exc), repr(exc)]
            await db.requests.close()
            try:
                await state.usage.events()
            except StateUnavailableError as exc:
                texts += [str(exc), repr(exc)]
        finally:
            await db.aclose()
        rows = []
        for table in TABLES:
            rows += [repr(r) for r in await world.rows(f"SELECT * FROM {table}")]
        scanned = [*texts, caplog.text, *rows]
        assert len(texts) >= 4
        assert not [t for t in scanned if PLANTED in t]
        await world.admin.execute(f"UPDATE {world.schema}.audit_log SET details = $1", PLANTED)
        rows = [repr(r) for r in await world.rows("SELECT * FROM audit_log")]
        assert [t for t in [*texts, caplog.text, *rows] if PLANTED in t]


# --- the benchmark --------------------------------------------------------------------------------


def test_the_durable_call_benchmark_needs_a_postgres_to_measure_against(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GATEWAY_POSTGRES_URL", raising=False)
    with pytest.raises(SystemExit):
        durable_calls.main(["--calls", "1"])
    assert "GATEWAY_POSTGRES_URL" in capsys.readouterr().err


@postgres
async def test_the_durable_call_benchmark_times_each_call_and_leaves_no_schema() -> None:
    url = os.environ[POSTGRES_URL_ENV]
    rows = await durable_calls.run(1, url)
    assert [r.store for r in rows] == [
        f"{what}, {where}"
        for where in ("sqlite", "postgres")
        for what in ("read", "write", "chat, gateway tier", "chat, one provider")
    ]
    admin = await asyncpg.connect(url)
    try:
        left = await admin.fetch("SELECT nspname FROM pg_namespace WHERE nspname LIKE 'gwbench_%'")
    finally:
        await admin.close()
    assert left == []
