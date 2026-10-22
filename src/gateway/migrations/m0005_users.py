"""Migration 5: users. See docs/27-users-and-sessions.md.

A user belongs to one organization. Only an argon2id hash of the password is stored. The
lockout columns live here so that a restart does not clear a lock. ``bootstrap`` is 0 for an
ordinary account, 1 for the bootstrap admin before its first login and 2 after it (the
environment password then no longer logs in). ``platform_admin`` stands in for the role until
step 5 moves it into role assignments.
"""

from __future__ import annotations

import sqlite3

from gateway.migrations.runner import Migration


def _up(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE users ("
        " id TEXT PRIMARY KEY,"
        " username TEXT NOT NULL UNIQUE,"
        " password_hash TEXT NOT NULL,"
        " organization_id TEXT NOT NULL REFERENCES organizations (id),"
        " created_at_ms REAL NOT NULL,"
        " disabled INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0, 1)),"
        " must_change_password INTEGER NOT NULL DEFAULT 0 CHECK (must_change_password IN (0, 1)),"
        " bootstrap INTEGER NOT NULL DEFAULT 0 CHECK (bootstrap IN (0, 1, 2)),"
        " failed_logins INTEGER NOT NULL DEFAULT 0 CHECK (failed_logins >= 0),"
        " first_failure_at_ms REAL,"
        " locked_until_ms REAL,"
        " platform_admin INTEGER NOT NULL DEFAULT 0 CHECK (platform_admin IN (0, 1)))"
    )
    connection.execute("CREATE INDEX users_by_organization ON users (organization_id)")


def _down(connection: sqlite3.Connection) -> None:
    connection.execute("DROP INDEX users_by_organization")
    connection.execute("DROP TABLE users")


MIGRATION = Migration(5, "users", _up, _down)
