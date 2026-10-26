"""Migration 13: where a chat answer came from, in its usage event. See docs/33-fallback-modes.md.

``provider`` (the gateway's own tier, ``local`` in rows written before PR 20 and ``gateway``
since, or an organization provider's id; never rewritten), ``provider_class`` (``local`` or
``hosted``), ``fallback_from`` and ``fallback_reason`` (set when another candidate was tried
first) and ``usage_estimated`` (1 when the provider reported no usage). All NULL for
``/v1/generate`` and for events stored before this migration. ``ADD COLUMN``s, so no table is
rebuilt and the server applies it itself. The reverse rebuilds ``usage_events`` without them
(no table refers to it; ``DROP COLUMN`` would need SQLite 3.35).
"""

from __future__ import annotations

import sqlite3

from gateway.migrations.runner import Migration

_COLUMNS = (
    "seq, application_id, request_id, model, prompt_tokens, cached_tokens, completion_tokens,"
    " base_cost_usd, billed_cost_usd, occurred_at_ms"
)


def _up(connection: sqlite3.Connection) -> None:
    connection.execute("ALTER TABLE usage_events ADD COLUMN provider TEXT")
    connection.execute(
        "ALTER TABLE usage_events ADD COLUMN provider_class TEXT"
        " CHECK (provider_class IS NULL OR provider_class IN ('local', 'hosted'))"
    )
    connection.execute("ALTER TABLE usage_events ADD COLUMN fallback_from TEXT")
    connection.execute("ALTER TABLE usage_events ADD COLUMN fallback_reason TEXT")
    connection.execute(
        "ALTER TABLE usage_events ADD COLUMN usage_estimated INTEGER"
        " CHECK (usage_estimated IS NULL OR usage_estimated IN (0, 1))"
    )


def _down(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE usage_events_old ("
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
        f"INSERT INTO usage_events_old ({_COLUMNS}) SELECT {_COLUMNS} FROM usage_events"
        " ORDER BY seq"
    )
    connection.execute("DROP INDEX usage_events_by_application_time")
    connection.execute("DROP INDEX usage_events_by_time")
    connection.execute("DROP TABLE usage_events")
    connection.execute("ALTER TABLE usage_events_old RENAME TO usage_events")
    connection.execute(
        "CREATE INDEX usage_events_by_application_time"
        " ON usage_events (application_id, occurred_at_ms)"
    )
    connection.execute("CREATE INDEX usage_events_by_time ON usage_events (occurred_at_ms)")


MIGRATION = Migration(13, "usage_provider", _up, _down)
