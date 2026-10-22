"""Migration 7: the audit log. See docs/27-users-and-sessions.md.

Who did what, to what, when, and with what outcome. ``details`` is JSON written only through
the allowlist in ``gateway.audit``, so no password, session id, CSRF token or hash can be
stored. ``actor_user_id`` and ``target_id`` are ids, never usernames typed at a login. No
foreign keys: an entry outlives the user it names.
"""

from __future__ import annotations

import sqlite3

from gateway.migrations.runner import Migration


def _up(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE audit_log ("
        " seq INTEGER PRIMARY KEY AUTOINCREMENT,"
        " at_ms REAL NOT NULL,"
        " actor_user_id TEXT,"
        " action TEXT NOT NULL,"
        " target_type TEXT NOT NULL,"
        " target_id TEXT,"
        " outcome TEXT NOT NULL,"
        " details TEXT NOT NULL)"
    )
    connection.execute("CREATE INDEX audit_log_by_time ON audit_log (at_ms)")


def _down(connection: sqlite3.Connection) -> None:
    connection.execute("DROP INDEX audit_log_by_time")
    connection.execute("DROP TABLE audit_log")


MIGRATION = Migration(7, "audit_log", _up, _down)
