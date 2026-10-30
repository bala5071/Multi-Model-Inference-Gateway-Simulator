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

import ipaddress
import sqlite3
from collections.abc import Coroutine, Mapping
from dataclasses import dataclass
from typing import Any, Literal, TypeVar, cast
from urllib.parse import parse_qs, urlsplit

from gateway.applications import ApplicationStore, SqliteApplicationStore
from gateway.audit import AuditStore, SqliteAuditStore
from gateway.clock import EpochSource, EpochStoreClock, StoreClock
from gateway.modes import ModeStore, SqliteModeStore
from gateway.providers import ProviderStore, SqliteProviderStore
from gateway.roles import RoleStore, SqliteRoleStore
from gateway.usage import SqliteUsageStore, UsageStore
from gateway.users import AccountStore, SqliteAccountStore

T = TypeVar("T")


STATE_ENV = "GATEWAY_STATE"
# The settings shared mode needs (gateway.postgres and gateway.valkey read them; they are named
# here because both modules import this one).
POSTGRES_URL_ENV = "GATEWAY_POSTGRES_URL"
COUNTERS_ENV = "GATEWAY_COUNTERS"
# A plain connection to a store that is not on this machine is refused unless this is "1": the
# operator vouches for the network (decision 2 of the scale-out plan, B1 of PR 3b).
PRIVATE_NETWORK_ENV = "GATEWAY_STATE_PRIVATE_NETWORK"


class SharedStateRefusedError(RuntimeError):
    """``GATEWAY_STATE=shared`` asked for without what it needs: the budgets on Postgres and the
    counters in Valkey."""


def check_state_setting(environ: Mapping[str, str]) -> None:
    """``GATEWAY_STATE``: ``sqlite`` (the default) or ``shared``. ``shared`` starts only with
    budgets on Postgres (``GATEWAY_POSTGRES_URL``): with each instance keeping its own budget
    totals, two instances could each spend one budget (docs/37). It also needs the counters in
    Valkey (``GATEWAY_COUNTERS=valkey``): with its own counters, each instance would grant its
    own rate limit and login attempts. Errors name settings, never their values."""
    kind = environ.get(STATE_ENV, "sqlite")
    if kind == "sqlite":
        return
    if kind != "shared":
        raise ValueError(f"{STATE_ENV} must be sqlite or shared, not {kind!r}")
    if not environ.get(POSTGRES_URL_ENV):
        raise SharedStateRefusedError(
            f"{STATE_ENV}=shared needs {POSTGRES_URL_ENV}: budgets are shared between instances "
            "only on Postgres, and without them each instance would enforce budgets on its own "
            f"totals (docs/37). Unset {STATE_ENV} to run one instance on SQLite."
        )
    if environ.get(COUNTERS_ENV) != "valkey":
        raise SharedStateRefusedError(
            f"{STATE_ENV}=shared needs {COUNTERS_ENV}=valkey: with counters of its own, each "
            "instance would grant its own rate limit and login attempts (docs/37)."
        )


class InsecureStoreAddressError(RuntimeError):
    """A store off this machine asked for without a verified TLS connection. The message names
    the setting, never the address."""


def _is_loopback(host: str | None) -> bool:
    if not host or host.startswith("/"):
        return True  # a Unix socket: this machine
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_store_address(
    kind: Literal["postgres", "valkey"], url: str, setting: str, environ: Mapping[str, str]
) -> None:
    """Before connecting: a store that is not on this machine must be reached over TLS with
    its certificate and host name checked (Postgres ``sslmode=verify-full``; Valkey
    ``rediss://``, with the client's defaults of a required certificate and a checked host
    name left on), unless ``GATEWAY_STATE_PRIVATE_NETWORK=1``. Loopback needs neither."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        raise InsecureStoreAddressError(f"{setting} is not a valid address") from None
    query = {k.lower(): [v.lower() for v in vs] for k, vs in parse_qs(parts.query).items()}
    if kind == "postgres":
        # A host given as a parameter overrides the one in the address.
        given = [h for v in query.get("host", []) for h in v.split(",")]
        hosts: list[str | None] = [*given] or [host]
        local = all(_is_loopback(h) for h in hosts)
        verified = query.get("sslmode") == ["verify-full"]
    else:
        local = _is_loopback(host)
        verified = (
            parts.scheme == "rediss"
            and query.get("ssl_cert_reqs", ["required"]) == ["required"]
            and query.get("ssl_check_hostname", ["true"]) == ["true"]
        )
    if local or verified or environ.get(PRIVATE_NETWORK_ENV) == "1":
        return
    wanted = "sslmode=verify-full" if kind == "postgres" else "a rediss:// address"
    raise InsecureStoreAddressError(
        f"{setting} is not on this machine and does not ask for a verified TLS connection "
        f"({wanted}); set {PRIVATE_NETWORK_ENV}=1 only for a network you vouch for"
    )


class StateUnavailableError(Exception):
    """A shared store or counter could not answer (unreachable, too slow, or the instance's
    clock too far from it). Whatever it would have decided is refused: answered 503
    ``state_unavailable``, never admitted (docs/scale-out-plan.md, section 4)."""


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
    """The stores of one database, and the clock their windows are decided on."""

    usage: UsageStore
    applications: ApplicationStore
    accounts: AccountStore
    roles: RoleStore
    audit: AuditStore
    providers: ProviderStore
    modes: ModeStore
    clock: StoreClock


def sqlite_state(connection: sqlite3.Connection, epoch: EpochSource) -> StateBackend:
    """Every store over one migrated SQLite connection; windows on ``epoch``."""
    return StateBackend(
        usage=SqliteUsageStore(connection),
        applications=SqliteApplicationStore(connection),
        accounts=SqliteAccountStore(connection),
        roles=SqliteRoleStore(connection),
        audit=SqliteAuditStore(connection),
        providers=SqliteProviderStore(connection),
        modes=SqliteModeStore(connection),
        clock=EpochStoreClock(epoch),
    )
