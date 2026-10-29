"""The CLI's application and key commands go through the same service as the management
endpoints, and are audited the same way, with the operator as no user. See
docs/30-management-endpoints.md."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from gateway.apikeys import PEPPER_ENV
from gateway.audit import AuditAction, AuditLog, SqliteAuditStore
from gateway.database import open_database
from gateway.state import run_now
from helpers import TEST_PEPPER

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from manage import main as manage


@pytest.fixture
def environ(tmp_path: Path) -> dict[str, str]:
    return {"GATEWAY_DATABASE": str(tmp_path / "gateway.db"), PEPPER_ENV: TEST_PEPPER.decode()}


def _entries(
    environ: dict[str, str],
) -> list[tuple[AuditAction, str | None, str | None, dict[str, object]]]:
    connection = open_database(environ["GATEWAY_DATABASE"])
    try:
        return [
            (e.event.action, e.event.actor_user_id, e.event.target_id, dict(e.event.details))
            for e in run_now(AuditLog(SqliteAuditStore(connection)).entries())
        ]
    finally:
        connection.close()


def test_application_and_key_commands_are_audited_by_key_id(
    environ: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    create = ["create-application", "--id", "shop", "--name", "Shop", "--plan", "pro"]
    assert manage(create, environ) == 0
    assert manage(["create-key", "--application", "shop", "--label", "cli"], environ) == 0
    key = capsys.readouterr().out.strip()
    key_id = key[4:20]
    assert manage(["rotate-key", "--key-id", key_id, "--grace-hours", "1"], environ) == 0
    new_key = capsys.readouterr().out.strip()
    new_id = new_key[4:20]
    assert manage(["revoke-key", "--key-id", new_id], environ) == 0
    entries = _entries(environ)
    assert [(a, actor, target) for a, actor, target, _ in entries] == [
        (AuditAction.APPLICATION_CREATED, None, "shop"),
        (AuditAction.KEY_ISSUED, None, key_id),
        (AuditAction.KEY_ROTATED, None, key_id),
        (AuditAction.KEY_REVOKED, None, new_id),
    ]
    assert entries[0][3] == {"organization_id": "default", "plan": "pro"}
    assert entries[1][3] == {"application_id": "shop", "organization_id": "default"}
    assert entries[2][3]["key_id"] == new_id
    text = repr(entries)
    assert key not in text
    assert new_key not in text


def test_the_cli_keeps_its_behaviour_no_expiry_and_no_limit(
    environ: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    """The operator is not held to the HTTP path's limits (docs/30)."""
    assert (
        manage(["create-application", "--id", "shop", "--name", "Shop", "--plan", "pro"], environ)
        == 0
    )
    for i in range(12):
        assert manage(["create-key", "--application", "shop", "--label", f"k{i}"], environ) == 0
    capsys.readouterr()
    assert manage(["list-keys", "--application", "shop"], environ) == 0
    listed = capsys.readouterr().out.splitlines()
    assert len(listed) == 12
    assert all("no expiry" in line for line in listed)


def test_the_cli_reports_conflicts_without_a_traceback(
    environ: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    create = ["create-application", "--id", "shop", "--name", "Shop", "--plan", "pro"]
    assert manage(create, environ) == 0
    twin = ["create-application", "--id", "shop2", "--name", "Shop", "--plan", "pro"]
    assert manage(twin, environ) == 1
    assert "already has an application named 'Shop'" in capsys.readouterr().err
    unknown = ["create-application", "--id", "x", "--name", "X", "--plan", "platinum"]
    assert manage(unknown, environ) == 1
    assert "unknown plan 'platinum'" in capsys.readouterr().err
    assert manage(["revoke-key", "--key-id", "0123456789abcdef"], environ) == 1
    assert "no key" in capsys.readouterr().err
