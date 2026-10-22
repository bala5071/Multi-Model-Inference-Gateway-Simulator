"""Building blocks for the account tests: a migrated in-memory database, the account service
on a SimClock with inline hashing at the cheapest argon2 cost, and an app serving /v1/auth.
Test values, not secrets."""

from __future__ import annotations

import random
import re
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx

from gateway.app import create_app
from gateway.applications import SqliteApplicationStore
from gateway.audit import AuditLog
from gateway.auth import SESSION_COOKIE, AccountAccess, AccountService
from gateway.clock import ClockEpoch, SimClock
from gateway.continuous import build_continuous_registry
from gateway.database import open_database
from gateway.localonly import LocalOnlyPolicy
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.organizations import new_organization
from gateway.passwords import CHEAP, InlineHashRunner, PasswordHasher, Passwords
from gateway.users import AccountStore
from helpers import TEST_PEPPER

ORIGIN_MS = 1.75e12
PASSWORD = "plum harbor ninety violet"
OTHER_PASSWORD = "seven copper kites at dawn"
COOKIE_PATTERN = re.compile(SESSION_COOKIE + r"=([A-Za-z0-9_-]{43}); ")


@dataclass
class Accounts:
    clock: SimClock
    epoch: ClockEpoch
    connection: sqlite3.Connection
    organizations: SqliteApplicationStore
    store: AccountStore
    audit: AuditLog
    service: AccountService

    def restart(self) -> AccountService:
        """A new service on the same database: what a restarted process has."""
        return make_service(self.clock, self.epoch, self.connection, self.organizations)


def make_service(
    clock: SimClock,
    epoch: ClockEpoch,
    connection: sqlite3.Connection,
    organizations: SqliteApplicationStore,
    *,
    seed: int = 1,
    audit_retention_ms: float | None = None,
    unknown_user_rows_per_minute: int = 60,
    secret: bytes = TEST_PEPPER,
) -> AccountService:
    return AccountService(
        AccountStore(connection),
        AuditLog(connection, unknown_user_rows_per_minute=unknown_user_rows_per_minute),
        Passwords(PasswordHasher(CHEAP), InlineHashRunner()),
        organizations=organizations,
        clock=clock,
        epoch=epoch,
        secret=secret,
        random_bytes=random.Random(seed).randbytes,
        audit_retention_ms=audit_retention_ms,
    )


def make_accounts(**kwargs: object) -> Accounts:
    clock = SimClock()
    epoch = ClockEpoch(clock, origin_ms=ORIGIN_MS)
    connection = open_database(":memory:")
    MigrationRunner(connection, MIGRATIONS, epoch).migrate()
    organizations = SqliteApplicationStore(connection)
    organizations.add_organization(new_organization("solo", "Solo", "individual", created_at_ms=0))
    organizations.add_organization(new_organization("north", "North", "business", created_at_ms=0))
    service = make_service(clock, epoch, connection, organizations, **kwargs)  # type: ignore[arg-type]
    return Accounts(clock, epoch, connection, organizations, service.store, service.audit, service)


@dataclass
class Console:
    client: httpx.AsyncClient
    accounts: Accounts


@asynccontextmanager
async def console(accounts: Accounts, *, cookie_secure: bool = False) -> AsyncIterator[Console]:
    """The gateway with console accounts, reached from 127.0.0.1 as http://localhost."""
    registry = build_continuous_registry(accounts.clock, seed=7)
    app = create_app(
        registry,
        accounts.clock,
        accounts=AccountAccess(accounts.service, cookie_secure=cookie_secure),
        local_only=LocalOnlyPolicy(),
        epoch=accounts.epoch,
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield Console(client, accounts)
    await registry.aclose()


def session_id_of(response: httpx.Response) -> str:
    match = COOKIE_PATTERN.match(response.headers["set-cookie"])
    assert match is not None, "no session cookie"
    return match.group(1)


async def http_login(
    console: Console, username: str, password: str, **headers: str
) -> tuple[httpx.Response, str | None, str | None]:
    """POST /v1/auth/login; returns the response, the session id and the CSRF token (None
    when refused). The client's cookie jar is cleared: tests send cookies explicitly."""
    response = await console.client.post(
        "/v1/auth/login", json={"username": username, "password": password}, headers=headers
    )
    console.client.cookies.clear()
    if response.status_code != 200:
        return response, None, None
    return response, session_id_of(response), response.json()["csrf_token"]


def cookie(session_id: str) -> dict[str, str]:
    return {"Cookie": f"{SESSION_COOKIE}={session_id}"}


def with_csrf(session_id: str, csrf: str) -> dict[str, str]:
    return {**cookie(session_id), "X-CSRF-Token": csrf}
