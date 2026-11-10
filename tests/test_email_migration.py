"""Migration 18 and Postgres schema 7 on a populated database (docs/48, the A5 plan's section 9),
and each Postgres constraint they add reached by a raw row of its own.

The populated database: two organizations with their applications and default rules, webhook
channels and their sealed secrets, deliveries in every state, caps rows, a suppression summary,
the ceilings rows, incidents with ``notify_channels``, and rules naming the channels. Checked:
every row of every table, column by column, and every count; the channels' ids; the foreign
keys; a failure planted after the copy leaving everything as it was; the reverse giving version
17 back; the reverse refused while an email channel or a recipient's delivery exists; the server
refusing the rebuild on an existing database (A5-15); the CLI backing up first, and its dry run.
Test values, not secrets.
"""

from __future__ import annotations

import random
import sqlite3
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from gateway.alertrules import RuleService
from gateway.alerts.channels import Channel, StoredSecret
from gateway.alerts.outbox import Delivery, DeliveryState, delivery_row
from gateway.apikeys import ApiKeyService
from gateway.audit import AuditLog
from gateway.clock import ClockEpoch, SimClock
from gateway.database import open_database
from gateway.management import ManagementService
from gateway.migrations import MIGRATIONS, MigrationError, MigrationRunner
from gateway.migrations import m0018_email_channels as m0018
from gateway.organizations import new_organization
from gateway.routing import load_plans
from gateway.state import StateBackend, sqlite_state
from gateway.vault import SealedKey

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from manage import main as manage

postgres = pytest.mark.postgres
NOW = 1.8e12
WEBHOOKS = ("chn_00000000000000a1", "chn_00000000000000a2", "chn_00000000000000b1")
Execute = Callable[[str, tuple[Any, ...]], Awaitable[Any]]
# The tables the seed fills: those migration 18 touches or that name a channel.
SEEDED = (
    "organizations", "applications", "alert_rules", "incidents", "alert_channels",
    "channel_secrets", "notification_deliveries", "notification_caps", "suppression_summaries",
    "instance_ceilings",
)  # fmt: skip


async def seed(state: StateBackend, execute: Execute) -> None:
    """Every table of version 17 given rows, through the stores where they exist."""
    epoch = ClockEpoch(SimClock(), NOW)
    audit = AuditLog(state.audit)
    rules = RuleService(state.rules, audit, epoch, None, random.Random(1).randbytes)
    keys = ApiKeyService(state.applications, b"p" * 32, epoch)
    management = ManagementService(
        keys, load_plans(None), audit, epoch, on_created=rules.create_defaults
    )
    for organization in ("north", "south"):
        await state.applications.add_organization(
            new_organization(organization, organization.title(), "business", created_at_ms=1.0)
        )
        await management.create_application(
            organization, f"{organization} shop", "pro", application_id=f"{organization}-shop"
        )
    for number, (channel_id, organization) in enumerate(
        zip(WEBHOOKS, ("north", "north", "south"), strict=True)
    ):
        await state.channels.insert_channel(
            Channel(
                channel_id, organization, f"Hook {number}", "webhook", number != 1, number == 0,
                f"https://hooks{number}.example.com", 1 + number, NOW, NOW + number,
                # Every column given a value, so a column the copy dropped would show.
                NOW - 5.0 - number, NOW - 7.0 - number, number,
            )
        )  # fmt: skip
        for kind in ("webhook_url", "webhook_signing_key"):
            await state.channels.put_secret(
                StoredSecret(
                    channel_id, kind, SealedKey("k1", bytes([number]) * 12, b"\x01\x02" * 8, None)
                )
            )
    await state.channels.put_secret(
        StoredSecret(
            WEBHOOKS[0], "webhook_signing_key_previous",
            SealedKey("k1", b"\x09" * 12, b"\x03" * 16, None), NOW + 1.0,
        )
    )  # fmt: skip
    deliveries = [
        Delivery(
            f"dlv_00000000000000{index:02x}",
            f"ntf_00000000000000{index:02x}",
            WEBHOOKS[index % 2],
            "north",
            state_,
            index,
            NOW + index if state_ == DeliveryState.QUEUED else None,
            "clm_x" if state_ == DeliveryState.SENDING else None,
            NOW + 60_000.0 if state_ == DeliveryState.SENDING else None,
            None if state_ == DeliveryState.QUEUED else "timeout",
            f'{{"n":{index}}}',
            NOW,
            NOW,
        )
        for index, state_ in enumerate(DeliveryState)
    ]
    # Written with version 17's own columns (the store writes recipient_id, which 17 has not).
    for delivery in deliveries:
        await execute(
            "INSERT INTO notification_deliveries (id, notification_id, channel_id,"
            " organization_id, state, attempts, next_attempt_at_ms, claimed_by,"
            " claim_expires_at_ms, last_code, payload, created_at_ms, updated_at_ms)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            delivery_row(delivery)[:13],
        )
    assert await state.outbox.count_notification("rule", "rul_0000000000000001", 7, 6)
    assert await state.outbox.count_notification("organization", "north", 7, 60)
    assert await state.outbox.add_summary("north", 7, "ntf_00000000000000ee", NOW)
    await state.ceilings.report("ins_one", "d1", '{"per_rule":6}', 60_000.0, 3_600_000.0)
    await state.ceilings.report("ins_two", "d2", '{"per_rule":7}', 60_000.0, 3_600_000.0)
    # Rules naming the channels, and incidents that notified them.
    await execute(
        "UPDATE alert_rules SET channels = ? WHERE application_id = ?",
        (f'["{WEBHOOKS[0]}", "{WEBHOOKS[1]}"]', "north-shop"),
    )
    for number, (application, channels) in enumerate(
        (("north-shop", f'["{WEBHOOKS[0]}"]'), ("south-shop", f'["{WEBHOOKS[2]}"]'))
    ):
        await execute(
            "INSERT INTO incidents (id, organization_id, application_id, rule_id, rule_version,"
            " series_key, state, severity, opened_at_ms, false_count, firing_sent,"
            " firing_again_due, reopened, revision, updated_at_ms, notify_channels)"
            " VALUES (?, ?, ?, ?, 1, '', 'firing', 'critical', ?, 0, 1, 0, 0, 1, ?, ?)",
            (f"inc_000000000000000{number}", application.split("-")[0], application,
             f"rul_000000000000000{number}", NOW, NOW, channels),
        )  # fmt: skip


# --- SQLite --------------------------------------------------------------------------------------


def _at_17(path: Path | str) -> tuple[sqlite3.Connection, MigrationRunner]:
    connection = open_database(path)
    runner = MigrationRunner(connection, MIGRATIONS, ClockEpoch(SimClock()))
    assert runner.migrate(17)[-1] == 17
    return connection, runner


async def _seeded(path: Path | str = ":memory:") -> tuple[sqlite3.Connection, MigrationRunner]:
    connection, runner = _at_17(path)

    async def execute(sql: str, args: tuple[Any, ...]) -> None:
        connection.execute(sql, args)

    await seed(sqlite_state(connection, ClockEpoch(SimClock(), NOW)), execute)
    return connection, runner


def _snapshot(connection: sqlite3.Connection) -> dict[str, tuple[list[str], list[tuple[Any, ...]]]]:
    """Every table's columns and rows, in rowid order (a table's own order)."""
    tables = [
        r[0]
        for r in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            " AND name <> 'schema_migrations' ORDER BY name"
        )
    ]
    found = {}
    for table in tables:
        columns = [r[1] for r in connection.execute(f"PRAGMA table_info({table})")]
        rows = connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        found[table] = (columns, [tuple(r) for r in rows])
    return found


def _same_rows(
    before: dict[str, tuple[list[str], list[tuple[Any, ...]]]], connection: sqlite3.Connection
) -> None:
    """Every table of ``before`` has the same rows now, column by column (a column added since
    is left out), in the same order, and the same count."""
    for table, (columns, rows) in before.items():
        now = connection.execute(
            f"SELECT {', '.join(columns)} FROM {table} ORDER BY rowid"
        ).fetchall()
        assert [tuple(r) for r in now] == rows, table
        assert len(now) == len(rows), table


def _schema_sql(connection: sqlite3.Connection, name: str) -> str:
    """The object's definition; a renamed table's name is stored quoted, which is the same."""
    row = connection.execute("SELECT sql FROM sqlite_master WHERE name = ?", (name,)).fetchone()
    return str(row[0]).replace(f'"{name}"', name) if row else ""


async def test_migration_18_keeps_every_row_id_key_and_count_of_a_populated_database() -> None:
    connection, runner = await _seeded()
    before = _snapshot(connection)
    assert [t for t in SEEDED if not before[t][1]] == []
    assert runner.migrate() == (18,)
    _same_rows(before, connection)
    # Every channel kept its id; a new one gets its own, and an email channel has no origin.
    ids = [r[0] for r in connection.execute("SELECT id FROM alert_channels ORDER BY rowid")]
    assert ids == list(WEBHOOKS)
    connection.execute(
        "INSERT INTO alert_channels (id, organization_id, name, type, enabled, is_default,"
        " url_origin, version, created_at_ms, updated_at_ms, consecutive_failures)"
        " VALUES ('chn_00000000000000c1', 'north', 'Mail', 'email', 1, 0, NULL, 1, 0, 0, 0)"
    )
    for bad in (("webhook", "NULL"), ("email", "'https://x.example.com'"), ("sms", "NULL")):
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                "INSERT INTO alert_channels (id, organization_id, name, type, enabled, is_default,"
                f" url_origin, version, created_at_ms, updated_at_ms, consecutive_failures)"
                f" VALUES ('chn_00000000000000c2', 'north', 'Bad', '{bad[0]}', 1, 0, {bad[1]}, 1,"
                " 0, 0, 0)"
            )
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):  # the name index, made again
        connection.execute(
            "INSERT INTO alert_channels (id, organization_id, name, type, enabled, is_default,"
            " url_origin, version, created_at_ms, updated_at_ms, consecutive_failures)"
            " VALUES ('chn_00000000000000c3', 'north', 'Mail', 'email', 1, 0, NULL, 1, 0, 0, 0)"
        )
    # The foreign keys: checked, and on again after the rebuild.
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    # The outbox: one row per (notification, channel, recipient), a NULL recipient once.
    insert = (
        "INSERT INTO notification_deliveries (id, notification_id, channel_id, organization_id,"
        " state, attempts, payload, created_at_ms, updated_at_ms, recipient_id)"
        " VALUES (?, 'ntf_x', 'chn_00000000000000c1', 'north', 'queued', 0, '{}', 0, 0, ?)"
    )
    connection.execute(insert, ("dlv_r1", "rcp_0000000000000001"))
    connection.execute(insert, ("dlv_r2", "rcp_0000000000000002"))
    connection.execute(insert, ("dlv_r3", None))
    for again in (("dlv_r4", "rcp_0000000000000001"), ("dlv_r5", None)):
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            connection.execute(insert, again)
    connection.close()


async def test_a_failure_after_the_copy_leaves_the_database_exactly_as_it_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection, runner = await _seeded()
    before = _snapshot(connection)
    schema = _schema_sql(connection, "alert_channels")

    def planted(*_: object) -> bool:
        raise RuntimeError("planted failure after the copy")

    monkeypatch.setattr(m0018, "_has_column", planted)
    with pytest.raises(RuntimeError, match="planted"):
        runner.migrate()
    assert runner.current_version() == 17
    assert _snapshot(connection) == before
    assert _schema_sql(connection, "alert_channels") == schema
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    monkeypatch.undo()
    assert runner.migrate() == (18,)  # the control
    connection.close()


async def test_the_reverse_gives_version_17_and_every_row_back() -> None:
    connection, runner = await _seeded()
    before = _snapshot(connection)
    schema = _schema_sql(connection, "alert_channels")
    index = _schema_sql(connection, "notification_deliveries_one")
    assert runner.migrate() == (18,)
    assert runner.rollback(17) == (18,)
    _same_rows(before, connection)
    assert _schema_sql(connection, "alert_channels") == schema
    assert _schema_sql(connection, "notification_deliveries_one") == index
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master")}
    assert not tables & {"alert_recipients", "confirmation_caps", "platform_secrets"}
    # Applied again, the kept column is found and left.
    assert runner.migrate() == (18,)
    _same_rows(before, connection)
    connection.close()


@pytest.mark.parametrize("kind", ["email channel", "recipient delivery"])
async def test_the_reverse_is_refused_while_email_data_exists(kind: str) -> None:
    connection, runner = await _seeded()
    assert runner.migrate() == (18,)
    if kind == "email channel":
        connection.execute(
            "INSERT INTO alert_channels (id, organization_id, name, type, enabled, is_default,"
            " url_origin, version, created_at_ms, updated_at_ms, consecutive_failures)"
            " VALUES ('chn_00000000000000c1', 'north', 'Mail', 'email', 1, 0, NULL, 1, 0, 0, 0)"
        )
    else:
        connection.execute(
            "UPDATE notification_deliveries SET recipient_id = 'rcp_0000000000000001'"
            " WHERE id = 'dlv_0000000000000000'"
        )
    before = _snapshot(connection)
    with pytest.raises(MigrationError, match="would lose it"):
        runner.rollback(17)
    assert runner.current_version() == 18
    assert _snapshot(connection) == before
    connection.close()


async def test_the_server_refuses_the_rebuild_on_an_existing_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A5-15: a database at 17 is refused at start, naming migration 18 and the CLI; it is
    left at 17. The control: a new, empty database is migrated by the server itself."""
    from gateway.app import create_default_app

    path = tmp_path / "gateway.db"
    connection, _ = await _seeded(path)
    connection.close()
    for name, value in {
        "GATEWAY_DATABASE": str(path),
        "GATEWAY_KEY_PEPPER": "p" * 40,
        "GATEWAY_VAULT_KEY": "A" * 43,
        "GATEWAY_ADMIN_USERNAME": "admin",
        "GATEWAY_ADMIN_PASSWORD": "a-long-admin-password-for-tests-only",
    }.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(MigrationError, match="needs migration 18, which rebuilds a table"):
        create_default_app()
    connection = open_database(path)
    runner = MigrationRunner(connection, MIGRATIONS, ClockEpoch(SimClock()))
    assert runner.current_version() == 17
    connection.close()


def test_the_cli_backs_up_before_migration_18_and_its_dry_run_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import asyncio

    path = tmp_path / "gateway.db"
    connection, _ = asyncio.run(_seeded(path))
    before = _snapshot(connection)
    connection.close()
    environ = {"GATEWAY_DATABASE": str(path)}
    assert manage(["migrate", "--dry-run"], environ) == 0
    out = capsys.readouterr()
    assert "migration 18 email_channels (rebuilds a table)" in out.out + out.err
    connection = open_database(path)
    assert MigrationRunner(connection, MIGRATIONS, ClockEpoch(SimClock())).current_version() == 17
    assert _snapshot(connection) == before
    connection.close()
    assert manage(["migrate"], environ) == 0
    backups = [p for p in tmp_path.iterdir() if p != path and p.name.startswith("gateway.db.")]
    backups = [p for p in backups if not p.name.endswith(("-wal", "-shm", ".lock"))]
    assert len(backups) == 1, backups
    backup = open_database(backups[0])
    assert _snapshot(backup) == before  # the backup is the database before the rebuild
    backup.close()
    connection = open_database(path)
    assert MigrationRunner(connection, MIGRATIONS, ClockEpoch(SimClock())).current_version() == 18
    _same_rows(before, connection)
    connection.close()


# --- Postgres schema 7 ---------------------------------------------------------------------------


async def _pg_snapshot(world: Any) -> dict[str, tuple[list[str], list[tuple[Any, ...]]]]:
    from gateway.postgres import TABLES

    found = {}
    for table in TABLES:
        columns = [
            r[0]
            for r in await world.rows(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = $1 AND table_name = $2 ORDER BY ordinal_position",
                world.schema,
                table,
            )
        ]
        if not columns:
            continue
        rows = await world.rows(f"SELECT {', '.join(columns)} FROM {table}")
        found[table] = (columns, sorted(rows, key=repr))
    return found


async def _pg_same_rows(
    before: dict[str, tuple[list[str], list[tuple[Any, ...]]]], world: Any
) -> None:
    for table, (columns, rows) in before.items():
        now = await world.rows(f"SELECT {', '.join(columns)} FROM {table}")
        assert sorted(now, key=repr) == rows, table


async def _pg_seeded(world: Any) -> None:
    from gateway.postgres import postgres_state, rollback_schema

    [db] = world.instances
    assert await rollback_schema(db, 6) == (7,)

    async def execute(sql: str, args: tuple[Any, ...]) -> None:
        numbered, index = "", 0
        for char in sql:
            if char == "?":
                index += 1
                numbered += f"${index}"
            else:
                numbered += char
        await world.rows(numbered, *args)

    await seed(postgres_state(db), execute)


@postgres
async def test_schema_7_keeps_every_row_of_a_populated_database_and_rolls_back() -> None:
    from gateway.postgres import apply_schema, rollback_schema
    from postgres_helpers import pg_world

    async with pg_world() as world:
        [db] = world.instances
        await _pg_seeded(world)
        before = await _pg_snapshot(world)
        assert [t for t in SEEDED if not before[t][1]] == []
        assert await apply_schema(db) == 7
        await _pg_same_rows(before, world)
        validated = await world.rows(
            "SELECT conname, convalidated FROM pg_constraint c JOIN pg_namespace n"
            " ON n.oid = c.connamespace WHERE n.nspname = $1 AND conname LIKE 'alert_channels_%'"
            " ORDER BY conname",
            world.schema,
        )
        assert ("alert_channels_origin_check", True) in validated
        assert ("alert_channels_type_check", True) in validated
        assert await rollback_schema(db, 6) == (7,)
        await _pg_same_rows(before, world)
        assert await apply_schema(db) == 7  # applied again: the kept column is left


@postgres
@pytest.mark.parametrize("kind", ["email channel", "recipient delivery"])
async def test_schema_7s_rollback_is_refused_while_email_data_exists(kind: str) -> None:
    import asyncpg

    from gateway.postgres import rollback_schema
    from postgres_helpers import pg_world

    async with pg_world() as world:
        [db] = world.instances
        await world.rows(
            "INSERT INTO alert_channels (id, organization_id, name, type, enabled, is_default,"
            " url_origin, version, created_at_ms, updated_at_ms, consecutive_failures)"
            " VALUES ('chn_00000000000000c1', 'north', 'Mail', $1, 1, 0, $2, 1, 0, 0, 0)",
            *(("email", None) if kind == "email channel" else ("webhook", "https://h.example")),
        )
        if kind == "recipient delivery":
            await world.rows(
                "INSERT INTO notification_deliveries (id, notification_id, channel_id,"
                " organization_id, state, attempts, payload, created_at_ms, updated_at_ms,"
                " recipient_id) VALUES ('dlv_r', 'ntf_r', 'chn_00000000000000c1', 'north',"
                " 'queued', 0, '{}', 0, 0, 'rcp_0000000000000001')"
            )
        with pytest.raises(asyncpg.exceptions.RaiseError, match="schema 7 not rolled back"):
            await rollback_schema(db, 6)
        assert await world.rows("SELECT max(version) FROM gateway_schema") == [(7,)]
        assert await world.rows("SELECT count(*) FROM alert_recipients") == [(0,)]


CHANNEL = (
    "INSERT INTO alert_channels (id, organization_id, name, type, enabled, is_default,"
    " url_origin, version, created_at_ms, updated_at_ms, consecutive_failures)"
    " VALUES ($1, 'north', $1, $2, 1, 0, $3, 1, 0, 0, 0)"
)
RECIPIENT = (
    "INSERT INTO alert_recipients (id, channel_id, organization_id, address, state, code_digest,"
    " code_expires_at_ms, wrong_entries, added_at_ms, verified_at_ms)"
    " VALUES ($1, 'chn_00000000000000c1', 'north', $2, $3, $4, NULL, $5, 0, $6)"
)
DELIVERY = (
    "INSERT INTO notification_deliveries (id, notification_id, channel_id, organization_id,"
    " state, attempts, payload, created_at_ms, updated_at_ms, recipient_id)"
    " VALUES ($1, 'ntf_u', 'chn_00000000000000c1', 'north', 'queued', 0, '{}', 0, 0, $2)"
)


@postgres
@pytest.mark.parametrize(
    ("sql", "args", "constraint"),
    [
        (CHANNEL, ("chn_t1", "webhook", None), "alert_channels_origin_check"),
        (CHANNEL, ("chn_t2", "email", "https://h.example"), "alert_channels_origin_check"),
        (CHANNEL, ("chn_t3", "sms", None), "alert_channels_type_check"),
        (RECIPIENT, ("rcp_s", "s@example.com", "pending", None, 0, None),
         "alert_recipients_state_check"),
        (RECIPIENT, ("rcp_w", "w@example.com", "unverified", None, 6, None),
         "alert_recipients_wrong_check"),
        (RECIPIENT, ("rcp_n", "n@example.com", "unverified", None, -1, None),
         "alert_recipients_wrong_check"),
        (RECIPIENT, ("rcp_c", "c@example.com", "verified", b"\x01" * 32, 0, 1.0),
         "alert_recipients_code_check"),
        (RECIPIENT, ("rcp_v", "v@example.com", "verified", None, 0, None),
         "alert_recipients_verified_check"),
        (RECIPIENT, ("rcp_u", "u@example.com", "unverified", None, 0, 1.0),
         "alert_recipients_verified_check"),
        ("INSERT INTO confirmation_caps (address_digest, window_start_ms, sent)"
         " VALUES ('d', 0, $1)", (0,), "confirmation_caps_sent_check"),
        ("INSERT INTO platform_secrets (kind, key_id, nonce, ciphertext, host, port, username,"
         " updated_at_ms) VALUES ($1, 'k', '\\x00', '\\x00', 'h', 587, 'u', 0)",
         ("webhook_url",), "platform_secrets_kind_check"),
        ("INSERT INTO platform_secrets (kind, key_id, nonce, ciphertext, host, port, username,"
         " updated_at_ms) VALUES ('smtp_password', 'k', '\\x00', '\\x00', 'h', $1, 'u', 0)",
         (0,), "platform_secrets_port_check"),
        ("INSERT INTO platform_secrets (kind, key_id, nonce, ciphertext, host, port, username,"
         " updated_at_ms) VALUES ('smtp_password', 'k', '\\x00', '\\x00', 'h', $1, 'u', 0)",
         (65536,), "platform_secrets_port_check"),
    ],
)  # fmt: skip
async def test_each_postgres_check_refuses_its_raw_row(
    sql: str, args: tuple[Any, ...], constraint: str
) -> None:
    from postgres_helpers import pg_world
    from test_alert_channel_stores import _pg_raw

    async with pg_world() as world:
        await world.rows(CHANNEL, "chn_00000000000000c1", "email", None)
        assert await _pg_raw(world, sql, *args) == constraint
        # The control: the same table takes a valid row.
        assert await _pg_raw(world, RECIPIENT, "rcp_ok", "ok@example.com", "unverified",
                             None, 0, None) is None  # fmt: skip


@postgres
async def test_each_postgres_unique_index_refuses_a_second_row() -> None:
    from postgres_helpers import pg_world
    from test_alert_channel_stores import _pg_raw

    async with pg_world() as world:
        await world.rows(CHANNEL, "chn_00000000000000c1", "email", None)
        row = ("ops@example.com", "unverified", None, 0, None)
        assert await _pg_raw(world, RECIPIENT, "rcp_1", *row) is None
        assert await _pg_raw(world, RECIPIENT, "rcp_2", *row) == "alert_recipients_one"
        # Another address on the channel is a row of its own: the control.
        assert await _pg_raw(world, RECIPIENT, "rcp_3", "b@example.com", *row[1:]) is None
        # The outbox: one row per (notification, channel, recipient); a NULL recipient once.
        assert await _pg_raw(world, DELIVERY, "dlv_1", "rcp_1") is None
        assert await _pg_raw(world, DELIVERY, "dlv_2", "rcp_3") is None
        assert await _pg_raw(world, DELIVERY, "dlv_3", None) is None
        assert await _pg_raw(world, DELIVERY, "dlv_4", "rcp_1") == "notification_deliveries_one"
        assert await _pg_raw(world, DELIVERY, "dlv_5", None) == "notification_deliveries_one"


def test_the_preview_says_it_rebuilds_and_keeps_the_channels() -> None:
    import asyncio

    connection, _ = asyncio.run(_seeded())
    from gateway.migrations import plan_migrations

    plan = plan_migrations(connection, MIGRATIONS)
    assert plan.rebuilds_tables
    [(migration, preview)] = plan.pending
    assert migration.version == 18
    assert any("3 channel(s) kept" in change for change in preview.changes)
    connection.close()
