"""The durable state of one gateway database, as one bundle, and store calls at start-up.

See docs/34-state-interfaces.md and docs/scale-out-plan.md, section 2.1.

Every store is a protocol whose methods are coroutines. ``sqlite_state`` builds the SQLite
implementations over one connection, so one ``write_transaction()`` covers the writes of all
of them, as before. ``app.py`` (the default server) and ``scripts/manage.py`` build them here
and nowhere else.

``run_now`` completes a store call where no event loop runs yet: ``create_app`` checking the
stored applications' plans, and the default server creating the bootstrap admin. That is only
possible for a call that never suspends, which is true of the SQLite and in-memory stores; it
raises for any other, so a store that waits on a network cannot be used there by mistake.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any, TypeVar, cast

from gateway.applications import ApplicationStore, SqliteApplicationStore
from gateway.audit import AuditStore, SqliteAuditStore
from gateway.modes import ModeStore, SqliteModeStore
from gateway.providers import ProviderStore, SqliteProviderStore
from gateway.roles import RoleStore, SqliteRoleStore
from gateway.usage import SqliteUsageStore, UsageStore
from gateway.users import AccountStore, SqliteAccountStore

T = TypeVar("T")


class StoreSuspendedError(RuntimeError):
    """A store call given to ``run_now`` waited for something: it needs an event loop."""


def run_now(call: Coroutine[Any, Any, T]) -> T:
    """The result of ``call``, run to completion here, without an event loop. Raises
    ``StoreSuspendedError`` (after closing it) if it suspends instead of completing."""
    try:
        call.send(None)
    except StopIteration as done:
        return cast(T, done.value)
    call.close()
    raise StoreSuspendedError(
        "a store call suspended outside an event loop; run it in the lifespan instead"
    )


@dataclass(frozen=True, slots=True)
class StateBackend:
    """The stores of one database."""

    usage: UsageStore
    applications: ApplicationStore
    accounts: AccountStore
    roles: RoleStore
    audit: AuditStore
    providers: ProviderStore
    modes: ModeStore


def sqlite_state(connection: sqlite3.Connection) -> StateBackend:
    """Every store over one migrated SQLite connection."""
    return StateBackend(
        usage=SqliteUsageStore(connection),
        applications=SqliteApplicationStore(connection),
        accounts=SqliteAccountStore(connection),
        roles=SqliteRoleStore(connection),
        audit=SqliteAuditStore(connection),
        providers=SqliteProviderStore(connection),
        modes=SqliteModeStore(connection),
    )
