"""Alert rules (docs/44): every route and its action as documented, nothing across organizations
or applications and missing equal to forbidden on every endpoint, who may read and write, each
field's refusal, versions and conflicts, the cap of 50, the audit of every change, the default
rules (with a new application, from migration 15's data step, from Postgres schema 4's, and
restored), the dry run with the last hour's preview, and the leak scan.

The world of ``test_management_api``: north (alice and carol org_admins, vera viewer, nina
app_admin of north-shop, bob no role) and south (sid org_admin), each with an application
named Shop, north also Blog; root is platform_admin. Test values, not secrets."""

from __future__ import annotations

import dataclasses
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.routing import APIRoute

from accounts_helpers import PASSWORD
from gateway.alertrecorder import EDGES, MINUTE_MS, bucket
from gateway.alertrules import (
    DEFAULT_RULES,
    MAX_RULES,
    ORGANIZATION_DEFAULTS,
    RuleInvalidError,
    SqliteRuleStore,
    default_rule_id,
    validate_rule,
)
from gateway.apikeys import ApplicationAccess
from gateway.app import create_app, declaration_of
from gateway.applications import SqliteApplicationStore, application_from_template
from gateway.audit import AuditAction, AuditEntry
from gateway.auth import AccountAccess
from gateway.clock import SystemEpoch
from gateway.continuous import build_continuous_registry
from gateway.database import open_database
from gateway.localonly import LocalOnlyPolicy
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.permissions import PERMISSIONS
from gateway.routing import load_plans
from gateway.state import run_now
from test_management_api import World, make_world

REPO = Path(__file__).resolve().parent.parent
FORBIDDEN_BYTES = b'{"error":{"code":"forbidden","message":"Not permitted, or not found."}}'
Session = tuple[str, str]
SHOP = "/north/applications/north-shop/alert-rules"
RULE = {
    "name": "Errors",
    "signal": "error_rate",
    "comparison": ">",
    "threshold": 10,
    "window": 15,
    "severity": "warning",
}


@dataclass(frozen=True)
class Endpoint:
    method: str
    path: str
    action: str
    success: int


def documented_endpoints() -> list[Endpoint]:
    text = (REPO / "docs" / "44-alert-rules.md").read_text(encoding="utf-8")
    pattern = re.compile(r"^\| `(GET|POST|PUT|DELETE)` \| `([^`]+)` \| `([a-z_.]+)` \| (\d{3}) \|$")
    found = []
    for line in text.splitlines():
        match = pattern.match(line.strip())
        if match:
            method, path, action, success = match.groups()
            found.append(Endpoint(method, "/{org}" + path, action, int(success)))
    return found


ENDPOINTS = documented_endpoints()


@dataclass
class Console:
    client: httpx.AsyncClient
    world: World
    app: Any

    async def login(self, username: str) -> Session:
        response = await self.client.post(
            "/v1/auth/login", json={"username": username, "password": PASSWORD}
        )
        self.client.cookies.clear()
        assert response.status_code == 200, response.text
        sid = response.headers["set-cookie"].split(";")[0].split("=", 1)[1]
        return sid, response.json()["csrf_token"]

    async def call(
        self, session: Session, method: str, path: str, body: Any = None
    ) -> httpx.Response:
        headers = {"Cookie": f"gw_session={session[0]}", "X-CSRF-Token": session[1]}
        url = "/v1/admin/orgs" + path
        if method == "GET":
            return await self.client.get(url, headers=headers)
        return await self.client.request(
            method, url, json=body if body is not None else {}, headers=headers
        )

    async def restore(self, session: Session, app: str) -> None:
        org = "south" if app.startswith("south") else "north"
        done = await self.call(
            session, "POST", f"/{org}/applications/{app}/alert-rules/restore-defaults"
        )
        assert done.status_code == 200, done.text


@asynccontextmanager
async def console(world: World | None = None) -> AsyncIterator[Console]:
    world = world if world is not None else make_world()
    accounts = world.accounts
    registry = build_continuous_registry(accounts.clock, seed=7)
    app = create_app(
        registry,
        accounts.clock,
        applications=ApplicationAccess(world.keys, world.templates),
        accounts=AccountAccess(accounts.service),
        local_only=LocalOnlyPolicy(),
        epoch=accounts.epoch,
        rules=SqliteRuleStore(accounts.connection),
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield Console(client, world, app)
    await registry.aclose()


async def _audit(world: World, *actions: AuditAction) -> list[AuditEntry]:
    return [e for e in await world.accounts.audit.entries() if e.event.action in actions]


def _rows(world: World) -> list[tuple[Any, ...]]:
    return world.accounts.connection.execute("SELECT * FROM alert_rules ORDER BY id").fetchall()


def _raw(response: httpx.Response) -> tuple[int, list[tuple[str, str]], bytes]:
    return response.status_code, sorted(response.headers.multi_items()), response.content


def _path(endpoint: Endpoint, org: str, app: str, rule: str) -> str:
    return endpoint.path.replace("{org}", org).replace("{app}", app).replace("{rule}", rule)


async def _current(c: Console, session: Session, endpoint: Endpoint, path: str) -> int:
    """The rule's version now, for a walk that writes it on each endpoint."""
    if "{rule}" not in endpoint.path:
        return 1
    rule_path = path.removesuffix("/enable").removesuffix("/disable")
    found = await c.call(session, "GET", rule_path)
    return int(found.json()["version"]) if found.status_code == 200 else 1


def _body(endpoint: Endpoint, version: int = 1) -> Any:
    if endpoint.method == "PUT":
        return {"expected_version": version, "rule": {**RULE, "threshold": 20}}
    if endpoint.path.endswith(("/enable", "/disable")) or endpoint.method == "DELETE":
        return {"expected_version": version}
    if endpoint.path.endswith("/alert-rules") or endpoint.path.endswith("/validate"):
        return RULE
    return None


# --- the routes, as documented ------------------------------------------------------------------


async def test_each_alert_rule_route_is_documented_and_declares_its_action() -> None:
    assert len(ENDPOINTS) == 9
    async with console() as c:
        routes = set()
        for route in c.app.routes:
            if isinstance(route, APIRoute) and "/alert-rules" in route.path:
                [declaration] = declaration_of(route)
                assert declaration.kind == "requires", route.path
                for method in route.methods or ():
                    routes.add(
                        (method, route.path.removeprefix("/v1/admin/orgs"), declaration.action)
                    )
    assert routes == {(e.method, e.path, e.action) for e in ENDPOINTS}
    for endpoint in ENDPOINTS:
        assert PERMISSIONS[endpoint.action].step_up is False, endpoint  # no step-up (section 12)


# --- cross-organization, cross-application, and missing equals forbidden ------------------------


async def test_nothing_crosses_organizations_or_applications_and_missing_looks_forbidden() -> None:
    """alice, org_admin of north, on every rule endpoint: south's path, an organization that
    does not exist, south's application through north, an application that does not exist, a
    rule of north-blog under north-shop, a rule of south's under north, a rule id that does not
    exist and one not in a rule id's form: the same 403, byte for byte; nothing written or
    audited."""
    async with console() as c:
        alice, sid = await c.login("alice"), await c.login("sid")
        for app in ("north-shop", "north-blog"):
            await c.restore(alice, app)
        await c.restore(sid, "south-shop")
        blog_rule = default_rule_id("north-blog", "error_rate")
        south_rule = default_rule_id("south-shop", "error_rate")
        before_rows = _rows(c.world)
        before_audit = await c.world.accounts.audit.entries()
        for endpoint in ENDPOINTS:
            shop_rule = default_rule_id("north-shop", "error_rate")
            attempts = {
                "south's own path": _path(endpoint, "south", "south-shop", south_rule),
                "no such organization": _path(endpoint, "nowhere", "north-shop", shop_rule),
                "south's application via north": _path(endpoint, "north", "south-shop", south_rule),
                "no such application": _path(endpoint, "north", "app_00000000", shop_rule),
            }
            if "{rule}" in endpoint.path:
                attempts |= {
                    "north-blog's rule under north-shop": _path(
                        endpoint, "north", "north-shop", blog_rule
                    ),
                    "south's rule under north-shop": _path(
                        endpoint, "north", "north-shop", south_rule
                    ),
                    "no such rule": _path(endpoint, "north", "north-shop", "rul_" + "0" * 16),
                    "not a rule id": _path(endpoint, "north", "north-shop", "rul_XYZ"),
                }
            answers = {
                name: _raw(await c.call(alice, endpoint.method, path, _body(endpoint)))
                for name, path in attempts.items()
            }
            first = next(iter(answers.values()))
            assert first[0] == 403, (endpoint, answers)
            assert first[2] == FORBIDDEN_BYTES
            for name, answer in answers.items():
                assert answer == first, (endpoint.path, name)
        assert _rows(c.world) == before_rows
        assert await c.world.accounts.audit.entries() == before_audit


async def test_south_can_do_all_of_it_to_its_own() -> None:
    """The control: the same endpoints work for south's org_admin on south's rules."""
    async with console() as c:
        sid = await c.login("sid")
        await c.restore(sid, "south-shop")
        rule = default_rule_id("south-shop", "error_rate")
        for endpoint in sorted(ENDPOINTS, key=lambda e: e.method == "DELETE"):
            path = _path(endpoint, "south", "south-shop", rule)
            version = await _current(c, sid, endpoint, path)
            answer = await c.call(sid, endpoint.method, path, _body(endpoint, version))
            assert answer.status_code == endpoint.success, (endpoint, answer.text)


# --- who may ------------------------------------------------------------------------------------


async def test_viewers_read_rules_and_validate_and_change_none() -> None:
    async with console() as c:
        alice, vera = await c.login("alice"), await c.login("vera")
        await c.restore(alice, "north-blog")
        before = _rows(c.world)
        rule = default_rule_id("north-blog", "error_rate")
        for endpoint in ENDPOINTS:
            answer = await c.call(
                vera, endpoint.method, _path(endpoint, "north", "north-blog", rule), _body(endpoint)
            )
            if endpoint.action == "alert_rules.read":
                assert answer.status_code == 200, (endpoint, answer.text)
            else:
                assert answer.content == FORBIDDEN_BYTES, endpoint
        assert _rows(c.world) == before


async def test_an_app_admin_keeps_to_its_application_and_no_role_gets_nothing() -> None:
    async with console() as c:
        alice, nina = await c.login("alice"), await c.login("nina")
        for app in ("north-shop", "north-blog"):
            await c.restore(alice, app)
        mine = await c.call(nina, "POST", SHOP, RULE)
        assert mine.status_code == 201, mine.text
        for endpoint in ENDPOINTS:
            rule = default_rule_id("north-blog", "error_rate")
            answer = await c.call(
                nina, endpoint.method, _path(endpoint, "north", "north-blog", rule), _body(endpoint)
            )
            assert answer.content == FORBIDDEN_BYTES, endpoint
        for name in ("bob", "root"):
            session = await c.login(name)
            for endpoint in ENDPOINTS:
                rule = default_rule_id("north-shop", "error_rate")
                answer = await c.call(
                    session, endpoint.method, _path(endpoint, "north", "north-shop", rule),
                    _body(endpoint),
                )  # fmt: skip
                assert answer.content == FORBIDDEN_BYTES, (name, endpoint)


# --- a rule, field by field ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"name": " Errors"}, "name"),
        ({"name": "x" * 65}, "name"),
        ({"name": "Errors 100%"}, "name"),
        ({"signal": "latency_total_p42"}, "signal"),
        ({"parameter": "any"}, "parameter"),
        ({"comparison": "=="}, "comparison"),
        ({"threshold": 101}, "threshold"),
        ({"threshold": True}, "threshold"),
        ({"window": 10}, "window"),
        ({"for_minutes": 61}, "for_minutes"),
        ({"severity": "page"}, "severity"),
        ({"channels": ["chn_0000000000000000"]}, "channels"),
        ({"cooldown_minutes": 4}, "cooldown_minutes"),
        ({"repeat_every_minutes": 59}, "repeat_every_minutes"),
        (
            {
                "quiet_hours": {
                    "utc_offset": "+15:00",
                    "start": "22:00",
                    "end": "07:00",
                    "days": ["mon"],
                }
            },
            "quiet_hours",
        ),
        (
            {
                "quiet_hours": {
                    "utc_offset": "+01:00",
                    "start": "24:00",
                    "end": "07:00",
                    "days": ["mon"],
                }
            },
            "quiet_hours",
        ),
        ({"enabled": "yes"}, "enabled"),
        ({"colour": "red"}, "colour"),
    ],
)
def test_each_field_is_refused_outside_its_values(change: dict[str, Any], field: str) -> None:
    with pytest.raises(RuleInvalidError) as caught:
        validate_rule({**RULE, **change})
    assert caught.value.field == field


def test_a_rule_takes_its_signals_parameter_and_window_and_snaps_a_latency() -> None:
    spec = validate_rule({**RULE, "signal": "latency_total_p99", "threshold": 1000})
    assert spec.threshold == 1024.0  # the nearest edge
    assert validate_rule({**RULE, "signal": "fallback_rate", "parameter": "any"}).parameter == "any"
    budget = validate_rule({**RULE, "signal": "budget_used", "window": None, "threshold": 90})
    assert (budget.window, budget.channels) == (None, (ORGANIZATION_DEFAULTS,))
    assert validate_rule({**RULE, "channels": []}).channels == ()
    with pytest.raises(RuleInvalidError, match="window"):
        validate_rule({**RULE, "signal": "budget_used"})  # judged over the period: no window
    assert EDGES[bucket(1000)] == 1024.0


async def test_create_change_enable_disable_delete_with_versions_and_the_audit() -> None:
    async with console() as c:
        alice = await c.login("alice")
        made = await c.call(alice, "POST", SHOP, RULE)
        assert made.status_code == 201, made.text
        rule = made.json()
        path = f"{SHOP}/{rule['id']}"
        assert (rule["version"], rule["default_key"], rule["threshold"]) == (1, None, 10.0)
        changed = await c.call(
            alice, "PUT", path, {"expected_version": 1, "rule": {**RULE, "threshold": 20}}
        )
        assert changed.json()["version"] == 2
        stale = await c.call(
            alice, "PUT", path, {"expected_version": 1, "rule": {**RULE, "threshold": 30}}
        )
        assert (stale.status_code, stale.json()["error"]["code"]) == (409, "rule_version_conflict")
        assert "the rule is at version 2, not 1" in stale.json()["error"]["message"]
        disabled = await c.call(alice, "POST", path + "/disable", {"expected_version": 2})
        assert (disabled.json()["enabled"], disabled.json()["version"]) == (False, 3)
        enabled = await c.call(alice, "POST", path + "/enable", {"expected_version": 3})
        assert (enabled.json()["enabled"], enabled.json()["version"]) == (True, 4)
        same = await c.call(alice, "POST", path + "/enable", {"expected_version": 4})
        assert same.json()["version"] == 4  # nothing changed, no version
        gone_stale = await c.call(alice, "DELETE", path, {"expected_version": 3})
        assert gone_stale.status_code == 409
        gone = await c.call(alice, "DELETE", path, {"expected_version": 4})
        assert gone.status_code == 200
        after = await c.call(alice, "GET", path)
        assert after.content == FORBIDDEN_BYTES
        listed = (await c.call(alice, "GET", SHOP)).json()
    assert listed == {"rules": [], "next": None}
    entries = await _audit(
        c.world,
        AuditAction.ALERT_RULE_CREATED, AuditAction.ALERT_RULE_CHANGED,
        AuditAction.ALERT_RULE_DISABLED, AuditAction.ALERT_RULE_ENABLED,
        AuditAction.ALERT_RULE_DELETED,
    )  # fmt: skip
    alice_id = (await c.world.accounts.store.find_user("alice")).id  # type: ignore[union-attr]
    assert [(e.event.action.value, e.event.target_id, e.event.details) for e in entries] == [
        ("alert_rule_created", rule["id"], {"application_id": "north-shop"}),
        ("alert_rule_changed", rule["id"],
         {"application_id": "north-shop", "rule_fields": ["threshold"]}),
        ("alert_rule_disabled", rule["id"],
         {"application_id": "north-shop", "rule_fields": ["enabled"]}),
        ("alert_rule_enabled", rule["id"],
         {"application_id": "north-shop", "rule_fields": ["enabled"]}),
        ("alert_rule_deleted", rule["id"], {"application_id": "north-shop"}),
    ]  # fmt: skip
    assert {e.event.actor_user_id for e in entries} == {alice_id}
    refused = await _audit(c.world, AuditAction.MANAGEMENT_REFUSED)
    assert [e.event.details["reason"] for e in refused] == ["rule_version_conflict"] * 2


async def test_an_application_has_at_most_fifty_rules() -> None:
    async with console() as c:
        alice = await c.login("alice")
        for i in range(MAX_RULES):
            made = await c.call(alice, "POST", SHOP, {**RULE, "name": f"Rule {i}"})
            assert made.status_code == 201, made.text
        over = await c.call(alice, "POST", SHOP, RULE)
        first = (await c.call(alice, "GET", SHOP + "?limit=20")).json()
        second = (await c.call(alice, "GET", SHOP + f"?limit=40&after={first['next']}")).json()
    assert (over.status_code, over.json()["error"]["code"]) == (409, "rule_limit")
    assert len(first["rules"]) == 20
    assert len(second["rules"]) == 30
    assert second["next"] is None
    assert len(_rows(c.world)) == MAX_RULES


async def test_a_rule_body_that_is_not_valid_is_refused_naming_its_field() -> None:
    async with console() as c:
        alice = await c.login("alice")
        refused = await c.call(alice, "POST", SHOP, {**RULE, "channels": ["somewhere"]})
        dry = await c.call(alice, "POST", SHOP + "/validate", {**RULE, "threshold": -1})
    assert refused.status_code == 422
    assert refused.json()["error"]["details"][0]["field"] == "channels"
    assert dry.status_code == 200
    assert (dry.json()["valid"], dry.json()["invalid_field"]) == (False, "threshold")
    assert _rows(c.world) == []


# --- default rules ------------------------------------------------------------------------------


def _default_ids(application_id: str) -> list[str]:
    return sorted(default_rule_id(application_id, key) for key in DEFAULT_RULES)


async def test_a_new_application_gets_the_default_rules_in_its_transaction() -> None:
    async with console() as c:
        alice = await c.login("alice")
        made = await c.call(alice, "POST", "/north/applications", {"name": "Fresh", "plan": "pro"})
        app_id = made.json()["application_id"]
        listed = (await c.call(alice, "GET", f"/north/applications/{app_id}/alert-rules")).json()
    assert sorted(r["id"] for r in listed["rules"]) == _default_ids(app_id)
    assert {r["default_key"] for r in listed["rules"]} == set(DEFAULT_RULES)
    assert all(r["channels"] == [ORGANIZATION_DEFAULTS] for r in listed["rules"])
    on = {r["default_key"] for r in listed["rules"] if r["enabled"]}
    assert on == {"budget_used", "budget_exhausted", "error_rate", "circuit_open", "key_expires"}
    assert not any(r["changed_from_default"] for r in listed["rules"])


async def test_restore_defaults_recreates_a_deleted_default_and_resets_an_edited_one() -> None:
    async with console() as c:
        alice = await c.login("alice")
        await c.restore(alice, "north-shop")
        error_rate = f"{SHOP}/{default_rule_id('north-shop', 'error_rate')}"
        budget = f"{SHOP}/{default_rule_id('north-shop', 'budget_used')}"
        weakened = await c.call(
            alice, "PUT", error_rate,
            {"expected_version": 1, "rule": {**RULE, "name": "Error rate above 5 percent",
                                             "threshold": 50}},
        )  # fmt: skip
        assert weakened.json()["changed_from_default"] is True
        await c.call(alice, "DELETE", budget, {"expected_version": 1})
        restored = await c.call(alice, "POST", SHOP + "/restore-defaults")
        again = await c.call(alice, "POST", SHOP + "/restore-defaults")
        back = (await c.call(alice, "GET", error_rate)).json()
        recreated = await c.call(alice, "GET", budget)
    assert restored.json() == {"written": 2}
    assert again.json() == {"written": 0}
    assert (back["threshold"], back["version"], back["changed_from_default"]) == (5.0, 3, False)
    assert recreated.status_code == 200
    assert len(await _audit(c.world, AuditAction.ALERT_RULES_RESTORED)) == 3


def test_migration_15_gives_every_existing_application_the_default_rules(tmp_path: Path) -> None:
    connection = open_database(tmp_path / "gateway.db")
    runner = MigrationRunner(connection, MIGRATIONS, SystemEpoch())
    runner.migrate(14)  # migration 4 made the default organization
    applications = SqliteApplicationStore(connection)
    for app_id in ("acme", "globex"):
        run_now(
            applications.add_application(
                application_from_template(
                    app_id, app_id.title(), load_plans(), "pro", organization_id="default",
                    created_at_ms=5.0,
                )
            )
        )  # fmt: skip
    assert runner.migrate(15) == (15,)
    rows = connection.execute("SELECT id, application_id, version FROM alert_rules").fetchall()
    assert sorted(r[0] for r in rows) == sorted(_default_ids("acme") + _default_ids("globex"))
    assert {r[2] for r in rows} == {1}
    import asyncio

    store = SqliteRuleStore(connection)
    found = asyncio.run(store.get_rule("acme", default_rule_id("acme", "error_rate")))
    assert found is not None
    assert found.spec == DEFAULT_RULES["error_rate"]
    assert runner.rollback(14) == (15,)
    assert connection.execute(
        "SELECT count(*) FROM sqlite_master WHERE name = 'alert_rules'"
    ).fetchone() == (0,)
    connection.close()


@pytest.mark.postgres
async def test_postgres_schema_4_gives_every_existing_application_the_default_rules() -> None:
    from gateway.alertrules import COLUMNS, rule_from_row
    from gateway.applications import application_from_template
    from gateway.organizations import new_organization
    from gateway.postgres import apply_schema, postgres_state, rollback_schema
    from gateway.routing import load_plans
    from postgres_helpers import pg_world

    async with pg_world() as world:
        [db] = world.instances
        assert await rollback_schema(db, 3) == (5, 4)
        state = postgres_state(db)
        await state.applications.add_organization(
            new_organization("north", "North", "business", created_at_ms=0)
        )
        await state.applications.add_application(
            application_from_template(
                "app1", "Shop", load_plans(), "pro", organization_id="north", created_at_ms=1.0
            )
        )
        assert await apply_schema(db) == 5
        rows = await world.rows(f"SELECT {COLUMNS} FROM alert_rules ORDER BY id")
    assert sorted(r[0] for r in rows) == _default_ids("app1")
    by_key = {rule_from_row(r).default_key: rule_from_row(r) for r in rows}
    for key, spec in DEFAULT_RULES.items():
        assert by_key[key].spec == spec, key


# --- the dry run and its preview ----------------------------------------------------------------


async def test_validate_writes_nothing_and_previews_the_last_hour() -> None:
    """An error rate over 5 minutes, above 5%: in the rollups, 30 requests a minute, of which
    a third failed in minutes 10 to 14 of the hour before. The preview has 60 complete minutes,
    and the condition held in the windows that overlap the failures; a quiet application's
    minutes are too few to judge; a budget rule has no preview."""
    async with console() as c:
        await c.world.accounts.clock.sleep_ms(90 * MINUTE_MS)  # time for an hour of minutes
        alice = await c.login("alice")
        now = int((c.world.accounts.epoch.epoch_ms() - 25_000) // MINUTE_MS) - 1
        first = now - 59
        rollups = c.app.state.alert_rollups
        for minute in range(first - 4, now + 1):
            failed = 10 if first + 10 <= minute <= first + 14 else 0
            rollups.absorb(
                {("north-shop", minute): {"requests": 30, "status:5xx": failed, "kind:chat": 30}}
            )
        audit = await c.world.accounts.audit.entries()
        rule = {**RULE, "threshold": 5, "window": 5}
        answer = await c.call(alice, "POST", SHOP + "/validate", rule)
        assert answer.status_code == 200, answer.text
        dry = answer.json()
        quiet = (
            await c.call(alice, "POST", "/north/applications/north-blog/alert-rules/validate", rule)
        ).json()
        budget = (
            await c.call(alice, "POST", SHOP + "/validate",
                         {**RULE, "signal": "budget_used", "window": None})
        ).json()  # fmt: skip
        assert _rows(c.world) == []
        assert await c.world.accounts.audit.entries() == audit
    assert dry["valid"] is True
    assert dry["rule"]["threshold"] == 5.0
    minutes = dry["preview"]["minutes"]
    assert [m["minute"] for m in minutes] == list(range(first, now + 1))
    held = [m["minute"] - first for m in minutes if m["met"]]
    assert held == list(range(10, 19))  # a window of 5 ending in minutes 10 to 18 has failures
    assert dry["preview"]["met"] == 9
    assert minutes[0]["value"] == 0.0
    assert {m["why_not"] for m in quiet["preview"]["minutes"]} == {"too_few"}
    assert budget["preview"] is None


# --- the leak scan ------------------------------------------------------------------------------


async def test_no_session_token_password_or_key_reaches_rows_logs_or_answers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sent: list[bytes] = []

    async def record(request: httpx.Request) -> None:
        sent.append(await request.aread() + repr(request.headers.raw).encode())

    answers: list[httpx.Response] = []
    with caplog.at_level(logging.DEBUG):
        async with console() as c:
            c.client.event_hooks["request"].append(record)
            session = await c.login("alice")
            await c.restore(session, "north-shop")
            rule = default_rule_id("north-shop", "error_rate")
            for endpoint in sorted(ENDPOINTS, key=lambda e: e.method == "DELETE"):
                path = _path(endpoint, "north", "north-shop", rule)
                version = await _current(c, session, endpoint, path)
                answers.append(
                    await c.call(session, endpoint.method, path, _body(endpoint, version))
                )
            rows = repr(_rows(c.world))
            audit = repr(c.world.accounts.connection.execute("SELECT * FROM audit_log").fetchall())
    assert all(a.status_code in (200, 201) for a in answers), [a.text for a in answers]
    logged = "\n".join(r.getMessage() for r in caplog.records)
    answered = "\n".join(a.text + repr(sorted(a.headers.items())) for a in answers)
    planted = {
        "session": session[0],
        "csrf token": session[1],
        "password": PASSWORD,
        "api key": c.world.api_keys["north-shop"],
    }
    for name in ("session", "csrf token", "password"):
        assert any(planted[name].encode() in body for body in sent), name  # the controls
    for name, value in planted.items():
        for where, text in (("rows", rows), ("audit", audit), ("logs", logged),
                            ("answers", answered)):  # fmt: skip
            assert value not in text, (name, where)


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_the_store_writes_a_rule_only_at_the_version_named(kind: str, tmp_path: Path) -> None:
    """The second layer, under the service's check: the store's own conditional write."""
    from gateway.alertrules import (
        InMemoryRuleStore,
        RuleStore,
        RuleVersionConflictError,
        default_rules,
    )

    store: RuleStore
    if kind == "memory":
        store = InMemoryRuleStore()
    else:
        connection = open_database(tmp_path / "gateway.db")
        MigrationRunner(connection, MIGRATIONS, SystemEpoch()).migrate()
        store = SqliteRuleStore(connection)
    rule = default_rules("app1", 1.0)[0]
    await store.insert_rule(rule)
    with pytest.raises(RuleVersionConflictError):
        await store.insert_rule(rule)
    newer = dataclasses.replace(rule, version=2)
    with pytest.raises(RuleVersionConflictError):
        await store.put_rule(newer, 2)
    await store.put_rule(newer, 1)
    with pytest.raises(RuleVersionConflictError):
        await store.put_rule(dataclasses.replace(rule, version=3), 1)
    with pytest.raises(RuleVersionConflictError):
        await store.delete_rule("app1", rule.id, 1)
    with pytest.raises(RuleVersionConflictError):
        await store.delete_rule("app2", rule.id, 2)  # another application's
    await store.delete_rule("app1", rule.id, 2)
    assert await store.get_rule("app1", rule.id) is None


@pytest.mark.postgres
async def test_postgres_holds_the_cap_of_fifty_across_two_instances() -> None:
    """The cap is a count inside the write's transaction (the global write lock on Postgres):
    two instances creating the last rules at once store exactly fifty, the rest refused."""
    import asyncio
    import random as _random

    from gateway.alertrules import RuleService
    from gateway.audit import AuditLog
    from gateway.management import ManagementRefusal
    from gateway.postgres import PostgresAuditStore, PostgresRuleStore
    from postgres_helpers import pg_world

    async with pg_world(instances=2) as world:
        services = [
            RuleService(
                PostgresRuleStore(db), AuditLog(PostgresAuditStore(db)), SystemEpoch(), None,
                _random.Random(n).randbytes,
            )
            for n, db in enumerate(world.instances)
        ]  # fmt: skip
        for i in range(MAX_RULES - 2):
            await services[i % 2].create("app1", {**RULE, "name": f"Rule {i}"}, None)

        async def one(n: int) -> str:
            try:
                await services[n % 2].create("app1", {**RULE, "name": f"Last {n}"}, None)
            except ManagementRefusal as exc:
                return exc.reason.value
            return "created"

        outcomes = await asyncio.gather(*(one(n) for n in range(6)))
        count = await world.rows("SELECT count(*) FROM alert_rules WHERE application_id = 'app1'")
    assert sorted(outcomes) == ["created"] * 2 + ["rule_limit"] * 4
    assert count == [(MAX_RULES,)]
