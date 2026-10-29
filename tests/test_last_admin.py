"""The last org_admin and platform_admin stay: the count and the change are one transaction.
See docs/28-roles.md."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from accounts_helpers import ORIGIN_MS, PASSWORD, make_service
from gateway.applications import SqliteApplicationStore
from gateway.auth import AccountService, LastAdminError
from gateway.clock import ClockEpoch, SimClock
from gateway.database import open_database
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.organizations import new_organization
from gateway.permissions import Assignment, ScopeType

NORTH_ADMIN = Assignment("org_admin", ScopeType.ORGANIZATION, "north")


def _service(path: Path, *, busy_timeout_ms: int | None = None) -> AccountService:
    """A service on its own connection to the database at ``path``: what a second process
    (the CLI beside the server) has."""
    clock = SimClock()
    connection = open_database(path)
    if busy_timeout_ms is not None:
        connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
    return make_service(
        clock,
        ClockEpoch(clock, origin_ms=ORIGIN_MS),
        connection,
        SqliteApplicationStore(connection),
    )


@pytest.fixture
async def path(tmp_path: Path) -> Path:
    """north, with two org_admins: alice and carol."""
    database = tmp_path / "gateway.db"
    connection = open_database(database)
    MigrationRunner(connection, MIGRATIONS, ClockEpoch(SimClock())).migrate()
    connection.close()
    service = _service(database)
    await service._organizations.add_organization(
        new_organization("north", "North", "business", created_at_ms=0)
    )
    for name in ("alice", "carol"):
        await service.create_user(name, PASSWORD, "north")
        await service.grant_role(name, "org_admin", "organization", "north")
    return database


def _holders(path: Path) -> list[str]:
    connection = open_database(path)
    try:
        rows = connection.execute(
            "SELECT u.username FROM role_assignments AS r JOIN users AS u ON u.id = r.user_id"
            " WHERE r.role = 'org_admin' AND r.scope_id = 'north' ORDER BY u.username"
        ).fetchall()
    finally:
        connection.close()
    return [row[0] for row in rows]


async def test_two_revocations_back_to_back_leave_one(path: Path) -> None:
    service = _service(path)
    await service.revoke_role("alice", "org_admin", "organization", "north")
    with pytest.raises(LastAdminError, match="last org_admin of organization 'north'"):
        await service.revoke_role("carol", "org_admin", "organization", "north")
    assert _holders(path) == ["carol"]


async def test_a_revocation_from_another_connection_cannot_slip_in_between(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While one connection counts the other admins and revokes, a second connection's
    revocation of the other admin is held off (the write transaction is taken before the
    count), and once it can run it finds the last admin and is refused."""
    first = _service(path)
    second = _service(path, busy_timeout_ms=0)  # refused at once instead of waiting
    outcomes: list[str] = []
    counting = first.roles.enabled_holders

    async def count_then_race(assignment: Assignment) -> list[str]:
        holders = await counting(assignment)
        if assignment == NORTH_ADMIN and not outcomes:
            try:
                await second.revoke_role("carol", "org_admin", "organization", "north")
                outcomes.append("revoked in between")
            except sqlite3.OperationalError as exc:
                outcomes.append(f"held off: {exc}")
        return holders

    monkeypatch.setattr(first.roles, "enabled_holders", count_then_race)
    await first.revoke_role("alice", "org_admin", "organization", "north")
    assert outcomes == ["held off: database is locked"]
    with pytest.raises(LastAdminError):
        await second.revoke_role("carol", "org_admin", "organization", "north")
    assert _holders(path) == ["carol"]


async def test_disabling_is_guarded_the_same_way(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _service(path)
    second = _service(path, busy_timeout_ms=0)
    outcomes: list[str] = []
    counting = first.roles.enabled_holders

    async def count_then_race(assignment: Assignment) -> list[str]:
        holders = await counting(assignment)
        if assignment == NORTH_ADMIN and not outcomes:
            try:
                await second.disable_user("carol")
                outcomes.append("disabled in between")
            except sqlite3.OperationalError as exc:
                outcomes.append(f"held off: {exc}")
        return holders

    monkeypatch.setattr(first.roles, "enabled_holders", count_then_race)
    await first.disable_user("alice")
    assert outcomes == ["held off: database is locked"]
    with pytest.raises(LastAdminError):
        await second.disable_user("carol")
