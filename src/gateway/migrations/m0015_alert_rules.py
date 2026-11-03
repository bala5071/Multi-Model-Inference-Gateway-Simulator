"""Migration 15: applications' alert rules. See docs/44-alert-rules.md.

A new table, and a data step: every application that already exists gets the default rules,
with the ids a new application would get (``default_rule_id``). Reversing it drops the table
(and with it every rule). No table is rebuilt. Postgres has the same table in its schema
version 4, with the same data step in SQL.
"""

from __future__ import annotations

import sqlite3

from gateway.migrations.runner import Migration

CREATE = (
    "CREATE TABLE alert_rules ("
    " id TEXT PRIMARY KEY,"
    " application_id TEXT NOT NULL,"
    " name TEXT NOT NULL,"
    " signal TEXT NOT NULL,"
    " parameter TEXT,"
    " comparison TEXT NOT NULL CHECK (comparison IN ('>', '>=', '<', '<=')),"
    " threshold REAL NOT NULL,"
    " window_minutes INTEGER,"
    " for_minutes INTEGER NOT NULL,"
    " severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),"
    " channels TEXT NOT NULL,"
    " cooldown_minutes INTEGER NOT NULL,"
    " repeat_every_minutes INTEGER,"
    " quiet_hours TEXT,"
    " enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),"
    " version INTEGER NOT NULL CHECK (version >= 1),"
    " default_key TEXT,"
    " created_at_ms REAL NOT NULL,"
    " updated_at_ms REAL NOT NULL)"
)
INDEX = "CREATE INDEX alert_rules_by_application ON alert_rules (application_id, id)"


def _up(connection: sqlite3.Connection) -> None:
    # Imported here: the rules module needs the migrations' database helpers.
    from gateway.alertrules import COLUMNS, default_rules, rule_row

    connection.execute(CREATE)
    connection.execute(INDEX)
    # The data step: the default rules of every application there already is.
    for application_id, created_at_ms in connection.execute(
        "SELECT id, created_at_ms FROM applications ORDER BY id"
    ).fetchall():
        for rule in default_rules(application_id, float(created_at_ms)):
            connection.execute(
                f"INSERT INTO alert_rules ({COLUMNS}) VALUES ({', '.join('?' * 19)})",
                rule_row(rule),
            )


def _down(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TABLE alert_rules")


MIGRATION = Migration(15, "alert_rules", _up, _down)
