"""Migration 1: the usage events table.

One row per settled request. Token counts and costs only: no prompt text, response text
or token ids, ever (CLAUDE.md hard rule 7). ``seq`` is the row's own key; AUTOINCREMENT
means a number is never handed out twice, even after rows are deleted or the process
restarts. ``request_id`` is the per-process response id (``req-N``) and is not unique.
"""

from __future__ import annotations

import sqlite3

from gateway.migrations.runner import Migration


def _up(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE usage_events ("
        " seq INTEGER PRIMARY KEY AUTOINCREMENT,"
        " application_id TEXT NOT NULL,"
        " request_id TEXT NOT NULL,"
        " model TEXT NOT NULL,"
        " prompt_tokens INTEGER NOT NULL CHECK (prompt_tokens >= 0),"
        " cached_tokens INTEGER NOT NULL"
        "  CHECK (cached_tokens >= 0 AND cached_tokens <= prompt_tokens),"
        " completion_tokens INTEGER NOT NULL CHECK (completion_tokens >= 0),"
        " base_cost_usd REAL NOT NULL,"
        " billed_cost_usd REAL NOT NULL,"
        " occurred_at_ms REAL NOT NULL)"
    )
    connection.execute(
        "CREATE INDEX usage_events_by_application_time"
        " ON usage_events (application_id, occurred_at_ms)"
    )


def _down(connection: sqlite3.Connection) -> None:
    connection.execute("DROP INDEX usage_events_by_application_time")
    connection.execute("DROP TABLE usage_events")


MIGRATION = Migration(1, "usage_events", _up, _down)
