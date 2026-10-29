"""Role assignments and application-defined roles, and the store that keeps them.

See docs/28-roles.md. Who may grant what, and the last-admin rule, are in
``gateway.auth.AccountService``; what each role allows is ``gateway.permissions``. Store calls
are coroutines that never suspend, for the reason given in ``gateway.usage``.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Protocol

from gateway.permissions import Assignment, Role, ScopeType

# Application-defined role names: the same characters as usernames, and never a built-in name.
APP_ROLE_PATTERN = re.compile(r"[a-z0-9._-]{3,64}")


def check_app_role_name(name: str) -> str:
    if not APP_ROLE_PATTERN.fullmatch(name):
        raise ValueError("a role name needs 3 to 64 of a-z, 0-9, '.', '-' and '_'")
    if name in {role.value for role in Role}:
        raise ValueError(f"{name!r} is a built-in role")
    return name


class RoleStore(Protocol):
    """Role assignments and application-defined roles (docs/34-state-interfaces.md)."""

    async def assignments_for(self, user_id: str) -> tuple[Assignment, ...]: ...

    async def grant(
        self, user_id: str, assignment: Assignment, granted_by: str | None, at_ms: float
    ) -> None:
        """Raise ``ValueError`` if the user already holds it."""
        ...

    async def revoke(self, user_id: str, assignment: Assignment) -> bool: ...

    async def enabled_holders(self, assignment: Assignment) -> list[str]:
        """The ids of enabled users holding exactly this assignment."""
        ...

    async def platform_admin_exists(self) -> bool: ...

    async def define_app_role(self, application_id: str, name: str, at_ms: float) -> None: ...

    async def app_roles(self, application_id: str) -> list[str]: ...


class SqliteRoleStore:
    """The tables of migration 8."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        found = connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
            " AND name IN ('role_assignments', 'app_roles')"
        ).fetchone()
        if found[0] != 2:
            raise RuntimeError("role_assignments and app_roles do not exist; run the migrations")
        self._connection = connection

    async def assignments_for(self, user_id: str) -> tuple[Assignment, ...]:
        rows = self._connection.execute(
            "SELECT role, scope_type, scope_id FROM role_assignments WHERE user_id = ?"
            " ORDER BY rowid",
            (user_id,),
        ).fetchall()
        return tuple(Assignment(role, ScopeType(kind), scope) for role, kind, scope in rows)

    async def grant(
        self, user_id: str, assignment: Assignment, granted_by: str | None, at_ms: float
    ) -> None:
        """Raise ``ValueError`` if the user already holds it."""
        try:
            self._connection.execute(
                "INSERT INTO role_assignments (user_id, role, scope_type, scope_id, granted_by,"
                " granted_at_ms) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    assignment.role,
                    assignment.scope_type.value,
                    assignment.scope_id,
                    granted_by,
                    at_ms,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("the user already holds that role in that scope") from exc

    async def revoke(self, user_id: str, assignment: Assignment) -> bool:
        cursor = self._connection.execute(
            "DELETE FROM role_assignments WHERE user_id = ? AND role = ? AND scope_type = ?"
            " AND scope_id = ?",
            (user_id, assignment.role, assignment.scope_type.value, assignment.scope_id),
        )
        return cursor.rowcount == 1

    async def enabled_holders(self, assignment: Assignment) -> list[str]:
        """The ids of enabled users holding exactly this assignment."""
        rows = self._connection.execute(
            "SELECT r.user_id FROM role_assignments AS r JOIN users AS u ON u.id = r.user_id"
            " WHERE r.role = ? AND r.scope_type = ? AND r.scope_id = ? AND u.disabled = 0"
            " ORDER BY r.rowid",
            (assignment.role, assignment.scope_type.value, assignment.scope_id),
        ).fetchall()
        return [row[0] for row in rows]

    async def platform_admin_exists(self) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM role_assignments WHERE role = ? AND scope_type = ? LIMIT 1",
            (Role.PLATFORM_ADMIN.value, ScopeType.PLATFORM.value),
        ).fetchone()
        return row is not None

    # --- application-defined roles --------------------------------------------------------

    async def define_app_role(self, application_id: str, name: str, at_ms: float) -> None:
        try:
            self._connection.execute(
                "INSERT INTO app_roles (application_id, name, created_at_ms) VALUES (?, ?, ?)",
                (application_id, check_app_role_name(name), at_ms),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("that role exists, or there is no such application") from exc

    async def app_roles(self, application_id: str) -> list[str]:
        rows = self._connection.execute(
            "SELECT name FROM app_roles WHERE application_id = ? ORDER BY rowid", (application_id,)
        ).fetchall()
        return [row[0] for row in rows]
