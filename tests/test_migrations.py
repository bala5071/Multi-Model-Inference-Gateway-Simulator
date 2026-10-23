"""The migration runner: apply, reverse, idempotence, backfill, and refusing bad states."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from gateway.clock import ClockEpoch, SimClock
from gateway.database import open_database
from gateway.migrations import MIGRATIONS, Migration, MigrationError, MigrationRunner


def _tables(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    return {str(row[0]) for row in rows}


def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")]


# Two test-only migrations: a table, then a new column filled in from the existing rows.
def _create_items(c: sqlite3.Connection) -> None:
    c.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, price_cents INTEGER NOT NULL)")


def _drop_items(c: sqlite3.Connection) -> None:
    c.execute("DROP TABLE items")


def _add_dollars(c: sqlite3.Connection) -> None:
    c.execute("ALTER TABLE items ADD COLUMN price_usd REAL")


def _fill_dollars(c: sqlite3.Connection, applied_at_ms: float) -> None:
    c.execute("UPDATE items SET price_usd = price_cents / 100.0")


def _drop_dollars(c: sqlite3.Connection) -> None:
    # A rebuild, not ALTER TABLE ... DROP COLUMN, which needs SQLite 3.35 or later.
    c.execute("CREATE TABLE items_new (id INTEGER PRIMARY KEY, price_cents INTEGER NOT NULL)")
    c.execute("INSERT INTO items_new (id, price_cents) SELECT id, price_cents FROM items")
    c.execute("DROP TABLE items")
    c.execute("ALTER TABLE items_new RENAME TO items")


ITEMS = Migration(1, "items", _create_items, _drop_items)
DOLLARS = Migration(2, "price_usd", _add_dollars, _drop_dollars, backfill=_fill_dollars)


def _runner(
    connection: sqlite3.Connection, migrations: tuple[Migration, ...] = (ITEMS, DOLLARS)
) -> MigrationRunner:
    return MigrationRunner(connection, migrations, ClockEpoch(SimClock(), origin_ms=1000.0))


@pytest.fixture
def db() -> sqlite3.Connection:
    return open_database(":memory:")


def test_migrate_applies_every_migration_in_order_and_records_it(db: sqlite3.Connection) -> None:
    runner = _runner(db)
    assert runner.current_version() == 0

    assert runner.migrate() == (1, 2)

    assert runner.applied() == (1, 2)
    assert _columns(db, "items") == ["id", "price_cents", "price_usd"]
    rows = db.execute("SELECT version, name, applied_at_ms FROM schema_migrations").fetchall()
    assert rows == [(1, "items", 1000.0), (2, "price_usd", 1000.0)]


def test_migrate_is_idempotent(db: sqlite3.Connection) -> None:
    runner = _runner(db)
    runner.migrate()
    assert runner.migrate() == ()
    # A new runner on the same database sees the same versions and does nothing.
    assert _runner(db).migrate() == ()
    assert _runner(db).applied() == (1, 2)


def test_migrate_stops_at_a_target(db: sqlite3.Connection) -> None:
    runner = _runner(db)
    assert runner.migrate(1) == (1,)
    assert _columns(db, "items") == ["id", "price_cents"]
    assert runner.migrate() == (2,)


def test_backfill_fills_the_new_column_from_existing_rows(db: sqlite3.Connection) -> None:
    runner = _runner(db)
    runner.migrate(1)
    db.executemany("INSERT INTO items (id, price_cents) VALUES (?, ?)", [(1, 250), (2, 5)])

    runner.migrate()

    assert db.execute("SELECT id, price_usd FROM items ORDER BY id").fetchall() == [
        (1, 2.5),
        (2, 0.05),
    ]


def test_rollback_reverses_newest_first_and_forgets_the_versions(db: sqlite3.Connection) -> None:
    runner = _runner(db)
    runner.migrate()
    db.execute("INSERT INTO items (id, price_cents) VALUES (1, 250)")

    assert runner.rollback(1) == (2,)
    assert _columns(db, "items") == ["id", "price_cents"]
    assert db.execute("SELECT * FROM items").fetchall() == [(1, 250)]
    assert runner.applied() == (1,)

    assert runner.rollback(0) == (1,)
    assert "items" not in _tables(db)
    assert runner.applied() == ()
    assert runner.rollback(0) == ()


def test_up_down_up_gives_the_same_schema(db: sqlite3.Connection) -> None:
    runner = _runner(db)
    runner.migrate()
    schema = db.execute("SELECT sql FROM sqlite_master WHERE name = 'items'").fetchone()
    runner.rollback(0)
    runner.migrate()
    assert db.execute("SELECT sql FROM sqlite_master WHERE name = 'items'").fetchone() == schema


def test_a_failing_backfill_leaves_the_schema_as_it_was(db: sqlite3.Connection) -> None:
    def broken(c: sqlite3.Connection, applied_at_ms: float) -> None:
        c.execute("UPDATE items SET price_usd = 1")
        raise RuntimeError("backfill failed")

    runner = _runner(db, (ITEMS, Migration(2, "price_usd", _add_dollars, _drop_dollars, broken)))
    runner.migrate(1)
    with pytest.raises(RuntimeError, match="backfill failed"):
        runner.migrate()
    assert runner.applied() == (1,)
    assert _columns(db, "items") == ["id", "price_cents"]


def test_migrations_must_be_numbered_from_one_without_gaps(db: sqlite3.Connection) -> None:
    with pytest.raises(MigrationError, match="numbered"):
        _runner(db, (ITEMS, Migration(3, "x", _add_dollars, _drop_dollars)))
    with pytest.raises(MigrationError, match="numbered"):
        _runner(db, (DOLLARS, ITEMS))


def test_a_database_newer_than_the_code_is_refused(db: sqlite3.Connection) -> None:
    _runner(db).migrate()
    older_code = _runner(db, (ITEMS,))
    with pytest.raises(MigrationError, match="newer than this code"):
        older_code.migrate()


def test_targets_outside_the_known_versions_are_refused(db: sqlite3.Connection) -> None:
    runner = _runner(db)
    with pytest.raises(MigrationError, match="no migration 3"):
        runner.migrate(3)
    runner.migrate()
    with pytest.raises(MigrationError, match="use rollback"):
        runner.migrate(1)
    with pytest.raises(MigrationError, match="cannot roll back to 3"):
        runner.rollback(3)


def test_the_runner_needs_an_autocommit_connection() -> None:
    connection = sqlite3.connect(":memory:")  # default isolation level: implicit BEGIN
    with pytest.raises(MigrationError, match="autocommit"):
        _runner(connection)


# --- the gateway's own migrations ---------------------------------------------------------


def test_the_gateway_migrations_create_exactly_their_tables(tmp_path: Path) -> None:
    db = open_database(tmp_path / "gateway.db")
    runner = MigrationRunner(db, MIGRATIONS, ClockEpoch(SimClock()))

    assert runner.migrate(1) == (1,)
    assert _tables(db) == {"schema_migrations", "usage_events"}
    assert _columns(db, "usage_events") == [
        "seq",
        "application_id",
        "request_id",
        "model",
        "prompt_tokens",
        "cached_tokens",
        "completion_tokens",
        "base_cost_usd",
        "billed_cost_usd",
        "occurred_at_ms",
    ]

    assert runner.migrate(2) == (2,)
    assert _tables(db) == {"schema_migrations", "usage_events", "applications", "api_keys"}
    # No organization column: organizations arrive in A3 with their own migration.
    assert _columns(db, "applications") == [
        "id",
        "name",
        "plan_name",
        "models",
        "price_multiplier",
        "monthly_token_budget",
        "max_tokens",
        "requests_per_minute",
        "created_at_ms",
    ]
    assert _columns(db, "api_keys") == [
        "key_id",
        "application_id",
        "label",
        "key_hmac",
        "created_at_ms",
        "expires_at_ms",
        "revoked",
        "last_used_at_ms",
    ]

    # Migration 3 adds an index on usage_events by time, and no column.
    assert runner.migrate(3) == (3,)
    indexes = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "usage_events_by_time" in indexes
    assert len(_columns(db, "usage_events")) == 10

    # Migration 4 adds organizations and a required applications.organization_id
    # (docs/26-organizations.md; tests/test_organizations_migration.py covers it in depth).
    assert runner.migrate(4) == (4,)
    every_table = {"schema_migrations", "usage_events", "applications", "api_keys"}
    assert _tables(db) == every_table | {"organizations"}
    assert _columns(db, "organizations") == ["id", "name", "kind", "created_at_ms", "disabled"]
    assert _columns(db, "applications")[-1] == "organization_id"

    # Migrations 5 to 9: users, sessions, the audit log, roles, step-up (docs/27 to 29);
    # 10 and 11: providers and their sealed keys (docs/32).
    assert runner.migrate() == (5, 6, 7, 8, 9, 10, 11)
    accounts = {"users", "sessions", "audit_log", "role_assignments", "app_roles"}
    accounts |= {"provider_configs", "provider_keys"}
    assert _tables(db) == every_table | {"organizations"} | accounts

    assert runner.rollback(1) == (11, 10, 9, 8, 7, 6, 5, 4, 3, 2)
    assert _tables(db) == {"schema_migrations", "usage_events"}
    assert runner.rollback(0) == (1,)
    assert _tables(db) == {"schema_migrations"}
    assert runner.migrate() == (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)
    assert _tables(db) == every_table | {"organizations"} | accounts
