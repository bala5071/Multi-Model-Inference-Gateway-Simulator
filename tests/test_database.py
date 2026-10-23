"""Where the database lives, who can read it, and that git never picks it up."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from gateway.app import create_default_app
from gateway.clock import WallClock
from gateway.database import (
    APP_DIRECTORY,
    DATABASE_NAME,
    DatabaseInUseError,
    ProcessLock,
    database_path,
    default_database_path,
    open_database,
)

REPO = Path(__file__).resolve().parent.parent
POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="POSIX permissions only")


def test_the_default_location_is_the_users_data_folder_not_the_working_tree() -> None:
    windows = default_database_path({"LOCALAPPDATA": r"C:\Users\x\AppData\Local"}, "win32")
    assert windows == Path(r"C:\Users\x\AppData\Local") / APP_DIRECTORY / DATABASE_NAME
    linux = default_database_path({"HOME": "/home/x"}, "linux")
    assert linux == Path("/home/x/.local/share") / APP_DIRECTORY / DATABASE_NAME
    xdg = default_database_path({"XDG_DATA_HOME": "/data", "HOME": "/home/x"}, "linux")
    assert xdg == Path("/data") / APP_DIRECTORY / DATABASE_NAME
    # And on this machine, with its real environment, it is not inside the repository.
    real = default_database_path(dict(os.environ)).resolve()
    assert REPO not in real.parents


def test_gateway_database_names_another_location(tmp_path: Path) -> None:
    assert database_path({"GATEWAY_DATABASE": str(tmp_path / "x.db")}) == tmp_path / "x.db"
    assert database_path({"HOME": "/home/x"}).name == DATABASE_NAME


def test_opening_creates_the_folder(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "deeper" / "gateway.db"
    open_database(path).close()
    assert path.is_file()


@POSIX_ONLY
def test_the_database_and_its_wal_are_readable_by_their_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "private" / "gateway.db"
    connection = open_database(path)
    connection.execute("CREATE TABLE t (x)")
    connection.execute("INSERT INTO t VALUES (1)")
    for file in (path, path.with_name("gateway.db-wal")):
        assert stat.S_IMODE(file.stat().st_mode) == 0o600, file
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    connection.close()


@POSIX_ONLY
def test_opening_tightens_a_file_that_others_could_read(tmp_path: Path) -> None:
    path = tmp_path / "gateway.db"
    path.touch()
    path.chmod(0o644)
    open_database(path).close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "name",
    [
        "gateway.db",
        "gateway.db-wal",
        "gateway.db-shm",
        "data/gateway.db",
        "gateway.env",
        "x.sqlite3",
        # Backups: any name, anywhere in the tree, and the copy taken before a migration.
        "backup.db",
        "backups/gateway-2025-06-01.db",
        "gateway.db.before-migration-8-1792700000000.db",
        "copy.db-wal",
    ],
)
def test_git_ignores_the_database_and_the_env_file(name: str) -> None:
    done = subprocess.run(["git", "check-ignore", "--no-index", "-q", name], cwd=REPO, check=False)
    assert done.returncode == 0, f"{name} is not ignored"


# --- one gateway process per database --------------------------------------------------------


def test_a_second_lock_on_the_same_database_is_refused_with_a_clear_message(
    tmp_path: Path,
) -> None:
    database = tmp_path / "gateway.db"
    first = ProcessLock(database)
    with pytest.raises(DatabaseInUseError) as excinfo:
        ProcessLock(database)
    message = str(excinfo.value)
    assert "in use by another gateway process" in message
    assert "GATEWAY_DATABASE" in message
    ProcessLock(tmp_path / "other.db").release()  # another database is unaffected
    first.release()
    ProcessLock(database).release()  # free again once released


def test_a_second_default_server_on_the_same_database_refuses_to_start() -> None:
    first = create_default_app()
    with pytest.raises(DatabaseInUseError):
        create_default_app()
    first.state.database_lock.release()
    create_default_app().state.database_lock.release()


def test_a_crashed_process_leaves_no_stale_lock(tmp_path: Path) -> None:
    database = tmp_path / "gateway.db"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; from gateway.database import ProcessLock; "
            f"lock = ProcessLock(Path({str(database)!r})); print('held', flush=True); "
            "sys.stdin.read()",
        ],
        cwd=REPO,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(DatabaseInUseError):
            ProcessLock(database)
    finally:
        holder.kill()  # no clean shutdown: the operating system releases the lock
        holder.wait(timeout=30)
    # Nobody deletes or unlocks anything: the lock becomes free by itself. On Windows the
    # system releases a dead process's locks shortly after it exits, not at the same instant,
    # so wait for that state (up to 10 s) rather than expect it at once.
    clock, pause = WallClock(), threading.Event()
    deadline = clock.now_ms() + 10_000.0
    while True:
        try:
            ProcessLock(database).release()
            break
        except DatabaseInUseError:
            if clock.now_ms() > deadline:
                raise
            pause.wait(0.02)
