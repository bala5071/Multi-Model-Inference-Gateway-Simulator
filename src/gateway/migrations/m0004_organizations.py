"""Migration 4: organizations, and every application in one.

See docs/26-organizations.md. Adds ``organizations`` and a required
``applications.organization_id``. Every existing application goes into one organization,
``default`` (kind ``business``), which is created even when there are no applications, so
a new database and an upgraded one look the same.

SQLite cannot add a required foreign-key column to a table that has rows, so
``applications`` is rebuilt (SQLite's documented procedure: create the new table, copy,
drop the old one, rename): it is marked ``rebuilds_tables``, and the runner turns foreign
keys off around it and checks them before committing. The rebuild is in the backfill
because the copied rows need the ``default`` organization, created at the time the
migration is applied, to exist first. ``api_keys`` refers to ``applications`` by name, so
it refers to the rebuilt table. ``usage_events`` has no foreign key.

Application names become unique within an organization. If two existing applications share
a name, the migration refuses, naming them, and the database stays at version 3.

The reverse refuses unless the organizations are exactly what this migration made (only
``default``, unchanged and enabled): otherwise it would drop which application belongs to
which organization.
"""

from __future__ import annotations

import sqlite3

from gateway.migrations.runner import Migration, MigrationError, MigrationPreview
from gateway.organizations import DEFAULT_ORGANIZATION_ID, DEFAULT_ORGANIZATION_NAME

_COPIED = (
    "id, name, plan_name, models, price_multiplier, monthly_token_budget, max_tokens, "
    "requests_per_minute, created_at_ms"
)


def _applications_table(name: str, *, with_organization: bool) -> str:
    organization = (
        ", organization_id TEXT NOT NULL REFERENCES organizations (id)" if with_organization else ""
    )
    return (
        f"CREATE TABLE {name} ("
        " id TEXT PRIMARY KEY,"
        " name TEXT NOT NULL,"
        " plan_name TEXT NOT NULL,"
        " models TEXT NOT NULL,"
        " price_multiplier REAL NOT NULL CHECK (price_multiplier >= 0),"
        " monthly_token_budget INTEGER NOT NULL CHECK (monthly_token_budget >= 0),"
        " max_tokens INTEGER CHECK (max_tokens IS NULL OR max_tokens >= 1),"
        " requests_per_minute INTEGER"
        "  CHECK (requests_per_minute IS NULL OR requests_per_minute >= 1),"
        f" created_at_ms REAL NOT NULL{organization})"
    )


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    found = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return found is not None


def duplicate_names(connection: sqlite3.Connection) -> list[tuple[str, list[str]]]:
    """Application names used more than once, each with the ids that use it, sorted."""
    if not _has_table(connection, "applications"):
        return []
    ids_by_name: dict[str, list[str]] = {}
    for app_id, name in connection.execute("SELECT id, name FROM applications ORDER BY name, id"):
        ids_by_name.setdefault(name, []).append(app_id)
    return [(name, ids) for name, ids in ids_by_name.items() if len(ids) > 1]


def _conflicts(connection: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(
        f"application name {name!r} is used by {', '.join(ids)}: names must be unique within "
        "an organization, and these would all be in 'default'"
        for name, ids in duplicate_names(connection)
    )


def _preview(connection: sqlite3.Connection) -> MigrationPreview:
    count = 0
    if _has_table(connection, "applications"):
        count = int(connection.execute("SELECT count(*) FROM applications").fetchone()[0])
    return MigrationPreview(
        changes=(
            "create table organizations",
            f"create organization {DEFAULT_ORGANIZATION_ID!r} (kind business)",
            f"rebuild table applications with a required organization_id; "
            f"{count} application(s) into {DEFAULT_ORGANIZATION_ID!r}",
            "application names unique within an organization",
        ),
        conflicts=_conflicts(connection),
    )


def _up(connection: sqlite3.Connection) -> None:
    conflicts = _conflicts(connection)
    if conflicts:
        raise MigrationError(
            "migration 4 refused, nothing was changed: " + "; ".join(conflicts) + ". Rename "
            "one of each (docs/26-organizations.md), then migrate again."
        )
    connection.execute(
        "CREATE TABLE organizations ("
        " id TEXT PRIMARY KEY,"
        " name TEXT NOT NULL UNIQUE,"
        " kind TEXT NOT NULL CHECK (kind IN ('individual', 'business')),"
        " created_at_ms REAL NOT NULL,"
        " disabled INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0, 1)))"
    )


def _backfill(connection: sqlite3.Connection, applied_at_ms: float) -> None:
    connection.execute(
        "INSERT INTO organizations (id, name, kind, created_at_ms) VALUES (?, ?, 'business', ?)",
        (DEFAULT_ORGANIZATION_ID, DEFAULT_ORGANIZATION_NAME, applied_at_ms),
    )
    connection.execute(_applications_table("applications_new", with_organization=True))
    connection.execute(
        f"INSERT INTO applications_new ({_COPIED}, organization_id)"
        f" SELECT {_COPIED}, ? FROM applications ORDER BY rowid",
        (DEFAULT_ORGANIZATION_ID,),
    )
    connection.execute("DROP TABLE applications")
    connection.execute("ALTER TABLE applications_new RENAME TO applications")
    connection.execute(
        "CREATE UNIQUE INDEX applications_name_per_organization"
        " ON applications (organization_id, name)"
    )


def _down(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        "SELECT id, name, kind, disabled FROM organizations ORDER BY id"
    ).fetchall()
    if rows != [(DEFAULT_ORGANIZATION_ID, DEFAULT_ORGANIZATION_NAME, "business", 0)]:
        raise MigrationError(
            "migration 4 cannot be reversed: organizations other than the one it created "
            f"exist, or {DEFAULT_ORGANIZATION_ID!r} was changed, and reversing would lose "
            "which application belongs to which. Restore the backup taken before migrating."
        )
    connection.execute(_applications_table("applications_old", with_organization=False))
    connection.execute(
        f"INSERT INTO applications_old ({_COPIED})"
        f" SELECT {_COPIED} FROM applications ORDER BY rowid"
    )
    connection.execute("DROP INDEX applications_name_per_organization")
    connection.execute("DROP TABLE applications")
    connection.execute("ALTER TABLE applications_old RENAME TO applications")
    connection.execute("DROP TABLE organizations")


MIGRATION = Migration(
    4,
    "organizations",
    _up,
    _down,
    backfill=_backfill,
    rebuilds_tables=True,
    preview=_preview,
)
