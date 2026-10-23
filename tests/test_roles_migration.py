"""Migration 8 (roles): the backfill from users.platform_admin, the individual organizations'
org_admin, nothing for anyone else, the rebuild of users, the reverse, and the backup through
the CLI. See docs/28-roles.md. Version-7 databases are written with plain SQL."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

from gateway.app import create_default_app
from gateway.clock import ClockEpoch, SimClock
from gateway.database import check_backup, open_database
from gateway.migrations import MIGRATIONS, MigrationError, MigrationRunner
from helpers import TEST_PEPPER

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from manage import main as manage

NOW_MS = 1.75e12


def _epoch() -> ClockEpoch:
    return ClockEpoch(SimClock(), origin_ms=NOW_MS)


def _version7(path: Path) -> sqlite3.Connection:
    """Organizations: default (business), solo (individual, one member), pair (business),
    empty (individual, no member). Users: root (platform_admin), sam (solo's member), and
    bea and bob in pair, with a session for bob."""
    connection = open_database(path)
    MigrationRunner(connection, MIGRATIONS, _epoch()).migrate(7)
    for org_id, kind in (("solo", "individual"), ("pair", "business"), ("empty", "individual")):
        connection.execute(
            "INSERT INTO organizations (id, name, kind, created_at_ms) VALUES (?, ?, ?, 0)",
            (org_id, org_id.title(), kind),
        )
    for user_id, name, org, admin in (
        ("usr_0000000000000001", "root", "default", 1),
        ("usr_0000000000000002", "sam", "solo", 0),
        ("usr_0000000000000003", "bea", "pair", 0),
        ("usr_0000000000000004", "bob", "pair", 0),
    ):
        connection.execute(
            "INSERT INTO users (id, username, password_hash, organization_id, created_at_ms,"
            " platform_admin) VALUES (?, ?, '$argon2id$x', ?, 0, ?)",
            (user_id, name, org, admin),
        )
    connection.execute(
        "INSERT INTO sessions (id_hash, handle, user_id, csrf_hash, created_at_ms,"
        " last_seen_at_ms, expires_at_ms) VALUES ('h', 'ses_0000000000000001',"
        " 'usr_0000000000000004', 'c', 0, 0, 1)"
    )
    return connection


def _rows(connection: sqlite3.Connection, query: str) -> list[tuple[Any, ...]]:
    return connection.execute(query).fetchall()


def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in _rows(connection, f"PRAGMA table_info({table})")]


def _foreign_keys(connection: sqlite3.Connection, table: str) -> list[tuple[str, str, str]]:
    return [(r[2], r[3], r[4]) for r in _rows(connection, f"PRAGMA foreign_key_list({table})")]


@pytest.fixture
def v7(tmp_path: Path) -> sqlite3.Connection:
    return _version7(tmp_path / "gateway.db")


def test_the_backfill_gives_roles_only_where_the_data_says(v7: sqlite3.Connection) -> None:
    users_before = _rows(v7, "SELECT id, username, organization_id FROM users ORDER BY rowid")
    assert MigrationRunner(v7, MIGRATIONS, _epoch()).migrate(8) == (8,)
    assert _rows(
        v7,
        "SELECT user_id, role, scope_type, scope_id, granted_by, granted_at_ms"
        " FROM role_assignments ORDER BY rowid",
    ) == [
        ("usr_0000000000000001", "platform_admin", "platform", "*", None, NOW_MS),
        ("usr_0000000000000002", "org_admin", "organization", "solo", None, NOW_MS),
    ]
    # bea and bob, members of a business organization, hold nothing.
    assert "platform_admin" not in _columns(v7, "users")
    assert _rows(v7, "SELECT id, username, organization_id FROM users ORDER BY rowid") == (
        users_before
    )


def test_after_the_rebuild_every_foreign_key_points_at_the_new_users(
    v7: sqlite3.Connection,
) -> None:
    MigrationRunner(v7, MIGRATIONS, _epoch()).migrate()
    assert v7.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert _rows(v7, "PRAGMA foreign_key_check") == []
    assert _foreign_keys(v7, "sessions") == [("users", "user_id", "id")]
    assert _foreign_keys(v7, "role_assignments") == [("users", "user_id", "id")]
    assert _foreign_keys(v7, "app_roles") == [("applications", "application_id", "id")]
    assert _foreign_keys(v7, "users") == [("organizations", "organization_id", "id")]
    with pytest.raises(sqlite3.IntegrityError):
        v7.execute(
            "INSERT INTO role_assignments (user_id, role, scope_type, scope_id, granted_at_ms)"
            " VALUES ('usr_ffffffffffffffff', 'viewer', 'organization', 'pair', 0)"
        )
    with pytest.raises(sqlite3.IntegrityError):  # the platform scope's id is always "*"
        v7.execute(
            "INSERT INTO role_assignments (user_id, role, scope_type, scope_id, granted_at_ms)"
            " VALUES ('usr_0000000000000003', 'platform_admin', 'platform', 'pair', 0)"
        )
    with pytest.raises(sqlite3.IntegrityError):  # one row per user, role and scope
        v7.execute(
            "INSERT INTO role_assignments (user_id, role, scope_type, scope_id, granted_at_ms)"
            " VALUES ('usr_0000000000000001', 'platform_admin', 'platform', '*', 0)"
        )


def test_apply_reverse_and_apply_again(v7: sqlite3.Connection) -> None:
    columns = _columns(v7, "users")
    admins = _rows(v7, "SELECT id, platform_admin FROM users ORDER BY rowid")
    runner = MigrationRunner(v7, MIGRATIONS, _epoch())
    assert runner.migrate(8) == (8,)
    assert runner.migrate(8) == ()
    assert runner.rollback(7) == (8,)
    assert _columns(v7, "users") == columns
    assert _rows(v7, "SELECT id, platform_admin FROM users ORDER BY rowid") == admins
    tables = {r[0] for r in _rows(v7, "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert not {"role_assignments", "app_roles"} & tables
    assert v7.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert runner.migrate(8) == (8,)


@pytest.mark.parametrize("extra", ["viewer", "app role"])
def test_the_reverse_refuses_once_roles_hold_information(
    v7: sqlite3.Connection, extra: str
) -> None:
    runner = MigrationRunner(v7, MIGRATIONS, _epoch())
    runner.migrate()
    if extra == "viewer":
        v7.execute(
            "INSERT INTO role_assignments (user_id, role, scope_type, scope_id, granted_at_ms)"
            " VALUES ('usr_0000000000000003', 'viewer', 'organization', 'pair', 0)"
        )
    else:
        v7.execute(
            "INSERT INTO applications (id, name, plan_name, models, price_multiplier,"
            " monthly_token_budget, created_at_ms, organization_id)"
            " VALUES ('shop', 'Shop', 'pro', '[]', 1, 1, 0, 'pair')"
        )
        v7.execute("INSERT INTO app_roles VALUES ('shop', 'clinic_staff', 0)")
    with pytest.raises(MigrationError, match="cannot be reversed"):
        runner.rollback(7)
    assert runner.current_version() == 8


def test_migrate_backs_up_before_rebuilding_users(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "gateway.db"
    _version7(path).close()
    environ = {"GATEWAY_DATABASE": str(path), "GATEWAY_KEY_PEPPER": TEST_PEPPER.decode()}
    assert manage(["migrate", "--dry-run"], environ) == 0
    err = capsys.readouterr().err
    assert "migration 8 roles (rebuilds a table):" in err
    assert "platform_admin for 1 user(s)" in err
    assert "org_admin for the sole member of 1 individual organization(s)" in err
    assert "no role for the other 2 user(s)" in err
    assert manage(["migrate"], environ) == 0
    [backup] = tmp_path.glob("gateway.db.before-migration-8-*.db")
    assert check_backup(backup) == 7
    assert f"to {backup} before migration 8" in capsys.readouterr().err


def test_the_server_will_not_rebuild_users_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "server.db"
    _version7(path).close()
    monkeypatch.setenv("GATEWAY_DATABASE", str(path))
    with pytest.raises(MigrationError, match=r"needs migration 8.*manage\.py migrate"):
        create_default_app()
