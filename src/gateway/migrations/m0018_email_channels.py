"""Migration 18: email channels, their recipients, the confirmation caps and the platform's own
secrets. See docs/48-email-channels.md and docs/settings-and-alerts-a5-plan.md, section 9.

- **Rebuilds ``alert_channels``** (SQLite changes a ``CHECK`` only by rebuilding the table):
  ``type`` may be ``webhook`` or ``email``, and ``url_origin`` is required exactly for a webhook
  (``CHECK ((type = 'webhook') = (url_origin IS NOT NULL))``). Every row is copied in its
  rowid order with its id, and its unique name index made again. Marked ``rebuilds_tables``: the
  runner turns foreign keys off around it and checks them before committing, the server refuses
  to run it on an existing database, and ``scripts/manage.py migrate`` backs up first.
- **The outbox** gains ``notification_deliveries.recipient_id`` (NULL for a webhook) and its
  unique index becomes (notification, channel, ``COALESCE(recipient_id, '')``): one row per
  recipient of an email channel; SQLite and Postgres both treat NULLs as distinct.
- **New tables:** ``alert_recipients``, ``confirmation_caps`` (the per-address cap, by an HMAC
  of the address) and ``platform_secrets`` (the SMTP password, sealed in the platform scope).
- The four confirmation caps join the ceilings row as four more names in its JSON: the
  ``instance_ceilings`` table itself is unchanged.

Reversing it drops the new tables, gives the outbox its old unique index back and rebuilds
``alert_channels`` as migration 17 made it; it refuses while an email channel or a recipient's
delivery exists (it would lose them). ``recipient_id`` stays, unread (SQLite drops a column only
by rebuilding on the versions this project supports), and applying 18 again leaves it.
Postgres has the same in its schema version 7.
"""

from __future__ import annotations

import sqlite3

from gateway.migrations.runner import Migration, MigrationError, MigrationPreview

CHANNEL_COLUMNS = (
    "id, organization_id, name, type, enabled, is_default, url_origin, version, created_at_ms,"
    " updated_at_ms, last_delivered_at_ms, last_failed_at_ms, consecutive_failures"
)


def channels_table(name: str, *, email: bool) -> str:
    """``alert_channels`` as migration 18 makes it (``email``) or as migration 17 did."""
    types = "('webhook', 'email')" if email else "('webhook')"
    origin = " url_origin TEXT," if email else " url_origin TEXT NOT NULL,"
    origin_rule = ", CHECK ((type = 'webhook') = (url_origin IS NOT NULL))" if email else ""
    return (
        f"CREATE TABLE {name} ("
        " id TEXT PRIMARY KEY,"
        " organization_id TEXT NOT NULL,"
        " name TEXT NOT NULL,"
        f" type TEXT NOT NULL CHECK (type IN {types}),"
        " enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),"
        " is_default INTEGER NOT NULL CHECK (is_default IN (0, 1)),"
        f"{origin}"
        " version INTEGER NOT NULL CHECK (version >= 1),"
        " created_at_ms REAL NOT NULL,"
        " updated_at_ms REAL NOT NULL,"
        " last_delivered_at_ms REAL,"
        " last_failed_at_ms REAL,"
        f" consecutive_failures INTEGER NOT NULL CHECK (consecutive_failures >= 0){origin_rule})"
    )


RECIPIENTS = (
    "CREATE TABLE alert_recipients ("
    " id TEXT PRIMARY KEY,"
    " channel_id TEXT NOT NULL,"
    " organization_id TEXT NOT NULL,"
    " address TEXT NOT NULL,"
    " state TEXT NOT NULL CHECK (state IN ('unverified', 'verified')),"
    " code_digest BLOB,"
    " code_expires_at_ms REAL,"
    " wrong_entries INTEGER NOT NULL CHECK (wrong_entries BETWEEN 0 AND 5),"
    " added_at_ms REAL NOT NULL,"
    " verified_at_ms REAL,"
    " last_refused_at_ms REAL,"
    " CHECK (code_digest IS NULL OR state = 'unverified'),"
    " CHECK ((state = 'verified') = (verified_at_ms IS NOT NULL)))"
)
ONE_ADDRESS = "CREATE UNIQUE INDEX alert_recipients_one ON alert_recipients (channel_id, address)"
BY_CHANNEL = "CREATE INDEX alert_recipients_by_channel ON alert_recipients (channel_id, id)"
CONFIRMATION_CAPS = (
    "CREATE TABLE confirmation_caps ("
    " address_digest TEXT PRIMARY KEY,"
    " window_start_ms REAL NOT NULL,"
    " sent INTEGER NOT NULL CHECK (sent >= 1))"
)
PLATFORM_SECRETS = (
    "CREATE TABLE platform_secrets ("
    " kind TEXT PRIMARY KEY CHECK (kind IN ('smtp_password')),"
    " key_id TEXT NOT NULL,"
    " nonce BLOB NOT NULL,"
    " ciphertext BLOB NOT NULL,"
    " host TEXT NOT NULL,"
    " port INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535),"
    " username TEXT NOT NULL,"
    " updated_at_ms REAL NOT NULL)"
)
RECIPIENT_COLUMN = "ALTER TABLE notification_deliveries ADD COLUMN recipient_id TEXT"
ONE_PER_RECIPIENT = (
    "CREATE UNIQUE INDEX notification_deliveries_one ON notification_deliveries"
    " (notification_id, channel_id, COALESCE(recipient_id, ''))"
)
ONE_PER_CHANNEL = (
    "CREATE UNIQUE INDEX notification_deliveries_one ON notification_deliveries"
    " (notification_id, channel_id)"
)
CHANNEL_NAMES = "CREATE UNIQUE INDEX alert_channels_name ON alert_channels (organization_id, name)"

REVERSE_CONFLICT = (
    "an email channel or a recipient's delivery exists; reversing migration 18 would lose it"
)


def _count(connection: sqlite3.Connection, sql: str) -> int:
    return int(connection.execute(sql).fetchone()[0])


def _has_table(connection: sqlite3.Connection, table: str) -> bool:
    found = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return found is not None


def _has_column(connection: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in connection.execute(f"PRAGMA table_info({table})"))


def _rebuild_channels(connection: sqlite3.Connection, *, email: bool) -> None:
    connection.execute(channels_table("alert_channels_new", email=email))
    connection.execute(
        f"INSERT INTO alert_channels_new ({CHANNEL_COLUMNS})"
        f" SELECT {CHANNEL_COLUMNS} FROM alert_channels ORDER BY rowid"
    )
    connection.execute("DROP INDEX alert_channels_name")
    connection.execute("DROP TABLE alert_channels")
    connection.execute("ALTER TABLE alert_channels_new RENAME TO alert_channels")
    connection.execute(CHANNEL_NAMES)


def _preview(connection: sqlite3.Connection) -> MigrationPreview:
    channels = (
        _count(connection, "SELECT count(*) FROM alert_channels")
        if _has_table(connection, "alert_channels")
        else 0
    )
    return MigrationPreview(
        changes=(
            f"rebuild table alert_channels ({channels} channel(s) kept, with their ids): type "
            "webhook or email, url_origin only for a webhook",
            "add column notification_deliveries.recipient_id; its unique index per recipient",
            "create tables alert_recipients, confirmation_caps and platform_secrets",
        )
    )


def _up(connection: sqlite3.Connection) -> None:
    _rebuild_channels(connection, email=True)
    # Applied again after a rollback, the column is there already (the rollback keeps it).
    if not _has_column(connection, "notification_deliveries", "recipient_id"):
        connection.execute(RECIPIENT_COLUMN)
    connection.execute("DROP INDEX notification_deliveries_one")
    connection.execute(ONE_PER_RECIPIENT)
    for statement in (RECIPIENTS, ONE_ADDRESS, BY_CHANNEL, CONFIRMATION_CAPS, PLATFORM_SECRETS):
        connection.execute(statement)


def _down(connection: sqlite3.Connection) -> None:
    email = _count(connection, "SELECT count(*) FROM alert_channels WHERE type = 'email'")
    deliveries = _count(
        connection,
        "SELECT count(*) FROM notification_deliveries WHERE recipient_id IS NOT NULL",
    )
    if email or deliveries:
        raise MigrationError("migration 18 not reversed, nothing was changed: " + REVERSE_CONFLICT)
    for table in ("platform_secrets", "confirmation_caps", "alert_recipients"):
        connection.execute(f"DROP TABLE {table}")
    connection.execute("DROP INDEX notification_deliveries_one")
    connection.execute(ONE_PER_CHANNEL)
    _rebuild_channels(connection, email=False)


MIGRATION = Migration(18, "email_channels", _up, _down, preview=_preview, rebuilds_tables=True)
