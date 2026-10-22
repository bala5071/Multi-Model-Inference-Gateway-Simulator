"""Backing up and restoring the gateway's database. See docs/21-persistence.md."""

from __future__ import annotations

import os
import random
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

from gateway.apikeys import PEPPER_ENV, ApiKeyService, InvalidApiKeyError
from gateway.applications import SqliteApplicationStore, seed_demo_applications
from gateway.clock import ClockEpoch, SimClock
from gateway.database import (
    DatabaseInUseError,
    ProcessLock,
    backup_database,
    check_backup,
    open_database,
    restore_database,
)
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.routing import load_plans
from gateway.usage import SqliteUsageStore, UsageEvent
from helpers import TEST_PEPPER

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from manage import main as manage


def _populated(path: Path) -> tuple[sqlite3.Connection, ApiKeyService, str]:
    """A migrated database with the demo applications, one key and three usage events."""
    clock = SimClock()
    epoch = ClockEpoch(clock, origin_ms=1.75e12)
    connection = open_database(path)
    MigrationRunner(connection, MIGRATIONS, epoch).migrate()
    store = SqliteApplicationStore(connection)
    seed_demo_applications(store, load_plans(), created_at_ms=epoch.epoch_ms())
    keys = ApiKeyService(store, TEST_PEPPER, epoch, random_bytes=random.Random(5).randbytes)
    key = keys.issue("acme", "backend").key
    usage = SqliteUsageStore(connection)
    for i in range(3):
        usage.append(UsageEvent("acme", f"req-{i}", "economy", 10, 0, 5, 0.1, 0.1, 1.75e12 + i))
    return connection, keys, key


def _usage_rows(path: Path) -> list[tuple[object, ...]]:
    connection = open_database(path)
    try:
        return connection.execute("SELECT * FROM usage_events ORDER BY seq").fetchall()
    finally:
        connection.close()


def test_a_backup_restores_the_database_exactly_as_it_was(tmp_path: Path) -> None:
    live = tmp_path / "gateway.db"
    connection, keys, key = _populated(live)
    copy = tmp_path / "backups" / "gateway-backup.db"
    backup_database(connection, copy)  # while the database is open and in use
    before = _usage_rows(live)

    # Things change after the backup: a key is revoked, usage is added, a table is damaged.
    keys.revoke(key[4:20])
    SqliteUsageStore(connection).append(UsageEvent("acme", "late", "economy", 1, 0, 1, 0, 0, 2e12))
    connection.execute("DELETE FROM applications WHERE id = 'globex'")
    connection.close()

    restore_database(copy, live)

    assert _usage_rows(live) == before
    restored = open_database(live)
    store = SqliteApplicationStore(restored)
    assert [a.id for a in store.list_applications()] == ["initech", "globex", "acme"]
    # The key works again, with the same pepper: the pepper is not in the backup.
    assert ApiKeyService(store, TEST_PEPPER, ClockEpoch(SimClock())).authenticate(key).id == "acme"
    other = ApiKeyService(store, b"a-different-pepper-of-enough-length!", ClockEpoch(SimClock()))
    with pytest.raises(InvalidApiKeyError):
        other.authenticate(key)
    assert MigrationRunner(restored, MIGRATIONS, ClockEpoch(SimClock())).applied() == (1, 2, 3, 4)
    restored.close()


def test_a_backup_never_holds_a_key_or_the_pepper(tmp_path: Path) -> None:
    connection, _, key = _populated(tmp_path / "gateway.db")
    copy = tmp_path / "copy.db"
    backup_database(connection, copy)
    connection.close()
    data = copy.read_bytes()
    assert key.encode() not in data
    assert TEST_PEPPER not in data


def test_a_backup_never_overwrites_a_file(tmp_path: Path) -> None:
    connection, _, _ = _populated(tmp_path / "gateway.db")
    existing = tmp_path / "existing.db"
    existing.write_bytes(b"precious")
    with pytest.raises(FileExistsError):
        backup_database(connection, existing)
    connection.close()
    assert existing.read_bytes() == b"precious"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions only")
def test_a_backup_is_readable_by_its_owner_only(tmp_path: Path) -> None:
    connection, _, _ = _populated(tmp_path / "gateway.db")
    copy = tmp_path / "copy.db"
    backup_database(connection, copy)
    connection.close()
    assert stat.S_IMODE(copy.stat().st_mode) == 0o600


def test_restore_refuses_while_a_server_holds_the_database(tmp_path: Path) -> None:
    live = tmp_path / "gateway.db"
    connection, _, _ = _populated(live)
    copy = tmp_path / "copy.db"
    backup_database(connection, copy)
    connection.close()
    server = ProcessLock(live)  # what a running gateway holds
    try:
        with pytest.raises(DatabaseInUseError):
            restore_database(copy, live)
    finally:
        server.release()
    restore_database(copy, live)


def test_restore_refuses_a_file_that_is_not_an_intact_gateway_database(tmp_path: Path) -> None:
    live = tmp_path / "gateway.db"
    _populated(live)[0].close()
    before = _usage_rows(live)
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"not a database at all, just some bytes" * 100)
    other = tmp_path / "other.db"
    sqlite3.connect(other).execute("CREATE TABLE t (x)").connection.close()
    for bad, message in (
        (garbage, "not a database"),
        (other, "not a gateway database"),
        (tmp_path / "x", "not a file"),
    ):
        with pytest.raises((ValueError, sqlite3.DatabaseError), match=message):
            restore_database(bad, live)
    assert _usage_rows(live) == before  # untouched
    with pytest.raises(ValueError, match="not a gateway database"):
        check_backup(other)


def test_backup_and_restore_through_the_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environ = {"GATEWAY_DATABASE": str(tmp_path / "gateway.db"), PEPPER_ENV: TEST_PEPPER.decode()}
    assert manage(["seed-demo"], environ) == 0
    assert manage(["create-key", "--application", "acme", "--label", "x"], environ) == 0
    key = capsys.readouterr().out.strip()
    copy = tmp_path / "copy.db"
    assert manage(["backup", "--to", str(copy)], environ) == 0
    assert "not the pepper" in capsys.readouterr().err
    assert manage(["backup", "--to", str(copy)], environ) == 1  # never overwrites

    assert manage(["revoke-key", "--key-id", key[4:20]], environ) == 0
    lock = ProcessLock(tmp_path / "gateway.db")
    try:
        assert manage(["restore", "--from", str(copy)], environ) == 1  # a server is running
        assert "in use by another gateway process" in capsys.readouterr().err
    finally:
        lock.release()
    assert manage(["restore", "--from", str(copy)], environ) == 0
    assert manage(["list-keys", "--application", "acme"], environ) == 0
    assert "  active  " in capsys.readouterr().out  # revoked after the backup: active again
