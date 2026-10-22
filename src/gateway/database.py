"""Opening the gateway's SQLite database.

See docs/19-usage-store.md.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# How long a write waits for another connection's lock before failing, in milliseconds.
BUSY_TIMEOUT_MS = 5000


def open_database(path: str | Path, *, synchronous: str = "FULL") -> sqlite3.Connection:
    """Open (creating if needed) a SQLite database for the gateway's stores.

    The connection is in autocommit mode: each statement is its own transaction unless the
    caller issues ``BEGIN``. Write-ahead logging lets readers run while a write is in
    progress, and the busy timeout makes a second writer wait instead of failing at once.
    ``synchronous`` is SQLite's durability setting: ``FULL`` (the default) syncs the log on
    every commit, so a committed usage row survives a power cut; ``NORMAL`` is faster and
    can lose the last commits, but not corrupt the file.

    ``":memory:"`` gives a private in-memory database (no write-ahead log), for tests.
    """
    if synchronous not in ("FULL", "NORMAL"):
        raise ValueError("synchronous must be 'FULL' or 'NORMAL'")
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
