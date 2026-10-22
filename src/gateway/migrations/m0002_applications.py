"""Migration 2: applications and their API keys.

No organization column: organizations arrive in A3 with their own migration. Keys are
stored as an HMAC only (``key_hmac``); the key itself never reaches the database.
"""

from __future__ import annotations

import sqlite3

from gateway.migrations.runner import Migration


def _up(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE applications ("
        " id TEXT PRIMARY KEY,"
        " name TEXT NOT NULL,"
        " plan_name TEXT NOT NULL,"
        " models TEXT NOT NULL,"
        " price_multiplier REAL NOT NULL CHECK (price_multiplier >= 0),"
        " monthly_token_budget INTEGER NOT NULL CHECK (monthly_token_budget >= 0),"
        " max_tokens INTEGER CHECK (max_tokens IS NULL OR max_tokens >= 1),"
        " requests_per_minute INTEGER"
        "  CHECK (requests_per_minute IS NULL OR requests_per_minute >= 1),"
        " created_at_ms REAL NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE api_keys ("
        " key_id TEXT PRIMARY KEY,"
        " application_id TEXT NOT NULL REFERENCES applications (id),"
        " label TEXT NOT NULL,"
        " key_hmac TEXT NOT NULL,"
        " created_at_ms REAL NOT NULL,"
        " expires_at_ms REAL,"
        " revoked INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1)),"
        " last_used_at_ms REAL)"
    )
    connection.execute("CREATE INDEX api_keys_by_application ON api_keys (application_id)")


def _down(connection: sqlite3.Connection) -> None:
    connection.execute("DROP INDEX api_keys_by_application")
    connection.execute("DROP TABLE api_keys")
    connection.execute("DROP TABLE applications")


MIGRATION = Migration(2, "applications_and_api_keys", _up, _down)
