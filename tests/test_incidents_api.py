"""The incident endpoints (docs/45): every route and its action as documented, nothing across
organizations or applications and missing equal to forbidden on every endpoint, who may read
and acknowledge, the audit of an acknowledgement, and the leak scan of an evaluation run through
firing with a planted prompt, key, provider key and username.

The world of ``test_management_api``: north (alice org_admin, vera viewer, nina app_admin of
north-shop, bob no role) and south (sid org_admin); root is platform_admin. Test values, not
secrets."""

from __future__ import annotations

import hashlib
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
from gateway.alertrecorder import MINUTE_MS
from gateway.alertrules import AlertRule, SqliteRuleStore, validate_rule
from gateway.apikeys import ApplicationAccess
from gateway.app import create_app, declaration_of
from gateway.audit import AuditAction
from gateway.auth import AccountAccess
from gateway.continuous import build_continuous_registry
from gateway.evaluator import Series, decide
from gateway.incidents import Fence, SqliteIncidentStore
from gateway.localonly import LocalOnlyPolicy
from gateway.permissions import PERMISSIONS
from gateway.signals import SignalValue
from test_management_api import World, make_world

REPO = Path(__file__).resolve().parent.parent
FORBIDDEN_BYTES = b'{"error":{"code":"forbidden","message":"Not permitted, or not found."}}'
Session = tuple[str, str]
RULE = {
    "name": "Errors",
    "signal": "error_rate",
    "comparison": ">",
    "threshold": 5,
    "window": 5,
    "for_minutes": 0,
    "severity": "critical",
}


@dataclass(frozen=True)
class Endpoint:
    method: str
    path: str
    action: str
    success: int


def documented_endpoints() -> list[Endpoint]:
    text = (REPO / "docs" / "45-alert-evaluation.md").read_text(encoding="utf-8")
    pattern = re.compile(r"^\| `(GET|POST)` \| `([^`]+)` \| `([a-z_.]+)` \| (\d{3}) \|$")
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
    incidents: SqliteIncidentStore
    ids: dict[str, str]

    async def login(self, username: str) -> Session:
        response = await self.client.post(
            "/v1/auth/login", json={"username": username, "password": PASSWORD}
        )
        self.client.cookies.clear()
        assert response.status_code == 200, response.text
        sid = response.headers["set-cookie"].split(";")[0].split("=", 1)[1]
        return sid, response.json()["csrf_token"]

    async def call(self, session: Session, method: str, path: str) -> httpx.Response:
        headers = {"Cookie": f"gw_session={session[0]}", "X-CSRF-Token": session[1]}
        url = "/v1/admin/orgs" + path
        if method == "GET":
            return await self.client.get(url, headers=headers)
        return await self.client.post(url, json={}, headers=headers)


async def _fire(incidents: SqliteIncidentStore, application_id: str, organization_id: str) -> str:
    """A firing incident of ``application_id``, made as the evaluator makes it."""
    fence = Fence("test", (await incidents.attempt_lease("test")).epoch)
    rule = AlertRule(
        "rul_" + hashlib.sha256(b"rule" + application_id.encode()).hexdigest()[:16],
        application_id, validate_rule(RULE), 1, None, 0.0, 0.0,
    )  # fmt: skip
    counter = iter(range(1, 100))

    def new_id(prefix: str) -> str:
        digest = hashlib.sha256(application_id.encode()).hexdigest()[:12]
        return prefix + digest + f"{next(counter):04x}"

    change = decide(Series(rule, organization_id, "", SignalValue(9.0)), None, None, 1.0, new_id)
    assert change is not None
    assert await incidents.apply(fence, change)
    return change.incident.id


@asynccontextmanager
async def console() -> AsyncIterator[Console]:
    world = make_world()
    accounts = world.accounts
    incidents = SqliteIncidentStore(accounts.connection, accounts.epoch)
    registry = build_continuous_registry(accounts.clock, seed=7)
    app = create_app(
        registry,
        accounts.clock,
        applications=ApplicationAccess(world.keys, world.templates),
        accounts=AccountAccess(accounts.service),
        local_only=LocalOnlyPolicy(),
        epoch=accounts.epoch,
        rules=SqliteRuleStore(accounts.connection),
        incidents=incidents,
    )
    ids = {
        app_id: await _fire(incidents, app_id, org)
        for app_id, org in (
            ("north-shop", "north"),
            ("north-blog", "north"),
            ("south-shop", "south"),
        )
    }
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield Console(client, world, app, incidents, ids)
    await registry.aclose()


def _path(endpoint: Endpoint, org: str, app: str, incident: str) -> str:
    return endpoint.path.replace("{org}", org).replace("{app}", app).replace("{incident}", incident)


def _raw(response: httpx.Response) -> tuple[int, list[tuple[str, str]], bytes]:
    return response.status_code, sorted(response.headers.multi_items()), response.content


def _rows(c: Console) -> list[tuple[Any, ...]]:
    connection = c.world.accounts.connection
    return connection.execute("SELECT * FROM incidents ORDER BY id").fetchall()


async def test_each_incident_route_is_documented_and_declares_its_action() -> None:
    assert len(ENDPOINTS) == 4
    async with console() as c:
        routes = set()
        for route in c.app.routes:
            # The open counts are note 51's (test_console_reads), checked against its table.
            if (
                isinstance(route, APIRoute)
                and "/incidents" in route.path
                and not route.path.endswith("/incidents/counts")
            ):
                [declaration] = declaration_of(route)
                assert declaration.kind == "requires", route.path
                for method in route.methods or ():
                    routes.add(
                        (method, route.path.removeprefix("/v1/admin/orgs"), declaration.action)
                    )
    assert routes == {(e.method, e.path, e.action) for e in ENDPOINTS}
    for endpoint in ENDPOINTS:
        assert PERMISSIONS[endpoint.action].step_up is False, endpoint


async def test_nothing_crosses_organizations_or_applications_and_missing_looks_forbidden() -> None:
    """alice, org_admin of north: south's own path, an organization that does not exist,
    south's application through north, an application that does not exist, north-blog's
    incident under north-shop, south's incident under north-shop, an incident that does not
    exist and one not in an incident id's form: the same 403, byte for byte; nothing changed or
    audited."""
    async with console() as c:
        alice = await c.login("alice")
        before_rows, before_audit = _rows(c), await c.world.accounts.audit.entries()
        shop, blog, south = (c.ids[a] for a in ("north-shop", "north-blog", "south-shop"))
        for endpoint in ENDPOINTS:
            attempts = {
                "south's own path": _path(endpoint, "south", "south-shop", south),
                "no such organization": _path(endpoint, "nowhere", "north-shop", shop),
            }
            if "{app}" in endpoint.path:
                attempts |= {
                    "south's application via north": _path(endpoint, "north", "south-shop", south),
                    "no such application": _path(endpoint, "north", "app_00000000", shop),
                }
            if "{incident}" in endpoint.path:
                attempts |= {
                    "north-blog's incident under north-shop": _path(
                        endpoint, "north", "north-shop", blog
                    ),
                    "south's incident under north-shop": _path(
                        endpoint, "north", "north-shop", south
                    ),
                    "no such incident": _path(endpoint, "north", "north-shop", "inc_" + "0" * 16),
                    "not an incident id": _path(endpoint, "north", "north-shop", "inc_XYZ"),
                }
            answers = {
                name: _raw(await c.call(alice, endpoint.method, path))
                for name, path in attempts.items()
            }
            first = next(iter(answers.values()))
            assert first[0] == 403, (endpoint, answers)
            assert first[2] == FORBIDDEN_BYTES
            for name, answer in answers.items():
                assert answer == first, (endpoint.path, name)
        assert _rows(c) == before_rows
        assert await c.world.accounts.audit.entries() == before_audit


async def test_south_sees_and_acknowledges_its_own_and_lists_only_its_own() -> None:
    async with console() as c:
        sid = await c.login("sid")
        south = c.ids["south-shop"]
        for endpoint in ENDPOINTS:
            answer = await c.call(
                sid, endpoint.method, _path(endpoint, "south", "south-shop", south)
            )
            assert answer.status_code == endpoint.success, (endpoint, answer.text)
        listed = (await c.call(sid, "GET", "/south/incidents")).json()["incidents"]
    assert [i["id"] for i in listed] == [south]


async def test_viewers_read_and_never_acknowledge_and_an_app_admin_keeps_to_its_own() -> None:
    async with console() as c:
        vera, nina = await c.login("vera"), await c.login("nina")
        shop, blog = c.ids["north-shop"], c.ids["north-blog"]
        before = _rows(c)
        for endpoint in ENDPOINTS:
            answer = await c.call(
                vera, endpoint.method, _path(endpoint, "north", "north-blog", blog)
            )
            if endpoint.action == "incidents.acknowledge":
                assert answer.content == FORBIDDEN_BYTES
            else:
                assert answer.status_code == 200, (endpoint, answer.text)
        assert _rows(c) == before
        for endpoint in ENDPOINTS:
            if "{app}" not in endpoint.path:
                answer = await c.call(nina, endpoint.method, _path(endpoint, "north", "x", "x"))
            else:
                answer = await c.call(
                    nina, endpoint.method, _path(endpoint, "north", "north-blog", blog)
                )
            assert answer.content == FORBIDDEN_BYTES, endpoint
        mine = await c.call(
            nina, "POST", f"/north/applications/north-shop/incidents/{shop}/acknowledge"
        )
        assert mine.status_code == 200, mine.text
        for name in ("bob", "root"):
            session = await c.login(name)
            for endpoint in ENDPOINTS:
                answer = await c.call(
                    session, endpoint.method, _path(endpoint, "north", "north-shop", shop)
                )
                assert answer.content == FORBIDDEN_BYTES, (name, endpoint)


async def test_acknowledge_once_is_audited_and_the_second_is_a_conflict() -> None:
    async with console() as c:
        alice = await c.login("alice")
        shop = c.ids["north-shop"]
        path = f"/north/applications/north-shop/incidents/{shop}"
        done = await c.call(alice, "POST", path + "/acknowledge")
        again = await c.call(alice, "POST", path + "/acknowledge")
        view = (await c.call(alice, "GET", path)).json()
        listed = (await c.call(alice, "GET", "/north/incidents?state=acknowledged")).json()
        firing = (await c.call(alice, "GET", "/north/incidents?state=firing")).json()
        bad = await c.call(alice, "GET", "/north/incidents?state=burning")
        by_app = (await c.call(alice, "GET", "/north/incidents?application=north-blog")).json()
    alice_id = (await c.world.accounts.store.find_user("alice")).id  # type: ignore[union-attr]
    assert (done.json()["state"], done.json()["acknowledged_by"]) == ("acknowledged", alice_id)
    assert (again.status_code, again.json()["error"]["code"]) == (409, "incident_not_open")
    assert [n["kind"] for n in view["notifications"]] == ["firing"]
    assert [i["id"] for i in listed["incidents"]] == [shop]
    assert [i["id"] for i in firing["incidents"]] == [c.ids["north-blog"]]
    assert bad.status_code == 422
    assert [i["application_id"] for i in by_app["incidents"]] == ["north-blog"]
    [entry] = [
        e for e in await c.world.accounts.audit.entries()
        if e.event.action is AuditAction.INCIDENT_ACKNOWLEDGED
    ]  # fmt: skip
    assert (entry.event.target_id, entry.event.actor_user_id) == (shop, alice_id)
    assert entry.event.details == {"application_id": "north-shop"}
    refused = [
        e for e in await c.world.accounts.audit.entries()
        if e.event.action is AuditAction.MANAGEMENT_REFUSED
    ]  # fmt: skip
    assert [e.event.details["reason"] for e in refused] == ["incident_not_open"]


# --- the leak scan --------------------------------------------------------------------------------


async def test_an_evaluation_through_firing_leaks_no_prompt_key_or_username(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Requests with a planted prompt and north-shop's key run until its error rate fires
    (through the gateway's recorder and the evaluator itself), an org_admin reads and
    acknowledges the incident. Every incident and notification row, every audit row, every log
    record, every event and every answer is scanned for the prompt, the API key and the
    username; the controls find each where it was used."""
    planted_prompt = "planted prompt: the heron code is 4417"
    with caplog.at_level(logging.DEBUG):
        async with console() as c:
            sent: list[bytes] = []

            async def record(request: httpx.Request) -> None:
                sent.append(await request.aread() + repr(request.headers.raw).encode())

            c.client.event_hooks["request"].append(record)
            alice = await c.login("alice")
            rules = SqliteRuleStore(c.world.accounts.connection)
            await rules.insert_rule(
                AlertRule("rul_00000000000000ee", "north-shop", validate_rule(RULE), 1, None,
                          0.0, 0.0)
            )  # fmt: skip
            # The minutes the evaluator will read: half of the requests failed.
            minute = int(c.world.accounts.epoch.epoch_ms() // MINUTE_MS)
            for m in range(minute - 10, minute + 5):
                c.app.state.alert_rollups.absorb(
                    {("north-shop", m): {"requests": 40, "status:5xx": 20}}
                )
            answers = [
                await c.client.post(
                    "/v1/generate",
                    json={"model": "economy", "prompt": planted_prompt},
                    headers={"Authorization": f"Bearer {c.world.api_keys['north-shop']}"},
                )
            ]
            await c.world.accounts.clock.sleep_ms(2 * MINUTE_MS)
            assert await c.app.state.alert_evaluator.step()
            listed = await c.call(alice, "GET", "/north/applications/north-shop/incidents")
            answers.append(listed)
            fired = [
                i for i in listed.json()["incidents"] if i["rule_id"] == "rul_00000000000000ee"
            ]
            assert fired, listed.text
            path = f"/north/applications/north-shop/incidents/{fired[0]['id']}"
            answers += [
                await c.call(alice, "GET", path),
                await c.call(alice, "POST", path + "/acknowledge"),
                await c.call(alice, "GET", "/north/incidents"),
                await c.client.get("/healthz"),
                await c.client.get("/v1/monitor/snapshot?events=100"),
            ]
            connection = c.world.accounts.connection
            stored = repr(
                connection.execute("SELECT * FROM incidents").fetchall()
                + connection.execute("SELECT * FROM incident_notifications").fetchall()
                + connection.execute("SELECT * FROM alert_lease").fetchall()
            )
            audit = repr(connection.execute("SELECT * FROM audit_log").fetchall())
    logged = "\n".join(r.getMessage() for r in caplog.records)
    answered = "\n".join(a.text for a in answers)
    planted = {
        "prompt": planted_prompt,
        "api key": c.world.api_keys["north-shop"],
        "username": "alice",
    }
    assert any(planted_prompt.encode() in b for b in sent)  # the controls
    assert any(planted["api key"].encode() in b for b in sent)
    assert any(b"alice" in b for b in sent)
    for name, value in planted.items():
        for where, text in (("rows", stored), ("audit", audit), ("logs", logged),
                            ("answers", answered)):  # fmt: skip
            assert value not in text, (name, where)
