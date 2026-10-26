"""Migration 12: fallback modes. See docs/33-fallback-modes.md.

One row per organization or application that has set a mode; no row means ``local_only``, the
default. A table of its own rather than a column on ``organizations`` and ``applications``: a
new table, so neither applying nor reversing it rebuilds a table that foreign keys refer to.
"""

from __future__ import annotations

import sqlite3

from gateway.migrations.runner import Migration


def _up(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE modes ("
        " scope_type TEXT NOT NULL CHECK (scope_type IN ('organization', 'application')),"
        " scope_id TEXT NOT NULL,"
        " mode TEXT NOT NULL CHECK (mode IN"
        "  ('local_only', 'local_with_hosted_fallback', 'hosted_primary_with_local_fallback')),"
        " updated_at_ms REAL NOT NULL,"
        " PRIMARY KEY (scope_type, scope_id))"
    )


def _down(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TABLE modes")


MIGRATION = Migration(12, "modes", _up, _down)
