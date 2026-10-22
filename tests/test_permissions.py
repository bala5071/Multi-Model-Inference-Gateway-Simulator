"""Roles, the permission table, ``authorize``, ``requires`` and the route declarations. See
docs/28-roles.md.

The invariants below are written apart from the table: they say what must hold whatever the
table contains, so a table that drifted (or an ``authorize`` that allowed everything) fails
them, which the table checking itself could not show.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.routing import APIRoute
from starlette.routing import Mount, Route

from accounts_helpers import PASSWORD, Accounts, cookie, make_accounts, ready_user, with_csrf
from gateway.app import (
    DECLARATION,
    RouteDeclaration,
    create_app,
    create_simulated_live_app,
    declaration_of,
)
from gateway.auth import AccountAccess
from gateway.clock import SimClock
from gateway.continuous import build_continuous_registry
from gateway.localonly import LocalOnlyPolicy, is_local_only
from gateway.organizations import new_organization
from gateway.permissions import (
    PERMISSIONS,
    Actor,
    Assignment,
    Level,
    Resource,
    ScopeType,
    authorize,
    render_matrix,
)
from helpers import application_access, default_server

REPO = Path(__file__).resolve().parent.parent
ORG, OTHER_ORG = "org-a", "org-b"
APP, SIBLING_APP, OTHER_APP = "app-1", "app-2", "app-9"
USER = "usr_00000000000000aa"
READ_VERBS = {"list", "read", "aggregate", "read_organization", "read_application"}


def _is_write(action: str) -> bool:
    return action.split(".", 1)[1] not in READ_VERBS


RESOURCES = {
    "platform": Resource.platform(),
    "own organization": Resource.organization(ORG),
    "other organization": Resource.organization(OTHER_ORG),
    "own application": Resource.application(ORG, APP),
    "sibling application": Resource.application(ORG, SIBLING_APP),
    "other organization's application": Resource.application(OTHER_ORG, OTHER_APP),
}
ASSIGNMENTS = {
    "platform_admin": Assignment("platform_admin", ScopeType.PLATFORM, "*"),
    "org_admin": Assignment("org_admin", ScopeType.ORGANIZATION, ORG),
    "app_admin": Assignment("app_admin", ScopeType.APPLICATION, APP),
    "viewer of the organization": Assignment("viewer", ScopeType.ORGANIZATION, ORG),
    "viewer of the application": Assignment("viewer", ScopeType.APPLICATION, APP),
    "clinic_staff": Assignment("clinic_staff", ScopeType.APPLICATION, APP),
}


def _actor(*assignments: Assignment, **flags: bool) -> Actor:
    return Actor(USER, ORG, assignments, **flags)


def _every(actor: Actor, resources: Iterator[str] | None = None) -> Iterator[tuple[str, str]]:
    """(action, resource name) pairs where ``actor`` is allowed."""
    names = list(resources) if resources is not None else list(RESOURCES)
    for action, name in itertools.product(PERMISSIONS, names):
        if authorize(actor, action, RESOURCES[name]).allowed:
            yield action, name


# --- invariants -------------------------------------------------------------------------------


def test_a_user_with_no_role_is_denied_everything() -> None:
    assert list(_every(_actor())) == []
    assert list(_every(_actor(ASSIGNMENTS["clinic_staff"]))) == []  # app roles grant nothing
    assert authorize(None, "users.list", RESOURCES["own organization"]).allowed is False


@pytest.mark.parametrize("flag", ["disabled", "organization_disabled"])
def test_a_disabled_user_or_organization_is_denied_everything(flag: str) -> None:
    everyone = _actor(*ASSIGNMENTS.values(), **{flag: True})
    assert list(_every(everyone)) == []


@pytest.mark.parametrize("name", [n for n in ASSIGNMENTS if n != "platform_admin"])
def test_nothing_crosses_organizations(name: str) -> None:
    actor = _actor(ASSIGNMENTS[name])
    crossing = ["other organization", "other organization's application"]
    assert list(_every(actor, iter(crossing))) == []


def test_platform_admin_never_reaches_inside_an_application() -> None:
    actor = _actor(ASSIGNMENTS["platform_admin"])
    inside = [n for n, r in RESOURCES.items() if r.level is Level.APPLICATION]
    assert list(_every(actor, iter(inside))) == []
    allowed = {action for action, _ in _every(actor)}
    assert not any(a.startswith(("application_keys.", "provider_keys.")) for a in allowed)
    assert not any(a.startswith(("application_roles.", "mode.")) for a in allowed)


@pytest.mark.parametrize("name", ["viewer of the organization", "viewer of the application"])
def test_a_viewer_never_changes_anything(name: str) -> None:
    allowed = {action for action, _ in _every(_actor(ASSIGNMENTS[name]))}
    assert allowed
    assert not any(_is_write(action) for action in allowed)
    assert allowed <= {
        "applications.list",
        "applications.read",
        "usage.read_organization",
        "usage.read_application",
    }


def test_an_app_admin_never_reaches_another_application_or_the_organization() -> None:
    actor = _actor(ASSIGNMENTS["app_admin"])
    elsewhere = [n for n in RESOURCES if n != "own application"]
    assert list(_every(actor, iter(elsewhere))) == []


def test_only_platform_admin_acts_on_the_platform() -> None:
    for name, assignment in ASSIGNMENTS.items():
        allowed = list(_every(_actor(assignment), iter(["platform"])))
        assert bool(allowed) is (name == "platform_admin"), name


def test_unknown_actions_and_wrong_resources_are_denied() -> None:
    actor = _actor(*ASSIGNMENTS.values())
    assert not authorize(actor, "users.delete_everything", RESOURCES["own organization"]).allowed
    # users.create acts on an organization; asked of an application or the platform, denied.
    assert not authorize(actor, "users.create", RESOURCES["own application"]).allowed
    assert not authorize(actor, "users.create", RESOURCES["platform"]).allowed


def test_what_each_role_can_do_at_least() -> None:
    # Anchors, so the invariants above are not satisfied by denying everything.
    org_admin = _actor(ASSIGNMENTS["org_admin"])
    app_admin = _actor(ASSIGNMENTS["app_admin"])
    platform_admin = _actor(ASSIGNMENTS["platform_admin"])
    assert authorize(org_admin, "users.create", RESOURCES["own organization"]).allowed
    assert authorize(org_admin, "application_keys.issue", RESOURCES["sibling application"]).allowed
    assert authorize(app_admin, "application_keys.issue", RESOURCES["own application"]).allowed
    assert authorize(platform_admin, "organizations.create", RESOURCES["platform"]).allowed
    assert authorize(platform_admin, "users.create", RESOURCES["other organization"]).allowed


# --- the table, as documented and as generated ------------------------------------------------


def _documented_table() -> dict[str, tuple[str, frozenset[str], bool, str]]:
    text = (REPO / "docs" / "28-roles.md").read_text(encoding="utf-8")
    rows = {}
    pattern = re.compile(r"^\| `([a-z_.]+)` \| (\w+) \| ([^|]+) \| (yes|) ?\| ([^|]+) \|$")
    for line in text.splitlines():
        match = pattern.match(line.strip())
        if match:
            action, level, allowed, step_up, wired = match.groups()
            pairs = frozenset(p.strip().strip("`") for p in allowed.split(","))
            rows[action] = (level, pairs, step_up == "yes", wired.strip())
    return rows


def test_the_documented_table_is_the_table() -> None:
    documented = _documented_table()
    actual = {
        p.action: (
            p.level.value,
            frozenset(f"{role.value}@{rel.value}" for role, rel in p.allowed),
            p.step_up,
            p.wired.value,
        )
        for p in PERMISSIONS.values()
    }
    assert documented == actual


def test_the_generated_matrix_is_up_to_date() -> None:
    committed = (REPO / "docs" / "permission-matrix.md").read_text(encoding="utf-8")
    assert committed.replace("\r\n", "\n") == render_matrix(), (
        "regenerate: uv run python -m gateway.permissions --matrix > docs/permission-matrix.md"
    )


# --- requires over HTTP -----------------------------------------------------------------------


@asynccontextmanager
async def _admin(accounts: Accounts) -> AsyncIterator[httpx.AsyncClient]:
    """The gateway with accounts, and two routes declared with ``requires``."""
    registry = build_continuous_registry(accounts.clock, seed=7)
    app = create_app(
        registry,
        accounts.clock,
        accounts=AccountAccess(accounts.service),
        local_only=LocalOnlyPolicy(),
        epoch=accounts.epoch,
    )
    requires = app.state.requires

    def organization(request: Request) -> Resource | None:
        found = accounts.organizations.get_organization(request.path_params["org"])
        return Resource.organization(found.id) if found is not None else None

    @app.get("/v1/admin/orgs/{org}/probe", dependencies=[requires("users.list", organization)])
    async def read_probe(org: str) -> dict[str, str]:
        return {"read": org}

    @app.post("/v1/admin/orgs/{org}/probe", dependencies=[requires("users.create", organization)])
    async def write_probe(org: str) -> dict[str, str]:
        return {"wrote": org}

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield client
    await registry.aclose()


async def _login(client: httpx.AsyncClient, username: str) -> tuple[str, str]:
    response = await client.post(
        "/v1/auth/login", json={"username": username, "password": PASSWORD}
    )
    client.cookies.clear()
    assert response.status_code == 200, response.text
    csrf = response.json()["csrf_token"]
    sid = response.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    return sid, csrf


@pytest.fixture
def org_accounts() -> Accounts:
    """north: alice (org_admin), carol (org_admin), vera (viewer), bob (no role);
    south: sid (org_admin); default: root (platform_admin)."""
    accounts = make_accounts()
    accounts.organizations.add_organization(
        new_organization("south", "South", "business", created_at_ms=0)
    )
    for name, org in (
        ("alice", "north"),
        ("carol", "north"),
        ("vera", "north"),
        ("bob", "north"),
        ("sid", "south"),
        ("root", "default"),
    ):
        ready_user(accounts, name, PASSWORD, org)
    service = accounts.service
    for name, role, org in (
        ("alice", "org_admin", "north"),
        ("carol", "org_admin", "north"),
        ("vera", "viewer", "north"),
        ("sid", "org_admin", "south"),
    ):
        service.grant_role(name, role, "organization", org)
    service.grant_role("root", "platform_admin", "platform", "*")
    return accounts


def _raw(response: httpx.Response) -> tuple[int, list[tuple[str, str]], bytes]:
    return response.status_code, sorted(response.headers.multi_items()), response.content


async def test_requires_checks_the_session_the_role_and_the_organization(
    org_accounts: Accounts,
) -> None:
    async with _admin(org_accounts) as client:
        assert (await client.get("/v1/admin/orgs/north/probe")).status_code == 401
        alice = await _login(client, "alice")
        own = await client.get("/v1/admin/orgs/north/probe", headers=cookie(alice[0]))
        assert (own.status_code, own.json()) == (200, {"read": "north"})
        other = await client.get("/v1/admin/orgs/south/probe", headers=cookie(alice[0]))
        missing = await client.get("/v1/admin/orgs/nowhere/probe", headers=cookie(alice[0]))
        assert other.status_code == 403
        assert other.json()["error"]["code"] == "forbidden"
        # Not there and not permitted: the same answer, byte for byte.
        assert _raw(missing) == _raw(other)
        bob = await _login(client, "bob")
        assert (
            await client.get("/v1/admin/orgs/north/probe", headers=cookie(bob[0]))
        ).status_code == 403
        root = await _login(client, "root")
        assert (
            await client.get("/v1/admin/orgs/south/probe", headers=cookie(root[0]))
        ).status_code == 200


async def test_requires_needs_the_csrf_token_on_a_change(org_accounts: Accounts) -> None:
    async with _admin(org_accounts) as client:
        sid, csrf = await _login(client, "alice")
        without = await client.post("/v1/admin/orgs/north/probe", headers=cookie(sid))
        assert (without.status_code, without.json()["error"]["code"]) == (403, "csrf_failed")
        ok = await client.post("/v1/admin/orgs/north/probe", headers=with_csrf(sid, csrf))
        assert ok.status_code == 200
        vera = await _login(client, "vera")
        viewer = await client.post("/v1/admin/orgs/north/probe", headers=with_csrf(*vera))
        assert viewer.status_code == 403


async def test_a_removed_role_is_gone_at_the_next_request(org_accounts: Accounts) -> None:
    async with _admin(org_accounts) as client:
        sid, _ = await _login(client, "alice")
        assert (
            await client.get("/v1/admin/orgs/north/probe", headers=cookie(sid))
        ).status_code == 200
        org_accounts.service.revoke_role("alice", "org_admin", "organization", "north")
        # The same session, no new login: refused.
        assert (
            await client.get("/v1/admin/orgs/north/probe", headers=cookie(sid))
        ).status_code == 403


async def test_requires_waits_for_a_password_change(org_accounts: Accounts) -> None:
    user = org_accounts.store.find_user("alice")
    assert user is not None
    async with _admin(org_accounts) as client:
        sid, _ = await _login(client, "alice")
        org_accounts.store.set_password(
            user.id, user.password_hash, must_change=True, bootstrap=user.bootstrap
        )
        blocked = await client.get("/v1/admin/orgs/north/probe", headers=cookie(sid))
    assert blocked.json()["error"]["code"] == "password_change_required"


def test_requires_refuses_an_undeclared_action() -> None:
    accounts = make_accounts()
    clock = SimClock()
    app = create_app(
        build_continuous_registry(clock, seed=7), clock, accounts=AccountAccess(accounts.service)
    )
    with pytest.raises(ValueError, match="unknown action"):
        app.state.requires("users.delete_everything", lambda request: None)


# --- every route declares itself ----------------------------------------------------------------

# Routes FastAPI itself adds (the documentation), and the dashboard's static files, which are
# a mount rather than an endpoint: the explicit lists.
DOCUMENTATION = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
LOCAL_ONLY_MOUNTS = {"/dashboard"}


async def _never(_: float) -> None:
    return None


def _variants() -> Iterator[tuple[str, FastAPI]]:
    clock = SimClock()
    yield "bare", create_app(build_continuous_registry(clock, seed=7), clock)
    access, _ = application_access(clock)
    yield (
        "applications",
        create_app(build_continuous_registry(clock, seed=7), clock, applications=access),
    )
    yield (
        "load control",
        create_app(build_continuous_registry(clock, seed=7), clock, load_control=True),
    )
    yield (
        "accounts",
        create_app(
            build_continuous_registry(clock, seed=7),
            clock,
            accounts=AccountAccess(make_accounts().service),
        ),
    )
    yield "simulated live", create_simulated_live_app(wait=_never, dashboard_dir=None)
    with default_server() as app:
        yield "default server", app


def test_every_route_of_every_app_declares_itself() -> None:
    seen_lists: set[str] = set()
    checked = 0
    for variant, app in _variants():
        for route in app.routes:
            path = getattr(route, "path", "")
            if isinstance(route, APIRoute):
                declarations = declaration_of(route)
                assert len(declarations) == 1, (variant, path, declarations)
                declaration = declarations[0]
                if declaration.kind == "local_only":
                    assert is_local_only(path), (variant, path)
                    assert declaration.action in PERMISSIONS
                else:
                    assert declaration.kind == "public", (variant, path)
                checked += 1
            elif isinstance(route, Mount):
                assert path in LOCAL_ONLY_MOUNTS, (variant, path)
                assert is_local_only(path)
                seen_lists.add(path)
            elif isinstance(route, Route):
                assert path in DOCUMENTATION, (variant, path)
                seen_lists.add(path)
            else:
                pytest.fail(f"{variant}: a route of an unknown kind at {path}")
    assert checked > 50
    # The explicit lists match real routes, so they cannot go stale.
    assert seen_lists >= DOCUMENTATION


def test_a_route_without_a_declaration_is_found() -> None:
    clock = SimClock()
    app = create_app(build_continuous_registry(clock, seed=7), clock)

    @app.get("/v1/forgotten")
    async def forgotten() -> dict[str, Any]:
        return {}

    [route] = [r for r in app.routes if getattr(r, "path", "") == "/v1/forgotten"]
    assert declaration_of(route) == []
    assert not hasattr(forgotten, DECLARATION)
    assert RouteDeclaration("public", None, "x").kind == "public"
