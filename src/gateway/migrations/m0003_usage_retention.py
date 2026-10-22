"""Migration 3: an index on usage_events by time, so pruning old events does not scan the
table (docs/21-persistence.md). No new column."""

from __future__ import annotations

import sqlite3

from gateway.migrations.runner import Migration


def _up(connection: sqlite3.Connection) -> None:
    connection.execute("CREATE INDEX usage_events_by_time ON usage_events (occurred_at_ms)")


def _down(connection: sqlite3.Connection) -> None:
    connection.execute("DROP INDEX usage_events_by_time")


MIGRATION = Migration(3, "usage_events_by_time", _up, _down)
