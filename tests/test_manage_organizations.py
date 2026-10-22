"""The CLI for organizations, and migrating through it: the dry run, the backup taken before
a table is rebuilt, and refusing when the backup fails. See docs/26-organizations.md."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from gateway.apikeys import PEPPER_ENV, ApiKeyService, InvalidApiKeyError
from gateway.applications import SqliteApplicationStore
from gateway.clock import ClockEpoch, SimClock
from gateway.database import ProcessLock, check_backup, open_database
from gateway.migrations import MIGRATIONS, MigrationRunner
from helpers import TEST_PEPPER, version3_database

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import manage as manage_module
from manage import main as manage


@pytest.fixture
def environ(tmp_path: Path) -> dict[str, str]:
    return {"GATEWAY_DATABASE": str(tmp_path / "gateway.db"), PEPPER_ENV: TEST_PEPPER.decode()}


def _version(path: Path) -> int:
    connection = open_database(path)
    try:
        return MigrationRunner(connection, MIGRATIONS, ClockEpoch(SimClock())).current_version()
    finally:
        connection.close()


def _key(capsys: pytest.CaptureFixture[str], environ: dict[str, str], application: str) -> str:
    assert manage(["create-key", "--application", application, "--label", "x"], environ) == 0
    return capsys.readouterr().out.strip()


def _authenticate(environ: dict[str, str], key: str) -> str:
    connection = open_database(environ["GATEWAY_DATABASE"])
    try:
        store = SqliteApplicationStore(connection)
        return ApiKeyService(store, TEST_PEPPER, ClockEpoch(SimClock())).authenticate(key).id
    finally:
        connection.close()


# --- organizations --------------------------------------------------------------------------


def test_create_list_disable_and_enable_organizations(
    environ: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    create = ["create-organization", "--id", "north", "--name", "North", "--kind", "business"]
    assert manage(create, environ) == 0
    assert (
        manage(
            ["create-organization", "--id", "solo", "--name", "Solo", "--kind", "individual"],
            environ,
        )
        == 0
    )
    capsys.readouterr()
    assert manage(create, environ) == 1
    assert "exists" in capsys.readouterr().err
    assert (
        manage(
            ["create-organization", "--id", "x", "--name", "No​Name", "--kind", "business"], environ
        )
        == 1
    )
    assert "U+200B" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        manage(["create-organization", "--id", "x", "--name", "X", "--kind", "household"], environ)
    capsys.readouterr()

    assert manage(["list-organizations"], environ) == 0
    assert capsys.readouterr().out.splitlines() == [
        "default  'default'  kind=business  enabled  applications=0",
        "north  'North'  kind=business  enabled  applications=0",
        "solo  'Solo'  kind=individual  enabled  applications=0",
    ]
    assert manage(["disable-organization", "--organization", "north"], environ) == 0
    assert manage(["list-organizations"], environ) == 0
    assert "north  'North'  kind=business  disabled" in capsys.readouterr().out
    assert manage(["enable-organization", "--organization", "north"], environ) == 0
    assert manage(["disable-organization", "--organization", "nobody"], environ) == 1
    assert "no organization 'nobody'" in capsys.readouterr().err


def test_applications_go_into_a_named_organization_or_default(
    environ: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    for org in ("north", "south"):
        assert (
            manage(
                ["create-organization", "--id", org, "--name", org.title(), "--kind", "business"],
                environ,
            )
            == 0
        )
        assert (
            manage(
                [
                    "create-application",
                    "--id",
                    f"{org}-shop",
                    "--name",
                    "Shop",
                    "--plan",
                    "pro",
                    "--organization",
                    org,
                ],
                environ,
            )
            == 0
        )
    assert (
        manage(
            ["create-application", "--id", "legacy", "--name", "Shop", "--plan", "free"], environ
        )
        == 0
    )
    capsys.readouterr()
    # The same name twice in one organization is refused.
    assert (
        manage(
            [
                "create-application",
                "--id",
                "north-2",
                "--name",
                "Shop",
                "--plan",
                "pro",
                "--organization",
                "north",
            ],
            environ,
        )
        == 1
    )
    assert "already has an application named 'Shop'" in capsys.readouterr().err
    assert (
        manage(
            [
                "create-application",
                "--id",
                "z",
                "--name",
                "Z",
                "--plan",
                "pro",
                "--organization",
                "nobody",
            ],
            environ,
        )
        == 1
    )
    assert "no organization 'nobody'" in capsys.readouterr().err

    assert manage(["list-applications", "--organization", "north"], environ) == 0
    assert capsys.readouterr().out.splitlines() == [
        "north-shop  'Shop'  plan=pro  organization=north"
    ]
    assert manage(["list-applications"], environ) == 0
    assert [line.split()[0] for line in capsys.readouterr().out.splitlines()] == [
        "north-shop",
        "south-shop",
        "legacy",
    ]

    # Disabled: its keys stop working, no new application, existing data stays.
    north_key = _key(capsys, environ, "north-shop")
    south_key = _key(capsys, environ, "south-shop")
    assert manage(["disable-organization", "--organization", "north"], environ) == 0
    with pytest.raises(InvalidApiKeyError):
        _authenticate(environ, north_key)
    assert _authenticate(environ, south_key) == "south-shop"
    assert (
        manage(
            [
                "create-application",
                "--id",
                "north-3",
                "--name",
                "Other",
                "--plan",
                "pro",
                "--organization",
                "north",
            ],
            environ,
        )
        == 1
    )
    assert "disabled" in capsys.readouterr().err
    assert manage(["enable-organization", "--organization", "north"], environ) == 0
    assert _authenticate(environ, north_key) == "north-shop"


def test_organization_usage_adds_up_its_applications_only(
    environ: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    for org in ("north", "south"):
        assert (
            manage(
                ["create-organization", "--id", org, "--name", org.title(), "--kind", "business"],
                environ,
            )
            == 0
        )
        assert (
            manage(
                [
                    "create-application",
                    "--id",
                    f"{org}-shop",
                    "--name",
                    "Shop",
                    "--plan",
                    "pro",
                    "--organization",
                    org,
                ],
                environ,
            )
            == 0
        )
    capsys.readouterr()
    assert manage(["organization-usage", "--organization", "north"], environ) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("organization north  period_start_ms=")
    assert lines[1].split() == ["north-shop", "used=0", "budget=5000000"]
    assert "sum_of_budgets=5000000" in lines[2]
    assert all("south" not in line for line in lines)
    assert manage(["organization-usage", "--organization", "nobody"], environ) == 1


def test_the_existing_key_commands_keep_working(
    environ: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert manage(["seed-demo"], environ) == 0
    key = _key(capsys, environ, "acme")
    assert _authenticate(environ, key) == "acme"
    assert manage(["list-keys", "--application", "acme"], environ) == 0
    assert "  active  " in capsys.readouterr().out
    assert manage(["rotate-key", "--key-id", key[4:20], "--grace-hours", "1"], environ) == 0
    assert manage(["revoke-key", "--key-id", key[4:20]], environ) == 0
    with pytest.raises(InvalidApiKeyError):
        _authenticate(environ, key)


# --- migrating ------------------------------------------------------------------------------


def test_the_dry_run_touches_nothing(
    tmp_path: Path, environ: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    path = Path(environ["GATEWAY_DATABASE"])
    assert manage(["migrate", "--dry-run"], environ) == 0
    assert not path.exists()  # no database was created
    assert "no database" in capsys.readouterr().err

    version3_database(path, [("a", "Shop"), ("b", "Shop"), ("c", "Tools")])
    files = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert manage(["migrate", "--dry-run"], environ) == 1
    err = capsys.readouterr().err
    assert "database is at version 3; this code knows 7" in err
    assert "migration 4 organizations (rebuilds a table):" in err
    assert "3 application(s) into 'default'" in err
    assert "CONFLICT: application name 'Shop' is used by a, b" in err
    assert "a backup would be taken first" in err
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == files
    assert _version(path) == 3


def test_migrate_refuses_conflicts_before_taking_a_backup(
    tmp_path: Path, environ: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    path = Path(environ["GATEWAY_DATABASE"])
    version3_database(path, [("a", "Shop"), ("b", "Shop")])
    assert manage(["migrate"], environ) == 1
    assert "'Shop' is used by a, b" in capsys.readouterr().err
    assert _version(path) == 3
    assert not list(tmp_path.glob("*.before-migration-*"))
    # A backup can still be taken, before fixing the names; it does not migrate.
    copy = tmp_path / "before-fix.db"
    assert manage(["backup", "--to", str(copy)], environ) == 0
    assert check_backup(copy) == 3
    assert _version(path) == 3


def test_migrate_backs_up_before_rebuilding_a_table(
    tmp_path: Path, environ: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    path = Path(environ["GATEWAY_DATABASE"])
    version3_database(path, [("acme", "Acme"), ("initech", "Initech")])
    assert manage(["migrate"], environ) == 0
    err = capsys.readouterr().err
    [backup] = tmp_path.glob("gateway.db.before-migration-4-*.db")
    assert f"backed up {path} to {backup} before migration 4" in err
    assert check_backup(backup) == 3  # the database as it was
    # Checking it left no -wal or -shm file beside it: only the backup itself.
    assert [p.name for p in tmp_path.glob("gateway.db.before-*")] == [backup.name]
    assert _version(path) == 7
    copy = sqlite3.connect(backup)
    assert copy.execute("SELECT id FROM applications ORDER BY rowid").fetchall() == [
        ("acme",),
        ("initech",),
    ]
    copy.close()
    # Nothing left to rebuild: no second backup.
    assert manage(["migrate"], environ) == 0
    assert len(list(tmp_path.glob("*.before-migration-*"))) == 1


def test_a_new_database_needs_no_backup(
    tmp_path: Path, environ: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert manage(["migrate"], environ) == 0
    assert "backed up" not in capsys.readouterr().err
    assert not list(tmp_path.glob("*.before-migration-*"))


def test_migrate_refuses_when_the_backup_fails(
    tmp_path: Path,
    environ: dict[str, str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = Path(environ["GATEWAY_DATABASE"])
    version3_database(path, [("acme", "Acme")])

    def broken(source: sqlite3.Connection, target: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(manage_module, "backup_database", broken)
    assert manage(["migrate"], environ) == 1
    assert "the backup before migration 4 failed, so nothing was migrated: disk full" in (
        capsys.readouterr().err
    )
    assert _version(path) == 3
    # Any other command that opens the database refuses the same way.
    assert manage(["list-applications"], environ) == 1
    assert _version(path) == 3


def test_migrate_refuses_while_a_gateway_holds_the_database(
    environ: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    path = Path(environ["GATEWAY_DATABASE"])
    version3_database(path, [("acme", "Acme")])
    lock = ProcessLock(path)
    try:
        assert manage(["migrate"], environ) == 1
        assert "in use by another gateway process" in capsys.readouterr().err
    finally:
        lock.release()
    assert _version(path) == 3
    assert manage(["migrate"], environ) == 0


def test_the_dry_run_sees_rows_a_live_connection_has_not_checkpointed(
    environ: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    path = Path(environ["GATEWAY_DATABASE"])
    version3_database(path, [("a", "Shop")])
    live = open_database(path)  # as a running gateway holds it: a write-ahead log exists
    try:
        live.execute(
            "INSERT INTO applications (id, name, plan_name, models, price_multiplier,"
            " monthly_token_budget, created_at_ms) VALUES ('b', 'Shop', 'pro', '[]', 1, 1, 0)"
        )
        assert path.with_name(path.name + "-wal").exists()
        assert manage(["migrate", "--dry-run"], environ) == 1
        assert "CONFLICT: application name 'Shop' is used by a, b" in capsys.readouterr().err
    finally:
        live.close()
