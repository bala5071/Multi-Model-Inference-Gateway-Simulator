"""Migration 16: the alert evaluator's lease, incidents, and the notifications recorded on them.
See docs/45-alert-evaluation.md.

New tables only, nothing rebuilt; reversing it drops them. The lease is one row, made by the
first attempt to take it, so a fresh database has no rows (as ``migrate-to-postgres`` needs of
its target). At most one open incident per (rule, series key): a partial unique index. Postgres
has the same tables in its schema version 5.
"""

from __future__ import annotations

import sqlite3

from gateway.migrations.runner import Migration

LEASE = (
    "CREATE TABLE alert_lease ("
    " id INTEGER PRIMARY KEY CHECK (id = 1),"
    " holder TEXT,"
    " epoch INTEGER NOT NULL,"
    " expires_at_ms REAL NOT NULL,"
    " last_cycle_completed_at_ms REAL,"
    " last_cycle_epoch INTEGER)"
)
INCIDENTS = (
    "CREATE TABLE incidents ("
    " id TEXT PRIMARY KEY,"
    " organization_id TEXT NOT NULL,"
    " application_id TEXT NOT NULL,"
    " rule_id TEXT NOT NULL,"
    " rule_version INTEGER NOT NULL,"
    " series_key TEXT NOT NULL,"
    " state TEXT NOT NULL CHECK (state IN ('pending', 'firing', 'acknowledged', 'resolved')),"
    " severity TEXT NOT NULL,"
    " opened_at_ms REAL NOT NULL,"
    " pending_since_ms REAL,"
    " fired_at_ms REAL,"
    " acknowledged_at_ms REAL,"
    " acknowledged_by TEXT,"
    " resolved_at_ms REAL,"
    " resolve_reason TEXT,"
    " value REAL,"
    " false_count INTEGER NOT NULL,"
    " no_data_since_ms REAL,"
    " last_notified_at_ms REAL,"
    " last_reminder_at_ms REAL,"
    " firing_sent INTEGER NOT NULL,"
    " firing_again_due INTEGER NOT NULL,"
    " reopened INTEGER NOT NULL,"
    " revision INTEGER NOT NULL,"
    " updated_at_ms REAL NOT NULL)"
)
ONE_OPEN = (
    "CREATE UNIQUE INDEX incidents_one_open ON incidents (rule_id, series_key)"
    " WHERE state <> 'resolved'"
)
BY_ORGANIZATION = "CREATE INDEX incidents_by_organization ON incidents (organization_id, id)"
NOTIFICATIONS = (
    "CREATE TABLE incident_notifications ("
    " id TEXT PRIMARY KEY,"
    " incident_id TEXT NOT NULL,"
    " application_id TEXT NOT NULL,"
    " kind TEXT NOT NULL,"
    " state TEXT NOT NULL CHECK (state IN ('recorded', 'held', 'released')),"
    " at_ms REAL NOT NULL,"
    " reason TEXT)"
)
BY_INCIDENT = (
    "CREATE INDEX incident_notifications_by_incident ON incident_notifications (incident_id)"
)


def _up(connection: sqlite3.Connection) -> None:
    for statement in (LEASE, INCIDENTS, ONE_OPEN, BY_ORGANIZATION, NOTIFICATIONS, BY_INCIDENT):
        connection.execute(statement)


def _down(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TABLE incident_notifications")
    connection.execute("DROP TABLE incidents")
    connection.execute("DROP TABLE alert_lease")


MIGRATION = Migration(16, "incidents", _up, _down)
