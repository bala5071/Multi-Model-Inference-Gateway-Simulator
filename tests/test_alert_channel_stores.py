"""A4 group 1 below HTTP (docs/46): the channel and outbox stores on memory and SQLite alike;
the empty application id refused on every path an application can appear (the plan's section
21.1: the stores, the management service and the CLI, the plans file, the HTTP endpoint, the
copy to Postgres and back, migration 17 and Postgres schema 6), and no path that renames one;
``channel_secrets`` read by the channel stores only; ``rotate-vault-key`` resealing channel
secrets; and on Postgres, every constraint reached by a test of its own, the cap of 20 channels
on two instances at once, and the caps' conditional update at its cap.

Test values, not secrets."""

from __future__ import annotations

import ast
import asyncio
import base64
import random
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from gateway.alerts.channels import (
    KEY_KIND,
    URL_KIND,
    Channel,
    ChannelNameTakenError,
    ChannelService,
    ChannelVersionConflictError,
    InMemoryChannelStore,
    SqliteChannelStore,
    StoredSecret,
    binding_of,
)
from gateway.alerts.notices import Notices
from gateway.alerts.outbox import (
    Delivery,
    DeliveryState,
    InMemoryOutboxStore,
    SqliteOutboxStore,
)
from gateway.applications import (
    InMemoryApplicationStore,
    SqliteApplicationStore,
    application_from_template,
)
from gateway.audit import AuditLog, SqliteAuditStore
from gateway.clock import ClockEpoch, SimClock
from gateway.database import open_database
from gateway.management import ManagementService
from gateway.migrations import MIGRATIONS, MigrationError, MigrationRunner, plan_migrations
from gateway.organizations import EmptyApplicationIdError, new_organization
from gateway.permissions import Actor
from gateway.routing import PlansConfigError, load_plans
from gateway.state import run_now
from gateway.vault import VAULT_KEY_ENV, VAULT_KEY_PREVIOUS_ENV, SealedKey, Vault

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from manage import main as manage

REPO = Path(__file__).resolve().parent.parent
PLANS = load_plans()
MASTER = bytes(range(32))
OTHER = bytes(range(32, 64))
NOW = 1.8e12
URL = "https://hooks.example.com/services/planted-store-token"
postgres = pytest.mark.postgres


class _Marker:
    def __init__(self) -> None:
        self.marked = 0

    def changed(self) -> None:
        self.marked += 1


ACTOR = Actor("usr_00000000000000aa", "north", ())


def _application(app_id: str, org: str = "north", name: str = "Shop") -> Any:
    """An application as a store is given it. The template refuses an empty id too, so one is
    made from a good one: a store's own check is what is tested."""
    made = application_from_template("x", name, PLANS, "pro", organization_id=org,
                                     created_at_ms=NOW)  # fmt: skip
    return replace(made, id=app_id)


def _sqlite(tmp_path: Path | None = None) -> sqlite3.Connection:
    connection = open_database(":memory:" if tmp_path is None else tmp_path / "gateway.db")
    MigrationRunner(connection, MIGRATIONS, ClockEpoch(SimClock())).migrate()
    return connection


# --- the stores, memory and SQLite alike ---------------------------------------------------------


async def _channel_scenario(channels: Any, outbox: Any) -> list[Any]:
    out: list[Any] = []
    first = Channel("chn_00000000000000a1", "north", "Ops", "webhook", True, True,
                    "https://hooks.example.com", 1, NOW, NOW)  # fmt: skip
    second = replace(first, id="chn_00000000000000a2", name="Pager", is_default=False)
    await channels.insert_channel(first)
    await channels.insert_channel(second)
    for call in (
        channels.insert_channel(replace(second, id="chn_00000000000000a3")),
        channels.insert_channel(second),
        channels.put_channel(replace(second, version=2), 7),
        channels.put_channel(replace(second, name="Ops", version=2), 1),
    ):
        try:
            await call
            out.append("ok")
        except (ChannelNameTakenError, ChannelVersionConflictError) as exc:
            out.append(type(exc).__name__)
    await channels.put_channel(replace(second, name="Pager 2", version=2), 1)
    sealed = SealedKey("k1", b"\x00" * 12, b"\x01\x02", None)
    await channels.put_secret(StoredSecret(first.id, URL_KIND, sealed))
    await channels.put_secret(StoredSecret(first.id, KEY_KIND, sealed, NOW))
    await channels.put_secret(StoredSecret(second.id, URL_KIND, sealed))
    out += [
        await channels.get_channel(second.id),
        await channels.list_channels("north", 1),
        await channels.list_channels("north", 9, first.id),
        await channels.organization_channels("north"),
        await channels.count_channels("north"),
        await channels.count_channels("south"),
        await channels.secrets(first.id),
        await channels.all_secrets(),
    ]
    rows = [
        Delivery(
            f"dlv_{n:016x}",
            f"ntf_{n % 2:016x}",
            c,
            "north",
            DeliveryState.QUEUED,
            0,
            NOW,
            None,
            None,
            None,
            "{}",
            NOW,
            NOW,
        )
        for n, c in enumerate((first.id, second.id, first.id), start=1)
    ]
    out += [
        await outbox.queue(rows),
        await outbox.queue(rows),
        await outbox.channel_deliveries(first.id, 9),
        await outbox.notification_deliveries(["ntf_0000000000000001"]),
        await outbox.fail_channel(first.id, "channel_gone", NOW + 1),
        await outbox.channel_deliveries(first.id, 9),
        [await outbox.count_notification("rule", "r", 1, 2) for _ in range(3)],
        await outbox.count_notification("rule", "r", 2, 2),
        await outbox.add_summary("north", 1, "ntf_00000000000000ee", NOW),
        await outbox.add_summary("north", 1, "ntf_00000000000000ef", NOW),
        await channels.delete_channel(first.id),
        await channels.delete_channel(first.id),
        await channels.secrets(first.id),
        await channels.all_secrets(),
    ]
    return out


async def test_the_channel_and_outbox_stores_answer_alike_in_memory_and_on_sqlite() -> None:
    connection = _sqlite()
    lite = await _channel_scenario(SqliteChannelStore(connection), SqliteOutboxStore(connection))
    memory = await _channel_scenario(InMemoryChannelStore(), InMemoryOutboxStore())
    assert memory == lite
    assert lite[:4] == [
        "ChannelNameTakenError", "ChannelVersionConflictError", "ChannelVersionConflictError",
        "ChannelNameTakenError",
    ]  # fmt: skip


async def test_the_cap_of_twenty_is_counted_in_the_service() -> None:
    store = InMemoryChannelStore()
    service = ChannelService(
        store, Vault(MASTER), AuditLog(SqliteAuditStore(_sqlite())), ClockEpoch(SimClock()),
        random.Random(1).randbytes,
    )  # fmt: skip
    for n in range(20):
        await service.create("north", name=f"H{n}", url=URL, is_default=False,
                             channel_type="webhook", actor=ACTOR, marker=_Marker())  # fmt: skip
    marker = _Marker()
    with pytest.raises(ValueError, match="at most 20"):
        await service.create("north", name="H20", url=URL, is_default=False,
                             channel_type="webhook", actor=ACTOR, marker=marker)  # fmt: skip
    assert marker.marked == 0  # refused before anything changed: the window goes back
    assert await store.count_channels("north") == 20


# --- the empty application id, on every path ------------------------------------------------------


def _stores_with_north() -> list[Any]:
    memory = InMemoryApplicationStore()
    lite = SqliteApplicationStore(_sqlite())
    for store in (memory, lite):
        run_now(
            store.add_organization(
                new_organization("north", "North", "business", created_at_ms=0.0)
            )
        )
    return [memory, lite]


@pytest.mark.parametrize("which", [0, 1], ids=["memory", "sqlite"])
async def test_every_store_refuses_an_application_with_an_empty_id(which: int) -> None:
    store = _stores_with_north()[which]
    with pytest.raises(EmptyApplicationIdError):
        await store.add_application(_application(""))
    assert await store.list_applications() == []
    await store.add_application(_application("shop"))  # the control
    assert [a.id for a in await store.list_applications()] == ["shop"]


async def test_the_management_service_and_its_cli_id_refuse_it() -> None:
    connection = _sqlite()
    store = SqliteApplicationStore(connection)
    await store.add_organization(new_organization("north", "North", "business", created_at_ms=0.0))
    from gateway.apikeys import ApiKeyService
    from helpers import TEST_PEPPER

    audit = AuditLog(SqliteAuditStore(connection))
    service = ManagementService(
        ApiKeyService(store, TEST_PEPPER, ClockEpoch(SimClock())), PLANS, audit,
        ClockEpoch(SimClock()),
    )  # fmt: skip
    with pytest.raises(EmptyApplicationIdError):
        await service.create_application("north", "Shop", "pro", application_id="")
    assert await store.list_applications() == []
    assert await audit.entries() == []  # refused before anything was written
    made = await service.create_application("north", "Shop", "pro")  # generated: never empty
    assert made.id.startswith("app_")
    assert len(made.id) == 20


def test_the_cli_refuses_an_empty_id_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from gateway.apikeys import PEPPER_ENV
    from helpers import TEST_PEPPER

    environ = {"GATEWAY_DATABASE": str(tmp_path / "gateway.db"), PEPPER_ENV: TEST_PEPPER.decode()}
    assert (
        manage(["create-application", "--id", "", "--name", "Shop", "--plan", "pro"], environ) == 1
    )
    assert "may not be empty" in capsys.readouterr().err
    connection = open_database(tmp_path / "gateway.db")
    assert connection.execute("SELECT count(*) FROM applications").fetchone() == (0,)
    connection.close()
    # The control: an id given is used.
    assert manage(["create-application", "--id", "shop", "--name", "Shop", "--plan", "pro"],
                  environ) == 0  # fmt: skip


def test_the_plans_file_may_not_name_an_application_with_an_empty_id(tmp_path: Path) -> None:
    """The bootstrap's demo applications come from the plans file."""
    packaged = (REPO / "src" / "gateway" / "plans.yaml").read_text(encoding="utf-8")
    path = tmp_path / "plans.yaml"
    path.write_text(
        packaged.replace(
            "applications:\n", 'applications:\n  "":\n    name: Empty\n    plan: pro\n', 1
        ),
        encoding="utf-8",
    )
    with pytest.raises(PlansConfigError, match="may not be empty"):
        load_plans(path)
    assert load_plans(REPO / "src" / "gateway" / "plans.yaml").applications  # the control


async def test_the_http_endpoint_never_takes_an_id_and_an_empty_segment_matches_no_route() -> None:
    import httpx

    from test_alert_channels_api import console

    async with console() as c:
        alice = await c.login("alice")
        given = await c.call(alice, "POST", "/north/applications",
                             {"name": "New", "plan": "pro", "id": ""})  # fmt: skip
        assert given.status_code == 422
        made = await c.call(alice, "POST", "/north/applications", {"name": "New", "plan": "pro"})
        assert made.status_code == 201, made.text
        made_id = made.json()["application_id"]
        assert made_id.startswith("app_")
        assert len(made_id) == 20
        for path in (
            "/north/applications//alert-channels",
            "/north/applications//incidents",
            "/north/applications//alert-rules",
        ):
            answer = await c.call(alice, "GET", path)
            assert answer.status_code == 404, (path, answer.status_code)
        assert isinstance(c.client, httpx.AsyncClient)


def test_no_statement_renames_an_application() -> None:
    """Section 21.1: no path changes an application's id (if one is added, it must call
    check_application_id). Every SQL string in the package that updates applications leaves
    its id alone."""
    found = []
    for path in (REPO / "src" / "gateway").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = " ".join(node.value.split()).upper()
                if text.startswith("UPDATE APPLICATIONS SET"):
                    found.append((path.name, text))
    assert all(" ID =" not in t.split(" WHERE ")[0] for _, t in found), found


def test_migration_17_refuses_while_an_application_has_an_empty_id(tmp_path: Path) -> None:
    connection = open_database(tmp_path / "gateway.db")
    runner = MigrationRunner(connection, MIGRATIONS, ClockEpoch(SimClock()))
    runner.migrate(16)
    connection.execute(
        "INSERT INTO applications (id, name, plan_name, models, price_multiplier,"
        " monthly_token_budget, max_tokens, requests_per_minute, created_at_ms, organization_id)"
        " VALUES ('', 'Empty', 'pro', '[]', 1.0, 1000, NULL, NULL, 0, 'default')"
    )
    plan = plan_migrations(connection, MIGRATIONS)
    assert any("empty id" in c for c in plan.conflicts)
    with pytest.raises(MigrationError, match="migration 17 refused"):
        runner.migrate()
    assert runner.current_version() == 16
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master")}
    assert "alert_channels" not in tables
    connection.execute("DELETE FROM applications WHERE id = ''")
    assert runner.migrate() == (17,)  # the control
    # Rolled back and applied again, the column it added is kept and found.
    assert runner.rollback(16) == (17,)
    assert runner.migrate() == (17,)
    connection.close()


# --- channel_secrets is read by the channel stores only -------------------------------------------


def _sql_using(table: str) -> set[tuple[str, str]]:
    """(module, enclosing class or '<module>') of every string in the package naming ``table``."""
    found: set[tuple[str, str]] = set()
    for path in (REPO / "src" / "gateway").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # Docstrings say where a table is read; they read nothing.
        docstrings = {
            id(n.body[0].value)
            for n in ast.walk(tree)
            if isinstance(n, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and n.body
            and isinstance(n.body[0], ast.Expr)
            and isinstance(n.body[0].value, ast.Constant)
        }
        for top in tree.body:
            owner = top.name if isinstance(top, ast.ClassDef) else "<module>"
            for node in ast.walk(top):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and table in node.value
                    and id(node) not in docstrings
                ):
                    found.add((path.relative_to(REPO / "src" / "gateway").as_posix(), owner))
    return found


def test_only_the_channel_stores_read_channel_secrets() -> None:
    assert _sql_using("channel_secrets") == {
        ("alerts/channels.py", "SqliteChannelStore"),
        ("postgres.py", "PostgresChannelStore"),
        # The schema, and the copy between databases, which moves every table as it is.
        ("migrations/m0017_alert_channels.py", "<module>"),
        ("postgres.py", "<module>"),
        ("pg_migrate.py", "<module>"),
    }


def test_the_check_finds_another_reader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.modules[__name__], "REPO", tmp_path)
    (tmp_path / "src" / "gateway").mkdir(parents=True)
    (tmp_path / "src" / "gateway" / "chatty.py").write_text(
        'class Reader:\n    SQL = "SELECT * FROM channel_secrets"\n', encoding="utf-8"
    )
    assert _sql_using("channel_secrets") == {("chatty.py", "Reader")}


# --- rotate-vault-key ---------------------------------------------------------------------------


def _key(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def test_rotate_vault_key_reseals_channel_secrets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "gateway.db"
    connection = open_database(path)
    MigrationRunner(connection, MIGRATIONS, ClockEpoch(SimClock())).migrate()
    store = SqliteChannelStore(connection)
    old = ChannelService(
        store, Vault(OTHER), AuditLog(SqliteAuditStore(connection)), ClockEpoch(SimClock()),
        random.Random(3).randbytes,
    )  # fmt: skip
    made = run_now(old.create("default", name="Ops", url=URL, is_default=True,
                              channel_type="webhook", actor=ACTOR, marker=_Marker()))  # fmt: skip
    connection.close()
    environ = {"GATEWAY_DATABASE": str(path), VAULT_KEY_ENV: _key(MASTER),
               VAULT_KEY_PREVIOUS_ENV: _key(OTHER)}  # fmt: skip
    assert manage(["rotate-vault-key"], environ) == 0
    err = capsys.readouterr().err
    assert "and 2 alert channel secret(s)" in err
    assert "planted-store-token" not in err
    assert made.signing_key not in err
    connection = open_database(path)
    store = SqliteChannelStore(connection)
    channel = run_now(store.get_channel(made.channel.id))
    assert channel is not None
    secrets = run_now(store.secrets(channel.id))
    vault = Vault(MASTER)  # the new master alone opens them now
    assert vault.open(secrets[URL_KIND].sealed, binding_of(channel, URL_KIND)).reveal() == URL
    key = vault.open(secrets[KEY_KIND].sealed, binding_of(channel, KEY_KIND)).reveal()
    assert key == made.signing_key
    connection.close()


# --- Postgres: each constraint reached by a test of its own ------------------------------------


async def _pg_raw(world: Any, sql: str, *args: Any) -> str | None:
    """Run ``sql`` as the admin; the name of the constraint it broke, or None."""
    import asyncpg

    await world.admin.execute(f"SET search_path TO {world.schema}")
    try:
        await world.admin.execute(sql, *args)
    except asyncpg.exceptions.IntegrityConstraintViolationError as exc:
        return str(exc.constraint_name)
    return None


CHANNEL_ROW = (
    "INSERT INTO alert_channels (id, organization_id, name, type, enabled, is_default,"
    " url_origin, version, created_at_ms, updated_at_ms, consecutive_failures)"
    " VALUES ($1, 'north', $2, $3, $4, $5, 'https://h.example', $6, 0, 0, $7)"
)
DELIVERY_ROW = (
    "INSERT INTO notification_deliveries (id, notification_id, channel_id, organization_id,"
    " state, attempts, payload, created_at_ms, updated_at_ms)"
    " VALUES ($1, $2, 'chn_00000000000000a1', 'north', $3, $4, '{}', 0, 0)"
)


@postgres
async def test_postgres_unique_channel_names_are_kept_by_their_own_index() -> None:
    from gateway.postgres import PostgresChannelStore
    from postgres_helpers import pg_world

    async with pg_world() as world:
        assert await _pg_raw(world, CHANNEL_ROW, "chn_a", "Ops", "webhook", 1, 1, 1, 0) is None
        # A different id, the same organization and name: the unique index, not the key.
        broke = await _pg_raw(world, CHANNEL_ROW, "chn_b", "Ops", "webhook", 1, 1, 1, 0)
        assert broke == "alert_channels_name"
        [db] = world.instances
        store = PostgresChannelStore(db)
        made = Channel("chn_00000000000000c1", "north", "Ops", "webhook", True, True,
                       "https://h.example", 1, NOW, NOW)  # fmt: skip
        with pytest.raises(ChannelNameTakenError):
            await store.insert_channel(made)
        # Another organization may use the name.
        await store.insert_channel(replace(made, organization_id="south"))


@postgres
async def test_postgres_one_delivery_per_notification_and_channel() -> None:
    from postgres_helpers import pg_world

    async with pg_world() as world:
        assert await _pg_raw(world, DELIVERY_ROW, "dlv_a", "ntf_1", "queued", 0) is None
        # Distinct delivery ids, the same notification and channel: the unique index.
        broke = await _pg_raw(world, DELIVERY_ROW, "dlv_b", "ntf_1", "queued", 0)
        assert broke == "notification_deliveries_one"


@postgres
async def test_postgres_one_summary_an_organization_an_hour() -> None:
    from postgres_helpers import pg_world

    sql = (
        "INSERT INTO suppression_summaries (organization_id, hour, notification_id,"
        " created_at_ms) VALUES ('north', 7, $1, 0)"
    )
    async with pg_world() as world:
        assert await _pg_raw(world, sql, "ntf_1") is None
        assert await _pg_raw(world, sql, "ntf_2") == "suppression_summaries_one"


@postgres
@pytest.mark.parametrize(
    ("sql", "args", "constraint"),
    [
        (CHANNEL_ROW, ("chn_t", "T", "email", 1, 1, 1, 0), "alert_channels_type_check"),
        (CHANNEL_ROW, ("chn_e", "E", "webhook", 2, 1, 1, 0), "alert_channels_enabled_check"),
        (CHANNEL_ROW, ("chn_d", "D", "webhook", 1, 2, 1, 0), "alert_channels_is_default_check"),
        (CHANNEL_ROW, ("chn_v", "V", "webhook", 1, 1, 0, 0), "alert_channels_version_check"),
        (
            CHANNEL_ROW,
            ("chn_f", "F", "webhook", 1, 1, 1, -1),
            "alert_channels_consecutive_failures_check",
        ),
        (
            "INSERT INTO channel_secrets (channel_id, kind, key_id, nonce, ciphertext)"
            " VALUES ('chn_a', $1, 'k', '\\x00', '\\x00')",
            ("provider_key",),
            "channel_secrets_kind_check",
        ),
        (DELIVERY_ROW, ("dlv_s", "ntf_s", "lost", 0), "notification_deliveries_state_check"),
        (DELIVERY_ROW, ("dlv_a", "ntf_a", "queued", -1), "notification_deliveries_attempts_check"),
        (
            "INSERT INTO notification_caps (scope, scope_id, hour, sent) VALUES ($1, 'x', 1, 0)",
            ("user",),
            "notification_caps_scope_check",
        ),
        (
            "INSERT INTO notification_caps (scope, scope_id, hour, sent)"
            " VALUES ('rule', 'x', 1, $1)",
            (-1,),
            "notification_caps_sent_check",
        ),
    ],
)
async def test_postgres_checks_refuse_raw_rows(
    sql: str, args: tuple[Any, ...], constraint: str
) -> None:
    from postgres_helpers import pg_world

    async with pg_world() as world:
        assert await _pg_raw(world, sql, *args) == constraint


@postgres
async def test_postgres_caps_count_exactly_the_cap_from_two_instances_at_once() -> None:
    """M25's statement: the count is one conditional upsert, so two instances counting at the
    same time stop at the cap together."""
    from gateway.postgres import PostgresOutboxStore
    from postgres_helpers import pg_world

    async with pg_world(instances=2) as world:
        stores = [PostgresOutboxStore(db) for db in world.instances]
        counted = await asyncio.gather(
            *(stores[n % 2].count_notification("rule", "rul_x", 9, 4) for n in range(12))
        )
        assert sum(counted) == 4
        assert await world.rows("SELECT sent FROM notification_caps") == [(4,)]


@postgres
async def test_postgres_twenty_channels_from_two_instances_at_once() -> None:
    from gateway.postgres import PostgresAuditStore, PostgresChannelStore
    from postgres_helpers import pg_world

    async with pg_world(instances=2) as world:
        services = [
            ChannelService(
                PostgresChannelStore(db), Vault(MASTER), AuditLog(PostgresAuditStore(db)),
                ClockEpoch(SimClock()), random.Random(n).randbytes,
            )
            for n, db in enumerate(world.instances)
        ]  # fmt: skip
        for n in range(18):
            await services[0].create(
                "north",
                name=f"H{n}",
                url=URL,
                is_default=False,
                channel_type="webhook",
                actor=ACTOR,
                marker=_Marker(),
            )

        async def one(n: int) -> str:
            try:
                await services[n % 2].create(
                    "north",
                    name=f"R{n}",
                    url=URL,
                    is_default=False,
                    channel_type="webhook",
                    actor=ACTOR,
                    marker=_Marker(),
                )
            except ValueError as exc:
                return type(exc).__name__
            return "made"

        outcomes = await asyncio.gather(*(one(n) for n in range(6)))
        assert sorted(outcomes) == ["ManagementRefusal"] * 4 + ["made"] * 2
        assert await world.rows("SELECT count(*) FROM alert_channels") == [(20,)]


@postgres
async def test_postgres_schema_6_refuses_while_an_application_has_an_empty_id() -> None:
    import asyncpg

    from gateway.postgres import apply_schema, rollback_schema
    from postgres_helpers import pg_world

    async with pg_world() as world:
        [db] = world.instances
        assert await rollback_schema(db, 5) == (6,)
        await world.rows(
            "INSERT INTO applications (id, name, plan_name, models, price_multiplier,"
            " monthly_token_budget, created_at_ms, organization_id)"
            " VALUES ('', 'Empty', 'pro', '[]', 1.0, 1000, 0, 'default')"
        )
        with pytest.raises(asyncpg.exceptions.RaiseError, match="schema 6 refused"):
            await apply_schema(db)
        assert await world.rows("SELECT max(version) FROM gateway_schema") == [(5,)]
        await world.rows("DELETE FROM applications WHERE id = ''")
        assert await apply_schema(db) == 6  # the control


@postgres
async def test_the_copy_to_postgres_and_back_refuses_an_empty_application_id(
    tmp_path: Path,
) -> None:
    from gateway.pg_migrate import MigrationRefused, migrate_to_postgres, migrate_to_sqlite
    from postgres_helpers import pg_world

    connection = _sqlite(tmp_path)
    connection.execute(
        "INSERT INTO applications (id, name, plan_name, models, price_multiplier,"
        " monthly_token_budget, max_tokens, requests_per_minute, created_at_ms, organization_id)"
        " VALUES ('', 'Empty', 'pro', '[]', 1.0, 1000, NULL, NULL, 0, 'default')"
    )
    async with pg_world() as world:
        [db] = world.instances
        with pytest.raises(MigrationRefused, match="empty id"):
            await migrate_to_postgres(connection, db, dry_run=False)
        assert await world.rows("SELECT count(*) FROM applications") == [(0,)]
        # Back: a Postgres row of the same kind (written past the stores) is refused too.
        await world.rows(
            "INSERT INTO applications (id, name, plan_name, models, price_multiplier,"
            " monthly_token_budget, created_at_ms, organization_id)"
            " VALUES ('', 'Empty', 'pro', '[]', 1.0, 1000, 0, 'default')"
        )
        target = tmp_path / "back.db"
        with pytest.raises(MigrationRefused, match="empty id"):
            await migrate_to_sqlite(db, target, dry_run=False)
        assert not target.exists()
    connection.close()


@postgres
async def test_postgres_store_refuses_an_application_with_an_empty_id() -> None:
    from gateway.postgres import PostgresApplicationStore
    from postgres_helpers import pg_world

    async with pg_world() as world:
        [db] = world.instances
        store = PostgresApplicationStore(db)
        await store.add_organization(
            new_organization("north", "North", "business", created_at_ms=0.0)
        )
        with pytest.raises(EmptyApplicationIdError):
            await store.add_application(_application(""))
        assert await world.rows("SELECT count(*) FROM applications") == [(0,)]
        await store.add_application(_application("shop"))  # the control


async def test_notices_go_only_to_enabled_channels_of_the_organization() -> None:
    """The queue-time check (M15's second): ids are looked up among the organization's own
    channels, so another organization's id reaches nothing."""
    channels, outbox = InMemoryChannelStore(), InMemoryOutboxStore()
    apps = InMemoryApplicationStore()
    await apps.add_organization(new_organization("north", "North", "business", created_at_ms=0.0))
    await apps.add_application(_application("north-shop"))
    north = Channel("chn_00000000000000a1", "north", "N", "webhook", True, False, "o", 1, 0, 0)
    off = replace(north, id="chn_00000000000000a2", name="Off", enabled=False)
    south = replace(north, id="chn_00000000000000b1", organization_id="south", name="S")
    for c in (north, off, south):
        await channels.insert_channel(c)
    notices = Notices(channels, outbox, apps, ClockEpoch(SimClock()), random.Random(2).randbytes)
    found = await notices.destinations("north", [north.id, off.id, south.id])
    assert [c.id for c in found] == [north.id]
