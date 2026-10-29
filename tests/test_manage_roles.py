"""Roles through the CLI: --as checked against the actor's roles, who must change their
password, the escalation rules and the last admin. See docs/28-roles.md."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from gateway.audit import AuditLog, SqliteAuditStore
from gateway.database import open_database
from gateway.roles import SqliteRoleStore
from gateway.state import run_now
from gateway.users import SqliteAccountStore, User
from helpers import TEST_PEPPER

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from manage import main as manage

PASSWORD = "plum harbor ninety violet"


@pytest.fixture
def environ(tmp_path: Path) -> dict[str, str]:
    return {
        "GATEWAY_DATABASE": str(tmp_path / "gateway.db"),
        "GATEWAY_KEY_PEPPER": TEST_PEPPER.decode(),
        "GATEWAY_ARGON2_MEMORY_KIB": "8",
        "GATEWAY_ARGON2_TIME_COST": "1",
    }


def _prompt(_: str) -> str:
    return PASSWORD


def run(environ: dict[str, str], line: str) -> int:
    return manage(line.split(), environ, _prompt)


def _user(environ: dict[str, str], username: str) -> User:
    connection = open_database(environ["GATEWAY_DATABASE"])
    try:
        user = run_now(SqliteAccountStore(connection).find_user(username))
    finally:
        connection.close()
    assert user is not None
    return user


@pytest.fixture
def world(environ: dict[str, str], capsys: pytest.CaptureFixture[str]) -> dict[str, str]:
    """root: platform_admin in default. north: alice (org_admin), bob (no role), and the
    application north-shop. south: sid (org_admin). solo: an individual organization."""
    for line in (
        "create-user --username root --platform-admin",
        "create-organization --id north --name North --kind business",
        "create-organization --id south --name South --kind business",
        "create-organization --id solo --name Solo --kind individual",
        "create-user --username alice --organization north",
        "create-user --username bob --organization north",
        "create-user --username sid --organization south",
        "create-application --id north-shop --name Shop --plan pro --organization north",
        "grant-role --username alice --role org_admin --organization north",
        "grant-role --username sid --role org_admin --organization south",
    ):
        assert run(environ, line) == 0, line
    capsys.readouterr()
    return environ


# --- who must change their password -----------------------------------------------------------


def test_accounts_made_by_an_outsider_must_change_their_password(
    world: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    # The operator (no --as) is a member of no organization: alice, bob and sid already
    # had to change theirs (the fixture made them that way).
    assert _user(world, "bob").must_change_password is True
    # A platform_admin outside the organization: the new account must change its password.
    assert run(world, "create-user --username carl --organization north --as root") == 0
    assert _user(world, "carl").must_change_password is True
    assert "must change its password at first login" in capsys.readouterr().err
    # The organization's own org_admin: it need not.
    assert run(world, "create-user --username dina --organization north --as alice") == 0
    assert _user(world, "dina").must_change_password is False
    # A platform_admin who is a member of the organization (root is in default): need not.
    assert run(world, "create-user --username erik --organization default --as root") == 0
    assert _user(world, "erik").must_change_password is False
    # Nobody else may create users there.
    assert run(world, "create-user --username fay --organization north --as bob") == 1
    assert run(world, "create-user --username fay --organization north --as sid") == 1
    assert "not permitted: users.create" in capsys.readouterr().err


def test_the_creation_is_audited_with_the_operator_acting_as_a_user(
    world: dict[str, str],
) -> None:
    assert run(world, "create-user --username carl --organization north --as root") == 0
    connection = open_database(world["GATEWAY_DATABASE"])
    root = run_now(SqliteAccountStore(connection).find_user("root"))
    carl = run_now(SqliteAccountStore(connection).find_user("carl"))
    entries = run_now(AuditLog(SqliteAuditStore(connection)).entries())
    connection.close()
    assert root is not None
    assert carl is not None
    [created] = [
        e.event
        for e in entries
        if e.event.target_id == carl.id and e.event.action.value == "user_created"
    ]
    # The operator acted (no user is the actor), within root's roles (acting_as).
    assert created.actor_user_id is None
    assert created.details["acting_as"] == root.id
    assert created.details["must_change_password"] is True
    assert created.details["organization_id"] == "north"


# --- granting ---------------------------------------------------------------------------------


def test_grant_list_and_revoke(world: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        run(world, "grant-role --username bob --role viewer --organization north --as alice") == 0
    )
    assert (
        run(world, "grant-role --username bob --role app_admin --application north-shop --as alice")
        == 0
    )
    capsys.readouterr()
    assert run(world, "list-roles --username bob --as alice") == 0
    assert capsys.readouterr().out.splitlines() == [
        "viewer  organization  north",
        "app_admin  application  north-shop",
    ]
    assert (
        run(world, "revoke-role --username bob --role viewer --organization north --as alice") == 0
    )
    assert run(world, "list-roles --username bob") == 0
    assert capsys.readouterr().out.splitlines() == ["app_admin  application  north-shop"]
    # Not twice, and not a role in a scope outside the user's organization.
    assert run(world, "revoke-role --username bob --role viewer --organization north") == 1
    assert run(world, "grant-role --username bob --role viewer --organization south") == 1
    assert "only within the user's own organization" in capsys.readouterr().err


def test_an_app_admin_cannot_grant_org_admin(world: dict[str, str]) -> None:
    assert run(world, "grant-role --username bob --role app_admin --application north-shop") == 0
    assert run(world, "create-user --username carl --organization north") == 0
    assert (
        run(world, "grant-role --username carl --role org_admin --organization north --as bob") == 1
    )
    assert run(world, "grant-role --username carl --role viewer --organization north --as bob") == 1


def test_an_org_admin_cannot_grant_outside_its_organization(
    world: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        run(world, "grant-role --username sid --role viewer --organization south --as alice") == 1
    )
    assert "not permitted: roles.assign" in capsys.readouterr().err
    assert run(world, "list-roles --username sid --as alice") == 1


def test_platform_admin_is_granted_only_by_the_operator(
    world: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    for actor in ("root", "alice"):
        assert (
            run(world, f"grant-role --username bob --role platform_admin --platform --as {actor}")
            == 1
        )
    assert run(world, "create-user --username zed --platform-admin --as root") == 1
    assert "only by the operator" in capsys.readouterr().err
    assert run(world, "grant-role --username bob --role platform_admin --platform") == 0


def test_a_platform_admin_appoints_org_admins_and_nothing_else(world: dict[str, str]) -> None:
    assert (
        run(world, "grant-role --username bob --role org_admin --organization north --as root") == 0
    )
    assert run(world, "grant-role --username bob --role viewer --organization north --as root") == 1
    assert (
        run(world, "grant-role --username bob --role app_admin --application north-shop --as root")
        == 1
    )


def test_application_roles_must_be_defined_and_grant_nothing(
    world: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(world, "grant-role --username bob --role clinic_staff --application north-shop") == 1
    assert "defines no role 'clinic_staff'" in capsys.readouterr().err
    connection = open_database(world["GATEWAY_DATABASE"])
    run_now(SqliteRoleStore(connection).define_app_role("north-shop", "clinic_staff", 0.0))
    connection.close()
    assert run(world, "grant-role --username bob --role clinic_staff --application north-shop") == 0
    # It grants nothing in the gateway: bob still cannot list north's roles.
    assert run(world, "list-roles --username alice --as bob") == 1
    assert run(world, "grant-role --username bob --role clinic_staff --organization north") == 1


# --- the last admin ---------------------------------------------------------------------------


def test_the_last_org_admin_cannot_be_revoked_or_disabled(
    world: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(world, "revoke-role --username alice --role org_admin --organization north") == 1
    assert "last org_admin of organization 'north'" in capsys.readouterr().err
    assert run(world, "disable-user --username alice") == 1
    assert _user(world, "alice").disabled is False
    # With a second org_admin, either can go.
    assert run(world, "grant-role --username bob --role org_admin --organization north") == 0
    assert run(world, "revoke-role --username alice --role org_admin --organization north") == 0
    # A disabled org_admin does not count: bob is now the last enabled one.
    assert run(world, "grant-role --username alice --role org_admin --organization north") == 0
    assert run(world, "disable-user --username alice") == 0
    assert run(world, "disable-user --username bob") == 1


def test_the_last_platform_admin_cannot_be_revoked_or_disabled(world: dict[str, str]) -> None:
    assert run(world, "revoke-role --username root --role platform_admin --platform") == 1
    assert run(world, "disable-user --username root") == 1
    assert run(world, "grant-role --username bob --role platform_admin --platform") == 0
    assert run(world, "revoke-role --username root --role platform_admin --platform") == 0


def test_an_individual_organizations_member_is_its_org_admin(
    world: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(world, "create-user --username indy --organization solo") == 0
    capsys.readouterr()
    assert run(world, "list-roles --username indy") == 0
    assert capsys.readouterr().out.splitlines() == ["org_admin  organization  solo"]
    # The one member is also the last org_admin: disable the organization instead.
    assert run(world, "disable-user --username indy") == 1


# --- --as only narrows --------------------------------------------------------------------------

WORLD = (
    "create-user --username root --platform-admin",
    "create-organization --id north --name North --kind business",
    "create-organization --id south --name South --kind business",
    "create-user --username alice --organization north",
    "create-user --username bob --organization north",
    "create-user --username carol --organization north",
    "create-user --username sid --organization south",
    "create-application --id north-shop --name Shop --plan pro --organization north",
    "grant-role --username alice --role org_admin --organization north",
    "grant-role --username carol --role org_admin --organization north",
    "grant-role --username sid --role org_admin --organization south",
)
COMMANDS = (
    "create-user --username new --organization north",
    "create-user --username new --organization south",
    "grant-role --username bob --role viewer --organization north",
    "grant-role --username bob --role org_admin --organization north",
    "grant-role --username bob --role platform_admin --platform",
    "revoke-role --username carol --role org_admin --organization north",
    "disable-user --username bob",
    "reset-password --username sid",
    "list-roles --username sid",
)


def _fresh(tmp_path: Path, name: str) -> dict[str, str]:
    environ = {
        "GATEWAY_DATABASE": str(tmp_path / f"{name}.db"),
        "GATEWAY_KEY_PEPPER": TEST_PEPPER.decode(),
        "GATEWAY_ARGON2_MEMORY_KIB": "8",
        "GATEWAY_ARGON2_TIME_COST": "1",
    }
    for line in WORLD:
        assert run(environ, line) == 0, line
    return environ


def test_as_only_narrows_what_the_operator_may_do(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Each command, in two identical fresh databases: as the operator, and --as a user.
    Nothing succeeds with --as that fails without it; some things the operator may do fail
    with it."""
    narrowed = []
    for number, (command, actor) in enumerate(
        (c, a) for c in COMMANDS for a in ("alice", "bob", "root", "sid")
    ):
        as_operator = run(_fresh(tmp_path, f"op{number}"), command)
        as_user = run(_fresh(tmp_path, f"as{number}"), f"{command} --as {actor}")
        assert as_operator == 0, command  # the operator may do every one of them
        assert not (as_user == 0 and as_operator != 0), (command, actor)
        if as_operator == 0 and as_user != 0:
            narrowed.append((command, actor))
    capsys.readouterr()
    # Every command works for the operator; --as refuses some to every user.
    assert {actor for _, actor in narrowed} == {"alice", "bob", "root", "sid"}
    assert ("grant-role --username bob --role platform_admin --platform", "root") in narrowed


def test_a_role_is_held_only_within_the_users_own_organization(
    world: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        run(world, "create-application --id south-shop --name Shop --plan pro --organization south")
        == 0
    )
    capsys.readouterr()
    # bob is in north: neither south itself nor an application of south, even by the operator.
    assert run(world, "grant-role --username bob --role viewer --organization south") == 1
    assert run(world, "grant-role --username bob --role app_admin --application south-shop") == 1
    assert capsys.readouterr().err.count("only within the user's own organization") == 2
    # In his own organization, both are fine.
    assert run(world, "grant-role --username bob --role viewer --organization north") == 0
    assert run(world, "grant-role --username bob --role app_admin --application north-shop") == 0
