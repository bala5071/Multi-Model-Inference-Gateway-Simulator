"""Versioned schema migrations for the gateway's SQLite database.

See docs/19-usage-store.md, and docs/26-organizations.md for table rebuilds.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from gateway.clock import EpochSource

Step = Callable[[sqlite3.Connection], None]
# A backfill also gets the time the migration is recorded as applied (from the injected
# EpochSource), for rows it creates.
Backfill = Callable[[sqlite3.Connection, float], None]


@dataclass(frozen=True, slots=True)
class MigrationPreview:
    """What applying a migration would do to this database, and what would stop it."""

    changes: tuple[str, ...]
    conflicts: tuple[str, ...] = ()


Preview = Callable[[sqlite3.Connection], MigrationPreview]


@dataclass(frozen=True, slots=True)
class Migration:
    """One schema change, numbered from 1 with no gaps.

    ``up`` makes the change and ``down`` undoes it. ``backfill``, if given, fills in data
    for the new schema from what is already stored; it runs after ``up``, in the same
    transaction, so a failed backfill leaves the schema as it was. None of them may commit:
    the runner owns the transaction (so no ``executescript``, which commits first).

    ``rebuilds_tables`` marks a migration that replaces a table (SQLite cannot add a
    required foreign-key column in place). The runner then turns foreign keys off around its
    transaction, as SQLite's documented procedure requires (the pragma cannot change inside
    a transaction), checks them with ``PRAGMA foreign_key_check`` before committing, and turns
    them back on whatever happens. Operators back the database up before such a migration
    (``scripts/manage.py migrate`` does it).

    ``preview``, if given, reads the database (never writes it) and says what the migration
    would change and what would make it refuse; ``plan_migrations`` reports it.
    """

    version: int
    name: str
    up: Step
    down: Step
    backfill: Backfill | None = None
    rebuilds_tables: bool = False
    preview: Preview | None = None


class MigrationError(RuntimeError):
    """The database and the known migrations do not agree."""


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    """What ``migrate`` would do: the database's version and each pending migration."""

    current_version: int
    pending: tuple[tuple[Migration, MigrationPreview], ...]

    @property
    def conflicts(self) -> tuple[str, ...]:
        return tuple(c for _, preview in self.pending for c in preview.conflicts)

    @property
    def rebuilds_tables(self) -> bool:
        return any(migration.rebuilds_tables for migration, _ in self.pending)


def plan_migrations(
    connection: sqlite3.Connection, migrations: Sequence[Migration]
) -> MigrationPlan:
    """What applying ``migrations`` would do, without writing anything: safe on a read-only
    connection. A database with no ``schema_migrations`` table is at version 0. Each preview
    reads the database as it is now, before any of the pending migrations."""
    has_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    current = 0
    if has_table is not None:
        current = int(connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0])
    if current > len(migrations):
        raise MigrationError(
            f"the database is at version {current}, newer than this code "
            f"(which knows {len(migrations)})"
        )
    pending = tuple(
        (m, m.preview(connection) if m.preview is not None else MigrationPreview((m.name,)))
        for m in migrations[current:]
    )
    return MigrationPlan(current, pending)


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

    def pending(self, target: int | None = None) -> tuple[Migration, ...]:
        """The migrations ``migrate(target)`` would apply, in order."""
        goal = self.latest if target is None else target
        return self._migrations[self.current_version() : max(goal, 0)]

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
        foreign_keys = bool(connection.execute("PRAGMA foreign_keys").fetchone()[0])
        if migration.rebuilds_tables:
            connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if forward:
                    applied_at_ms = self._epoch.epoch_ms()
                    migration.up(connection)
                    if migration.backfill is not None:
                        migration.backfill(connection, applied_at_ms)
                    connection.execute(
                        "INSERT INTO schema_migrations (version, name, applied_at_ms)"
                        " VALUES (?, ?, ?)",
                        (migration.version, migration.name, applied_at_ms),
                    )
                else:
                    migration.down(connection)
                    connection.execute(
                        "DELETE FROM schema_migrations WHERE version = ?", (migration.version,)
                    )
                if migration.rebuilds_tables:
                    broken = connection.execute("PRAGMA foreign_key_check").fetchall()
                    if broken:
                        raise MigrationError(
                            f"migration {migration.version} would leave {len(broken)} rows "
                            f"pointing at nothing (first: {broken[0]}); nothing was changed"
                        )
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")
        finally:
            if migration.rebuilds_tables:
                connection.execute(f"PRAGMA foreign_keys = {'ON' if foreign_keys else 'OFF'}")
