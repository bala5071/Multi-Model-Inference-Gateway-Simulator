"""Migration 8: role assignments and application-defined roles. See docs/28-roles.md.

Adds ``role_assignments`` (who holds which role, in which scope, granted by whom and when) and
``app_roles`` (names an application defines, such as ``clinic_staff``; they grant nothing in
the gateway). The platform scope's ``scope_id`` is ``*``, so the unique index works (SQLite
treats NULLs as distinct).

The backfill moves ``users.platform_admin`` into assignments, gives the sole member of each
individual organization ``org_admin`` of it, and gives every other user nothing: membership
alone grants nothing. Then ``users`` is rebuilt without the column (SQLite's procedure, so it
is marked ``rebuilds_tables``: foreign keys are off around it and checked before committing,
and ``manage.py migrate`` backs the database up first). ``sessions`` and ``role_assignments``
refer to ``users`` by name, so they refer to the rebuilt table.

The reverse puts ``platform_admin`` back into the column, and drops the individual
organizations' ``org_admin`` (its sole member, which the data already says). It refuses while
any other assignment, or any application role, exists: reversing would lose it.
"""

from __future__ import annotations

import sqlite3

from gateway.migrations.runner import Migration, MigrationError, MigrationPreview

_USER_COLUMNS = (
    "id, username, password_hash, organization_id, created_at_ms, disabled, "
    "must_change_password, bootstrap, failed_logins, first_failure_at_ms, locked_until_ms"
)


def _users_table(name: str, *, with_platform_admin: bool) -> str:
    admin = (
        ", platform_admin INTEGER NOT NULL DEFAULT 0 CHECK (platform_admin IN (0, 1))"
        if with_platform_admin
        else ""
    )
    return (
        f"CREATE TABLE {name} ("
        " id TEXT PRIMARY KEY,"
        " username TEXT NOT NULL UNIQUE,"
        " password_hash TEXT NOT NULL,"
        " organization_id TEXT NOT NULL REFERENCES organizations (id),"
        " created_at_ms REAL NOT NULL,"
        " disabled INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0, 1)),"
        " must_change_password INTEGER NOT NULL DEFAULT 0 CHECK (must_change_password IN (0, 1)),"
        " bootstrap INTEGER NOT NULL DEFAULT 0 CHECK (bootstrap IN (0, 1, 2)),"
        " failed_logins INTEGER NOT NULL DEFAULT 0 CHECK (failed_logins >= 0),"
        " first_failure_at_ms REAL,"
        f" locked_until_ms REAL{admin})"
    )


def _rebuild_users(connection: sqlite3.Connection, *, with_platform_admin: bool) -> None:
    connection.execute(_users_table("users_new", with_platform_admin=with_platform_admin))
    if with_platform_admin:
        connection.execute(
            f"INSERT INTO users_new ({_USER_COLUMNS}, platform_admin)"
            f" SELECT {_USER_COLUMNS}, EXISTS (SELECT 1 FROM role_assignments AS r"
            "  WHERE r.user_id = users.id AND r.role = 'platform_admin')"
            " FROM users ORDER BY rowid"
        )
    else:
        connection.execute(
            f"INSERT INTO users_new ({_USER_COLUMNS})"
            f" SELECT {_USER_COLUMNS} FROM users ORDER BY rowid"
        )
    connection.execute("DROP INDEX users_by_organization")
    connection.execute("DROP TABLE users")
    connection.execute("ALTER TABLE users_new RENAME TO users")
    connection.execute("CREATE INDEX users_by_organization ON users (organization_id)")


def _sole_members(connection: sqlite3.Connection) -> list[tuple[str, str]]:
    """(user id, organization id) for each individual organization with exactly one user."""
    return connection.execute(
        "SELECT min(u.id), o.id FROM organizations AS o JOIN users AS u"
        " ON u.organization_id = o.id WHERE o.kind = 'individual'"
        " GROUP BY o.id HAVING count(*) = 1 ORDER BY o.id"
    ).fetchall()


def _preview(connection: sqlite3.Connection) -> MigrationPreview:
    has_users = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'users'"
    ).fetchone()
    admins = members = users = 0
    if has_users is not None:
        users = int(connection.execute("SELECT count(*) FROM users").fetchone()[0])
        admins = int(
            connection.execute("SELECT count(*) FROM users WHERE platform_admin = 1").fetchone()[0]
        )
        members = len(_sole_members(connection))
    return MigrationPreview(
        changes=(
            "create tables role_assignments and app_roles",
            f"platform_admin for {admins} user(s) that had users.platform_admin",
            f"org_admin for the sole member of {members} individual organization(s)",
            f"no role for the other {max(users - admins - members, 0)} user(s)",
            "rebuild table users without platform_admin",
        )
    )


def _up(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE role_assignments ("
        " user_id TEXT NOT NULL REFERENCES users (id),"
        " role TEXT NOT NULL,"
        " scope_type TEXT NOT NULL"
        "  CHECK (scope_type IN ('platform', 'organization', 'application')),"
        " scope_id TEXT NOT NULL,"
        " granted_by TEXT,"
        " granted_at_ms REAL NOT NULL,"
        " CHECK ((scope_type = 'platform') = (scope_id = '*')),"
        " UNIQUE (user_id, role, scope_type, scope_id))"
    )
    connection.execute(
        "CREATE INDEX role_assignments_by_scope ON role_assignments (scope_type, scope_id)"
    )
    connection.execute(
        "CREATE TABLE app_roles ("
        " application_id TEXT NOT NULL REFERENCES applications (id),"
        " name TEXT NOT NULL,"
        " created_at_ms REAL NOT NULL,"
        " PRIMARY KEY (application_id, name))"
    )


def _backfill(connection: sqlite3.Connection, applied_at_ms: float) -> None:
    connection.execute(
        "INSERT INTO role_assignments (user_id, role, scope_type, scope_id, granted_by,"
        " granted_at_ms) SELECT id, 'platform_admin', 'platform', '*', NULL, ? FROM users"
        " WHERE platform_admin = 1 ORDER BY rowid",
        (applied_at_ms,),
    )
    connection.executemany(
        "INSERT INTO role_assignments (user_id, role, scope_type, scope_id, granted_by,"
        " granted_at_ms) VALUES (?, 'org_admin', 'organization', ?, NULL, ?)",
        [(user_id, org_id, applied_at_ms) for user_id, org_id in _sole_members(connection)],
    )
    _rebuild_users(connection, with_platform_admin=False)


def _down(connection: sqlite3.Connection) -> None:
    # platform_admin goes back into the column; an individual organization's org_admin is
    # its sole member, which the data already says. Anything else would be lost.
    others = connection.execute(
        "SELECT count(*) FROM role_assignments AS r WHERE r.role != 'platform_admin' AND NOT ("
        " r.role = 'org_admin' AND r.scope_type = 'organization' AND EXISTS ("
        "  SELECT 1 FROM organizations AS o JOIN users AS u ON u.organization_id = o.id"
        "  WHERE o.id = r.scope_id AND o.kind = 'individual' AND u.id = r.user_id))"
    ).fetchone()[0]
    app_roles = connection.execute("SELECT count(*) FROM app_roles").fetchone()[0]
    if others or app_roles:
        raise MigrationError(
            "migration 8 cannot be reversed: role assignments other than platform_admin and the "
            "org_admin of individual organizations, or application roles, exist, and reversing "
            "would lose them. Restore the backup taken before migrating."
        )
    _rebuild_users(connection, with_platform_admin=True)
    connection.execute("DROP TABLE app_roles")
    connection.execute("DROP INDEX role_assignments_by_scope")
    connection.execute("DROP TABLE role_assignments")


MIGRATION = Migration(
    8, "roles", _up, _down, backfill=_backfill, rebuilds_tables=True, preview=_preview
)
