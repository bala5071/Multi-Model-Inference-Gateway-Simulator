"""Moving a gateway's data from SQLite to Postgres, and back (docs/scale-out-plan.md, 2.3, 2.4).

See docs/36-postgres.md. Run by the operator, offline, through ``scripts/manage.py``:

- **To Postgres** (``migrate_to_postgres``): the SQLite database must be at the latest migration
  and the Postgres one new (no schema yet, or one just applied with nothing in it but the
  ``default`` organization). Every table is copied in foreign-key order in one Postgres
  transaction under the write lock: ids, ``seq`` values, HMACs and sealed keys byte for
  byte, rows in their SQLite order (which the ``ord`` columns then keep). Before committing,
  every table's rows are compared with SQLite's; any difference rolls everything back.
- **Back to SQLite** (``migrate_to_sqlite``): into a new file only, never over one, migrated to
  the latest version, filled from one snapshot of Postgres (repeatable read), and compared the
  same way before it is kept.
- **Dry runs** make every write and every check, then keep nothing: to Postgres in a
  transaction rolled back at the end, back to SQLite into a database in memory.

Never a key, a password or a value is printed: the report holds table names, row counts and
sha256 digests of the rows.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gateway.clock import SystemEpoch
from gateway.database import open_database
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.postgres import TABLES, WRITE_LOCK, PostgresDatabase, ensure_schema

_DEFAULT_ORGANIZATION = "default"


class MigrationRefused(RuntimeError):
    """The migration cannot be made: the message says why, never a value."""


@dataclass(frozen=True, slots=True)
class TableReport:
    table: str
    rows: int
    sha256: str


@dataclass(frozen=True, slots=True)
class MigrationReport:
    direction: str
    dry_run: bool
    tables: tuple[TableReport, ...]

    def lines(self) -> list[str]:
        head = f"{self.direction}{' (dry run: nothing written)' if self.dry_run else ''}"
        return [head] + [f"  {t.table}: {t.rows} rows, sha256 {t.sha256}" for t in self.tables]


def columns_of(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    """A table's columns, in SQLite's order (Postgres has the same names, plus ``ord``)."""
    if table not in TABLES:
        raise ValueError(table)
    return tuple(row[1] for row in connection.execute(f"PRAGMA table_info({table})"))


def _normal(value: Any) -> Any:
    if isinstance(value, memoryview | bytearray):
        return bytes(value)
    return value


def _digest(rows: Sequence[tuple[Any, ...]]) -> str:
    return hashlib.sha256(repr(sorted(rows, key=repr)).encode()).hexdigest()


def _sqlite_rows(connection: sqlite3.Connection, table: str) -> list[tuple[Any, ...]]:
    columns = ", ".join(columns_of(connection, table))
    return [
        tuple(_normal(v) for v in row)
        for row in connection.execute(f"SELECT {columns} FROM {table} ORDER BY rowid")
    ]


# How Postgres is read: the tables with an ``ord`` column in that order, which must equal SQLite's
# rowid order (it is the order the stores list them in); the others by their key.
ORDER_BY = {
    "organizations": "ord",
    "applications": "ord",
    "api_keys": "ord",
    "usage_events": "seq",
    "users": "id",
    "sessions": "ord",
    "audit_log": "seq",
    "role_assignments": "ord",
    "app_roles": "ord",
    "provider_configs": "id",
    "provider_keys": "provider_id",
    "modes": "scope_type, scope_id",
    "settings_current": "scope_type, scope_id",
    "settings_versions": "scope_type, scope_id, version",
}
IN_ORDER = frozenset(t for t, order in ORDER_BY.items() if order in ("ord", "seq"))


async def _postgres_rows(
    connection: Any, table: str, columns: Sequence[str]
) -> list[tuple[Any, ...]]:
    rows = await connection.fetch(
        f"SELECT {', '.join(columns)} FROM {table} ORDER BY {ORDER_BY[table]}"
    )
    return [tuple(_normal(v) for v in row.values()) for row in rows]


def _check_sqlite_current(connection: sqlite3.Connection) -> None:
    current = MigrationRunner(connection, MIGRATIONS, SystemEpoch()).current_version()
    if current != len(MIGRATIONS):
        raise MigrationRefused(
            f"the SQLite database is at migration {current}, not {len(MIGRATIONS)}: run "
            "`scripts/manage.py migrate` first"
        )


async def _check_postgres_fresh(connection: Any) -> None:
    for table in TABLES:
        count = int(await connection.fetchval(f"SELECT count(*) FROM {table}"))
        expected = 1 if table == "organizations" else 0
        if count != expected:
            raise MigrationRefused(
                f"the Postgres database is not new ({table} has {count} rows): migrate only "
                "into a database whose schema was just applied"
            )
    found = await connection.fetchval("SELECT id FROM organizations")
    if found != _DEFAULT_ORGANIZATION:
        raise MigrationRefused("the Postgres database is not new (an unexpected organization)")


def _compare(
    expected: dict[str, list[tuple[Any, ...]]], found: dict[str, list[tuple[Any, ...]]]
) -> None:
    """The same rows in every table, and in the same order where the order is kept."""
    for table in TABLES:
        want, got = expected[table], found[table]
        if table not in IN_ORDER:
            want, got = sorted(want, key=repr), sorted(got, key=repr)
        if want != got:
            raise MigrationRefused(
                f"{table} differs after the copy ({len(expected[table])} rows expected, "
                f"{len(found[table])} found): nothing was kept"
            )


class _DryRun(Exception):
    """Raised inside the transaction of a dry run, after every check, to roll it all back."""


async def migrate_to_postgres(
    sqlite_connection: sqlite3.Connection, db: PostgresDatabase, *, dry_run: bool
) -> MigrationReport:
    """Create the Postgres schema if it has none, copy every table, check it, and commit; or
    with ``dry_run`` do all of that and roll it back. Raises ``MigrationRefused`` with nothing
    written."""
    _check_sqlite_current(sqlite_connection)
    source = {t: _sqlite_rows(sqlite_connection, t) for t in TABLES}
    columns = {t: columns_of(sqlite_connection, t) for t in TABLES}
    report = MigrationReport(
        "SQLite to Postgres",
        dry_run,
        tuple(TableReport(t, len(source[t]), _digest(source[t])) for t in TABLES),
    )
    try:
        async with db.management.acquire() as connection, connection.transaction():
            await ensure_schema(connection)
            # The write lock: no management write of a starting gateway can come between.
            await connection.execute("SELECT pg_advisory_xact_lock($1)", WRITE_LOCK)
            await _check_postgres_fresh(connection)
            await connection.execute("DELETE FROM organizations")
            for table in TABLES:
                names = columns[table]
                marks = ", ".join(f"${i}" for i in range(1, len(names) + 1))
                overriding = " OVERRIDING SYSTEM VALUE" if table in _SEQUENCED else ""
                await connection.executemany(
                    f"INSERT INTO {table} ({', '.join(names)}){overriding} VALUES ({marks})",
                    source[table],
                )
            for table in _SEQUENCED:
                await connection.execute(
                    f"SELECT setval(pg_get_serial_sequence('{table}', 'seq'),"
                    f" coalesce((SELECT max(seq) FROM {table}), 0) + 1, false)"
                )
            copied = {t: await _postgres_rows(connection, t, columns[t]) for t in TABLES}
            _compare(source, copied)
            if dry_run:
                raise _DryRun
    except _DryRun:
        pass
    return report


_SEQUENCED = ("usage_events", "audit_log")


def _refuse_existing(target: Path) -> None:
    if target.exists():
        raise MigrationRefused(f"{target} exists; the copy back never overwrites a file")


def _fill_sqlite(
    connection: sqlite3.Connection,
    columns: dict[str, tuple[str, ...]],
    source: dict[str, list[tuple[Any, ...]]],
) -> None:
    """Migrate a new SQLite database to the latest version and fill it in one transaction,
    checked against ``source``; rolled back if anything differs."""
    MigrationRunner(connection, MIGRATIONS, SystemEpoch()).migrate()
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("DELETE FROM organizations")
        for table in TABLES:
            names = columns[table]
            marks = ", ".join("?" for _ in names)
            connection.executemany(
                f"INSERT INTO {table} ({', '.join(names)}) VALUES ({marks})", source[table]
            )
        _compare(source, {t: _sqlite_rows(connection, t) for t in TABLES})
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")


def _write_sqlite(
    target: Path | None,
    columns: dict[str, tuple[str, ...]],
    source: dict[str, list[tuple[Any, ...]]],
) -> None:
    """The new SQLite file, filled and checked; removed if anything fails (it was made here, a
    moment ago). ``None`` fills a database in memory: the dry run, every check, no file."""
    if target is None:
        memory = open_database(":memory:")
        try:
            _fill_sqlite(memory, columns, source)
        finally:
            memory.close()
        return
    connection = open_database(target)
    try:
        _fill_sqlite(connection, columns, source)
    except BaseException:
        connection.close()
        for leftover in (target, target.with_name(target.name + "-wal"),
                         target.with_name(target.name + "-shm")):  # fmt: skip
            leftover.unlink(missing_ok=True)
        raise
    connection.close()


def _latest_columns() -> dict[str, tuple[str, ...]]:
    probe = open_database(":memory:")
    try:
        MigrationRunner(probe, MIGRATIONS, SystemEpoch()).migrate()
        return {t: columns_of(probe, t) for t in TABLES}
    finally:
        probe.close()


async def migrate_to_sqlite(
    db: PostgresDatabase, target: Path, *, dry_run: bool
) -> MigrationReport:
    """Copy one snapshot of Postgres into a new SQLite file at the latest migration, check it,
    and keep it; or with ``dry_run`` do the same into memory. Never writes over a file."""
    _refuse_existing(target)
    columns = _latest_columns()
    async with (
        db.management.acquire() as connection,
        connection.transaction(isolation="repeatable_read", readonly=True),
    ):
        source = {t: await _postgres_rows(connection, t, columns[t]) for t in TABLES}
    _write_sqlite(None if dry_run else target, columns, source)
    return MigrationReport(
        "Postgres to SQLite",
        dry_run,
        tuple(TableReport(t, len(source[t]), _digest(source[t])) for t in TABLES),
    )
