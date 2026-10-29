"""The CLI for console accounts: passwords only from a prompt, never printed. See
docs/27-users-and-sessions.md."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from gateway.audit import AuditLog, SqliteAuditStore
from gateway.database import open_database
from gateway.state import run_now
from gateway.users import SqliteAccountStore, User
from helpers import TEST_PEPPER

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from manage import main as manage

PASSWORD = "plum harbor ninety violet"
OTHER = "seven copper kites at dawn"


@pytest.fixture
def environ(tmp_path: Path) -> dict[str, str]:
    return {
        "GATEWAY_DATABASE": str(tmp_path / "gateway.db"),
        "GATEWAY_KEY_PEPPER": TEST_PEPPER.decode(),
        # The lowest argon2 cost, so the CLI's hashing stays fast in tests.
        "GATEWAY_ARGON2_MEMORY_KIB": "8",
        "GATEWAY_ARGON2_TIME_COST": "1",
    }


def prompt(*answers: str) -> Callable[[str], str]:
    """A password prompt that answers from a list."""
    queue = list(answers)

    def read(question: str) -> str:
        return queue.pop(0)

    return read


def _user(environ: dict[str, str], username: str) -> User | None:
    connection = open_database(environ["GATEWAY_DATABASE"])
    try:
        return run_now(SqliteAccountStore(connection).find_user(username))
    finally:
        connection.close()


def _existing(environ: dict[str, str], username: str) -> User:
    user = _user(environ, username)
    assert user is not None
    return user


def test_create_user_reads_the_password_twice_and_never_prints_it(
    environ: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert manage(["create-user", "--username", "Alice"], environ, prompt(PASSWORD, PASSWORD)) == 0
    out, err = capsys.readouterr()
    assert out == ""
    assert "created user 'alice' in organization 'default'" in err
    assert PASSWORD not in out + err
    # Mismatched entries change nothing.
    assert manage(["create-user", "--username", "bob"], environ, prompt(PASSWORD, OTHER)) == 1
    out, err = capsys.readouterr()
    assert "differ" in err
    assert PASSWORD not in out + err
    assert OTHER not in out + err
    assert _user(environ, "bob") is None


def test_the_policy_refusal_names_the_rule_not_the_password(
    environ: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    weak = "carol likes long walks"
    assert manage(["create-user", "--username", "carol"], environ, prompt(weak, weak)) == 1
    out, err = capsys.readouterr()
    assert "may not contain the username" in err
    assert weak not in out + err


def test_platform_admins_and_individual_organizations(
    environ: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        manage(
            ["create-user", "--username", "root", "--platform-admin"],
            environ,
            prompt(PASSWORD, PASSWORD),
        )
        == 0
    )
    assert "(platform_admin)" in capsys.readouterr().err
    assert (
        manage(
            ["create-organization", "--id", "solo", "--name", "Solo", "--kind", "individual"],
            environ,
        )
        == 0
    )
    assert (
        manage(
            ["create-user", "--username", "only", "--organization", "solo"],
            environ,
            prompt(PASSWORD, PASSWORD),
        )
        == 0
    )
    capsys.readouterr()
    assert (
        manage(
            ["create-user", "--username", "second", "--organization", "solo"],
            environ,
            prompt(PASSWORD, PASSWORD),
        )
        == 1
    )
    assert "already has its one member" in capsys.readouterr().err


def test_disable_enable_reset_and_unlock(
    environ: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert manage(["create-user", "--username", "alice"], environ, prompt(PASSWORD, PASSWORD)) == 0
    assert manage(["disable-user", "--username", "alice"], environ) == 0
    assert _existing(environ, "alice").disabled is True
    assert manage(["enable-user", "--username", "alice"], environ) == 0
    assert _existing(environ, "alice").disabled is False
    connection = open_database(environ["GATEWAY_DATABASE"])
    user_id = run_now(SqliteAccountStore(connection).find_user("alice")).id  # type: ignore[union-attr]
    run_now(SqliteAccountStore(connection).set_login_state(user_id, 0, None, 9e15))
    connection.close()
    assert manage(["unlock-user", "--username", "alice"], environ) == 0
    assert _existing(environ, "alice").locked_until_ms is None
    capsys.readouterr()
    assert manage(["reset-password", "--username", "alice"], environ, prompt(OTHER, OTHER)) == 0
    out, err = capsys.readouterr()
    assert "must be changed at the next login" in err
    assert OTHER not in out + err
    assert _existing(environ, "alice").must_change_password is True
    assert manage(["disable-user", "--username", "nobody"], environ) == 1
    assert "no user 'nobody'" in capsys.readouterr().err

    connection = open_database(environ["GATEWAY_DATABASE"])
    actions = [
        e.event.action.value for e in run_now(AuditLog(SqliteAuditStore(connection)).entries())
    ]
    connection.close()
    assert actions == [
        "user_created",
        "user_disabled",
        "user_enabled",
        "user_unlocked",
        "password_reset",
    ]


def test_the_user_commands_never_take_a_password_argument() -> None:
    for command in ("create-user", "reset-password"):
        with pytest.raises(SystemExit):
            manage([command, "--username", "alice", "--password", PASSWORD], {})
