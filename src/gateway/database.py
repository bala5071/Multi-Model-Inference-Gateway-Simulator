"""Opening the gateway's SQLite database, and where it lives by default.

See docs/19-usage-store.md and docs/20-applications-api-keys.md. The database holds key
hashes and usage, so it lives outside the working tree by default and, where the operating
system has POSIX permissions, only its owner can read it.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from collections.abc import Mapping
from pathlib import Path

# How long a write waits for another connection's lock before failing, in milliseconds.
BUSY_TIMEOUT_MS = 5000
DATABASE_ENV = "GATEWAY_DATABASE"
APP_DIRECTORY = "inference-gateway"
DATABASE_NAME = "gateway.db"


def default_database_path(environ: Mapping[str, str], platform: str = sys.platform) -> Path:
    """Where the database goes when ``GATEWAY_DATABASE`` names none: the user's local data
    folder, never the working tree. ``%LOCALAPPDATA%`` on Windows, ``$XDG_DATA_HOME`` (or
    ``~/.local/share``) elsewhere."""
    if platform == "win32":
        base = environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = environ.get("XDG_DATA_HOME") or str(
            Path(environ.get("HOME") or Path.home()) / ".local" / "share"
        )
    return Path(base) / APP_DIRECTORY / DATABASE_NAME


def database_path(environ: Mapping[str, str]) -> Path:
    """``GATEWAY_DATABASE`` if set, else the default location."""
    named = environ.get(DATABASE_ENV)
    return Path(named) if named else default_database_path(environ)


def _restrict(path: Path) -> None:
    """Create the file (and its folder) readable by its owner only, where the OS allows.

    On POSIX: folder 0700, file 0600; SQLite creates the -wal and -shm files with the
    file's permissions. On Windows ``chmod`` cannot express this; the default location is
    inside the user's own profile, which other users cannot read.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(descriptor)
    os.chmod(path, 0o600)


def open_database(path: str | Path, *, synchronous: str = "FULL") -> sqlite3.Connection:
    """Open (creating if needed) a SQLite database for the gateway's stores.

    The connection is in autocommit mode: each statement is its own transaction unless the
    caller issues ``BEGIN``. Write-ahead logging lets readers run while a write is in
    progress, and the busy timeout makes a second writer wait instead of failing at once.
    ``synchronous`` is SQLite's durability setting: ``FULL`` (the default) syncs the log on
    every commit, so a committed usage row survives a power cut; ``NORMAL`` is faster and
    can lose the last commits, but not corrupt the file.

    ``":memory:"`` gives a private in-memory database (no write-ahead log), for tests. A file
    is created owner-only where the OS supports it (see ``_restrict``).
    """
    if synchronous not in ("FULL", "NORMAL"):
        raise ValueError("synchronous must be 'FULL' or 'NORMAL'")
    if str(path) != ":memory:":
        _restrict(Path(path))
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    if str(path) != ":memory:":
        mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if mode != "wal":
            connection.close()
            raise RuntimeError(f"could not put {path} in WAL mode (SQLite says {mode!r})")
    connection.execute(f"PRAGMA synchronous = {synchronous}")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


class DatabaseInUseError(RuntimeError):
    """Another gateway process holds this database."""


class ProcessLock:
    """An exclusive lock on ``<database>.lock``, held for the life of a gateway process.

    One gateway process per database is the supported setup (docs/21-persistence.md): each
    process keeps the current period's budget totals in memory, so two would each enforce
    budgets on their own totals. The lock is the operating system's (``flock`` on POSIX,
    ``msvcrt.locking`` on Windows) on an open file: it is released when the process exits,
    even if it crashes, so a dead process leaves no stale lock. It is not reliable on network
    filesystems, where advisory locks may not be shared between machines.
    """

    def __init__(self, database: Path) -> None:
        self.path = database.with_name(database.name + ".lock")
        _restrict(self.path)
        self.path.touch(exist_ok=True)
        self._file = self.path.open("r+b")
        try:
            self._lock()
        except OSError as exc:
            self._file.close()
            raise DatabaseInUseError(
                f"{database} is in use by another gateway process (it holds {self.path}). "
                "Run one gateway process per database (docs/21-persistence.md), or point "
                "GATEWAY_DATABASE at another file."
            ) from exc

    def _lock(self) -> None:
        if sys.platform == "win32":
            import msvcrt

            self._file.seek(0)
            msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release(self) -> None:
        """Release the lock now (closing the file releases it too)."""
        if self._file.closed:
            return
        if sys.platform == "win32":
            import msvcrt

            self._file.seek(0)
            msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
        self._file.close()


def backup_database(source: sqlite3.Connection, target: Path) -> None:
    """A consistent copy of a live database, through SQLite's online backup API.

    Safe while the server runs: the copy is a snapshot of one moment, whatever is being
    written. The target must not exist; it is created owner-only like the database. The
    copy holds what the database holds (applications, key HMACs, usage), never a key and
    never the pepper, which must be backed up separately (docs/21-persistence.md).
    """
    if target.exists():
        raise FileExistsError(f"{target} exists; a backup never overwrites a file")
    _restrict(target)
    copy = sqlite3.connect(target)
    try:
        source.backup(copy)
    finally:
        copy.close()


def _immutable_uri(path: Path) -> str:
    return f"file:{path.as_posix()}?mode=ro&immutable=1"


def check_backup(backup: Path) -> int:
    """Raise ``ValueError`` unless ``backup`` is an intact gateway database; return the
    number of migrations it has applied."""
    if not backup.is_file():
        raise ValueError(f"{backup} is not a file")
    # Immutable: a backup is a closed file (its log checkpointed when the copy closed), so
    # SQLite reads it as it is and creates no -wal or -shm file beside it.
    connection = sqlite3.connect(_immutable_uri(backup), uri=True)
    try:
        status = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if status != "ok":
            raise ValueError(f"{backup} is damaged: {status}")
        found = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone()
        if found is None:
            raise ValueError(f"{backup} is not a gateway database (no schema_migrations)")
        return int(connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0])
    finally:
        connection.close()


def restore_database(backup: Path, target: Path) -> None:
    """Replace the database at ``target`` with the contents of ``backup``.

    Refuses while a gateway process holds ``target`` (its ``ProcessLock``): stop the server
    first. Checks the backup's integrity before touching the target, then copies it in
    through the backup API, so the target is replaced consistently, write-ahead log
    included. A restored database that is behind is migrated forward by the server when it
    next starts, except for a migration that rebuilds a table, which needs
    ``scripts/manage.py migrate`` first (docs/26-organizations.md).
    """
    check_backup(backup)
    lock = ProcessLock(target)
    try:
        source = sqlite3.connect(_immutable_uri(backup), uri=True)
        destination = open_database(target)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
    finally:
        lock.release()
