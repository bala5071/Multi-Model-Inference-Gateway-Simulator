"""Where the database lives, who can read it, and that git never picks it up."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from gateway.database import (
    APP_DIRECTORY,
    DATABASE_NAME,
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
    ],
)
def test_git_ignores_the_database_and_the_env_file(name: str) -> None:
    done = subprocess.run(["git", "check-ignore", "--no-index", "-q", name], cwd=REPO, check=False)
    assert done.returncode == 0, f"{name} is not ignored"
