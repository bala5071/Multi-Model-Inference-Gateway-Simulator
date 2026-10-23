"""Migration 9: a session's step-up window. See docs/29-step-up.md.

``sessions.step_up_until_ms``: until when (epoch time) this session may perform actions the
permission table flags for step-up. NULL when it has none. An ``ADD COLUMN``, so no table is
rebuilt and the server applies it itself. The reverse rebuilds ``sessions`` without the column
(no table refers to ``sessions``, so foreign keys need no special handling; ``DROP COLUMN``
would need SQLite 3.35).
"""

from __future__ import annotations

import sqlite3

from gateway.migrations.runner import Migration

_COLUMNS = (
    "id_hash, handle, user_id, csrf_hash, created_at_ms, last_seen_at_ms, expires_at_ms, revoked"
)


def _up(connection: sqlite3.Connection) -> None:
    connection.execute("ALTER TABLE sessions ADD COLUMN step_up_until_ms REAL")


def _down(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE sessions_old ("
        " id_hash TEXT PRIMARY KEY,"
        " handle TEXT NOT NULL UNIQUE,"
        " user_id TEXT NOT NULL REFERENCES users (id),"
        " csrf_hash TEXT NOT NULL,"
        " created_at_ms REAL NOT NULL,"
        " last_seen_at_ms REAL NOT NULL,"
        " expires_at_ms REAL NOT NULL,"
        " revoked INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1)))"
    )
    connection.execute(
        f"INSERT INTO sessions_old ({_COLUMNS}) SELECT {_COLUMNS} FROM sessions ORDER BY rowid"
    )
    connection.execute("DROP INDEX sessions_by_expiry")
    connection.execute("DROP INDEX sessions_by_user")
    connection.execute("DROP TABLE sessions")
    connection.execute("ALTER TABLE sessions_old RENAME TO sessions")
    connection.execute("CREATE INDEX sessions_by_user ON sessions (user_id)")
    connection.execute("CREATE INDEX sessions_by_expiry ON sessions (expires_at_ms)")


MIGRATION = Migration(9, "session_step_up", _up, _down)
