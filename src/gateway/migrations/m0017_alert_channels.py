"""Migration 17: alert channels, their sealed secrets, the notification outbox, the caps, the
suppression summaries, each instance's cap values (``instance_ceilings``), and the incident's
first channels. See docs/46-alert-channels.md and
docs/settings-and-alerts-a4-plan.md, section 12.

New tables only, and one column added in place (``incidents.notify_channels``): nothing is
rebuilt. Reversing it drops the tables; the column stays, unread (SQLite drops a column only
by rebuilding the table on the versions this project supports), and applying it again finds
the column there and leaves it.

**No application may have an empty id** (the plan's section 21.1): the built-in
``channel_delivery_failing`` keeps its incidents under the empty application id, which must
belong to no application. The migration refuses to apply while one has it, and its preview
says so. Postgres has the same tables in its schema version 6, with the same refusal.
"""

from __future__ import annotations

import sqlite3

from gateway.migrations.runner import Migration, MigrationError, MigrationPreview

CHANNELS = (
    "CREATE TABLE alert_channels ("
    " id TEXT PRIMARY KEY,"
    " organization_id TEXT NOT NULL,"
    " name TEXT NOT NULL,"
    " type TEXT NOT NULL CHECK (type IN ('webhook')),"
    " enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),"
    " is_default INTEGER NOT NULL CHECK (is_default IN (0, 1)),"
    " url_origin TEXT NOT NULL,"
    " version INTEGER NOT NULL CHECK (version >= 1),"
    " created_at_ms REAL NOT NULL,"
    " updated_at_ms REAL NOT NULL,"
    " last_delivered_at_ms REAL,"
    " last_failed_at_ms REAL,"
    " consecutive_failures INTEGER NOT NULL CHECK (consecutive_failures >= 0))"
)
CHANNEL_NAMES = "CREATE UNIQUE INDEX alert_channels_name ON alert_channels (organization_id, name)"
SECRETS = (
    "CREATE TABLE channel_secrets ("
    " channel_id TEXT NOT NULL,"
    " kind TEXT NOT NULL CHECK (kind IN"
    " ('webhook_url', 'webhook_signing_key', 'webhook_signing_key_previous')),"
    " key_id TEXT NOT NULL,"
    " nonce BLOB NOT NULL,"
    " ciphertext BLOB NOT NULL,"
    " expires_at_ms REAL,"
    " PRIMARY KEY (channel_id, kind))"
)
DELIVERIES = (
    "CREATE TABLE notification_deliveries ("
    " id TEXT PRIMARY KEY,"
    " notification_id TEXT NOT NULL,"
    " channel_id TEXT NOT NULL,"
    " organization_id TEXT NOT NULL,"
    " state TEXT NOT NULL CHECK (state IN"
    " ('queued', 'sending', 'delivered', 'failed', 'suppressed')),"
    " attempts INTEGER NOT NULL CHECK (attempts >= 0),"
    " next_attempt_at_ms REAL,"
    " claimed_by TEXT,"
    " claim_expires_at_ms REAL,"
    " last_code TEXT,"
    " payload TEXT NOT NULL,"
    " created_at_ms REAL NOT NULL,"
    " updated_at_ms REAL NOT NULL)"
)
ONE_PER_CHANNEL = (
    "CREATE UNIQUE INDEX notification_deliveries_one ON notification_deliveries"
    " (notification_id, channel_id)"
)
DUE = (
    "CREATE INDEX notification_deliveries_due ON notification_deliveries"
    " (state, next_attempt_at_ms)"
)
BY_CHANNEL = (
    "CREATE INDEX notification_deliveries_by_channel ON notification_deliveries (channel_id, id)"
)
CAPS = (
    "CREATE TABLE notification_caps ("
    " scope TEXT NOT NULL CHECK (scope IN ('rule', 'organization')),"
    " scope_id TEXT NOT NULL,"
    " hour INTEGER NOT NULL,"
    " sent INTEGER NOT NULL CHECK (sent >= 0),"
    " PRIMARY KEY (scope, scope_id, hour))"
)
SUMMARIES = (
    "CREATE TABLE suppression_summaries ("
    " organization_id TEXT NOT NULL,"
    " hour INTEGER NOT NULL,"
    " notification_id TEXT NOT NULL,"
    " created_at_ms REAL NOT NULL)"
)
ONE_SUMMARY = (
    "CREATE UNIQUE INDEX suppression_summaries_one ON suppression_summaries (organization_id, hour)"
)
# Each live instance's four cap values and their digest, compared on every lease attempt; a
# mismatch is reported, never refused (the main plan's decision 19, for the caps only).
CEILINGS = (
    "CREATE TABLE instance_ceilings ("
    " instance_id TEXT PRIMARY KEY,"
    " digest TEXT NOT NULL,"
    " ceilings TEXT NOT NULL,"
    " seen_at_ms REAL NOT NULL)"
)
# NULL until the incident first notifies: then the channel ids it went to, set once.
NOTIFY_CHANNELS = "ALTER TABLE incidents ADD COLUMN notify_channels TEXT"

EMPTY_ID_CONFLICT = (
    "an application has an empty id; the built-in channel_delivery_failing keeps its "
    "incidents under the empty application id, so none may have it (docs/46)"
)


def empty_application_ids(connection: sqlite3.Connection) -> int:
    """How many applications have an empty id (none may: the plan's section 21.1). A preview
    reads the database before the earlier pending migrations: with no table yet, none."""
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'applications'"
    ).fetchone()
    if exists is None:
        return 0
    return int(connection.execute("SELECT count(*) FROM applications WHERE id = ''").fetchone()[0])


def _conflicts(connection: sqlite3.Connection) -> tuple[str, ...]:
    return (EMPTY_ID_CONFLICT,) if empty_application_ids(connection) else ()


def _preview(connection: sqlite3.Connection) -> MigrationPreview:
    return MigrationPreview(
        changes=(
            "create tables alert_channels, channel_secrets, notification_deliveries, "
            "notification_caps, suppression_summaries and instance_ceilings",
            "add column incidents.notify_channels",
        ),
        conflicts=_conflicts(connection),
    )


def _has_column(connection: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in connection.execute(f"PRAGMA table_info({table})"))


def _up(connection: sqlite3.Connection) -> None:
    conflicts = _conflicts(connection)
    if conflicts:
        raise MigrationError("migration 17 refused, nothing was changed: " + "; ".join(conflicts))
    for statement in (
        CHANNELS, CHANNEL_NAMES, SECRETS, DELIVERIES, ONE_PER_CHANNEL, DUE, BY_CHANNEL, CAPS,
        SUMMARIES, ONE_SUMMARY, CEILINGS,
    ):  # fmt: skip
        connection.execute(statement)
    # Applied again after a rollback, the column is there already (the rollback keeps it).
    if not _has_column(connection, "incidents", "notify_channels"):
        connection.execute(NOTIFY_CHANNELS)


def _down(connection: sqlite3.Connection) -> None:
    for table in (
        "instance_ceilings",
        "suppression_summaries",
        "notification_caps",
        "notification_deliveries",
        "channel_secrets",
        "alert_channels",
    ):
        connection.execute(f"DROP TABLE {table}")


MIGRATION = Migration(17, "alert_channels", _up, _down, preview=_preview)
