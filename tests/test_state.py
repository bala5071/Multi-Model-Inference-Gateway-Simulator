"""The store bundle, ``run_now``, and the write transaction the stores of one database share.

See docs/34-state-interfaces.md.
"""

from __future__ import annotations

import types
from collections.abc import Generator, Iterator

import pytest

from gateway.apikeys import ApiKeyService
from gateway.audit import AuditLog
from gateway.clock import ClockEpoch, SimClock
from gateway.database import open_database
from gateway.management import KeyLimits, ManagementService
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.organizations import new_organization
from gateway.routing import load_plans
from gateway.state import StateBackend, StoreSuspendedError, run_now, sqlite_state
from gateway.users import User
from helpers import TEST_PEPPER


@pytest.fixture
def state() -> Iterator[StateBackend]:
    connection = open_database(":memory:")
    MigrationRunner(connection, MIGRATIONS, ClockEpoch(SimClock())).migrate()
    yield sqlite_state(connection, ClockEpoch(SimClock()))
    connection.close()


# --- run_now ---------------------------------------------------------------------------------


def test_run_now_completes_a_store_call_without_an_event_loop(state: StateBackend) -> None:
    run_now(
        state.applications.add_organization(
            new_organization("north", "North", "business", created_at_ms=0)
        )
    )
    found = run_now(state.applications.get_organization("north"))
    assert found is not None
    assert found.name == "North"


def test_run_now_refuses_a_call_that_suspends_and_leaves_no_coroutine_behind() -> None:
    """A store that waits on a network cannot be used where no event loop runs: refused, and
    the coroutine is closed (an un-awaited one would fail this test, see pyproject.toml)."""

    @types.coroutine
    def suspend() -> Generator[None, None, None]:
        yield  # what awaiting a network read comes down to

    async def waits() -> int:
        await suspend()
        return 1

    with pytest.raises(StoreSuspendedError):
        run_now(waits())


# --- one write transaction over every store of a database -------------------------------------


def _user() -> User:
    return User("usr_" + "0" * 16, "alice", "hash", "default", 0.0)


async def test_a_write_transaction_covers_every_store_of_the_database(state: StateBackend) -> None:
    """Begun on one store, it holds the writes of the others: all of them or none."""

    async def both_then_fail() -> None:
        async with state.accounts.write_transaction():
            await state.applications.add_organization(
                new_organization("north", "North", "business", created_at_ms=0)
            )
            await state.accounts.add_user(_user())
            raise RuntimeError("stop")

    with pytest.raises(RuntimeError, match="stop"):
        await both_then_fail()
    assert await state.applications.get_organization("north") is None
    assert await state.accounts.find_user("alice") is None
    async with state.applications.write_transaction():
        await state.applications.add_organization(
            new_organization("north", "North", "business", created_at_ms=0)
        )
        await state.accounts.add_user(_user())
    assert await state.applications.get_organization("north") is not None
    assert await state.accounts.find_user("alice") is not None


async def test_a_key_and_its_audit_entry_are_written_together(
    state: StateBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The active-key count, the key and its audit entry are one transaction (scale-out plan,
    section 3.6): an audit write that fails leaves no key behind."""
    epoch = ClockEpoch(SimClock())
    keys = ApiKeyService(state.applications, TEST_PEPPER, epoch)
    audit = AuditLog(state.audit)
    service = ManagementService(keys, load_plans(), audit, epoch, limits=KeyLimits())
    await service.create_application("default", "Shop", "pro", application_id="shop")

    async def broken(*_: object) -> bool:
        raise RuntimeError("the audit log is unavailable")

    monkeypatch.setattr(audit, "write", broken)
    with pytest.raises(RuntimeError, match="unavailable"):
        await service.issue_key("shop", "main", expires_in_days=30)
    monkeypatch.undo()
    assert await state.applications.list_keys("shop") == []
    issued = await service.issue_key("shop", "main", expires_in_days=30)
    assert [k.key_id for k in await state.applications.list_keys("shop")] == [issued.record.key_id]
