"""Step-up: the endpoint, its refusals and caps, enforcement in requires for every flagged
action, what clears a window, and the checks that keep it from being bypassed. See
docs/29-step-up.md."""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import hashlib
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from accounts_helpers import (
    OTHER_PASSWORD,
    PASSWORD,
    Accounts,
    cookie,
    make_accounts,
    ready_user,
    with_csrf,
)
from gateway.app import create_app, local_only_route
from gateway.audit import AuditAction
from gateway.auth import (
    LOCK_FAILURES,
    MINUTE_MS,
    AccountAccess,
    StepUpFailedError,
    step_up_settings,
)
from gateway.clock import ClockEpoch, SimClock
from gateway.continuous import build_continuous_registry
from gateway.database import open_database
from gateway.localonly import LocalOnlyPolicy
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.permissions import PERMISSIONS, Level, Permission, Resource, Role

REPO = Path(__file__).resolve().parent.parent
FLAGGED = [action for action, permission in PERMISSIONS.items() if permission.step_up]
RESOURCE_FOR = {
    Level.PLATFORM: Resource.platform(),
    Level.ORGANIZATION: Resource.organization("north"),
    Level.APPLICATION: Resource.application("north", "north-shop"),
}
STEP_UP_REQUIRED = {
    "error": {
        "code": "step_up_required",
        "message": "This action needs a recent password: POST /v1/auth/step-up.",
    }
}


def _probe(action: str) -> Callable[[], Coroutine[Any, Any, dict[str, str]]]:
    async def handler() -> dict[str, str]:
        return {"handled": action}

    return handler


@dataclass
class Console:
    client: httpx.AsyncClient
    accounts: Accounts

    async def login(self, username: str, password: str = PASSWORD) -> tuple[str, str]:
        response = await self.client.post(
            "/v1/auth/login", json={"username": username, "password": password}
        )
        self.client.cookies.clear()
        assert response.status_code == 200, response.text
        sid = response.headers["set-cookie"].split(";")[0].split("=", 1)[1]
        return sid, response.json()["csrf_token"]

    async def step_up(
        self, session: tuple[str, str], password: str = PASSWORD, **headers: str
    ) -> httpx.Response:
        return await self.client.post(
            "/v1/auth/step-up",
            json={"password": password},
            headers={**with_csrf(*session), **headers},
        )

    async def act(self, session: tuple[str, str], action: str) -> httpx.Response:
        return await self.client.post(f"/v1/admin/probe/{action}", headers=with_csrf(*session))


@pytest.fixture
def accounts() -> Accounts:
    """north: alice and dora (org_admins), bob (no role); default: root (platform_admin)."""
    accounts = make_accounts()
    for name, org in (("alice", "north"), ("dora", "north"), ("bob", "north"), ("root", "default")):
        ready_user(accounts, name, PASSWORD, org)
    accounts.service.grant_role("alice", "org_admin", "organization", "north")
    accounts.service.grant_role("dora", "org_admin", "organization", "north")
    accounts.service.grant_role("root", "platform_admin", "platform", "*")
    return accounts


@asynccontextmanager
async def console(accounts: Accounts) -> AsyncIterator[Console]:
    """The gateway with accounts, and a route through requires for every action of the
    table, at /v1/admin/probe/<action>."""
    registry = build_continuous_registry(accounts.clock, seed=7)
    app = create_app(
        registry,
        accounts.clock,
        accounts=AccountAccess(accounts.service),
        local_only=LocalOnlyPolicy(),
        epoch=accounts.epoch,
    )
    requires = app.state.requires
    for action, permission in PERMISSIONS.items():
        resource = RESOURCE_FOR[permission.level]
        app.add_api_route(
            f"/v1/admin/probe/{action}",
            _probe(action),
            methods=["POST"],
            dependencies=[requires(action, lambda request, r=resource: r)],
        )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield Console(client, accounts)
    await registry.aclose()


def _who(action: str) -> str:
    """A user the table allows to do ``action``: alice (org_admin of north) where org_admin
    may, otherwise root (platform_admin)."""
    roles = {role for role, _ in PERMISSIONS[action].allowed}
    return "alice" if Role.ORG_ADMIN in roles else "root"


def _raw(response: httpx.Response) -> tuple[int, list[tuple[str, str]], bytes]:
    return response.status_code, sorted(response.headers.multi_items()), response.content


def _window(accounts: Accounts, session: tuple[str, str]) -> float | None:
    context = accounts.service.authenticate(session[0])
    return context.session.step_up_until_ms


# --- every flagged action is enforced ----------------------------------------------------------


async def test_every_flagged_action_needs_step_up_and_passes_with_it(accounts: Accounts) -> None:
    assert len(FLAGGED) == 18
    async with console(accounts) as c:
        for action in FLAGGED:
            session = await c.login(_who(action))
            refused = await c.act(session, action)
            assert (refused.status_code, refused.json()) == (403, STEP_UP_REQUIRED), action
            assert (await c.step_up(session)).status_code == 200
            served = await c.act(session, action)
            assert (served.status_code, served.json()) == (200, {"handled": action}), action
            await accounts.clock.sleep_ms(accounts.service.step_up_ms)
            again = await c.act(session, action)
            assert again.json() == STEP_UP_REQUIRED, action


async def test_an_action_not_flagged_needs_no_step_up(accounts: Accounts) -> None:
    async with console(accounts) as c:
        session = await c.login("alice")
        for action in ("applications.create", "users.list", "application_keys.list"):
            assert not PERMISSIONS[action].step_up
            assert (await c.act(session, action)).status_code == 200


async def test_authorization_comes_first_so_step_up_reveals_nothing(accounts: Accounts) -> None:
    async with console(accounts) as c:
        bob = await c.login("bob")  # no role at all
        refused = await c.act(bob, "users.create")
        assert refused.json()["error"]["code"] == "forbidden"  # not step_up_required
        await c.step_up(bob)
        assert (await c.act(bob, "users.create")).json()["error"]["code"] == "forbidden"


async def test_the_window_ends_at_exactly_five_minutes(accounts: Accounts) -> None:
    async with console(accounts) as c:
        session = await c.login("alice")
        granted = await c.step_up(session)
        until = granted.json()["step_up_until_ms"]
        assert until == accounts.epoch.epoch_ms() + 5 * MINUTE_MS
        await accounts.clock.sleep_ms(5 * MINUTE_MS - 0.001)
        assert (await c.act(session, "users.create")).status_code == 200
        await accounts.clock.sleep_ms(0.001)  # now exactly the end: closed
        assert (await c.act(session, "users.create")).json() == STEP_UP_REQUIRED
        me = await c.client.get("/v1/auth/me", headers=cookie(session[0]))
        assert me.json()["step_up_until_ms"] is None


# --- one session's, and cleared ----------------------------------------------------------------


async def test_step_up_belongs_to_one_session(accounts: Accounts) -> None:
    async with console(accounts) as c:
        first = await c.login("alice")
        await c.step_up(first)
        me = (await c.client.get("/v1/auth/me", headers=cookie(first[0]))).json()
        assert me["step_up_until_ms"] == accounts.epoch.epoch_ms() + 5 * MINUTE_MS
        second = await c.login("alice")  # a new login inherits nothing
        assert (await c.act(second, "users.create")).json() == STEP_UP_REQUIRED
        assert (await c.act(first, "users.create")).status_code == 200


async def test_logout_and_a_password_change_clear_it(accounts: Accounts) -> None:
    async with console(accounts) as c:
        session = await c.login("alice")
        await c.step_up(session)
        await c.client.post("/v1/auth/logout", headers=with_csrf(*session))
        assert _stored_window(accounts, session[0]) is None
        session = await c.login("alice")
        await c.step_up(session)
        changed = await c.client.post(
            "/v1/auth/change-password",
            json={"current_password": PASSWORD, "new_password": OTHER_PASSWORD},
            headers=with_csrf(*session),
        )
        assert changed.status_code == 200
        assert (await c.act(session, "users.create")).json() == STEP_UP_REQUIRED


async def test_a_revoked_session_cannot_use_its_window(accounts: Accounts) -> None:
    async with console(accounts) as c:
        other = await c.login("alice")
        await c.step_up(other)
        session = await c.login("alice")
        await c.client.post(
            "/v1/auth/change-password",
            json={"current_password": PASSWORD, "new_password": OTHER_PASSWORD},
            headers=with_csrf(*session),
        )
        # other was revoked by the password change: its window is gone with it.
        assert (await c.act(other, "users.create")).status_code == 401
        assert _stored_window(accounts, other[0]) is None


async def test_locking_disabling_or_a_role_change_clears_every_window(accounts: Accounts) -> None:
    async with console(accounts) as c:
        one, two = await c.login("alice"), await c.login("alice")
        await c.step_up(one)
        await c.step_up(two)
        accounts.service.grant_role("alice", "viewer", "organization", "north")
        assert (await c.act(one, "users.create")).json() == STEP_UP_REQUIRED
        assert (await c.act(two, "users.create")).json() == STEP_UP_REQUIRED
        await c.step_up(one)
        accounts.service.revoke_role("alice", "viewer", "organization", "north")
        assert (await c.act(one, "users.create")).json() == STEP_UP_REQUIRED
        # Locked by failed logins elsewhere: the windows close, the session survives.
        await c.step_up(one)
        for _ in range(LOCK_FAILURES):
            await c.client.post("/v1/auth/login", json={"username": "alice", "password": "x" * 13})
        assert (await c.act(one, "users.create")).json() == STEP_UP_REQUIRED
        assert (await c.client.get("/v1/auth/me", headers=cookie(one[0]))).status_code == 200
        # Disabling revokes every session; their windows go with them.
        dora = await c.login("dora")
        await c.step_up(dora)
        accounts.service.disable_user("dora")
        assert _stored_window(accounts, dora[0]) is None


# --- refusals ---------------------------------------------------------------------------------


async def test_every_refused_step_up_gets_the_same_bytes(accounts: Accounts) -> None:
    service = accounts.service
    async with console(accounts) as c:
        cases: dict[str, httpx.Response] = {}
        session = await c.login("alice")
        cases["wrong password"] = await c.step_up(session, "not the password at all")
        bob = await c.login("bob")
        for _ in range(5):  # the login limiter alone, the database lockout untouched
            service.limiter.record_failure(service.limiter.key("bob"))
        cases["limiter"] = await c.step_up(bob)
        dora = await c.login("dora")
        dora_user = accounts.store.find_user("dora")
        assert dora_user is not None
        accounts.store.set_login_state(dora_user.id, 0, None, accounts.epoch.epoch_ms() + MINUTE_MS)
        cases["locked"] = await c.step_up(dora)
        root = await c.login("root")
        root_user = accounts.store.find_user("root")
        assert root_user is not None
        accounts.store.set_password(
            root_user.id, root_user.password_hash, must_change=True, bootstrap=root_user.bootstrap
        )
        cases["must change password"] = await c.step_up(root)
        capped = await c.login("alice")
        for _ in range(3):
            await c.step_up(capped, "wrong again, and again")
        cases["session cap, right password"] = await c.step_up(capped)
    first = _raw(next(iter(cases.values())))
    assert first[0] == 403
    assert b"step_up_failed" in first[2]
    for name, response in cases.items():
        assert _raw(response) == first, name


async def test_a_wrong_password_counts_toward_the_lockout(accounts: Accounts) -> None:
    async with console(accounts) as c:
        sessions = [await c.login("alice") for _ in range(2)]
        for session, attempts in zip(sessions, (3, 2), strict=True):
            for _ in range(attempts):
                await c.step_up(session, "not the password at all")
        user = accounts.store.find_user("alice")
        assert user is not None
        assert user.locked_until_ms is not None  # five failures across two sessions
        refused = await c.client.post(
            "/v1/auth/login", json={"username": "alice", "password": PASSWORD}
        )
        assert refused.status_code == 401
    service = accounts.service
    assert service.limiter.blocked(service.limiter.key("alice"))


async def test_the_session_cap_refuses_without_verifying(
    accounts: Accounts, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifies: list[str] = []
    real = accounts.service._passwords.hasher.verify

    def counting(stored: str, password: str) -> bool:
        verifies.append("verify")
        return real(stored, password)

    monkeypatch.setattr(accounts.service._passwords.hasher, "verify", counting)
    async with console(accounts) as c:
        session = await c.login("alice")
        verifies.clear()
        for _ in range(3):
            assert (await c.step_up(session, "wrong")).status_code == 403
        assert len(verifies) == 3
        assert (await c.step_up(session)).status_code == 403  # the right password, capped
        assert len(verifies) == 3  # no verify ran
        other = await c.login("alice")  # the cap is per session
        assert (await c.step_up(other)).status_code == 200
        await accounts.clock.sleep_ms(5 * MINUTE_MS)
        assert (await c.step_up(session)).status_code == 200  # the cap's window has passed


async def test_no_password_is_verified_without_csrf_or_from_another_site(
    accounts: Accounts, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifies: list[str] = []

    def counting(stored: str, password: str) -> bool:
        verifies.append("verify")
        return False

    async with console(accounts) as c:
        sid, csrf = await c.login("alice")
        monkeypatch.setattr(accounts.service._passwords.hasher, "verify", counting)
        missing = await c.client.post(
            "/v1/auth/step-up", json={"password": PASSWORD}, headers=cookie(sid)
        )
        wrong = await c.client.post(
            "/v1/auth/step-up",
            json={"password": PASSWORD},
            headers={**cookie(sid), "X-CSRF-Token": "nope"},
        )
        foreign = await c.step_up((sid, csrf), Origin="https://evil.example")
    assert missing.json()["error"]["code"] == "csrf_failed"
    assert wrong.json()["error"]["code"] == "csrf_failed"
    assert foreign.json()["error"]["code"] == "origin_not_allowed"
    assert verifies == []


# --- the same steps, simulated time, audit -----------------------------------------------------


async def test_every_step_up_takes_the_same_steps(
    accounts: Accounts, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One verify against the session user's hash, then the decision, for a success and every
    refusal that verifies. The per-session cap refuses with no verify at all (docs/29)."""
    service = accounts.service
    steps: list[str] = []
    real = service._passwords.hasher.verify

    def verify(stored: str, password: str) -> bool:
        steps.append(stored.rsplit("$", 2)[0])
        return real(stored, password)

    monkeypatch.setattr(service._passwords.hasher, "verify", verify)
    contexts = {}
    for name in ("alice", "dora", "bob", "root"):
        login = await service.login(name, PASSWORD)
        contexts[name] = service.authenticate(login.session_id)
    dora = contexts["dora"].user
    locked = dataclasses.replace(dora, locked_until_ms=accounts.epoch.epoch_ms() + MINUTE_MS)
    must = dataclasses.replace(contexts["root"].user, must_change_password=True)
    for _ in range(5):
        service.limiter.record_failure(service.limiter.key("bob"))
    cases = {
        "success": (contexts["alice"], PASSWORD),
        "wrong password": (contexts["alice"], "not the password at all"),
        "locked": (dataclasses.replace(contexts["dora"], user=locked), PASSWORD),
        "limiter": (contexts["bob"], PASSWORD),
        "must change": (dataclasses.replace(contexts["root"], user=must), PASSWORD),
    }
    for name, (context, password) in cases.items():
        steps.clear()
        try:
            await service.step_up(context, password)
        except StepUpFailedError:
            assert name != "success"
        assert steps == ["$argon2id$v=19$m=8,t=1,p=1"], name


async def test_a_step_up_does_not_let_simulated_time_move(accounts: Accounts) -> None:
    service = accounts.service
    context = service.authenticate((await service.login("alice", PASSWORD)).session_id)
    woke: list[float] = []

    async def sleeper() -> None:
        await accounts.clock.sleep_ms(10.0)
        woke.append(accounts.clock.now_ms())

    pending = asyncio.create_task(sleeper())
    await accounts.clock.sleep_ms(0.0)
    await service.step_up(context, PASSWORD)
    with pytest.raises(StepUpFailedError):
        await service.step_up(context, "not the password at all")
    assert accounts.clock.now_ms() == 0.0
    assert woke == []
    await pending


async def test_step_up_is_audited_without_secrets(accounts: Accounts) -> None:
    async with console(accounts) as c:
        session = await c.login("alice")
        await c.act(session, "users.create")
        await c.step_up(session, "not the password at all")
        await c.step_up(session)
    events = [e.event for e in accounts.audit.entries()]
    required = [e for e in events if e.action is AuditAction.STEP_UP_REQUIRED]
    failed = [e for e in events if e.action is AuditAction.STEP_UP_FAILED]
    granted = [e for e in events if e.action is AuditAction.STEP_UP_GRANTED]
    assert [e.details for e in required] == [{"action": "users.create"}]
    assert [e.details for e in failed] == [{"reason": "wrong_password", "failures": 1}]
    assert len(granted) == 1
    assert set(granted[0].details) == {"step_up_until_ms"}
    assert all(e.target_id is not None and e.target_id.startswith("ses_") for e in granted)
    raw = repr(accounts.connection.execute("SELECT * FROM audit_log").fetchall())
    assert PASSWORD not in raw
    assert session[0] not in raw


# --- nothing can bypass requires ---------------------------------------------------------------


def test_local_only_route_refuses_a_flagged_action() -> None:
    with pytest.raises(ValueError, match="needs step-up"):
        local_only_route("users.create")
    local_only_route("monitoring.read")  # not flagged: fine


def test_the_table_has_no_consume_column_yet() -> None:
    # Consume-on-use (docs/29) is for A5; adding it must be a deliberate change to this test.
    assert [f.name for f in dataclasses.fields(Permission)] == [
        "action",
        "level",
        "allowed",
        "step_up",
        "wired",
    ]


def _app_source_violations(source: str) -> list[str]:
    """Imports or uses of ``authorize`` outside ``make_requires``, and any read of a session's
    step-up window, in ``app.py``."""
    tree = ast.parse(source)
    inside: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "make_requires":
            inside |= {id(child) for child in ast.walk(node)}
    found = []
    for node in ast.walk(tree):
        where = f"line {getattr(node, 'lineno', '?')}"
        if isinstance(node, ast.ImportFrom | ast.Import):
            names = {alias.name for alias in node.names} | {
                alias.asname for alias in node.names if alias.asname
            }
            if "authorize" in names and id(node) not in inside:
                found.append(f"{where}: imports authorize")
        elif isinstance(node, ast.Name) and node.id == "authorize" and id(node) not in inside:
            found.append(f"{where}: uses authorize")
        elif isinstance(node, ast.Attribute):
            if node.attr == "authorize" and id(node) not in inside:
                found.append(f"{where}: uses .authorize")
            if node.attr == "step_up_until_ms":
                found.append(f"{where}: reads step_up_until_ms")
    return found


def test_app_py_authorizes_only_inside_requires() -> None:
    source = (REPO / "src" / "gateway" / "app.py").read_text(encoding="utf-8")
    assert _app_source_violations(source) == []


@pytest.mark.parametrize(
    "planted",
    [
        "from gateway.permissions import authorize\n",
        "from gateway.permissions import authorize as allowed\n",
        "def handler(actor, resource):\n    return authorize(actor, 'users.create', resource)\n",
        "def handler(permissions):\n    return permissions.authorize\n",
        "def handler(context):\n    return context.session.step_up_until_ms\n",
    ],
)
def test_the_check_finds_a_planted_bypass(planted: str) -> None:
    source = (REPO / "src" / "gateway" / "app.py").read_text(encoding="utf-8")
    assert _app_source_violations(source + "\n\n" + planted) != []


# --- configuration, and migration 9 ------------------------------------------------------------


def test_step_up_settings_come_from_the_environment() -> None:
    assert step_up_settings({}) == (5.0, 3)
    assert step_up_settings(
        {"GATEWAY_STEP_UP_MINUTES": "10", "GATEWAY_STEP_UP_SESSION_FAILURES": "5"}
    ) == (10.0, 5)
    for bad in (
        {"GATEWAY_STEP_UP_MINUTES": "0"},
        {"GATEWAY_STEP_UP_MINUTES": "61"},
        {"GATEWAY_STEP_UP_SESSION_FAILURES": "0"},
        {"GATEWAY_STEP_UP_MINUTES": "soon"},
    ):
        with pytest.raises(ValueError, match="GATEWAY_STEP_UP"):
            step_up_settings(bad)


def test_migration_9_adds_the_column_and_its_reverse_keeps_every_session(tmp_path: Path) -> None:
    connection = open_database(tmp_path / "gateway.db")
    clock = SimClock()
    runner = MigrationRunner(connection, MIGRATIONS, ClockEpoch(clock))
    runner.migrate(8)
    connection.execute(
        "INSERT INTO users (id, username, password_hash, organization_id, created_at_ms)"
        " VALUES ('usr_0000000000000001', 'alice', 'h', 'default', 0)"
    )
    connection.execute(
        "INSERT INTO sessions (id_hash, handle, user_id, csrf_hash, created_at_ms,"
        " last_seen_at_ms, expires_at_ms) VALUES ('h', 'ses_0000000000000001',"
        " 'usr_0000000000000001', 'c', 1, 2, 3)"
    )
    before = connection.execute("SELECT * FROM sessions").fetchall()
    assert runner.migrate() == (9,)
    assert runner.migrate() == ()
    assert connection.execute("SELECT step_up_until_ms FROM sessions").fetchall() == [(None,)]
    assert runner.rollback(8) == (9,)
    assert connection.execute("SELECT * FROM sessions").fetchall() == before
    columns = [r[1] for r in connection.execute("PRAGMA table_info(sessions)")]
    assert "step_up_until_ms" not in columns
    indexes = {r[1] for r in connection.execute("PRAGMA index_list(sessions)")}
    assert {"sessions_by_user", "sessions_by_expiry"} <= indexes
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()


def _stored_window(accounts: Accounts, session_id: str) -> float | None:
    row = accounts.connection.execute(
        "SELECT step_up_until_ms FROM sessions WHERE id_hash = ?",
        (hashlib.sha256(session_id.encode()).hexdigest(),),
    ).fetchone()
    assert row is not None
    return None if row[0] is None else float(row[0])
