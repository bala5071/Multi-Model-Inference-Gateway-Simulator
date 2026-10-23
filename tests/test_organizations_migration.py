"""Migration 4 (organizations): apply, reverse, idempotence, the backfill on a database that
already has applications, keys and usage, the conflicts that make it refuse, foreign keys
around the table rebuild, the dry-run plan, and the server's refusal to rebuild a table
itself. See docs/26-organizations.md.

The version-3 databases here are written with plain SQL, as an existing installation is:
the stores of this version need migration 4.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import random
import sqlite3
import struct
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from gateway.apikeys import ApiKeyService, InvalidApiKeyError
from gateway.app import create_default_app
from gateway.applications import SqliteApplicationStore
from gateway.clock import ClockEpoch, SimClock
from gateway.database import open_database
from gateway.migrations import (
    MIGRATIONS,
    Migration,
    MigrationError,
    MigrationRunner,
    plan_migrations,
)
from gateway.migrations import m0004_organizations as m4
from gateway.organizations import DEFAULT_ORGANIZATION_ID, new_organization
from helpers import TEST_PEPPER

NOW_MS = 1.75e12
HOUR_MS = 3_600_000.0
V3_APP_COLUMNS = (
    "id, name, plan_name, models, price_multiplier, monthly_token_budget, max_tokens, "
    "requests_per_minute, created_at_ms"
)


def _epoch() -> ClockEpoch:
    return ClockEpoch(SimClock(), origin_ms=NOW_MS)


def _version3(path: Path) -> sqlite3.Connection:
    connection = open_database(path)
    MigrationRunner(connection, MIGRATIONS, _epoch()).migrate(3)
    return connection


def _add_application(connection: sqlite3.Connection, app_id: str, name: str, budget: int) -> None:
    connection.execute(
        f"INSERT INTO applications ({V3_APP_COLUMNS})"
        " VALUES (?, ?, 'pro', ?, 1.25, ?, NULL, 60, ?)",
        (app_id, name, '["economy", "standard"]', budget, NOW_MS - 10 * HOUR_MS),
    )


def _add_key(
    connection: sqlite3.Connection,
    rng: random.Random,
    app_id: str,
    *,
    revoked: bool = False,
    expires_at_ms: float | None = None,
    last_used_at_ms: float | None = None,
) -> str:
    """A key in the format the service issues, stored as the service stores it."""
    key_id = rng.randbytes(8).hex()
    secret = base64.urlsafe_b64encode(rng.randbytes(32)).rstrip(b"=").decode()
    key = f"gwk_{key_id}_{secret}"
    digest = hmac.new(TEST_PEPPER, key.encode(), hashlib.sha256).hexdigest()
    connection.execute(
        "INSERT INTO api_keys (key_id, application_id, label, key_hmac, created_at_ms,"
        " expires_at_ms, revoked, last_used_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            key_id,
            app_id,
            f"{app_id} key",
            digest,
            NOW_MS - HOUR_MS,
            expires_at_ms,
            int(revoked),
            last_used_at_ms,
        ),
    )
    return key


def _rows(connection: sqlite3.Connection, query: str) -> list[tuple[Any, ...]]:
    return connection.execute(query).fetchall()


def _fingerprint(rows: list[tuple[Any, ...]]) -> str:
    """A digest of the exact values: floats by their bits, everything else by its repr."""
    digest = hashlib.sha256()
    for row in rows:
        for value in row:
            if isinstance(value, float):
                digest.update(b"f" + struct.pack("<d", value))
            else:
                digest.update(repr(value).encode())
            digest.update(b"\x00")
    return digest.hexdigest()


def _foreign_keys(connection: sqlite3.Connection, table: str) -> list[tuple[str, str, str]]:
    """(referenced table, from column, to column) for each foreign key of ``table``."""
    return [(r[2], r[3], r[4]) for r in _rows(connection, f"PRAGMA foreign_key_list({table})")]


def _table_info(connection: sqlite3.Connection, table: str) -> list[tuple[Any, ...]]:
    return _rows(connection, f"PRAGMA table_info({table})")


def _foreign_keys_on(connection: sqlite3.Connection) -> bool:
    return bool(connection.execute("PRAGMA foreign_keys").fetchone()[0])


@pytest.fixture
def v3(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = _version3(tmp_path / "gateway.db")
    yield connection
    connection.close()


# --- a realistic database -------------------------------------------------------------------


def test_a_realistic_database_keeps_every_key_and_every_event_exactly(
    v3: sqlite3.Connection,
) -> None:
    """Several applications, keys in every state and thousands of usage events. After the
    migration every key authenticates as it did before (what version 3's key check decided
    for each state) and every stored row is the same, value for value."""
    rng = random.Random(42)
    apps = ["initech", "globex", "acme", "shop-one", "shop-two", "tools"]
    for i, app_id in enumerate(apps):
        _add_application(v3, app_id, app_id.title(), budget=1000 * (i + 1))
    expected: dict[str, str | None] = {}
    for app_id in apps:
        expected[_add_key(v3, rng, app_id, last_used_at_ms=NOW_MS - 5.0)] = app_id  # active
        expected[_add_key(v3, rng, app_id, revoked=True)] = None
        expected[_add_key(v3, rng, app_id, expires_at_ms=NOW_MS - 1.0)] = None  # expired
        # Rotated: the old key in its grace period, and its replacement.
        expected[_add_key(v3, rng, app_id, expires_at_ms=NOW_MS + HOUR_MS)] = app_id
        expected[_add_key(v3, rng, app_id)] = app_id
        # Rotated, with the grace period over.
        expected[_add_key(v3, rng, app_id, expires_at_ms=NOW_MS - HOUR_MS)] = None
    for seq in range(5000):
        prompt = rng.randrange(1, 4000)
        v3.execute(
            "INSERT INTO usage_events (application_id, request_id, model, prompt_tokens,"
            " cached_tokens, completion_tokens, base_cost_usd, billed_cost_usd, occurred_at_ms)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rng.choice(apps),
                f"req-{seq}",
                rng.choice(["economy", "standard", "premium"]),
                prompt,
                rng.randrange(0, prompt + 1),
                rng.randrange(0, 512),
                rng.random() / 1000,
                rng.random() / 700,
                NOW_MS - rng.random() * 40 * 24 * HOUR_MS,
            ),
        )
    events = "SELECT * FROM usage_events ORDER BY seq"
    event_types = (
        "SELECT typeof(seq), typeof(application_id), typeof(prompt_tokens),"
        " typeof(base_cost_usd), typeof(occurred_at_ms) FROM usage_events ORDER BY seq"
    )
    keys = "SELECT * FROM api_keys ORDER BY rowid"
    applications = f"SELECT {V3_APP_COLUMNS} FROM applications ORDER BY rowid"
    before = {q: _rows(v3, q) for q in (events, event_types, keys, applications)}
    assert len(before[events]) == 5000
    assert len(before[keys]) == 36

    assert MigrationRunner(v3, MIGRATIONS, _epoch()).migrate(4) == (4,)

    for query, rows in before.items():
        after = _rows(v3, query)
        assert after == rows, query
        assert _fingerprint(after) == _fingerprint(rows), query
    assert {r[0] for r in _rows(v3, "SELECT organization_id FROM applications")} == {
        DEFAULT_ORGANIZATION_ID
    }
    service = ApiKeyService(SqliteApplicationStore(v3), TEST_PEPPER, _epoch())
    for key, application in expected.items():
        if application is None:
            with pytest.raises(InvalidApiKeyError):
                service.authenticate(key)
        else:
            assert service.authenticate(key).id == application
    assert sum(a is not None for a in expected.values()) == 18


# --- foreign keys around the rebuild --------------------------------------------------------


def test_after_the_rebuild_the_foreign_keys_point_at_the_new_tables(
    v3: sqlite3.Connection,
) -> None:
    _add_application(v3, "acme", "Acme", 100)
    _add_key(v3, random.Random(1), "acme")
    assert _foreign_keys(v3, "api_keys") == [("applications", "application_id", "id")]
    assert _foreign_keys(v3, "usage_events") == []  # it never had one

    MigrationRunner(v3, MIGRATIONS, _epoch()).migrate()

    print(f"SQLite {sqlite3.sqlite_version}")
    assert _foreign_keys_on(v3)
    assert _rows(v3, "PRAGMA foreign_key_check") == []
    assert _foreign_keys(v3, "api_keys") == [("applications", "application_id", "id")]
    assert _foreign_keys(v3, "usage_events") == []
    assert _foreign_keys(v3, "applications") == [("organizations", "organization_id", "id")]
    # They are enforced against the rebuilt table, not just declared.
    with pytest.raises(sqlite3.IntegrityError):
        v3.execute(
            "INSERT INTO api_keys (key_id, application_id, label, key_hmac, created_at_ms)"
            " VALUES ('k', 'nobody', 'x', 'h', 0)"
        )
    with pytest.raises(sqlite3.IntegrityError):
        v3.execute("DELETE FROM applications WHERE id = 'acme'")  # it has a key
    with pytest.raises(sqlite3.IntegrityError):
        v3.execute("UPDATE applications SET organization_id = 'nobody'")


def test_a_rebuild_that_would_leave_dangling_rows_is_refused_and_foreign_keys_come_back(
    v3: sqlite3.Connection,
) -> None:
    _add_application(v3, "acme", "Acme", 100)
    _add_key(v3, random.Random(1), "acme")

    def drop_acme(connection: sqlite3.Connection, applied_at_ms: float) -> None:
        m4.MIGRATION.backfill(connection, applied_at_ms)  # type: ignore[misc]
        connection.execute("DELETE FROM applications WHERE id = 'acme'")  # its key dangles

    broken = replace(m4.MIGRATION, backfill=drop_acme)
    runner = MigrationRunner(v3, (*MIGRATIONS[:3], broken), _epoch())
    with pytest.raises(MigrationError, match="pointing at nothing"):
        runner.migrate()
    assert runner.current_version() == 3
    assert _foreign_keys_on(v3)
    assert [r[0] for r in _rows(v3, "SELECT id FROM applications")] == ["acme"]
    assert "organizations" not in {r[0] for r in _rows(v3, "SELECT name FROM sqlite_master")}


def test_a_failing_backfill_leaves_version_3_as_it_was(v3: sqlite3.Connection) -> None:
    _add_application(v3, "acme", "Acme", 100)
    schema = _rows(v3, "SELECT type, name, sql FROM sqlite_master ORDER BY name")

    def fail(connection: sqlite3.Connection, applied_at_ms: float) -> None:
        m4.MIGRATION.backfill(connection, applied_at_ms)  # type: ignore[misc]
        raise RuntimeError("backfill failed")

    runner = MigrationRunner(v3, (*MIGRATIONS[:3], replace(m4.MIGRATION, backfill=fail)), _epoch())
    with pytest.raises(RuntimeError, match="backfill failed"):
        runner.migrate()
    assert runner.current_version() == 3
    assert _rows(v3, "SELECT type, name, sql FROM sqlite_master ORDER BY name") == schema
    assert _foreign_keys_on(v3)


# --- apply, reverse, idempotence ------------------------------------------------------------


def test_the_default_organization_is_made_even_with_no_applications(
    tmp_path: Path,
) -> None:
    connection = open_database(tmp_path / "new.db")
    clock = SimClock()
    runner = MigrationRunner(connection, MIGRATIONS, ClockEpoch(clock, origin_ms=NOW_MS))
    assert runner.migrate() == (1, 2, 3, 4, 5, 6, 7, 8)
    assert runner.migrate() == ()  # idempotent
    [(org_id, name, kind, created, disabled)] = _rows(connection, "SELECT * FROM organizations")
    applied = _rows(connection, "SELECT applied_at_ms FROM schema_migrations WHERE version = 4")
    # Its creation time is the time the migration is recorded as applied, from the epoch.
    assert (org_id, name, kind, created, disabled) == ("default", "default", "business", NOW_MS, 0)
    assert applied == [(NOW_MS,)]
    connection.close()


def test_apply_reverse_and_apply_again(v3: sqlite3.Connection) -> None:
    for app_id in ("initech", "acme"):
        _add_application(v3, app_id, app_id.title(), 100)
    _add_key(v3, random.Random(1), "acme")
    original = {t: _table_info(v3, t) for t in ("applications", "api_keys", "usage_events")}
    rows = _rows(v3, f"SELECT {V3_APP_COLUMNS} FROM applications ORDER BY rowid")
    runner = MigrationRunner(v3, MIGRATIONS, _epoch())

    assert runner.migrate(4) == (4,)
    assert runner.rollback(3) == (4,)
    assert {t: _table_info(v3, t) for t in original} == original
    assert _rows(v3, f"SELECT {V3_APP_COLUMNS} FROM applications ORDER BY rowid") == rows
    assert _foreign_keys(v3, "api_keys") == [("applications", "application_id", "id")]
    assert _rows(v3, "PRAGMA foreign_key_check") == []
    assert _foreign_keys_on(v3)
    tables = {r[0] for r in _rows(v3, "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "organizations" not in tables

    assert runner.migrate(4) == (4,)
    assert runner.migrate(4) == ()
    assert _rows(v3, "SELECT id, organization_id FROM applications ORDER BY rowid") == [
        ("initech", "default"),
        ("acme", "default"),
    ]


@pytest.mark.parametrize("change", ["second organization", "renamed", "disabled"])
def test_the_reverse_refuses_once_organizations_hold_information(
    v3: sqlite3.Connection, change: str
) -> None:
    _add_application(v3, "acme", "Acme", 100)
    runner = MigrationRunner(v3, MIGRATIONS, _epoch())
    runner.migrate()
    store = SqliteApplicationStore(v3)
    if change == "second organization":
        store.add_organization(new_organization("north", "North", "business", created_at_ms=0))
    elif change == "renamed":
        v3.execute("UPDATE organizations SET name = 'Main' WHERE id = 'default'")
    else:
        store.set_organization_disabled("default", True)
    with pytest.raises(MigrationError, match="cannot be reversed"):
        runner.rollback(3)
    assert runner.current_version() == 4
    assert _rows(v3, "SELECT id, organization_id FROM applications") == [("acme", "default")]
    assert _foreign_keys_on(v3)


# --- names that would collide ---------------------------------------------------------------


def test_duplicate_names_make_the_migration_refuse_naming_them(v3: sqlite3.Connection) -> None:
    for app_id, name in (
        ("shop-b", "Shop"),
        ("shop-a", "Shop"),
        ("tools", "Tools"),
        ("t2", "Tools"),
        ("acme", "Acme"),
    ):
        _add_application(v3, app_id, name, 100)
    schema = _rows(v3, "SELECT type, name, sql FROM sqlite_master ORDER BY name")
    runner = MigrationRunner(v3, MIGRATIONS, _epoch())
    with pytest.raises(MigrationError) as excinfo:
        runner.migrate()
    message = str(excinfo.value)
    assert "'Shop' is used by shop-a, shop-b" in message
    assert "'Tools' is used by t2, tools" in message
    assert "Acme" not in message
    assert runner.current_version() == 3
    assert _rows(v3, "SELECT type, name, sql FROM sqlite_master ORDER BY name") == schema
    assert _foreign_keys_on(v3)

    # Renaming one of each is the fix.
    v3.execute("UPDATE applications SET name = 'Shop B' WHERE id = 'shop-b'")
    v3.execute("UPDATE applications SET name = 'Tools 2' WHERE id = 't2'")
    assert runner.migrate(4) == (4,)


# --- the plan, read-only --------------------------------------------------------------------


def test_the_plan_reports_changes_and_conflicts_without_writing(tmp_path: Path) -> None:
    path = tmp_path / "gateway.db"
    connection = _version3(path)
    _add_application(connection, "a", "Shop", 100)
    _add_application(connection, "b", "Shop", 100)
    connection.close()
    before = path.read_bytes()

    readonly = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    plan = plan_migrations(readonly, MIGRATIONS)
    readonly.close()
    assert plan.current_version == 3
    assert [m.version for m, _ in plan.pending] == [4, 5, 6, 7, 8]
    assert plan.rebuilds_tables
    preview = plan.pending[0][1]
    assert any("2 application(s) into 'default'" in c for c in preview.changes)
    assert plan.conflicts == (
        "application name 'Shop' is used by a, b: names must be unique within an organization, "
        "and these would all be in 'default'",
    )
    assert path.read_bytes() == before

    empty = sqlite3.connect(":memory:")
    fresh = plan_migrations(empty, MIGRATIONS)
    assert (fresh.current_version, len(fresh.pending), fresh.conflicts) == (0, 8, ())
    assert _rows(empty, "SELECT name FROM sqlite_master") == []  # nothing created
    empty.close()


def test_a_migration_without_a_preview_is_described_by_its_name() -> None:
    plain = Migration(1, "plain", lambda c: None, lambda c: None)
    plan = plan_migrations(sqlite3.connect(":memory:"), (plain,))
    assert plan.pending[0][1].changes == ("plain",)
    assert not plan.rebuilds_tables


# --- the server does not rebuild a table itself ---------------------------------------------


def test_the_server_refuses_a_table_rebuild_and_leaves_the_database_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "server.db"
    _version3(path).close()
    monkeypatch.setenv("GATEWAY_DATABASE", str(path))
    with pytest.raises(MigrationError, match=r"manage\.py migrate"):
        create_default_app()
    check = open_database(path)
    assert MigrationRunner(check, MIGRATIONS, _epoch()).current_version() == 3
    check.close()
    # Its lock was released: once migrated, the server starts on the same file.
    check = open_database(path)
    MigrationRunner(check, MIGRATIONS, _epoch()).migrate()
    check.close()
    app = create_default_app()
    app.state.database_lock.release()


def test_the_server_migrates_a_new_database_itself(tmp_path: Path) -> None:
    app = create_default_app()  # GATEWAY_DATABASE is a new file (tests/conftest.py)
    app.state.database_lock.release()
