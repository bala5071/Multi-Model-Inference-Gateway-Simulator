"""Versioned schema migrations for the gateway's SQLite database.

See docs/19-usage-store.md.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from gateway.clock import EpochSource

Step = Callable[[sqlite3.Connection], None]


@dataclass(frozen=True, slots=True)
class Migration:
    """One schema change, numbered from 1 with no gaps.

    ``up`` makes the change and ``down`` undoes it. ``backfill``, if given, fills in data
    for the new schema from what is already stored; it runs after ``up``, in the same
    transaction, so a failed backfill leaves the schema as it was. None of them may commit:
    the runner owns the transaction (so no ``executescript``, which commits first).
    """

    version: int
    name: str
    up: Step
    down: Step
    backfill: Step | None = None


class MigrationError(RuntimeError):
    """The database and the known migrations do not agree."""


class MigrationRunner:
    """Applies and reverses migrations, recording each applied version.

    Each migration runs in its own transaction together with its record in
    ``schema_migrations``, so a database is always at exactly one known version.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        migrations: Sequence[Migration],
        epoch: EpochSource,
    ) -> None:
        if connection.isolation_level is not None:
            raise MigrationError(
                "the connection must be in autocommit mode (isolation_level=None); "
                "use gateway.database.open_database"
            )
        versions = [m.version for m in migrations]
        if versions != list(range(1, len(migrations) + 1)):
            raise MigrationError(f"migrations must be numbered 1, 2, 3, ... in order: {versions}")
        self._connection = connection
        self._migrations = tuple(migrations)
        self._epoch = epoch
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version INTEGER PRIMARY KEY,"
            " name TEXT NOT NULL,"
            " applied_at_ms REAL NOT NULL)"
        )

    @property
    def latest(self) -> int:
        return len(self._migrations)

    def applied(self) -> tuple[int, ...]:
        rows = self._connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        versions = tuple(int(row[0]) for row in rows)
        if versions != tuple(range(1, len(versions) + 1)):
            raise MigrationError(f"applied versions have a gap: {versions}")
        if len(versions) > self.latest:
            raise MigrationError(
                f"the database is at version {len(versions)}, newer than this code "
                f"(which knows {self.latest}); refusing to touch it"
            )
        return versions

    def current_version(self) -> int:
        return len(self.applied())

    def migrate(self, target: int | None = None) -> tuple[int, ...]:
        """Apply every migration up to ``target`` (default: the latest). Returns the versions
        applied; none if the database is already there."""
        goal = self.latest if target is None else target
        if not 0 <= goal <= self.latest:
            raise MigrationError(f"no migration {goal}; versions are 1 to {self.latest}")
        current = self.current_version()
        if goal < current:
            raise MigrationError(
                f"the database is at version {current}; use rollback to go down to {goal}"
            )
        done = []
        for migration in self._migrations[current:goal]:
            self._in_transaction(migration, forward=True)
            done.append(migration.version)
        return tuple(done)

    def rollback(self, target: int) -> tuple[int, ...]:
        """Reverse migrations, newest first, until the database is at ``target``. Returns the
        versions reversed."""
        current = self.current_version()
        if not 0 <= target <= current:
            raise MigrationError(
                f"cannot roll back to {target}; the database is at version {current}"
            )
        done = []
        for migration in reversed(self._migrations[target:current]):
            self._in_transaction(migration, forward=False)
            done.append(migration.version)
        return tuple(done)

    def _in_transaction(self, migration: Migration, *, forward: bool) -> None:
        connection = self._connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            if forward:
                migration.up(connection)
                if migration.backfill is not None:
                    migration.backfill(connection)
                connection.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at_ms) VALUES (?, ?, ?)",
                    (migration.version, migration.name, self._epoch.epoch_ms()),
                )
            else:
                migration.down(connection)
                connection.execute(
                    "DELETE FROM schema_migrations WHERE version = ?", (migration.version,)
                )
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")
