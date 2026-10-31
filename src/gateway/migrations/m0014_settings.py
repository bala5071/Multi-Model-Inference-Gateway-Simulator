"""Migration 14: per-application settings and their versions. See docs/39-settings.md.

One row in ``settings_current`` per organization or application whose settings were ever set;
no row means the defaults. Every change is also a row of ``settings_versions``, with who, when,
the old and the new document. New tables only: neither applying nor reversing it rebuilds a
table that foreign keys refer to. Postgres has the same tables in its schema version 3.
"""

from __future__ import annotations

import sqlite3

from gateway.migrations.runner import Migration


def _up(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE settings_current ("
        " scope_type TEXT NOT NULL CHECK (scope_type IN ('organization', 'application')),"
        " scope_id TEXT NOT NULL,"
        " version INTEGER NOT NULL CHECK (version >= 1),"
        " document TEXT NOT NULL,"
        " schema_version INTEGER NOT NULL,"
        " updated_at_ms REAL NOT NULL,"
        " PRIMARY KEY (scope_type, scope_id))"
    )
    connection.execute(
        "CREATE TABLE settings_versions ("
        " scope_type TEXT NOT NULL CHECK (scope_type IN ('organization', 'application')),"
        " scope_id TEXT NOT NULL,"
        " version INTEGER NOT NULL CHECK (version >= 1),"
        " document TEXT NOT NULL,"
        " previous_document TEXT,"
        " changed_fields TEXT NOT NULL,"
        " changed_by_user_id TEXT,"
        " via TEXT NOT NULL,"
        " changed_at_ms REAL NOT NULL,"
        " PRIMARY KEY (scope_type, scope_id, version))"
    )


def _down(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TABLE settings_versions")
    connection.execute("DROP TABLE settings_current")


MIGRATION = Migration(14, "settings", _up, _down)
