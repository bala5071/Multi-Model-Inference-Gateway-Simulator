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
