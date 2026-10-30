"""The management endpoints under /v1/admin/orgs/{org}: users, applications and their keys.
See docs/30-management-endpoints.md.

Two business organizations, north and south, each with an application named "Shop" (and
north with a second, "Blog"), and a key on each; an individual organization, solo, empty.
Test values, not secrets."""

from __future__ import annotations

import random
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.routing import APIRoute

from accounts_helpers import PASSWORD, Accounts, make_accounts, ready_user
from gateway.apikeys import ApiKeyService, ApplicationAccess
from gateway.app import create_app, declaration_of
from gateway.audit import AuditAction, AuditEntry
from gateway.auth import MINUTE_MS, AccountAccess
from gateway.continuous import build_continuous_registry
from gateway.localonly import LocalOnlyPolicy
from gateway.management import (
    MAX_ACTIVE_KEYS,
    ManagementService,
    Stores,
    resolve_application,
    resolve_key,
    resolve_member,
    resolve_organization,
)
from gateway.organizations import new_organization
from gateway.passwords import PasswordPolicy, load_blocklist
from gateway.permissions import PERMISSIONS, Resource
from gateway.routing import PlansConfig, load_plans
from gateway.state import run_now
from helpers import TEST_PEPPER

REPO = Path(__file__).resolve().parent.parent
FORBIDDEN = {"error": {"code": "forbidden", "message": "Not permitted, or not found."}}
STEP_UP_REQUIRED_CODE = "step_up_required"
MISSING_KEY_ID = "0123456789abcdef"


@dataclass
class World:
    accounts: Accounts
    keys: ApiKeyService
    templates: PlansConfig
    # application id -> its first key's id, and the key itself (for /v1/generate)
    key_ids: dict[str, str]
    api_keys: dict[str, str]


def make_world(**service: Any) -> World:
    """north: alice and carol (org_admins), vera (viewer), nina (app_admin of north-shop), bob
    (no role); south: sid (org_admin), sam (no role); default: root (platform_admin)."""
    accounts = make_accounts(**service)
    run_now(
        accounts.organizations.add_organization(
            new_organization("south", "South", "business", created_at_ms=0)
        )
    )
    for name, org in (
        ("alice", "north"),
        ("carol", "north"),
        ("vera", "north"),
        ("nina", "north"),
        ("bob", "north"),
        ("sid", "south"),
        ("sam", "south"),
        ("root", "default"),
    ):
        ready_user(accounts, name, PASSWORD, org)
    templates = load_plans()
    keys = ApiKeyService(
        accounts.organizations, TEST_PEPPER, accounts.epoch, random_bytes=random.Random(3).randbytes
    )
    operator = ManagementService(keys, templates, accounts.audit, accounts.epoch)
    for app_id, org, name in (
        ("north-shop", "north", "Shop"),
        ("north-blog", "north", "Blog"),
        ("south-shop", "south", "Shop"),  # the same name, in another organization
    ):
        run_now(operator.create_application(org, name, "pro", application_id=app_id))
    service_ = accounts.service
    for name, role, scope_type, scope in (
        ("alice", "org_admin", "organization", "north"),
        ("carol", "org_admin", "organization", "north"),
        ("vera", "viewer", "organization", "north"),
        ("nina", "app_admin", "application", "north-shop"),
        ("sid", "org_admin", "organization", "south"),
        ("root", "platform_admin", "platform", "*"),
    ):
        run_now(service_.grant_role(name, role, scope_type, scope))
    key_ids, api_keys = {}, {}
    for app_id in ("north-shop", "north-blog", "south-shop"):
        issued = run_now(operator.issue_key(app_id, "main"))
        key_ids[app_id], api_keys[app_id] = issued.record.key_id, issued.key
    return World(accounts, keys, templates, key_ids, api_keys)


@pytest.fixture
async def world() -> World:
    return make_world()


Session = tuple[str, str]


@dataclass
class Admin:
    client: httpx.AsyncClient
    world: World

    async def login(self, username: str, password: str = PASSWORD) -> Session:
        response = await self.client.post(
            "/v1/auth/login", json={"username": username, "password": password}
        )
        self.client.cookies.clear()
        assert response.status_code == 200, response.text
        sid = response.headers["set-cookie"].split(";")[0].split("=", 1)[1]
        return sid, response.json()["csrf_token"]

    async def step_up(self, session: Session, password: str = PASSWORD) -> None:
        response = await self.client.post(
            "/v1/auth/step-up", json={"password": password}, headers=_headers(session)
        )
        assert response.status_code == 200, response.text

    async def call(
        self, session: Session, method: str, path: str, body: dict[str, Any] | None = None
    ) -> httpx.Response:
        url = "/v1/admin/orgs" + path
        if method == "GET":
            return await self.client.get(url, headers=_headers(session))
        return await self.client.post(url, json=body or {}, headers=_headers(session))

    async def post(
        self, session: Session, path: str, body: dict[str, Any] | None = None
    ) -> httpx.Response:
        return await self.call(session, "POST", path, body)

    async def get(self, session: Session, path: str) -> httpx.Response:
        return await self.call(session, "GET", path)


def _headers(session: Session) -> dict[str, str]:
    return {"Cookie": f"gw_session={session[0]}", "X-CSRF-Token": session[1]}


@asynccontextmanager
async def admin_console(world: World) -> AsyncIterator[Admin]:
    accounts = world.accounts
    registry = build_continuous_registry(accounts.clock, seed=7)
    app = create_app(
        registry,
        accounts.clock,
        applications=ApplicationAccess(world.keys, world.templates),
        accounts=AccountAccess(accounts.service),
        local_only=LocalOnlyPolicy(),
        epoch=accounts.epoch,
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield Admin(client, world)
    await registry.aclose()


def _raw(response: httpx.Response) -> tuple[int, list[tuple[str, str]], bytes]:
    return response.status_code, sorted(response.headers.multi_items()), response.content


async def _audit(world: World, action: AuditAction) -> list[AuditEntry]:
    return [e for e in await world.accounts.audit.entries() if e.event.action is action]


# --- the endpoints, as documented ------------------------------------------------------------


@dataclass(frozen=True)
class Endpoint:
    method: str
    path: str  # under /v1/admin/orgs, with {org}
    action: str
    step_up: bool
    success: int


def documented_endpoints() -> list[Endpoint]:
    text = (REPO / "docs" / "30-management-endpoints.md").read_text(encoding="utf-8")
    pattern = re.compile(r"^\| `(GET|POST)` \| `([^`]+)` \| `([a-z_.]+)` \| (yes|) ?\| (\d{3}) \|$")
    found = []
    for line in text.splitlines():
        match = pattern.match(line.strip())
        if match:
            method, path, action, step_up, success = match.groups()
            found.append(Endpoint(method, "/{org}" + path, action, step_up == "yes", int(success)))
    return found


ENDPOINTS = documented_endpoints()


async def test_the_routes_are_the_documented_endpoints() -> None:
    """Every management route, its method and the action it declares, equal the table in
    note 30, and each action's step-up flag equals the permission table's: a route declared
    with a weaker action fails here."""
    assert len(ENDPOINTS) == 13
    world = make_world()
    registry = build_continuous_registry(world.accounts.clock, seed=7)
    app = create_app(
        registry,
        world.accounts.clock,
        applications=ApplicationAccess(world.keys, world.templates),
        accounts=AccountAccess(world.accounts.service),
    )
    routes = set()
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path.startswith("/v1/admin"):
            [declaration] = declaration_of(route)
            assert declaration.kind == "requires"
            for method in route.methods or ():
                path = route.path.removeprefix("/v1/admin/orgs")
                routes.add((method, path, declaration.action))
    assert routes == {(e.method, e.path, e.action) for e in ENDPOINTS}
    for endpoint in ENDPOINTS:
        assert PERMISSIONS[endpoint.action].step_up is endpoint.step_up, endpoint


# Valid targets in north, the same kinds of thing in south, and missing ones.
NORTH = {"org": "north", "username": "bob", "app": "north-shop"}
SOUTH = {"org": "south", "username": "sam", "app": "south-shop"}


def _path(endpoint: Endpoint, org: str, username: str, app: str, key_id: str) -> str:
    return (
        endpoint.path.replace("{org}", org)
        .replace("{username}", username)
        .replace("{app}", app)
        .replace("{key_id}", key_id)
    )


BODIES: dict[str, dict[str, Any]] = {
    "users.create": {"username": "newbie"},
    "applications.create": {"name": "Fresh", "plan": "pro"},
    "application_keys.issue": {"label": "fresh"},
    "application_keys.rotate": {"grace_hours": 1},
}


# --- cross-organization, and missing equals forbidden -------------------------------------------


@pytest.mark.parametrize("stepped_up", [False, True], ids=["no step-up", "step-up"])
async def test_nothing_crosses_organizations_and_missing_looks_forbidden(
    world: World, stepped_up: bool
) -> None:
    """alice, org_admin of north, on every endpoint: south's organization, south's user,
    application and key reached through north's path, and things that do not exist, all get
    the same 403, byte for byte, with or without a step-up window (authorization comes first),
    and nothing changes."""
    async with admin_console(world) as admin:
        alice = await admin.login("alice")
        if stepped_up:
            await admin.step_up(alice)
        north_key = world.key_ids["north-shop"]
        south_key = world.key_ids["south-shop"]
        before = await world.accounts.audit.entries()
        for endpoint in ENDPOINTS:
            body = BODIES.get(endpoint.action)
            attempts = {
                "south's own path": _path(endpoint, "south", "sam", "south-shop", south_key),
                "no such organization": _path(endpoint, "nowhere", "bob", "north-shop", north_key),
            }
            if "{username}" in endpoint.path:
                attempts["south's user via north"] = _path(endpoint, "north", "sam", "x", "x")
                attempts["no such user"] = _path(endpoint, "north", "ghost", "x", "x")
            if "{app}" in endpoint.path:
                attempts["south's application via north"] = _path(
                    endpoint, "north", "x", "south-shop", south_key
                )
                attempts["no such application"] = _path(
                    endpoint, "north", "x", "app_0000000000000000", north_key
                )
            if "{key_id}" in endpoint.path:
                attempts["south's key via north's application"] = _path(
                    endpoint, "north", "x", "north-shop", south_key
                )
                attempts["north's other application's key"] = _path(
                    endpoint, "north", "x", "north-shop", world.key_ids["north-blog"]
                )
                attempts["no such key"] = _path(
                    endpoint, "north", "x", "north-shop", MISSING_KEY_ID
                )
            answers = {
                name: _raw(await admin.call(alice, endpoint.method, path, body))
                for name, path in attempts.items()
            }
            first = next(iter(answers.values()))
            assert first[0] == 403, (endpoint, answers)
            assert (
                first[2]
                == b'{"error":{"code":"forbidden","message":"Not permitted, or not found."}}'
            )
            for name, answer in answers.items():
                assert answer == first, (endpoint.action, name)
        # Refused before anything ran: no change, no step-up entry, no audit at all.
        assert await world.accounts.audit.entries() == before
    south = await world.accounts.store.find_user("sam")
    assert south is not None
    assert not south.disabled
    assert await world.keys.store.get_key(south_key) is not None
    assert not (await world.keys.store.get_key(south_key)).revoked  # type: ignore[union-attr]
    assert len(await world.keys.store.list_applications("south")) == 1


async def test_south_can_still_do_all_of_it_to_its_own(world: World) -> None:
    """The control for the test above: the same paths work for south's own org_admin."""
    async with admin_console(world) as admin:
        sid = await admin.login("sid")
        await admin.step_up(sid)
        assert (await admin.get(sid, "/south/users")).status_code == 200
        assert (await admin.get(sid, "/south/applications/south-shop")).status_code == 200
        key = world.key_ids["south-shop"]
        rotated = await admin.post(sid, f"/south/applications/south-shop/keys/{key}/rotate")
        assert rotated.status_code == 201


async def test_a_user_with_no_role_gets_forbidden_everywhere(world: World) -> None:
    async with admin_console(world) as admin:
        bob = await admin.login("bob")
        await admin.step_up(bob)
        for endpoint in ENDPOINTS:
            path = _path(endpoint, "north", "carol", "north-shop", world.key_ids["north-shop"])
            response = await admin.call(bob, endpoint.method, path, BODIES.get(endpoint.action))
            assert (response.status_code, response.json()) == (403, FORBIDDEN), endpoint


async def test_a_revoked_role_is_gone_at_the_next_request(world: World) -> None:
    async with admin_console(world) as admin:
        alice = await admin.login("alice")
        assert (await admin.get(alice, "/north/users")).status_code == 200
        await world.accounts.service.revoke_role("alice", "org_admin", "organization", "north")
        assert (await admin.get(alice, "/north/users")).json() == FORBIDDEN


async def test_platform_admin_manages_users_anywhere_and_no_application(world: World) -> None:
    async with admin_console(world) as admin:
        root = await admin.login("root")
        await admin.step_up(root)
        assert (await admin.get(root, "/north/users")).status_code == 200
        assert (await admin.post(root, "/south/users/sam/unlock")).status_code == 200
        key = world.key_ids["north-shop"]
        for endpoint in ENDPOINTS:
            if not endpoint.action.startswith("application"):
                continue
            path = _path(endpoint, "north", "x", "north-shop", key)
            response = await admin.call(root, endpoint.method, path, BODIES.get(endpoint.action))
            assert (response.status_code, response.json()) == (403, FORBIDDEN), endpoint


async def test_viewers_read_and_app_admins_keep_to_their_application(world: World) -> None:
    async with admin_console(world) as admin:
        vera = await admin.login("vera")
        await admin.step_up(vera)
        assert (await admin.get(vera, "/north/applications")).status_code == 200
        assert (await admin.get(vera, "/north/applications/north-blog")).status_code == 200
        issued = await admin.post(vera, "/north/applications/north-shop/keys", {"label": "x"})
        assert issued.json() == FORBIDDEN
        assert (await admin.get(vera, "/north/users")).json() == FORBIDDEN

        nina = await admin.login("nina")
        await admin.step_up(nina)
        mine = await admin.post(nina, "/north/applications/north-shop/keys", {"label": "x"})
        assert mine.status_code == 201
        other = await admin.post(nina, "/north/applications/north-blog/keys", {"label": "x"})
        assert other.json() == FORBIDDEN
        assert (await admin.get(nina, "/north/applications")).json() == FORBIDDEN
        assert (await admin.get(nina, "/north/users")).json() == FORBIDDEN


async def test_there_is_no_endpoint_for_roles(world: World) -> None:
    async with admin_console(world) as admin:
        alice = await admin.login("alice")
        await admin.step_up(alice)
        for path in ("/north/users/bob/roles", "/north/roles", "/north/users/bob/grant-role"):
            response = await admin.post(alice, path, {"role": "org_admin"})
            assert response.status_code in (404, 405), path
    assert not [e for e in ENDPOINTS if "role" in e.path]
    assert await world.accounts.service.list_roles("bob") == ()


# --- step-up -------------------------------------------------------------------------------------


async def test_every_flagged_endpoint_needs_step_up_over_http(world: World) -> None:
    flagged = [e for e in ENDPOINTS if e.step_up]
    assert {e.action for e in flagged} == {
        "users.create",
        "users.disable",
        "users.enable",
        "users.reset_password",
        "users.unlock",
        "application_keys.issue",
        "application_keys.rotate",
        "application_keys.revoke",
    }
    async with admin_console(world) as admin:
        alice = await admin.login("alice")
        key = world.key_ids["north-shop"]
        for endpoint in flagged:
            path = _path(endpoint, "north", "bob", "north-shop", key)
            refused = await admin.call(alice, endpoint.method, path, BODIES.get(endpoint.action))
            assert refused.status_code == 403, endpoint
            assert refused.json()["error"]["code"] == STEP_UP_REQUIRED_CODE, endpoint
        assert len(await _audit(world, AuditAction.STEP_UP_REQUIRED)) == len(flagged)
        await admin.step_up(alice)
        # In an order where each one has something to act on.
        for action in (
            "users.create",
            "users.disable",
            "users.enable",
            "users.unlock",
            "users.reset_password",
            "application_keys.issue",
            "application_keys.rotate",
            "application_keys.revoke",
        ):
            [endpoint] = [e for e in flagged if e.action == action]
            path = _path(endpoint, "north", "bob", "north-shop", key)
            served = await admin.call(alice, endpoint.method, path, BODIES.get(endpoint.action))
            assert served.status_code == endpoint.success, (action, served.text)


async def test_unlocking_needs_step_up_because_it_lifts_a_lockout(world: World) -> None:
    accounts = world.accounts
    async with admin_console(world) as admin:
        for _ in range(5):
            await admin.client.post(
                "/v1/auth/login", json={"username": "bob", "password": "not the password"}
            )
        admin.client.cookies.clear()
        bob = await accounts.store.find_user("bob")
        assert bob is not None
        now = accounts.epoch.epoch_ms()
        assert accounts.service.is_locked(bob, now)
        alice = await admin.login("alice")
        refused = await admin.post(alice, "/north/users/bob/unlock")
        assert refused.json()["error"]["code"] == STEP_UP_REQUIRED_CODE
        assert accounts.service.is_locked(await accounts.store.find_user("bob"), now)  # type: ignore[arg-type]
        await admin.step_up(alice)
        assert (await admin.post(alice, "/north/users/bob/unlock")).json() == {"unlocked": True}
        assert not accounts.service.is_locked(await accounts.store.find_user("bob"), now)  # type: ignore[arg-type]


# --- users ---------------------------------------------------------------------------------------


async def test_creating_a_user_returns_a_temporary_password_once(world: World) -> None:
    accounts = world.accounts
    async with admin_console(world) as admin:
        alice = await admin.login("alice")
        await admin.step_up(alice)
        made = await admin.post(alice, "/north/users", {"username": "Newbie"})
        assert made.status_code == 201
        assert made.headers["cache-control"] == "no-store"
        body = made.json()
        temporary = body["temporary_password"]
        assert body["user"]["username"] == "newbie"
        assert body["user"]["must_change_password"] is True
        assert set(body["user"]) == {
            "user_id",
            "username",
            "organization_id",
            "disabled",
            "must_change_password",
            "locked",
            "created_at_ms",
        }
        PasswordPolicy(load_blocklist()).check(temporary, "newbie")
        assert len(temporary) == 24
        # It logs in, and can do nothing but change it.
        newbie = await admin.login("newbie", temporary)
        refused = await admin.get(newbie, "/north/users")
        assert refused.json()["error"]["code"] == "password_change_required"
        # Only its hash is stored.
        user = await accounts.store.find_user("newbie")
        assert user is not None
        assert temporary not in user.password_hash
        [created] = [
            e for e in await _audit(world, AuditAction.USER_CREATED) if e.event.target_id == user.id
        ]
        assert created.event.actor_user_id == (await accounts.store.find_user("alice")).id  # type: ignore[union-attr]
        assert created.event.details["reason"] == "temporary_password"


async def test_no_password_is_accepted_over_http(world: World) -> None:
    async with admin_console(world) as admin:
        alice = await admin.login("alice")
        await admin.step_up(alice)
        chosen = await admin.post(
            alice, "/north/users", {"username": "newbie", "password": PASSWORD}
        )
        assert chosen.status_code == 422
        reset = await admin.post(alice, "/north/users/bob/reset-password", {"new_password": "x"})
        assert reset.status_code == 200  # the body is ignored: the endpoint takes none
        assert "temporary_password" in reset.json()
    assert await world.accounts.store.find_user("newbie") is None


async def test_usernames_are_global_and_every_conflict_is_audited(world: World) -> None:
    async with admin_console(world) as admin:
        alice = await admin.login("alice")
        await admin.step_up(alice)
        taken = await admin.post(alice, "/north/users", {"username": "sam"})  # sam is in south
        assert taken.status_code == 409
        assert taken.json()["error"]["code"] == "username_taken"
    [entry] = await _audit(world, AuditAction.MANAGEMENT_REFUSED)
    alice_id = (await world.accounts.store.find_user("alice")).id  # type: ignore[union-attr]
    assert entry.event.actor_user_id == alice_id
    assert entry.event.target_id == "north"
    assert dict(entry.event.details) == {"reason": "username_taken", "action": "users.create"}


async def test_an_individual_organization_takes_one_member_over_http(world: World) -> None:
    async with admin_console(world) as admin:
        root = await admin.login("root")
        await admin.step_up(root)
        assert (await admin.post(root, "/solo/users", {"username": "only"})).status_code == 201
        second = await admin.post(root, "/solo/users", {"username": "second"})
        assert (second.status_code, second.json()["error"]["code"]) == (409, "organization_full")
    reasons = [
        e.event.details["reason"] for e in await _audit(world, AuditAction.MANAGEMENT_REFUSED)
    ]
    assert reasons == ["organization_full"]


async def test_user_creation_attempts_are_capped_per_session() -> None:
    world = make_world(user_create_attempts=3)
    async with admin_console(world) as admin:
        alice = await admin.login("alice")
        await admin.step_up(alice)
        statuses = []
        for name in ("sam", "sid", "newbie", "another"):  # two taken, one made, then the cap
            response = await admin.post(alice, "/north/users", {"username": name})
            statuses.append((response.status_code, response.json().get("error", {}).get("code")))
        assert statuses == [
            (409, "username_taken"),
            (409, "username_taken"),
            (201, None),
            (429, "user_creation_limited"),
        ]
        assert await world.accounts.store.find_user("another") is None
        # Another session has its own count; this one's ends after an hour.
        carol = await admin.login("carol")
        await admin.step_up(carol)
        assert (await admin.post(carol, "/north/users", {"username": "another"})).status_code == 201
        # Past the hour (and past the session's idle limit: a new session of the same user
        # starts its own count anyway).
        await world.accounts.clock.sleep_ms(60 * MINUTE_MS)
        alice = await admin.login("alice")
        await admin.step_up(alice)
        assert (await admin.post(alice, "/north/users", {"username": "third"})).status_code == 201
    reasons = [
        e.event.details["reason"] for e in await _audit(world, AuditAction.MANAGEMENT_REFUSED)
    ]
    assert reasons == ["username_taken", "username_taken", "user_creation_limited"]


async def test_an_admin_cannot_disable_or_reset_their_own_account(world: World) -> None:
    accounts = world.accounts
    async with admin_console(world) as admin:
        alice = await admin.login("alice")
        await admin.step_up(alice)
        for action in ("disable", "reset-password"):
            response = await admin.post(alice, f"/north/users/alice/{action}")
            assert (response.status_code, response.json()["error"]["code"]) == (
                409,
                "own_account",
            ), action
        user = await accounts.store.find_user("alice")
        assert user is not None
        assert not user.disabled
        assert not user.must_change_password
        # Still logged in, still able to act on others.
        assert (await admin.post(alice, "/north/users/bob/disable")).status_code == 200
    reasons = [
        e.event.details["reason"] for e in await _audit(world, AuditAction.MANAGEMENT_REFUSED)
    ]
    assert reasons == ["own_account", "own_account"]


async def test_the_last_org_admin_cannot_be_disabled_over_http(world: World) -> None:
    async with admin_console(world) as admin:
        root = await admin.login("root")
        await admin.step_up(root)
        last = await admin.post(root, "/south/users/sid/disable")
        assert (last.status_code, last.json()["error"]["code"]) == (409, "last_admin")
        user = await world.accounts.store.find_user("sid")
        assert user is not None
        assert not user.disabled
        # north has two: one may go, then not the other.
        alice = await admin.login("alice")
        await admin.step_up(alice)
        assert (await admin.post(alice, "/north/users/carol/disable")).status_code == 200
        assert (await admin.post(root, "/north/users/alice/disable")).status_code == 409
    reasons = [
        e.event.details["reason"] for e in await _audit(world, AuditAction.MANAGEMENT_REFUSED)
    ]
    assert reasons == ["last_admin", "last_admin"]


async def test_a_reset_revokes_every_session_and_closes_step_up(world: World) -> None:
    accounts = world.accounts
    async with admin_console(world) as admin:
        carol = await admin.login("carol")
        await admin.step_up(carol)
        other = await admin.login("carol")
        alice = await admin.login("alice")
        await admin.step_up(alice)
        reset = await admin.post(alice, "/north/users/carol/reset-password")
        assert reset.status_code == 200
        assert reset.headers["cache-control"] == "no-store"
        assert reset.json()["sessions_revoked"] == 2
        temporary = reset.json()["temporary_password"]
        for session in (carol, other):
            assert (await admin.get(session, "/north/users")).status_code == 401
        user = await accounts.store.find_user("carol")
        assert user is not None
        assert user.must_change_password
        assert all(s.step_up_until_ms is None for s in await accounts.store.list_sessions(user.id))
        stored = accounts.connection.execute(
            "SELECT count(*) FROM sessions WHERE user_id = ? AND step_up_until_ms IS NOT NULL",
            (user.id,),
        ).fetchone()
        assert stored == (0,)
        # The old password is gone; the temporary one logs in, to change it.
        refused = await admin.client.post(
            "/v1/auth/login", json={"username": "carol", "password": PASSWORD}
        )
        assert refused.status_code == 401
        await admin.login("carol", temporary)


async def test_disable_enable_and_unlock(world: World) -> None:
    accounts = world.accounts
    async with admin_console(world) as admin:
        bob = await admin.login("bob")
        alice = await admin.login("alice")
        await admin.step_up(alice)
        disabled = await admin.post(alice, "/north/users/bob/disable")
        assert disabled.json() == {"disabled": True, "sessions_revoked": 1}
        assert (await admin.client.get("/v1/auth/me", headers=_headers(bob))).status_code == 401
        assert (await admin.post(alice, "/north/users/bob/enable")).json() == {"enabled": True}
        await admin.login("bob")
    actions = [e.event.action for e in await accounts.audit.entries()]
    assert AuditAction.USER_DISABLED in actions
    assert AuditAction.USER_ENABLED in actions


async def test_users_are_listed_a_page_at_a_time_without_roles(world: World) -> None:
    async with admin_console(world) as admin:
        alice = await admin.login("alice")
        seen: list[str] = []
        path = "/north/users?limit=2"
        while True:
            page = (await admin.get(alice, path)).json()
            assert len(page["users"]) <= 2
            seen += [u["username"] for u in page["users"]]
            if page["next"] is None:
                break
            path = f"/north/users?limit=2&after={page['next']}"
        assert seen == ["alice", "bob", "carol", "nina", "vera"]
        assert "roles" not in str(page)
        for bad in ("limit=0", "limit=101", "limit=x"):
            assert (await admin.get(alice, f"/north/users?{bad}")).status_code == 422
        # A cursor is compared, never looked up: south's names do not matter.
        after = (await admin.get(alice, "/north/users?after=sam")).json()
        assert [u["username"] for u in after["users"]] == ["vera"]


# --- applications --------------------------------------------------------------------------------


async def test_creating_an_application_from_a_plan_template(world: World) -> None:
    async with admin_console(world) as admin:
        alice = await admin.login("alice")
        made = await admin.post(alice, "/north/applications", {"name": "Fresh", "plan": "pro"})
        assert made.status_code == 201
        body = made.json()
        assert re.fullmatch(r"app_[0-9a-f]{16}", body["application_id"])
        assert body["organization_id"] == "north"
        assert body["plan"] == "pro"
        assert body["allowed_models"] == list(world.templates.plans["pro"].models)
        # The same name again in north is a conflict; "Shop" is free in neither, per org.
        again = await admin.post(alice, "/north/applications", {"name": "Fresh", "plan": "free"})
        assert (again.status_code, again.json()["error"]["code"]) == (
            409,
            "application_name_taken",
        )
        sid = await admin.login("sid")
        fresh_south = await admin.post(sid, "/south/applications", {"name": "Fresh", "plan": "pro"})
        assert fresh_south.status_code == 201
        # A plan's name, never raw numbers.
        for bad in (
            {"name": "X", "plan": "platinum"},
            {"name": "X", "plan": "pro", "monthly_token_budget": 10**12},
            {"name": "X", "models": ["premium"], "price_multiplier": 0},
            {"name": "bad\u200bname", "plan": "pro"},
        ):
            refused = await admin.post(alice, "/north/applications", bad)
            assert refused.status_code == 422, bad
        read = await admin.get(alice, f"/north/applications/{body['application_id']}")
        assert read.json() == body
    [created, *_] = [
        e
        for e in await _audit(world, AuditAction.APPLICATION_CREATED)
        if e.event.target_id == body["application_id"]
    ]
    assert dict(created.event.details) == {"organization_id": "north", "plan": "pro"}
    reasons = [
        e.event.details["reason"] for e in await _audit(world, AuditAction.MANAGEMENT_REFUSED)
    ]
    assert reasons == ["application_name_taken"]


async def test_applications_are_listed_a_page_at_a_time(world: World) -> None:
    async with admin_console(world) as admin:
        alice = await admin.login("alice")
        first = (await admin.get(alice, "/north/applications?limit=1")).json()
        assert [a["application_id"] for a in first["applications"]] == ["north-blog"]
        assert first["next"] == "north-blog"
        second = (await admin.get(alice, "/north/applications?limit=1&after=north-blog")).json()
        assert [a["application_id"] for a in second["applications"]] == ["north-shop"]
        assert second["next"] is None


# --- keys ----------------------------------------------------------------------------------------


async def test_a_key_is_shown_once_works_and_is_listed_without_secrets(world: World) -> None:
    async with admin_console(world) as admin:
        alice = await admin.login("alice")
        await admin.step_up(alice)
        issued = await admin.post(
            alice, "/north/applications/north-shop/keys", {"label": "backend 2"}
        )
        assert issued.status_code == 201
        assert issued.headers["cache-control"] == "no-store"
        key = issued.json()["key"]
        key_id = issued.json()["key_id"]
        assert key.startswith(f"gwk_{key_id}_")
        assert issued.json()["state"] == "active"
        # It works for the application it was issued to.
        generated = await admin.client.post(
            "/v1/generate",
            json={"model": "economy", "prompt": "hello there", "max_tokens": 2},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert generated.status_code == 200, generated.text
        listed = await admin.get(alice, "/north/applications/north-shop/keys")
        text = listed.text
        assert key not in text
        record = await world.keys.store.get_key(key_id)
        assert record is not None
        assert record.key_hmac not in text
        keys = listed.json()["keys"]
        assert {k["key_id"] for k in keys} == {key_id, world.key_ids["north-shop"]}
        assert set(keys[0]) == {
            "key_id",
            "label",
            "state",
            "created_at_ms",
            "expires_at_ms",
            "last_used_at_ms",
        }
    [entry] = [
        e for e in await _audit(world, AuditAction.KEY_ISSUED) if e.event.target_id == key_id
    ]
    assert entry.event.details["application_id"] == "north-shop"
    assert entry.event.details["organization_id"] == "north"


async def test_key_limits(world: World) -> None:
    async with admin_console(world) as admin:
        alice = await admin.login("alice")
        await admin.step_up(alice)
        path = "/north/applications/north-shop/keys"
        for body in (
            {"label": ""},
            {"label": " padded"},
            {"label": "x" * 65},
            {"label": "tab\there"},
            {"label": "caf\u00e9"},
            {"label": "ok", "expires_in_days": 366},
            {"label": "ok", "expires_in_days": 0},
            {"label": "ok", "expires_in_days": None},
        ):
            assert (await admin.post(alice, path, body)).status_code == 422, body
        # Every key over HTTP expires: 90 days unless asked, at most 365.
        made = (await admin.post(alice, path, {"label": "a b.c_d-e"})).json()
        assert made["expires_at_ms"] == made["created_at_ms"] + 90 * 24 * 60 * MINUTE_MS
        longest = (await admin.post(alice, path, {"label": "long", "expires_in_days": 365})).json()
        assert longest["expires_at_ms"] == longest["created_at_ms"] + 365 * 24 * 60 * MINUTE_MS
        # north-shop now has 3 active keys; up to the limit, then a conflict.
        for i in range(MAX_ACTIVE_KEYS - 3):
            assert (await admin.post(alice, path, {"label": f"k{i}"})).status_code == 201
        over = await admin.post(alice, path, {"label": "one too many"})
        assert (over.status_code, over.json()["error"]["code"]) == (409, "key_limit")
        # Rotating at the limit works: the old key is counted out.
        old = world.key_ids["north-shop"]
        rotated = await admin.post(alice, f"{path}/{old}/rotate", {"grace_hours": 0})
        assert rotated.status_code == 201
        # Revoking one makes room.
        assert (await admin.post(alice, f"{path}/{made['key_id']}/revoke")).status_code == 200
        assert (await admin.post(alice, path, {"label": "room again"})).status_code == 201
    reasons = [
        e.event.details["reason"] for e in await _audit(world, AuditAction.MANAGEMENT_REFUSED)
    ]
    assert reasons == ["key_limit"]


async def test_rotation_and_revocation(world: World) -> None:
    clock = world.accounts.clock
    old_key = world.api_keys["north-shop"]
    old_id = world.key_ids["north-shop"]

    async def generate(admin: Admin, key: str) -> int:
        response = await admin.client.post(
            "/v1/generate",
            json={"model": "economy", "prompt": "hi there", "max_tokens": 1},
            headers={"Authorization": f"Bearer {key}"},
        )
        return response.status_code

    async with admin_console(world) as admin:
        alice = await admin.login("alice")
        await admin.step_up(alice)
        path = f"/north/applications/north-shop/keys/{old_id}/rotate"
        for grace in (-1, 169, "x"):
            assert (await admin.post(alice, path, {"grace_hours": grace})).status_code == 422
        rotated = await admin.post(alice, path, {"grace_hours": 168})
        assert rotated.status_code == 201
        new_key, new_id = rotated.json()["key"], rotated.json()["key_id"]
        assert new_id != old_id
        assert await generate(admin, old_key) == 200
        assert await generate(admin, new_key) == 200
        await clock.sleep_ms(168 * 60 * MINUTE_MS)
        assert await generate(admin, old_key) == 401
        alice = await admin.login("alice")
        await admin.step_up(alice)
        again = await admin.post(alice, path, {})
        assert (again.status_code, again.json()["error"]["code"]) == (409, "key_not_active")
        revoked = await admin.post(alice, f"/north/applications/north-shop/keys/{new_id}/revoke")
        assert revoked.json() == {"key_id": new_id, "state": "revoked"}
        assert await generate(admin, new_key) == 401
    [rotation] = await _audit(world, AuditAction.KEY_ROTATED)
    assert rotation.event.target_id == old_id
    assert rotation.event.details["key_id"] == new_id
    assert rotation.event.details["grace_ms"] == 168 * 60 * MINUTE_MS
    [revocation] = await _audit(world, AuditAction.KEY_REVOKED)
    assert revocation.event.target_id == new_id


# --- every admin answer is no-store ---------------------------------------------------------------


async def test_every_admin_answer_is_no_store(world: World) -> None:
    async with admin_console(world) as admin:
        alice = await admin.login("alice")
        answers = [
            await admin.get(alice, "/north/users"),  # 200
            await admin.get(alice, "/south/users"),  # 403 forbidden
            await admin.post(alice, "/north/users", {"username": "x"}),  # 403 step-up
            await admin.get(alice, "/north/users?limit=0"),  # 422
            await admin.get(alice, "/north/nothing-here"),  # 404
            await admin.client.get("/v1/admin/orgs/north/users"),  # 401
            await admin.client.post("/v1/admin/orgs/north/applications", json={}),  # 401
            await admin.client.post(
                "/v1/admin/orgs/north/applications",
                json={},
                headers={"Cookie": f"gw_session={alice[0]}"},
            ),  # 403 csrf
            await admin.client.get(
                "/v1/admin/orgs/north/users", headers={"Host": "attacker.example"}
            ),  # 403 local only
        ]
        await admin.step_up(alice)
        answers.append(await admin.post(alice, "/north/users/alice/disable"))  # 409
        statuses = [a.status_code for a in answers]
        assert statuses == [200, 403, 403, 422, 404, 401, 401, 403, 403, 409]
        for answer in answers:
            assert answer.headers.get_list("cache-control") == ["no-store"], answer.request.url
        # Elsewhere, nothing changes.
        models = await admin.client.get("/v1/models")
        assert "cache-control" not in models.headers


# --- the resolvers on their own ----------------------------------------------------------------


async def test_each_resolver_answers_outside_its_organization_as_missing(world: World) -> None:
    stores = Stores(world.accounts.organizations, world.accounts.store)
    north_key, south_key = world.key_ids["north-shop"], world.key_ids["south-shop"]
    blog_key = world.key_ids["north-blog"]
    assert await resolve_organization(stores, "north") is not None
    assert await resolve_organization(stores, "nowhere") is None
    assert await resolve_member(stores, "north", "bob") is not None
    assert await resolve_member(stores, "north", "BOB") is not None
    assert await resolve_member(stores, "north", "sam") is None  # south's
    assert await resolve_member(stores, "north", "ghost") is None
    assert await resolve_member(stores, "north", "not a name!") is None
    assert await resolve_member(stores, "nowhere", "bob") is None
    assert await resolve_application(stores, "north", "north-shop") is not None
    assert await resolve_application(stores, "north", "south-shop") is None  # south's
    assert await resolve_application(stores, "south", "north-shop") is None
    assert await resolve_application(stores, "nowhere", "north-shop") is None
    assert await resolve_key(stores, "north", "north-shop", north_key) is not None
    assert await resolve_key(stores, "north", "north-shop", south_key) is None  # south's
    assert (
        await resolve_key(stores, "north", "north-shop", blog_key) is None
    )  # another application's
    assert await resolve_key(stores, "north", "south-shop", south_key) is None
    assert await resolve_key(stores, "north", "north-shop", MISSING_KEY_ID) is None


async def test_behind_a_broken_resolver_the_services_own_check_still_refuses(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defense in depth: with every resolver made to ignore the organization (as if broken),
    the services' own check of the record refuses south's user, application and key reached
    through north, with the same 403, byte for byte, and changes nothing."""
    import gateway.app as app_module

    stores = Stores(world.accounts.organizations, world.accounts.store)

    async def any_member(_: Stores, org: str, username: str) -> Any:
        found = await world.accounts.store.find_user(username)
        return await resolve_organization(stores, org) if found is not None else None

    async def any_application(_: Stores, org: str, app: str) -> Any:
        found = await world.accounts.organizations.get_application(app)
        return None if found is None else Resource.application(org, app)

    async def any_key(_: Stores, org: str, app: str, key_id: str) -> Any:
        return await any_application(_, org, app)

    monkeypatch.setattr(app_module, "resolve_member", any_member)
    monkeypatch.setattr(app_module, "resolve_application", any_application)
    monkeypatch.setattr(app_module, "resolve_key", any_key)
    south_key = world.key_ids["south-shop"]
    async with admin_console(world) as admin:
        alice = await admin.login("alice")
        await admin.step_up(alice)
        missing = _raw(await admin.get(alice, "/nowhere/users"))
        for method, path, body in (
            ("POST", "/north/users/sam/disable", None),
            ("POST", "/north/users/sam/reset-password", None),
            ("GET", "/north/applications/south-shop", None),
            ("POST", "/north/applications/south-shop/keys", {"label": "x"}),
            ("GET", "/north/applications/south-shop/keys", None),
            ("POST", f"/north/applications/south-shop/keys/{south_key}/revoke", None),
        ):
            answer = _raw(await admin.call(alice, method, path, body))
            assert answer == missing, path
    sam = await world.accounts.store.find_user("sam")
    assert sam is not None
    assert not sam.disabled
    assert not (await world.keys.store.get_key(south_key)).revoked  # type: ignore[union-attr]
    assert len(await world.keys.store.list_keys("south-shop")) == 1
