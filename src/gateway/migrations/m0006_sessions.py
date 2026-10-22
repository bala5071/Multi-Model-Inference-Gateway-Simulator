"""Migration 6: console sessions. See docs/27-users-and-sessions.md.

The session id and the CSRF token are stored only as SHA-256 hex digests: the database never
holds what a browser presents. ``handle`` is a random public id that the session list shows
in place of either. ``last_seen_at_ms`` is written at most once a minute per session.
"""

from __future__ import annotations

import sqlite3

from gateway.migrations.runner import Migration


def _up(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE sessions ("
        " id_hash TEXT PRIMARY KEY,"
        " handle TEXT NOT NULL UNIQUE,"
        " user_id TEXT NOT NULL REFERENCES users (id),"
        " csrf_hash TEXT NOT NULL,"
        " created_at_ms REAL NOT NULL,"
        " last_seen_at_ms REAL NOT NULL,"
        " expires_at_ms REAL NOT NULL,"
        " revoked INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1)))"
    )
    connection.execute("CREATE INDEX sessions_by_user ON sessions (user_id)")
    connection.execute("CREATE INDEX sessions_by_expiry ON sessions (expires_at_ms)")


def _down(connection: sqlite3.Connection) -> None:
    connection.execute("DROP INDEX sessions_by_expiry")
    connection.execute("DROP INDEX sessions_by_user")
    connection.execute("DROP TABLE sessions")


MIGRATION = Migration(6, "sessions", _up, _down)
