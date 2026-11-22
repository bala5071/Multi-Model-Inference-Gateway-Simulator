"""The platform owner can neither create a client's users nor reset their passwords, and the
operator's CLI still recovers a locked-out admin. See docs/28-roles.md."""

from __future__ import annotations

import logging
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from accounts_helpers import PASSWORD
from gateway.audit import AuditAction, AuditLog, SqliteAuditStore
from gateway.auth import AccountService, LoginFailedError
from gateway.clock import SystemEpoch
from gateway.database import open_database
from gateway.management import NotPermittedError
from gateway.permissions import PERMISSIONS, Resource, authorize
from gateway.state import run_now, sqlite_state
from gateway.users import User
from helpers import TEST_PEPPER
from test_management_api import _raw, admin_console, make_world
from test_secret_leaks import _table, find_leaks

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from manage import _accounts
from manage import main as manage

RECOVERY_PASSWORD = "walnut ferry twenty herons"
FINAL_PASSWORD = "cedar anchor fifty larks"
WRONG_PASSWORD = "not the password at all"

CREATE_PATHS = (
    "/north/users",  # a client
    "/solo/users",  # an empty individual organization: the seat that makes an org_admin
    "/default/users",  # the owner's own organization
    "/nowhere/users",  # missing
)
RESET_PATHS = (
    "/north/users/alice/reset-password",  # a client's org_admin
    "/north/users/bob/reset-password",  # a client's member
    "/north/users/nobody/reset-password",  # no such user
    "/south/users/alice/reset-password",  # a user of another organization
    "/nowhere/users/alice/reset-password",  # no such organization
    "/default/users/root/reset-password",  # its own account
)


def test_the_two_rows_are_org_admin_only() -> None:
    for action in ("users.create", "users.reset_password"):
        assert sorted(f"{r.value}@{s.value}" for r, s in PERMISSIONS[action].allowed) == [
            "org_admin@own_organization"
        ]


@pytest.mark.parametrize("with_step_up", [False, True])
async def test_the_owner_is_refused_with_the_bytes_of_a_forbidden_request(
    with_step_up: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = make_world()
    accounts = world.accounts
    made: list[str] = []
    draw = AccountService._temporary_password

    def counted(self: AccountService, username: str) -> str:
        made.append(username)
        return draw(self, username)

    monkeypatch.setattr(AccountService, "_temporary_password", counted)
    alice_before = await accounts.store.find_user("alice")
    users_before = accounts.connection.execute("SELECT COUNT(*) FROM users").fetchone()
    async with admin_console(world) as admin:
        alice = await admin.login("alice")
        bob = await admin.login("bob")
        await admin.step_up(bob)
        forbidden = _raw(await admin.post(bob, "/north/users", {"username": "owner-made"}))
        assert forbidden[0] == 403
        assert b'"forbidden"' in forbidden[2]
        root = await admin.login("root")
        if with_step_up:
            await admin.step_up(root)
        audit_before = len(await accounts.audit.entries())
        for path in CREATE_PATHS:
            answer = await admin.post(root, path, {"username": "owner-made"})
            assert _raw(answer) == forbidden, path
        for path in RESET_PATHS:
            assert _raw(await admin.post(root, path)) == forbidden, path
        # Nothing happened: no row, no password made, alice's session still works.
        assert len(await accounts.audit.entries()) == audit_before
        assert (await admin.get(alice, "/north/users")).status_code == 200
    assert made == []
    assert await accounts.store.find_user("owner-made") is None
    assert accounts.connection.execute("SELECT COUNT(*) FROM users").fetchone() == users_before
    alice_after = await accounts.store.find_user("alice")
    assert alice_before is not None
    assert alice_after is not None
    assert alice_after.password_hash == alice_before.password_hash
    assert alice_after.must_change_password is False


async def test_behind_the_route_the_service_refuses_the_owner_too() -> None:
    world = make_world()
    service = world.accounts.service
    root = await service.actor_named("root")
    before = len(await world.accounts.audit.entries())
    with pytest.raises(NotPermittedError, match=r"users\.create"):
        await service.create_user_with_temporary_password("owner-made", "solo", actor=root)
    with pytest.raises(NotPermittedError, match=r"users\.reset_password"):
        await service.reset_to_temporary_password("alice", actor=root)
    with pytest.raises(NotPermittedError, match=r"users\.reset_password"):
        await service.reset_password("alice", RECOVERY_PASSWORD, actor=root)
    assert len(await world.accounts.audit.entries()) == before


async def test_an_org_admin_still_creates_and_resets_in_its_own_organization() -> None:
    """The control for the refusals above: the same routes, the role that keeps them."""
    world = make_world()
    async with admin_console(world) as admin:
        alice = await admin.login("alice")
        await admin.step_up(alice)
        made = await admin.post(alice, "/north/users", {"username": "newbie"})
        assert made.status_code == 201
        reset = await admin.post(alice, "/north/users/bob/reset-password")
        assert reset.status_code == 200


async def test_the_owner_keeps_only_the_user_actions_that_hand_it_no_credential() -> None:
    world = make_world()
    root = await world.accounts.service.actor_named("root")
    client = Resource.organization("north")
    allowed = {
        action
        for action in PERMISSIONS
        if action.startswith("users.") and authorize(root, action, client).allowed
    }
    assert allowed == {"users.disable", "users.enable", "users.list", "users.unlock"}


# --- the CLI, the recovery path ------------------------------------------------------------------


@pytest.fixture
def environ(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> dict[str, str]:
    """root: platform_admin in default. north: alice, its one org_admin."""
    values = {
        "GATEWAY_DATABASE": str(tmp_path / "gateway.db"),
        "GATEWAY_KEY_PEPPER": TEST_PEPPER.decode(),
        "GATEWAY_ARGON2_MEMORY_KIB": "8",
        "GATEWAY_ARGON2_TIME_COST": "1",
    }
    for line in (
        "create-user --username root --platform-admin",
        "create-organization --id north --name North --kind business",
        "create-user --username alice --organization north",
        "grant-role --username alice --role org_admin --organization north",
    ):
        assert manage(line.split(), values, _answers(PASSWORD, PASSWORD)) == 0, line
    capsys.readouterr()
    return values


def _answers(*answers: str) -> Callable[[str], str]:
    queue = list(answers)

    def read(_: str) -> str:
        return queue.pop(0)

    return read


def _service(connection: sqlite3.Connection, values: dict[str, str]) -> AccountService:
    return _accounts(sqlite_state(connection, SystemEpoch()), values)


def _locked_out(values: dict[str, str]) -> None:
    """Five wrong passwords: alice's account is locked in the database, and the right password
    is refused."""
    connection = open_database(values["GATEWAY_DATABASE"])
    try:
        service = _service(connection, values)
        for _ in range(5):
            with pytest.raises(LoginFailedError):
                run_now(service.login("alice", WRONG_PASSWORD))
        user = run_now(service.store.find_user("alice"))
        assert user is not None
        assert user.locked_until_ms is not None
        fresh = _service(connection, values)  # no limiter state: the database lock alone
        with pytest.raises(LoginFailedError):
            run_now(fresh.login("alice", PASSWORD))
    finally:
        connection.close()


def _found(values: dict[str, str]) -> User:
    connection = open_database(values["GATEWAY_DATABASE"])
    try:
        user = run_now(_service(connection, values).store.find_user("alice"))
    finally:
        connection.close()
    assert user is not None
    return user


def test_the_cli_unlocks_a_locked_out_admin(environ: dict[str, str]) -> None:
    _locked_out(environ)
    assert manage(["unlock-user", "--username", "alice"], environ) == 0
    assert _found(environ).locked_until_ms is None
    connection = open_database(environ["GATEWAY_DATABASE"])
    try:
        result = run_now(_service(connection, environ).login("alice", PASSWORD))
    finally:
        connection.close()
    assert result.user.username == "alice"


def test_the_cli_resets_a_locked_out_admin_who_lost_the_password(
    environ: dict[str, str],
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The whole recovery, and the leak scan over everything it leaves behind."""
    caplog.set_level(logging.DEBUG)
    _locked_out(environ)
    answers = _answers(RECOVERY_PASSWORD, RECOVERY_PASSWORD)
    assert manage(["reset-password", "--username", "alice"], environ, answers) == 0
    recovered = _found(environ)
    assert recovered.locked_until_ms is None
    assert recovered.must_change_password is True

    connection = open_database(environ["GATEWAY_DATABASE"])
    try:
        service = _service(connection, environ)
        with pytest.raises(LoginFailedError):
            run_now(service.login("alice", PASSWORD))  # the old password no longer works
        service = _service(connection, environ)
        login = run_now(service.login("alice", RECOVERY_PASSWORD))
        context = run_now(service.authenticate(login.session_id))
        run_now(service.change_password(context, RECOVERY_PASSWORD, FINAL_PASSWORD))
        changed = run_now(service.store.find_user("alice"))
        assert changed is not None
        assert changed.must_change_password is False
        actor = run_now(service.actor_named("alice"))
        assert authorize(actor, "users.create", Resource.organization("north")).allowed
        entries = run_now(AuditLog(SqliteAuditStore(connection)).entries())
        [reset] = [e.event for e in entries if e.event.action is AuditAction.PASSWORD_RESET]
        assert reset.actor_user_id is None  # the operator, at the CLI
        assert reset.target_id == recovered.id

        secrets = {
            "recovery password": RECOVERY_PASSWORD,
            "final password": FINAL_PASSWORD,
            "wrong password": WRONG_PASSWORD,
            "session id": login.session_id,
            "csrf token": login.csrf_token,
        }
        out, err = capsys.readouterr()
        logs = "\n".join(f"{r.name} {r.getMessage()} {r.args!r}" for r in caplog.records)
        corpus = {
            "stdout": out,
            "stderr": err,
            "logs": logs,
            "audit_log": _table(connection, "audit_log", set()),
            "users": _table(connection, "users", {"password_hash"}),
            "sessions": _table(connection, "sessions", {"id_hash", "csrf_hash"}),
        }
        assert find_leaks(corpus, secrets) == []
        # The control: a password printed or logged by mistake is found.
        logging.getLogger("gateway").info("reset with %s", RECOVERY_PASSWORD)
        print(f"changed to {FINAL_PASSWORD}")
        out, _ = capsys.readouterr()
        corpus["stdout"] = out
        corpus["logs"] = "\n".join(f"{r.name} {r.getMessage()} {r.args!r}" for r in caplog.records)
        assert find_leaks(corpus, secrets) == [
            ("stdout", "final password"),
            ("logs", "recovery password"),
        ]
    finally:
        connection.close()


def test_as_the_owner_the_cli_cannot_reset_a_clients_admin(
    environ: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    before = _found(environ).password_hash
    answers = _answers(RECOVERY_PASSWORD, RECOVERY_PASSWORD)
    assert manage(["reset-password", "--username", "alice", "--as", "root"], environ, answers) == 1
    assert "not permitted: users.reset_password" in capsys.readouterr().err
    assert _found(environ).password_hash == before


def test_a_running_servers_limiter_outlasts_the_cli_recovery(environ: dict[str, str]) -> None:
    """The limit docs/28 states: the CLI clears the database lockout, but the limiter of a server
    already running is in that server's memory, so it can still refuse the right password."""
    connection = open_database(environ["GATEWAY_DATABASE"])
    try:
        running = _service(connection, environ)
        for _ in range(5):
            with pytest.raises(LoginFailedError):
                run_now(running.login("alice", WRONG_PASSWORD))
        assert manage(["unlock-user", "--username", "alice"], environ) == 0
        with pytest.raises(LoginFailedError):
            run_now(running.login("alice", PASSWORD))
        restarted = _service(connection, environ)
        assert run_now(restarted.login("alice", PASSWORD)).user.username == "alice"
    finally:
        connection.close()
