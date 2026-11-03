"""The settings endpoints (docs/40): every route and its action as documented, nothing across
organizations and missing equal to forbidden on every endpoint, who may read and write, versions,
revert, the dry run that writes nothing, the audit of every change, and the leak scan.

The world of ``test_management_api``: north (alice and carol org_admins, vera viewer, nina
app_admin of north-shop, bob no role) and south (sid org_admin, sam no role), each with an
application named Shop, north also Blog; root is platform_admin. Test values, not secrets."""

from __future__ import annotations

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
from gateway.apikeys import ApplicationAccess
from gateway.app import create_app, declaration_of
from gateway.audit import AuditAction, AuditEntry
from gateway.auth import AccountAccess
from gateway.continuous import build_continuous_registry
from gateway.localonly import LocalOnlyPolicy
from gateway.management import (
    Stores,
    resolve_application_settings_version,
    resolve_organization,
    resolve_organization_settings_version,
)
from gateway.permissions import PERMISSIONS
from gateway.settings import Scope, SqliteSettingsStore, canonical
from test_management_api import FORBIDDEN, World, make_world

REPO = Path(__file__).resolve().parent.parent
FORBIDDEN_BYTES = b'{"error":{"code":"forbidden","message":"Not permitted, or not found."}}'
Session = tuple[str, str]


@dataclass(frozen=True)
class Endpoint:
    method: str
    path: str  # under /v1/admin/orgs, with {org}
    action: str
    escalates: str | None  # the action a change that loosens egress takes (docs/42)
    success: int


def documented_endpoints() -> list[Endpoint]:
    text = (REPO / "docs" / "42-hosted-settings-and-presets.md").read_text(encoding="utf-8")
    pattern = re.compile(
        r"^\| `(GET|POST|PUT)` \| `([^`]+)` \| `([a-z_.]+)` \| (?:`([a-z_.]+)`)? ?\| (\d{3}) \|$"
    )
    found = []
    for line in text.splitlines():
        match = pattern.match(line.strip())
        if match:
            method, path, action, escalates, success = match.groups()
            found.append(Endpoint(method, "/{org}" + path, action, escalates, int(success)))
    return found


ENDPOINTS = documented_endpoints()
WRITES = [e for e in ENDPOINTS if e.method != "GET"]
BODIES: dict[str, dict[str, Any]] = {
    "PUT": {"expected_version": 0, "set": {"max_tokens": 32}},
    "validate": {"expected_version": 0, "set": {"max_tokens": 32}},
    "revert": {"expected_version": 2},
    "preset": {"expected_version": 2},
}


def _body(endpoint: Endpoint) -> dict[str, Any] | None:
    if endpoint.method == "PUT":
        return BODIES["PUT"]
    if endpoint.path.endswith("/validate"):
        return BODIES["validate"]
    if endpoint.path.endswith("/revert"):
        return BODIES["revert"]
    if endpoint.path.endswith("/presets/{name}"):
        return BODIES["preset"]
    return None


@dataclass
class Admin:
    client: httpx.AsyncClient
    world: World
    store: SqliteSettingsStore

    async def login(self, username: str) -> Session:
        response = await self.client.post(
            "/v1/auth/login", json={"username": username, "password": PASSWORD}
        )
        self.client.cookies.clear()
        assert response.status_code == 200, response.text
        sid = response.headers["set-cookie"].split(";")[0].split("=", 1)[1]
        return sid, response.json()["csrf_token"]

    async def call(
        self, session: Session, method: str, path: str, body: dict[str, Any] | None = None
    ) -> httpx.Response:
        headers = {"Cookie": f"gw_session={session[0]}", "X-CSRF-Token": session[1]}
        url = "/v1/admin/orgs" + path
        if method == "GET":
            return await self.client.get(url, headers=headers)
        return await self.client.request(method, url, json=body or {}, headers=headers)


@asynccontextmanager
async def settings_console(world: World) -> AsyncIterator[Admin]:
    accounts = world.accounts
    store = SqliteSettingsStore(accounts.connection)
    registry = build_continuous_registry(accounts.clock, seed=7)
    app = create_app(
        registry,
        accounts.clock,
        applications=ApplicationAccess(world.keys, world.templates),
        accounts=AccountAccess(accounts.service),
        local_only=LocalOnlyPolicy(),
        epoch=accounts.epoch,
        settings=store,
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield Admin(client, world, store)
    await registry.aclose()


@pytest.fixture
async def world() -> World:
    return make_world()


async def _versions(admin: Admin) -> None:
    """Two versions for north, north-shop and north-blog and south and south-shop, through the
    store as the CLI writes them: every version path has something to find."""
    for scope, scope_id in (
        (Scope.ORGANIZATION, "north"),
        (Scope.ORGANIZATION, "south"),
        (Scope.APPLICATION, "north-shop"),
        (Scope.APPLICATION, "north-blog"),
        (Scope.APPLICATION, "south-shop"),
    ):
        for version, value in ((0, 64), (1, 48)):
            async with admin.store.write_transaction():
                await admin.store.write(
                    scope,
                    scope_id,
                    expected_version=version,
                    document=canonical({"max_tokens": value}),
                    changed=("max_tokens",),
                    changed_by_user_id=None,
                    via="cli",
                    at_ms=1.0,
                )


def _path(endpoint: Endpoint, org: str, app: str, n: str = "1", name: str = "private") -> str:
    path = endpoint.path.replace("{org}", org).replace("{app}", app)
    return path.replace("{n}", n).replace("{name}", name)


def _raw(response: httpx.Response) -> tuple[int, list[tuple[str, str]], bytes]:
    return response.status_code, sorted(response.headers.multi_items()), response.content


async def _rows(world: World) -> list[tuple[Any, ...]]:
    connection = world.accounts.connection
    return (
        connection.execute(
            "SELECT * FROM settings_current ORDER BY scope_type, scope_id"
        ).fetchall()
        + connection.execute(
            "SELECT * FROM settings_versions ORDER BY scope_type, scope_id, version"
        ).fetchall()
    )


async def _audit(world: World, action: AuditAction) -> list[AuditEntry]:
    return [e for e in await world.accounts.audit.entries() if e.event.action is action]


# --- the routes, as documented ------------------------------------------------------------------


async def test_each_settings_route_is_documented_and_declares_its_action(world: World) -> None:
    """Every settings route, its method, the action it declares and the action it escalates to,
    equal note 42's table. A route declared with another action, not through ``requires``, or
    writing without declaring its escalation, fails here."""
    assert len(ENDPOINTS) == 14
    async with settings_console(world) as admin:
        app = admin.client._transport.app  # type: ignore[attr-defined]
        routes = set()
        for route in app.routes:
            if isinstance(route, APIRoute) and "/settings" in route.path:
                declarations = declaration_of(route)
                [declaration] = [d for d in declarations if d.kind == "requires"]
                escalations = [d.action for d in declarations if d.kind == "escalates"]
                assert len(declarations) == 1 + len(escalations) <= 2, route.path
                for method in route.methods or ():
                    routes.add(
                        (
                            method,
                            route.path.removeprefix("/v1/admin/orgs"),
                            declaration.action,
                            escalations[0] if escalations else None,
                        )
                    )
    assert routes == {(e.method, e.path, e.action, e.escalates) for e in ENDPOINTS}
    for endpoint in ENDPOINTS:
        # No route's own action asks for step-up: only the escalation does, when it is taken.
        assert PERMISSIONS[endpoint.action].step_up is False, endpoint
        if endpoint.escalates is not None:
            assert PERMISSIONS[endpoint.escalates].consume, endpoint
    # Every write that can change a stored value escalates; a dry run and a read never do.
    for endpoint in ENDPOINTS:
        changes = endpoint.method == "PUT" or endpoint.path.endswith(("/revert", "/{name}"))
        assert (endpoint.escalates is not None) is changes, endpoint


# --- cross-organization, and missing equals forbidden -------------------------------------------


async def test_nothing_crosses_organizations_and_missing_looks_forbidden(world: World) -> None:
    """alice, org_admin of north, on every settings endpoint: south's path, an organization that
    does not exist, south's application through north, an application that does not exist, and
    for version paths a version of another scope, one that does not exist, and one that is not
    a number: the same 403, byte for byte; nothing written, nothing audited."""
    async with settings_console(world) as admin:
        await _versions(admin)
        alice = await admin.login("alice")
        before_rows = await _rows(world)
        before_audit = await world.accounts.audit.entries()
        for endpoint in ENDPOINTS:
            attempts = {
                "south's own path": _path(endpoint, "south", "south-shop"),
                "no such organization": _path(endpoint, "nowhere", "north-shop"),
            }
            if "{app}" in endpoint.path:
                attempts["south's application via north"] = _path(endpoint, "north", "south-shop")
                attempts["no such application"] = _path(endpoint, "north", "app_00000000")
            if "{name}" in endpoint.path:
                attempts["no such preset"] = _path(endpoint, "north", "north-shop", name="nope")
            if "{n}" in endpoint.path:
                attempts["no such version"] = _path(endpoint, "north", "north-shop", "9")
                attempts["not a number"] = _path(endpoint, "north", "north-shop", "1x")
                attempts["zero"] = _path(endpoint, "north", "north-shop", "0")
                attempts["too long"] = _path(endpoint, "north", "north-shop", "1" * 10)
            answers = {
                name: _raw(await admin.call(alice, endpoint.method, path, _body(endpoint)))
                for name, path in attempts.items()
            }
            first = next(iter(answers.values()))
            assert first[0] == 403, (endpoint, answers)
            assert first[2] == FORBIDDEN_BYTES
            for name, answer in answers.items():
                assert answer == first, (endpoint.path, name)
        assert await _rows(world) == before_rows
        assert await world.accounts.audit.entries() == before_audit


async def test_a_version_of_another_application_of_the_organization_is_missing(
    world: World,
) -> None:
    """north-blog's version 3 does not exist under north-shop's path, even though north-shop
    has versions: each scope's versions are its own."""
    async with settings_console(world) as admin:
        await _versions(admin)
        async with admin.store.write_transaction():
            await admin.store.write(
                Scope.APPLICATION, "north-blog", expected_version=2, document="{}",
                changed=("max_tokens",), changed_by_user_id=None, via="cli", at_ms=2.0,
            )  # fmt: skip
        alice = await admin.login("alice")
        blog = await admin.call(alice, "GET", "/north/applications/north-blog/settings/versions/3")
        shop = await admin.call(alice, "GET", "/north/applications/north-shop/settings/versions/3")
        assert blog.status_code == 200
        assert shop.content == FORBIDDEN_BYTES


async def test_south_can_do_all_of_it_to_its_own(world: World) -> None:
    """The control for the cross-organization test: the same paths work for south's org_admin."""
    async with settings_console(world) as admin:
        await _versions(admin)
        sid = await admin.login("sid")
        for endpoint in ENDPOINTS:
            body = _body(endpoint)
            if endpoint.method == "PUT":
                body = {"expected_version": 2, "set": {"max_tokens": 16}}
            if endpoint.path.endswith("/validate"):
                body = {"expected_version": 3, "set": {"max_tokens": 8}}
            if endpoint.path.endswith("/revert"):
                body = {"expected_version": 3}
            if endpoint.path.endswith("/{name}"):
                body = {"expected_version": 4}
            answer = await admin.call(
                sid, endpoint.method, _path(endpoint, "south", "south-shop"), body
            )
            assert answer.status_code == endpoint.success, (endpoint, answer.text)


async def test_the_resolvers_on_their_own(world: World) -> None:
    async with settings_console(world) as admin:
        await _versions(admin)
        stores = Stores(world.accounts.organizations, world.accounts.store)
        org, app = resolve_organization_settings_version, resolve_application_settings_version
        store = admin.store
        assert await org(stores, store, "north", "1") is not None
        assert await org(stores, store, "north", "3") is None
        assert await org(stores, store, "nowhere", "1") is None
        assert await org(stores, store, "north", "01") is None
        assert await app(stores, store, "north", "north-shop", "2") is not None
        assert await app(stores, store, "north", "south-shop", "1") is None  # south's
        assert await app(stores, store, "south", "north-shop", "1") is None
        assert await app(stores, store, "north", "north-shop", "3") is None


# --- who may ----------------------------------------------------------------------------------


async def test_viewers_read_every_settings_endpoint_and_change_none(world: World) -> None:
    async with settings_console(world) as admin:
        await _versions(admin)
        vera = await admin.login("vera")
        before = await _rows(world)
        for endpoint in ENDPOINTS:
            answer = await admin.call(
                vera, endpoint.method, _path(endpoint, "north", "north-blog"), _body(endpoint)
            )
            if endpoint.method == "GET":
                assert answer.status_code == 200, endpoint
            else:
                assert (answer.status_code, answer.json()) == (403, FORBIDDEN), endpoint
        assert await _rows(world) == before


async def test_an_app_admin_keeps_to_its_application_and_never_the_organizations_caps(
    world: World,
) -> None:
    async with settings_console(world) as admin:
        await _versions(admin)
        nina = await admin.login("nina")
        mine = await admin.call(
            nina,
            "PUT",
            "/north/applications/north-shop/settings",
            {"expected_version": 2, "set": {"max_tokens": 16}},
        )
        assert mine.status_code == 200, mine.text
        for endpoint in ENDPOINTS:
            if "{app}" in endpoint.path:
                path = _path(endpoint, "north", "north-blog")
            else:
                path = _path(endpoint, "north", "x")
            answer = await admin.call(nina, endpoint.method, path, _body(endpoint))
            assert (answer.status_code, answer.json()) == (403, FORBIDDEN), endpoint


async def test_only_an_org_admin_writes_the_organizations_caps(world: World) -> None:
    async with settings_console(world) as admin:
        body = {"expected_version": 0, "set": {"requests_per_minute": 100}}
        for name in ("vera", "nina", "bob", "root", "sid"):
            session = await admin.login(name)
            for path in ("/north/settings", "/north/settings/validate"):
                method = "PUT" if path.endswith("settings") else "POST"
                answer = await admin.call(session, method, path, body)
                assert (answer.status_code, answer.json()) == (403, FORBIDDEN), (name, path)
        alice = await admin.login("alice")
        done = await admin.call(alice, "PUT", "/north/settings", body)
        assert done.status_code == 200, done.text


async def test_no_role_and_platform_admin_get_forbidden_everywhere(world: World) -> None:
    async with settings_console(world) as admin:
        await _versions(admin)
        for name in ("bob", "root"):
            session = await admin.login(name)
            for endpoint in ENDPOINTS:
                path = _path(endpoint, "north", "north-shop")
                answer = await admin.call(session, endpoint.method, path, _body(endpoint))
                assert (answer.status_code, answer.json()) == (403, FORBIDDEN), (name, endpoint)


async def test_no_settings_write_asks_for_step_up(world: World) -> None:
    """No setting of S1 to S11 changes where prompts go: alice writes without a step-up window
    and none is used."""
    async with settings_console(world) as admin:
        alice = await admin.login("alice")
        for method, path, body in (
            ("PUT", "/north/settings", {"expected_version": 0, "set": {"max_tokens": 64}}),
            ("POST", "/north/settings/revert", {"expected_version": 1}),
            (
                "PUT",
                "/north/applications/north-shop/settings",
                {"expected_version": 0, "set": {"allowed_tiers": ["economy"]}},
            ),
        ):
            answer = await admin.call(alice, method, path, body)
            assert answer.status_code in (200, 409), (path, answer.text)
        assert not await _audit(world, AuditAction.STEP_UP_REQUIRED)


# --- what the endpoints do ----------------------------------------------------------------------


async def test_set_view_revert_and_the_audit_of_every_change(world: World) -> None:
    async with settings_console(world) as admin:
        alice = await admin.login("alice")
        app = "/north/applications/north-shop/settings"
        org_cap = await admin.call(
            alice, "PUT", "/north/settings", {"expected_version": 0, "set": {"max_tokens": 40}}
        )
        assert org_cap.json() == {
            "version": 1,
            "changed": ["max_tokens"],
            "settings": {"max_tokens": 40},
        }
        first = await admin.call(
            alice,
            "PUT",
            app,
            {
                "expected_version": 0,
                "set": {"max_tokens": 30, "model_aliases": {"fast": "standard"}},
            },
        )
        assert first.status_code == 200, first.text
        second = await admin.call(
            alice,
            "PUT",
            app,
            {"expected_version": 1, "set": {"max_tokens": 20}, "unset": ["model_aliases"]},
        )
        assert second.json()["changed"] == ["model_aliases", "max_tokens"]
        view = (await admin.call(alice, "GET", app)).json()
        assert view["version"] == 2
        assert view["settings"] == {"max_tokens": 20}
        assert view["effective"]["max_tokens"] == {
            "value": 20,
            "source": "application",
            "capped_by": None,
            "stored": 20,
            "caps": {"organization": 40},
        }
        assert view["effective"]["allowed_tiers"]["value"] == ["economy", "standard"]
        assert view["effective"]["allowed_tiers"]["caps"] == {
            "platform": ["economy", "standard", "premium"],
            "plan": ["economy", "standard"],
        }
        reverted = await admin.call(alice, "POST", app + "/revert", {"expected_version": 2})
        assert reverted.json()["settings"] == {
            "max_tokens": 30,
            "model_aliases": {"fast": "standard"},
        }
        assert (await admin.call(alice, "GET", app)).json()["ignored_aliases"] == []
        await admin.call(
            alice,
            "PUT",
            "/north/settings",
            {"expected_version": 1, "set": {"allowed_tiers": ["economy"]}},
        )
        aliased = (await admin.call(alice, "GET", app)).json()
        assert aliased["ignored_aliases"] == ["fast"]  # standard is no longer allowed
        listed = (await admin.call(alice, "GET", app + "/versions")).json()
        assert [v["version"] for v in listed["versions"]] == [3, 2, 1]
        assert listed["versions"][0]["via"] == "revert:1"
        alice_user = await world.accounts.store.find_user("alice")
        assert alice_user is not None
        assert {v["changed_by_user_id"] for v in listed["versions"]} == {alice_user.id}
        one = (await admin.call(alice, "GET", app + "/versions/2")).json()
        assert one["settings"] == {"max_tokens": 20}
        assert one["previous_settings"] == {"max_tokens": 30, "model_aliases": {"fast": "standard"}}
        org_view = (await admin.call(alice, "GET", "/north/settings")).json()
        assert org_view["settings"] == {"allowed_tiers": ["economy"], "max_tokens": 40}
        # This console has no generate timeout of its own; the chat service has its defaults.
        assert org_view["platform_caps"]["generate_timeout_ms"] == {}
        assert org_view["platform_caps"]["chat_first_token_ms"] == {"platform": 30000.0}
        assert "default_quality" not in org_view["platform_caps"]  # not an organization's
    changed = await _audit(world, AuditAction.SETTINGS_CHANGED)
    reverted_rows = await _audit(world, AuditAction.SETTINGS_REVERTED)
    alice_id = (await world.accounts.store.find_user("alice")).id  # type: ignore[union-attr]
    assert [(e.event.target_type.value, e.event.target_id) for e in changed] == [
        ("organization", "north"),
        ("application", "north-shop"),
        ("application", "north-shop"),
        ("organization", "north"),
    ]
    unloosened = {"loosened_egress": False}
    assert [e.event.details for e in changed] == [
        {"settings_version": 1, "changed_fields": ["max_tokens"], **unloosened},
        {"settings_version": 1, "changed_fields": ["model_aliases", "max_tokens"], **unloosened},
        {"settings_version": 2, "changed_fields": ["model_aliases", "max_tokens"], **unloosened},
        {"settings_version": 2, "changed_fields": ["allowed_tiers"], **unloosened},
    ]
    assert [e.event.details for e in reverted_rows] == [
        {"settings_version": 3, "changed_fields": ["model_aliases", "max_tokens"], **unloosened}
    ]
    assert {e.event.actor_user_id for e in changed + reverted_rows} == {alice_id}


async def test_versions_are_listed_a_page_at_a_time(world: World) -> None:
    async with settings_console(world) as admin:
        alice = await admin.login("alice")
        for version in range(5):
            await admin.call(
                alice,
                "PUT",
                "/north/settings",
                {"expected_version": version, "set": {"max_tokens": 100 - version}},
            )
        page = (await admin.call(alice, "GET", "/north/settings/versions?limit=2")).json()
        assert [v["version"] for v in page["versions"]] == [5, 4]
        assert page["next_after"] == 4
        page = (await admin.call(alice, "GET", "/north/settings/versions?limit=2&after=4")).json()
        assert [v["version"] for v in page["versions"]] == [3, 2]
        page = (await admin.call(alice, "GET", "/north/settings/versions?limit=2&after=2")).json()
        assert [v["version"] for v in page["versions"]] == [1]
        assert page["next_after"] is None
        for query in ("limit=0", "limit=101", "after=0"):
            bad = await admin.call(alice, "GET", f"/north/settings/versions?{query}")
            assert bad.status_code == 422, query


async def test_refusals_are_answered_audited_and_write_nothing(world: World) -> None:
    async with settings_console(world) as admin:
        alice = await admin.login("alice")
        app = "/north/applications/north-shop/settings"
        await admin.call(
            alice, "PUT", "/north/settings", {"expected_version": 0, "set": {"max_tokens": 40}}
        )
        before = await _rows(world)
        looser_body: dict[str, Any] = {"expected_version": 0, "set": {"max_tokens": 41}}
        quality = {"expected_version": 1, "set": {"default_quality": "best"}}
        cases: list[tuple[str, str, dict[str, Any], int, str]] = [
            ("PUT", app, looser_body, 409, "setting_looser_than_cap"),
            ("PUT", app, {"expected_version": 5, "set": {"max_tokens": 4}}, 409,
             "settings_version_conflict"),
            ("POST", app + "/revert", {"expected_version": 0}, 409, "no_previous_version"),
            ("PUT", app, {"expected_version": 0, "set": {"max_tokens": 0}}, 422, "invalid_request"),
            ("PUT", app, {"expected_version": 0, "set": {"nope": 1}}, 422, "invalid_request"),
            ("PUT", "/north/settings", quality, 422, "invalid_request"),
            ("PUT", app, {"expected_version": 0, "bogus": 1}, 422, "invalid_request"),
        ]  # fmt: skip
        for method, path, body, status, code in cases:
            answer = await admin.call(alice, method, path, body)
            assert (answer.status_code, answer.json()["error"]["code"]) == (status, code), body
            assert answer.headers.get_list("cache-control") == ["no-store"]
        assert await _rows(world) == before
        looser = (await admin.call(alice, "PUT", app, looser_body)).json()["error"]["message"]
        assert "max_tokens (capped by the organization)" in looser
    refused = await _audit(world, AuditAction.MANAGEMENT_REFUSED)
    assert [(e.event.details["reason"], e.event.details["action"]) for e in refused] == [
        ("setting_looser_than_cap", "settings.write"),
        ("settings_version_conflict", "settings.write"),
        ("no_previous_version", "settings.write"),
        ("setting_looser_than_cap", "settings.write"),
    ]


# --- the dry run --------------------------------------------------------------------------------


async def test_validate_writes_nothing_and_says_what_the_write_would_do(world: World) -> None:
    """The dry run, against the write on the same inputs: the same fields changed, the same
    document, the same refusal; and after any number of dry runs, no row, no version, no
    audit entry."""
    async with settings_console(world) as admin:
        alice = await admin.login("alice")
        app = "/north/applications/north-shop/settings"
        await admin.call(
            alice, "PUT", "/north/settings", {"expected_version": 0, "set": {"max_tokens": 40}}
        )
        rows = await _rows(world)
        audit = await world.accounts.audit.entries()
        bodies = [
            {"expected_version": 0, "set": {"max_tokens": 30, "allowed_tiers": ["economy"]}},
            {"expected_version": 0, "set": {"max_tokens": 41}},
            {"expected_version": 3, "set": {"max_tokens": 1}},
            {"expected_version": 0, "set": {"max_tokens": 0}},
        ]
        dry = [(await admin.call(alice, "POST", app + "/validate", b)).json() for b in bodies]
        assert await _rows(world) == rows
        assert await world.accounts.audit.entries() == audit
        assert dry[0]["valid"] is True
        assert dry[0]["changed"] == ["allowed_tiers", "max_tokens"]
        assert dry[0]["settings"] == {"allowed_tiers": ["economy"], "max_tokens": 30}
        assert dry[0]["effective"]["max_tokens"] == 30
        assert dry[0]["effective"]["allowed_tiers"] == ["economy"]
        assert dry[0]["step_up_required"] is False
        assert (dry[1]["valid"], dry[1]["code"]) == (False, "setting_looser_than_cap")
        assert dry[1]["refusals"] == [{"setting": "max_tokens", "level": "organization"}]
        assert (dry[2]["valid"], dry[2]["code"]) == (False, "settings_version_conflict")
        assert (dry[3]["valid"], dry[3]["invalid_setting"]) == (False, "max_tokens")
        # The write on the same inputs does what the dry run said.
        written = await admin.call(alice, "PUT", app, bodies[0])
        assert written.json()["changed"] == dry[0]["changed"]
        assert written.json()["settings"] == dry[0]["settings"]
        refused = await admin.call(alice, "PUT", app, {**bodies[1], "expected_version": 1})
        assert refused.json()["error"]["code"] == dry[1]["code"]
        org_dry = await admin.call(
            alice,
            "POST",
            "/north/settings/validate",
            {"expected_version": 1, "set": {"max_tokens": 10}},
        )
        assert org_dry.json()["valid"] is True
        assert "effective" not in org_dry.json()
        assert (await admin.call(alice, "GET", "/north/settings")).json()["settings"] == {
            "max_tokens": 40
        }


# --- the leak scan ------------------------------------------------------------------------------


async def test_no_session_csrf_token_password_or_key_reaches_rows_logs_or_answers(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    """Every settings endpoint is called with alice's session and CSRF token, after a login with
    her password; an API key of north-shop exists. Every settings row and version, every audit
    row, every log record and every answer is scanned. The controls: the session, the token and
    the password are found in what was sent, so a scan that found nothing could have."""
    sent: list[bytes] = []

    async def record(request: httpx.Request) -> None:
        sent.append(await request.aread() + repr(request.headers.raw).encode())

    answers: list[httpx.Response] = []
    with caplog.at_level(logging.DEBUG):
        async with settings_console(world) as admin:
            admin.client.event_hooks["request"].append(record)
            await _versions(admin)
            session = await admin.login("alice")
            for endpoint in ENDPOINTS:
                body = _body(endpoint)
                if endpoint.method == "PUT":
                    body = {"expected_version": 2, "set": {"max_tokens": 12}}
                if endpoint.path.endswith("/{name}"):
                    body = {"expected_version": 4}
                answers.append(
                    await admin.call(
                        session, endpoint.method, _path(endpoint, "north", "north-shop"), body
                    )
                )
            rows = repr(await _rows(world))
            audit = repr(world.accounts.connection.execute("SELECT * FROM audit_log").fetchall())
    logged = "\n".join(r.getMessage() for r in caplog.records)
    answered = "\n".join(a.text + repr(sorted(a.headers.items())) for a in answers)
    planted = {
        "session": session[0],
        "csrf token": session[1],
        "password": PASSWORD,
        "api key": world.api_keys["north-shop"],
    }
    for name in ("session", "csrf token", "password"):
        assert any(planted[name].encode() in body for body in sent), name  # the controls
    for name, value in planted.items():
        assert value not in rows, name
        assert value not in audit, name
        assert value not in logged, name
        assert value not in answered, name


# --- what no route can observe, and the order of the checks -------------------------------------


async def test_resolve_organization_answers_an_organization_that_does_not_exist_as_missing(
    world: World,
) -> None:
    """The settings routes' organization resolver on its own. No route can observe this rule:
    for any organization but the actor's own, ``authorize`` refuses whatever the resolver says
    (docs/40). So it is pinned here, at the resolver, where mutation M13a fails it."""
    stores = Stores(world.accounts.organizations, world.accounts.store)
    found = await resolve_organization(stores, "north")
    assert found is not None
    assert (found.level.value, found.organization_id) == ("organization", "north")
    for missing in ("nowhere", "North", "", "north "):
        assert await resolve_organization(stores, missing) is None, missing


async def test_validate_answers_only_after_authorize(world: World) -> None:
    """The dry run on south's path, for alice of north: a valid change, an invalid setting, a
    stale version and a body that is not even well formed all get the same 403, byte for
    byte. Authorization comes before the body is looked at, so validate tells an outsider
    nothing about south's settings, their version or the catalogue's checks."""
    async with settings_console(world) as admin:
        await _versions(admin)
        alice = await admin.login("alice")
        bodies: list[dict[str, Any]] = [
            {"expected_version": 2, "set": {"max_tokens": 8}},  # would be valid for south
            {"expected_version": 2, "set": {"max_tokens": 0}},  # invalid value
            {"expected_version": 9, "set": {"max_tokens": 8}},  # stale version
            {"expected_version": 2, "set": {"no_such_setting": 1}},
            {"expected_version": -1},  # fails the body's own schema
            {"unexpected": True},
        ]
        for path in (
            "/south/settings/validate",
            "/south/applications/south-shop/settings/validate",
        ):
            answers = [_raw(await admin.call(alice, "POST", path, body)) for body in bodies]
            assert answers[0][0] == 403
            assert answers[0][2] == FORBIDDEN_BYTES
            assert all(answer == answers[0] for answer in answers), path
        # The control: south's own org_admin gets each body's real answer.
        sid = await admin.login("sid")
        own = [await admin.call(sid, "POST", "/south/settings/validate", body) for body in bodies]
        assert [a.status_code for a in own] == [200, 200, 200, 200, 422, 422]
        assert [a.json().get("valid") for a in own[:4]] == [True, False, False, False]
