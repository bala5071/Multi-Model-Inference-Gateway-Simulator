"""The operator's CLI for settings (docs/39): show, set, unset, revert, each change a version and
audited, refusals answered and nothing written, the server reading what the CLI wrote."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from gateway.apikeys import PEPPER_ENV
from gateway.audit import AuditAction, SqliteAuditStore
from gateway.database import open_database
from gateway.settings import Scope, SqliteSettingsStore
from gateway.state import run_now
from helpers import TEST_PEPPER

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from manage import main as manage


@pytest.fixture
def environ(tmp_path: Path) -> dict[str, str]:
    env = {"GATEWAY_DATABASE": str(tmp_path / "gateway.sqlite3"), PEPPER_ENV: TEST_PEPPER.decode()}
    assert manage(["seed-demo"], env) == 0
    return env


def _rows(environ: dict[str, str]) -> tuple[list[tuple[int, str]], list[str]]:
    connection = open_database(environ["GATEWAY_DATABASE"])
    try:
        store = SqliteSettingsStore(connection)
        versions = run_now(store.versions(Scope.APPLICATION, "globex", 50))
        actions = [e.event.action.value for e in run_now(SqliteAuditStore(connection).entries())]
        return [(v.version, v.document) for v in versions], actions
    finally:
        connection.close()


def test_set_show_unset_and_revert(
    environ: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    app = ["--application", "globex"]
    assert manage(["settings-show", *app], environ) == 0
    shown = capsys.readouterr().err
    assert "version 0" in shown
    assert "monthly_token_budget" in shown
    assert manage(
        ["settings-set", *app, "--expected-version", "0", "--set", "max_tokens=64",
         "--set", 'allowed_tiers=["economy"]'],
        environ,
    ) == 0  # fmt: skip
    assert "version 1" in capsys.readouterr().err
    assert (
        manage(["settings-set", *app, "--expected-version", "1", "--unset", "max_tokens"], environ)
        == 0
    )
    capsys.readouterr()
    assert manage(["settings-show", *app], environ) == 0
    shown = capsys.readouterr().err
    assert "version 2" in shown
    assert "allowed_tiers" in shown
    assert "application" in shown
    assert manage(["settings-revert", *app, "--expected-version", "2"], environ) == 0
    capsys.readouterr()
    versions, actions = _rows(environ)
    assert versions == [
        (3, '{"allowed_tiers":["economy"],"max_tokens":64}'),
        (2, '{"allowed_tiers":["economy"]}'),
        (1, '{"allowed_tiers":["economy"],"max_tokens":64}'),
    ]
    assert actions.count(AuditAction.SETTINGS_CHANGED.value) == 2
    assert actions.count(AuditAction.SETTINGS_REVERTED.value) == 1


def test_refusals_say_why_and_write_nothing(
    environ: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    app = ["--application", "globex"]
    org = ["--organization", "default"]
    budget = "monthly_token_budget=" + str(10**13)
    for argv, words in (
        (["settings-set", *app, "--expected-version", "0", "--set", budget], "plan"),
        (["settings-set", *app, "--expected-version", "3", "--set", "max_tokens=5"], "version 0"),
        (["settings-set", *app, "--expected-version", "0", "--set", "max_tokens=0"], "max_tokens"),
        (["settings-set", *app, "--expected-version", "0", "--set", "nope=1"], "not a setting"),
        (["settings-set", *app, "--expected-version", "0", "--set", "max_tokens"], "name=value"),
        (["settings-set", *org, "--expected-version", "0", "--set", "default_quality=\"best\""],
         "organization"),
        (["settings-revert", *app, "--expected-version", "0"], "no previous version"),
        (["settings-show", "--application", "nope"], "nope"),
    ):  # fmt: skip
        assert manage(argv, environ) == 1, argv
        assert words in capsys.readouterr().err, argv
    assert _rows(environ)[0] == []


def test_an_organization_cap_set_by_the_cli_clamps_its_applications(
    environ: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert manage(
        ["settings-set", "--organization", "default", "--expected-version", "0",
         "--set", "max_tokens=16"],
        environ,
    ) == 0  # fmt: skip
    capsys.readouterr()
    assert manage(["settings-show", "--application", "acme"], environ) == 0
    shown = capsys.readouterr().err
    line = next(row for row in shown.splitlines() if row.strip().startswith("max_tokens"))
    assert "16" in line
    assert "organization" in line
    connection = open_database(environ["GATEWAY_DATABASE"])
    try:
        row = run_now(SqliteSettingsStore(connection).current(Scope.ORGANIZATION, "default"))
    finally:
        connection.close()
    assert row is not None
    assert json.loads(row.document) == {"max_tokens": 16}
