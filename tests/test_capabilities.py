"""``GET /v1/auth/capabilities`` (docs/50): what the console's navigation shows, as the gateway
answers it.

What is pinned here:

- the answer for every role (the four built in, an application-defined role, no role), against
  the navigation table of docs/console-integration-plan.md, section 2.3, written out by hand so a
  change to who may do what shows up here too;
- that it agrees with the real routes: for every role, every organization relation (own, other,
  missing) and every application relation (own, other, missing), an action the answer allows is
  answered 200 by the route its screen loads first, and one it does not is answered 403
  ``forbidden``, the same bytes as a missing resource;
- that each navigation action is declared by that route, at the level the answer files it under;
- that a missing organization and a forbidden one give the same bytes, for every role;
- the session, a password to change, a disabled organization, an assignment to an application
  that is gone, the roles read on each request, the page of applications, the local-only guard,
  ``Cache-Control: no-store`` on every answer, and a leak scan with its planted control.

The world of ``test_management_api``: north (alice and carol org_admins, vera viewer, nina
app_admin of north-shop, bob no role), south (sid org_admin, sam no role), default (root
platform_admin), plus olive (viewer of north-blog only) and cleo (the application-defined role
clinic_staff on north-shop). Test values, not secrets."""

from __future__ import annotations

import json
import logging
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from fastapi.routing import APIRoute

import gateway.app as app_module
from accounts_helpers import PASSWORD, ready_user
from gateway.alertrules import SqliteRuleStore
from gateway.alerts.channels import ChannelService, SqliteChannelStore
from gateway.alerts.notices import Notices
from gateway.alerts.outbox import SqliteOutboxStore
from gateway.apikeys import ApplicationAccess
from gateway.app import CAPABILITIES_PATH, create_app, declaration_of
from gateway.auth import AccountAccess
from gateway.continuous import build_continuous_registry
from gateway.incidents import SqliteIncidentStore
from gateway.localonly import LocalOnlyPolicy
from gateway.permissions import NAVIGATION_ACTIONS, PERMISSIONS, Assignment, Level, ScopeType
from gateway.settings import SqliteSettingsStore
from gateway.state import run_now
from gateway.vault import Vault
from test_management_api import World, make_world

MASTER = b"m" * 32
FORBIDDEN = b'{"error":{"code":"forbidden","message":"Not permitted, or not found."}}'

Session = tuple[str, str]

# The route each navigation action's screen loads first: (method, path under the app). The
# route-pin test checks each one declares that action; the agreement test calls each GET.
ROUTES: dict[str, tuple[str, str]] = {
    "monitoring.read": ("GET", "/v1/monitor/snapshot"),
    "control.write": ("POST", "/v1/control/load/start"),
    "playground.use": ("POST", "/v1/playground/generate"),
    "users.list": ("GET", "/v1/admin/orgs/{org}/users"),
    "applications.list": ("GET", "/v1/admin/orgs/{org}/applications"),
    # The Add application form's first read (docs/51).
    "applications.create": ("GET", "/v1/admin/orgs/{org}/plans"),
    # The Applications list's figures (docs/51).
    "usage.read_organization": ("GET", "/v1/admin/orgs/{org}/metrics"),
    "org_settings.read": ("GET", "/v1/admin/orgs/{org}/settings"),
    "alert_channels.read": ("GET", "/v1/admin/orgs/{org}/alert-channels"),
    "incidents.read_organization": ("GET", "/v1/admin/orgs/{org}/incidents"),
    "applications.read": ("GET", "/v1/admin/orgs/{org}/applications/{app}"),
    "settings.read": ("GET", "/v1/admin/orgs/{org}/applications/{app}/settings"),
    "alert_rules.read": ("GET", "/v1/admin/orgs/{org}/applications/{app}/alert-rules"),
    "incidents.read": ("GET", "/v1/admin/orgs/{org}/applications/{app}/incidents"),
    "application_keys.list": ("GET", "/v1/admin/orgs/{org}/applications/{app}/keys"),
}

PLATFORM = frozenset(NAVIGATION_ACTIONS[Level.PLATFORM])
ORGANIZATION = frozenset(NAVIGATION_ACTIONS[Level.ORGANIZATION])
APPLICATION = frozenset(NAVIGATION_ACTIONS[Level.APPLICATION])
READS = frozenset({"applications.read", "settings.read", "alert_rules.read", "incidents.read"})


@dataclass(frozen=True)
class Expected:
    """One user's answer for their own organization: the allowed actions at each level, and
    for each application it names, the allowed actions there."""

    platform: frozenset[str] = frozenset()
    organization: frozenset[str] = frozenset()
    applications: tuple[tuple[str, frozenset[str]], ...] = ()


# docs/console-integration-plan.md, section 2.3, and docs/console-ia-v2-plan.md, section 2.3,
# for today's permission table.
EXPECTED: dict[str, Expected] = {
    "root": Expected(PLATFORM, frozenset({"users.list"})),
    "alice": Expected(
        organization=ORGANIZATION,
        applications=(("north-blog", APPLICATION), ("north-shop", APPLICATION)),
    ),
    "vera": Expected(
        organization=frozenset(
            {
                "applications.list",
                "usage.read_organization",
                "org_settings.read",
                "incidents.read_organization",
            }
        ),
        applications=(("north-blog", READS), ("north-shop", READS)),
    ),
    "nina": Expected(applications=(("north-shop", APPLICATION),)),
    "olive": Expected(applications=(("north-blog", READS),)),
    "cleo": Expected(),
    "bob": Expected(),
    "sid": Expected(organization=ORGANIZATION, applications=(("south-shop", APPLICATION),)),
    "sam": Expected(),
}
USERS = tuple(EXPECTED)
ORGANIZATIONS = ("north", "south", "default", "nowhere")
APPLICATIONS = ("north-shop", "north-blog", "south-shop", "ghost")


@dataclass
class Gateway:
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

    async def get(self, session: Session, path: str, **headers: str) -> httpx.Response:
        return await self.client.get(
            path, headers={"Cookie": f"gw_session={session[0]}", **headers}
        )

    async def capabilities(self, session: Session, organization: str | None = None) -> Any:
        answer = await self.get(session, _query(organization))
        assert answer.status_code == 200, answer.text
        return answer.json()


def _query(organization: str | None) -> str:
    return CAPABILITIES_PATH + ("" if organization is None else f"?organization={organization}")


def extend_world(world: World) -> None:
    """olive (viewer of north-blog only), cleo (clinic_staff on north-shop, granting nothing
    here), and the default organization's own application, so root has one it cannot read."""
    accounts = world.accounts
    service = accounts.service
    for name in ("olive", "cleo"):
        ready_user(accounts, name, PASSWORD, "north")

    async def setup() -> None:
        await service.grant_role("olive", "viewer", "application", "north-blog")
        await service.roles.define_app_role("north-shop", "clinic_staff", 0.0)
        await service.grant_role("cleo", "clinic_staff", "application", "north-shop")

    run_now(setup())


@asynccontextmanager
async def gateway(seed: int = 5) -> AsyncIterator[Gateway]:
    """Every route a navigation action's screen loads first: users, applications and keys,
    settings, alert rules, channels and incidents."""
    world = make_world()
    extend_world(world)
    accounts = world.accounts
    connection = accounts.connection
    store = SqliteChannelStore(connection)
    notices = Notices(
        store, SqliteOutboxStore(connection), accounts.organizations, accounts.epoch,
        random.Random(seed + 1).randbytes,
    )  # fmt: skip
    channels = ChannelService(
        store, Vault(MASTER), accounts.service.audit, accounts.epoch,
        random.Random(seed).randbytes, notices=notices,
    )  # fmt: skip
    registry = build_continuous_registry(accounts.clock, seed=7)
    app = create_app(
        registry,
        accounts.clock,
        applications=ApplicationAccess(world.keys, world.templates),
        accounts=AccountAccess(accounts.service),
        local_only=LocalOnlyPolicy(),
        epoch=accounts.epoch,
        rules=SqliteRuleStore(connection),
        incidents=SqliteIncidentStore(connection, accounts.epoch),
        channels=channels,
        settings=SqliteSettingsStore(connection),
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield Gateway(client, world, app)
    await registry.aclose()


def allowed_set(actions: dict[str, bool]) -> frozenset[str]:
    return frozenset(a for a, yes in actions.items() if yes)


def as_expected(answer: Any) -> Expected:
    return Expected(
        allowed_set(answer["platform"]),
        allowed_set(answer["organization"]),
        tuple(
            (a["application_id"], allowed_set(a["actions"]))
            for a in sorted(answer["applications"], key=lambda a: str(a["application_id"]))
        ),
    )


# --- the answer for every role ---------------------------------------------------------------


@pytest.mark.parametrize("username", USERS)
async def test_each_role_gets_the_navigation_of_the_plan(username: str) -> None:
    async with gateway() as g:
        answer = await g.capabilities(await g.login(username))
    assert as_expected(answer) == EXPECTED[username]
    # Every navigation action is answered, shown or hidden: none is left out.
    assert set(answer["platform"]) == PLATFORM
    assert set(answer["organization"]) == ORGANIZATION
    for application in answer["applications"]:
        assert set(application["actions"]) == APPLICATION
    assert answer["more_applications"] is False


async def test_the_roles_are_the_sessions_own_built_in_ones() -> None:
    async with gateway() as g:
        roles = {u: (await g.capabilities(await g.login(u)))["roles"] for u in USERS}
    assert roles["root"] == [{"role": "platform_admin", "scope_type": "platform", "scope_id": "*"}]
    assert roles["alice"] == [
        {"role": "org_admin", "scope_type": "organization", "scope_id": "north"}
    ]
    assert roles["olive"] == [
        {"role": "viewer", "scope_type": "application", "scope_id": "north-blog"}
    ]
    # An application-defined role is the application's to interpret: not shown, grants nothing.
    assert roles["cleo"] == []
    assert roles["bob"] == []


# --- agreement with the real routes ----------------------------------------------------------


def _path(action: str, organization: str, application: str = "") -> str:
    return ROUTES[action][1].replace("{org}", organization).replace("{app}", application)


@pytest.mark.parametrize("username", USERS)
async def test_the_answer_agrees_with_every_route_for_every_relation(username: str) -> None:
    """For every organization (own, another, missing) and every application path under it
    (its own, another organization's, missing): what the answer allows, the route answers 200;
    what it does not, the route answers 403 with the bytes of a missing resource."""
    disagreements: list[str] = []
    async with gateway() as g:
        session = await g.login(username)
        for organization in ORGANIZATIONS:
            answer = await g.capabilities(session, organization)
            for action in sorted(ORGANIZATION):
                route = await g.get(session, _path(action, organization))
                if not _agrees(answer["organization"][action], route):
                    disagreements.append(f"{organization} {action} {route.status_code}")
            listed = {a["application_id"]: a["actions"] for a in answer["applications"]}
            for application in APPLICATIONS:
                for action in sorted(APPLICATION):
                    allowed = listed.get(application, {}).get(action, False)
                    route = await g.get(session, _path(action, organization, application))
                    if not _agrees(allowed, route):
                        disagreements.append(
                            f"{organization}/{application} {action} {route.status_code}"
                        )
    assert disagreements == []


def _agrees(allowed: bool, route: httpx.Response) -> bool:
    if allowed:
        return route.status_code == 200
    return route.status_code == 403 and route.content == FORBIDDEN


async def test_every_navigation_action_is_declared_by_its_route_at_its_level() -> None:
    async with gateway() as g:
        app = g.app
    declared: dict[tuple[str, str], set[str]] = {}
    for route in app.routes:
        if isinstance(route, APIRoute):
            for method in route.methods or ():
                declared.setdefault((method, route.path), set()).update(
                    d.action for d in declaration_of(route) if d.action is not None
                )
    # Both directions: every navigation action has its route here, and every route here is
    # for a navigation action.
    every = [a for actions in NAVIGATION_ACTIONS.values() for a in actions]
    assert sorted(every) == sorted(ROUTES)
    assert len(every) == len(set(every))
    for level, actions in NAVIGATION_ACTIONS.items():
        for action in actions:
            assert PERMISSIONS[action].level is level, action
            assert declared.get(ROUTES[action], set()) == {action}, (action, ROUTES[action])


def test_no_navigation_action_needs_step_up() -> None:
    for actions in NAVIGATION_ACTIONS.values():
        for action in actions:
            assert not PERMISSIONS[action].step_up, action


# --- missing equals forbidden ----------------------------------------------------------------


@pytest.mark.parametrize("username", USERS)
async def test_a_missing_organization_and_a_forbidden_one_are_the_same_bytes(
    username: str,
) -> None:
    async with gateway() as g:
        session = await g.login(username)
        missing = await g.get(session, _query("nowhere"))
        # An organization this user may do nothing in: south for north's users, and north for
        # the others; root, who may list users anywhere, is checked below.
        other = "south" if username not in ("sid", "sam") else "north"
        forbidden = await g.get(session, _query(other))
        nonsense = await g.get(session, _query("..%2F"))
    assert missing.status_code == forbidden.status_code == nonsense.status_code == 200
    assert missing.content == nonsense.content
    if username != "root":
        assert missing.content == forbidden.content
    answer = json.loads(missing.content)
    assert not any(answer["organization"].values())
    assert answer["applications"] == []


async def test_for_root_a_missing_organization_is_one_it_may_do_nothing_in() -> None:
    """root may list users in every organization (its platform role), so an existing one shows
    that, and a missing one shows nothing: the same knowledge users.list already gives it."""
    async with gateway() as g:
        session = await g.login("root")
        north = await g.capabilities(session, "north")
        missing = await g.capabilities(session, "nowhere")
    assert allowed_set(north["organization"]) == {"users.list"}
    assert allowed_set(missing["organization"]) == frozenset()
    assert north["platform"] == missing["platform"]


async def test_an_assignment_to_an_application_that_is_gone_is_not_named() -> None:
    async with gateway() as g:
        accounts = g.world.accounts
        user = await accounts.store.find_user("olive")
        assert user is not None
        # Straight into the role store: the service would refuse an application that is gone.
        await accounts.service.roles.grant(
            user.id, Assignment("viewer", ScopeType.APPLICATION, "ghost"), None, 0.0
        )
        answer = await g.capabilities(await g.login("olive"))
    assert [a["application_id"] for a in answer["applications"]] == ["north-blog"]


# --- the session, a password to change, a disabled organization, fresh roles -------------------


async def test_no_session_and_an_unknown_session_are_401() -> None:
    async with gateway() as g:
        none = await g.client.get(CAPABILITIES_PATH)
        unknown = await g.get(("s" * 43, ""), CAPABILITIES_PATH)
    for answer in (none, unknown):
        assert answer.status_code == 401
        assert answer.json()["error"]["code"] == "session_required"
        assert answer.headers["cache-control"] == "no-store"


async def test_a_password_that_must_be_changed_is_refused_first() -> None:
    async with gateway() as g:
        await g.world.accounts.service.create_user("mona", PASSWORD, "north")
        await g.world.accounts.service.grant_role("mona", "org_admin", "organization", "north")
        session = await g.login("mona")
        answer = await g.get(session, CAPABILITIES_PATH)
    assert answer.status_code == 403
    assert answer.json()["error"]["code"] == "password_change_required"
    assert answer.headers["cache-control"] == "no-store"


async def test_a_disabled_organization_may_do_nothing() -> None:
    async with gateway() as g:
        accounts = g.world.accounts
        session = await g.login("alice")
        assert any((await g.capabilities(session))["organization"].values())
        await accounts.organizations.set_organization_disabled("north", True)
        answer = await g.capabilities(session)
    assert not any(answer["platform"].values())
    assert not any(answer["organization"].values())
    assert answer["applications"] == []


async def test_roles_are_read_on_every_request() -> None:
    async with gateway() as g:
        service = g.world.accounts.service
        session = await g.login("bob")
        assert as_expected(await g.capabilities(session)) == Expected()
        await service.grant_role("bob", "viewer", "organization", "north")
        assert as_expected(await g.capabilities(session)) == EXPECTED["vera"]
        await service.revoke_role("bob", "viewer", "organization", "north")
        assert as_expected(await g.capabilities(session)) == Expected()


async def test_the_organizations_applications_are_one_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "CAPABILITY_APPLICATIONS", 1)
    async with gateway() as g:
        answer = await g.capabilities(await g.login("alice"))
    assert [a["application_id"] for a in answer["applications"]] == ["north-blog"]
    assert answer["more_applications"] is True


# --- the guard and the cache ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("client", "headers", "code"),
    [
        (("10.0.0.5", 50000), {}, "local_only"),
        (("127.0.0.1", 50000), {"X-Forwarded-For": "127.0.0.1"}, "local_only"),
        (("127.0.0.1", 50000), {"Host": "gateway.example.com"}, "host_not_allowed"),
    ],
)
async def test_the_endpoint_is_local_only(
    client: tuple[str, int], headers: dict[str, str], code: str
) -> None:
    async with gateway() as g:
        session = await g.login("alice")
        transport = httpx.ASGITransport(app=g.app, client=client)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as remote:
            answer = await remote.get(
                CAPABILITIES_PATH, headers={"Cookie": f"gw_session={session[0]}", **headers}
            )
    assert answer.status_code == 403
    assert answer.json()["error"]["code"] == code
    assert answer.headers["cache-control"] == "no-store"


async def test_an_answer_is_never_cached() -> None:
    async with gateway() as g:
        answer = await g.get(await g.login("alice"), CAPABILITIES_PATH)
    assert answer.status_code == 200
    assert answer.headers["cache-control"] == "no-store"


async def test_nothing_is_audited() -> None:
    async with gateway() as g:
        session = await g.login("alice")
        connection = g.world.accounts.connection
        before = connection.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        for organization in ORGANIZATIONS:
            await g.capabilities(session, organization)
        after = connection.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    assert after == before


# --- the leak scan ----------------------------------------------------------------------------


async def test_no_session_id_csrf_token_or_password_leaks_and_the_scan_would_see_one(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    texts: dict[str, str] = {}
    secrets: dict[str, str] = {"password": PASSWORD}
    async with gateway() as g:
        for username in ("root", "alice", "olive", "bob"):
            session = await g.login(username)
            secrets[f"{username} session id"], secrets[f"{username} csrf"] = session
            for organization in (None, "north", "nowhere"):
                answer = await g.get(session, _query(organization))
                headers = [(k, v) for k, v in answer.headers.multi_items()]
                texts[f"{username} {organization}"] = f"{headers} {answer.text}"
        texts["refused"] = (await g.get(("x" * 43, ""), CAPABILITIES_PATH)).text
        connection = g.world.accounts.connection
        texts["audit_log"] = repr(connection.execute("SELECT * FROM audit_log").fetchall())

    def corpus() -> dict[str, str]:
        out, err = capsys.readouterr()
        captured["out"] += out
        captured["err"] += err
        return {
            **texts,
            "logs": "\n".join(f"{r.name} {r.getMessage()} {r.args!r}" for r in caplog.records),
            "stdout": captured["out"],
            "stderr": captured["err"],
        }

    def leaks() -> list[tuple[str, str]]:
        found = corpus()
        return [(w, n) for w, t in found.items() for n, s in secrets.items() if s and s in t]

    captured = {"out": "", "err": ""}
    assert len(secrets) == 9
    assert leaks() == []
    # The control: a session id logged by mistake, and a token printed, are found where they are.
    logging.getLogger("gateway").debug("oops: %s", secrets["alice session id"])
    print(f"and printed: {secrets['olive csrf']}")
    assert leaks() == [("logs", "alice session id"), ("stdout", "olive csrf")]
