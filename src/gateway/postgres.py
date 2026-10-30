"""The durable stores in Postgres, for several gateway instances (docs/36-postgres.md).

See docs/scale-out-plan.md, sections 2, 3.2, 3.3, 3.6 and 3.8. SQLite stays the default; these
are the same protocols over the same tables (one baseline, ``PG_BASELINE``, equal to SQLite
migrations 1 to 13, plus an ``ord`` column where the SQLite stores order by ``rowid``).

- **Two pools.** ``write_transaction()`` takes a connection from the management pool; every
  call outside one uses the request pool. A management write that holds the lock and its
  connection takes nothing a request needs (section 3.6, decision 19).
- **One write lock for management writes**, the transaction-level advisory lock
  ``WRITE_LOCK``, taken with ``pg_try_advisory_xact_lock`` in a loop on the injected clock: no
  longer than ``lock_timeout_ms``, then ``StateUnavailableError`` with nothing written. The
  holder is bounded by ``statement_timeout`` and ``idle_in_transaction_session_timeout``.
  Nothing on the request path takes it.
- **The database's clock decides its windows** (section 3.8): ``gateway_now_ms()`` in SQL, which
  a test schema replaces. The step-up claim and the lockout count read it in their statement;
  the services read it through this database as their ``StoreClock``; a key's lookup returns it.
- **Missing is forbidden.** A connection that cannot be had or is lost, a query out of time, a
  pool exhausted past its timeout: ``StateUnavailableError`` (answered 503), never a default.
- **Budgets** (``PostgresBudgets``, docs/37): reserve, settle and release in one statement each
  that decides, on the counter row of the application's period (section 3.1).
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import asyncpg

from gateway.applications import (
    _APP_COLUMNS,
    _KEY_COLUMNS,
    _ORG_COLUMNS,
    ApiKeyRecord,
    Application,
    KeyLookup,
    _application,
    _check_organization,
    _checked_name,
    _key,
    _name_taken,
    _organization,
)
from gateway.audit import AuditEntry, AuditEvent, Reason, _entry
from gateway.clock import Clock
from gateway.ledger import ApplicationUsage, BudgetExceededError, Expired, Reservation
from gateway.management import ManagementRefusal
from gateway.modes import DEFAULT_MODE, Mode
from gateway.organizations import Organization, normalize_name
from gateway.periods import BudgetPeriods, CalendarMonthsUTC
from gateway.permissions import Assignment, Role, ScopeType
from gateway.providers import _COLUMNS as _PROVIDER_COLUMNS
from gateway.providers import ProviderConfig, StoredKey, _config
from gateway.roles import check_app_role_name
from gateway.schema import RequestResult
from gateway.state import POSTGRES_URL_ENV, StateBackend, StateUnavailableError
from gateway.usage import _COLUMNS as _USAGE_COLUMNS
from gateway.usage import (
    ChatSource,
    ModelUsage,
    StoredUsageEvent,
    UsageEvent,
    _event,
    _totals,
)
from gateway.users import (
    _SESSION_COLUMNS,
    _USER_COLUMNS,
    Bootstrap,
    FailureCount,
    Session,
    User,
    _session,
    _user,
)
from gateway.vault import SealedKey

URL_ENV = POSTGRES_URL_ENV
# A Postgres URL without a password is refused unless this is "1": only the test database of
# docker-compose.postgres.yml, which trusts every local connection, goes without one.
NO_PASSWORD_FOR_TESTS_ENV = "GATEWAY_POSTGRES_TEST_NO_PASSWORD"
LOCK_TIMEOUT_ENV = "GATEWAY_STATE_LOCK_TIMEOUT_MS"
DEFAULT_LOCK_TIMEOUT_MS = 2000.0
# How long a management transaction may run, and sit idle, before Postgres ends it: a stuck
# holder releases the lock instead of keeping every other management write waiting.
STATEMENT_TIMEOUT_MS = 5000
IDLE_IN_TRANSACTION_MS = 10_000
# asyncpg's own limits on a query and on waiting for a pooled connection (decision 7 of PR 3).
COMMAND_TIMEOUT_S = 2.0
LOCK_POLL_MS = 20.0
# The advisory lock keys: management writes, and applying the schema. Fixed, distinct.
WRITE_LOCK = 0x6777_5752_4954_4531  # "gwWRITE1"
SCHEMA_LOCK = 0x6777_5343_4845_4D31  # "gwSCHEM1"
SCHEMA_VERSION = 2
# How long a settled reservation's id is kept, so that a settle tried again is not charged twice
# (docs/37).
SETTLED_KEEP_MS = 24 * 60 * 60 * 1000.0
# A settle or release that could not be written is tried again, waiting this long at first,
# doubling up to the most.
RETRY_FIRST_MS = 100.0
RETRY_MOST_MS = 5_000.0

LOG = logging.getLogger("gateway.postgres")

# The failures that mean the database could not answer: refused, never a default answer.
_UNAVAILABLE: tuple[type[BaseException], ...] = (
    OSError,
    TimeoutError,
    asyncpg.exceptions.PostgresConnectionError,
    asyncpg.exceptions.InterfaceError,
    asyncpg.exceptions.CannotConnectNowError,
    asyncpg.exceptions.TooManyConnectionsError,
    asyncpg.exceptions.QueryCanceledError,
    asyncpg.exceptions.IdleInTransactionSessionTimeoutError,
    asyncpg.exceptions.AdminShutdownError,
)

PG_BASELINE = """
CREATE FUNCTION gateway_now_ms() RETURNS double precision LANGUAGE sql VOLATILE AS
  $$ SELECT extract(epoch FROM clock_timestamp()) * 1000 $$;

CREATE TABLE usage_events (
  seq bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  application_id text NOT NULL, request_id text NOT NULL, model text NOT NULL,
  prompt_tokens integer NOT NULL CHECK (prompt_tokens >= 0),
  cached_tokens integer NOT NULL CHECK (cached_tokens >= 0 AND cached_tokens <= prompt_tokens),
  completion_tokens integer NOT NULL CHECK (completion_tokens >= 0),
  base_cost_usd double precision NOT NULL, billed_cost_usd double precision NOT NULL,
  occurred_at_ms double precision NOT NULL,
  provider text,
  provider_class text CHECK (provider_class IS NULL OR provider_class IN ('local', 'hosted')),
  fallback_from text, fallback_reason text,
  usage_estimated integer CHECK (usage_estimated IS NULL OR usage_estimated IN (0, 1)));
CREATE INDEX usage_events_by_application_time ON usage_events (application_id, occurred_at_ms);
CREATE INDEX usage_events_by_time ON usage_events (occurred_at_ms);

CREATE TABLE organizations (
  ord bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
  id text PRIMARY KEY, name text NOT NULL UNIQUE,
  kind text NOT NULL CHECK (kind IN ('individual', 'business')),
  created_at_ms double precision NOT NULL,
  disabled integer NOT NULL DEFAULT 0 CHECK (disabled IN (0, 1)));
INSERT INTO organizations (id, name, kind, created_at_ms, disabled)
  VALUES ('default', 'default', 'business', 0.0, 0);

CREATE TABLE applications (
  ord bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
  id text PRIMARY KEY, name text NOT NULL, plan_name text NOT NULL, models text NOT NULL,
  price_multiplier double precision NOT NULL CHECK (price_multiplier >= 0),
  monthly_token_budget bigint NOT NULL CHECK (monthly_token_budget >= 0),
  max_tokens integer CHECK (max_tokens IS NULL OR max_tokens >= 1),
  requests_per_minute integer CHECK (requests_per_minute IS NULL OR requests_per_minute >= 1),
  created_at_ms double precision NOT NULL,
  organization_id text NOT NULL REFERENCES organizations (id));
CREATE UNIQUE INDEX applications_name_per_organization ON applications (organization_id, name);

CREATE TABLE api_keys (
  ord bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
  key_id text PRIMARY KEY, application_id text NOT NULL REFERENCES applications (id),
  label text NOT NULL, key_hmac text NOT NULL, created_at_ms double precision NOT NULL,
  expires_at_ms double precision,
  revoked integer NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1)),
  last_used_at_ms double precision);
CREATE INDEX api_keys_by_application ON api_keys (application_id);

CREATE TABLE users (
  id text PRIMARY KEY, username text NOT NULL UNIQUE, password_hash text NOT NULL,
  organization_id text NOT NULL REFERENCES organizations (id),
  created_at_ms double precision NOT NULL,
  disabled integer NOT NULL DEFAULT 0 CHECK (disabled IN (0, 1)),
  must_change_password integer NOT NULL DEFAULT 0 CHECK (must_change_password IN (0, 1)),
  bootstrap integer NOT NULL DEFAULT 0 CHECK (bootstrap IN (0, 1, 2)),
  failed_logins integer NOT NULL DEFAULT 0 CHECK (failed_logins >= 0),
  first_failure_at_ms double precision, locked_until_ms double precision);
CREATE INDEX users_by_organization ON users (organization_id);

CREATE TABLE sessions (
  ord bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
  id_hash text PRIMARY KEY, handle text NOT NULL UNIQUE,
  user_id text NOT NULL REFERENCES users (id), csrf_hash text NOT NULL,
  created_at_ms double precision NOT NULL, last_seen_at_ms double precision NOT NULL,
  expires_at_ms double precision NOT NULL,
  revoked integer NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1)),
  step_up_until_ms double precision);
CREATE INDEX sessions_by_expiry ON sessions (expires_at_ms);
CREATE INDEX sessions_by_user ON sessions (user_id);

CREATE TABLE audit_log (
  seq bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  at_ms double precision NOT NULL, actor_user_id text, action text NOT NULL,
  target_type text NOT NULL, target_id text, outcome text NOT NULL, details text NOT NULL);
CREATE INDEX audit_log_by_time ON audit_log (at_ms);

CREATE TABLE role_assignments (
  ord bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
  user_id text NOT NULL REFERENCES users (id), role text NOT NULL,
  scope_type text NOT NULL CHECK (scope_type IN ('platform', 'organization', 'application')),
  scope_id text NOT NULL, granted_by text, granted_at_ms double precision NOT NULL,
  CHECK ((scope_type = 'platform') = (scope_id = '*')),
  UNIQUE (user_id, role, scope_type, scope_id));
CREATE INDEX role_assignments_by_scope ON role_assignments (scope_type, scope_id);

CREATE TABLE app_roles (
  ord bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
  application_id text NOT NULL REFERENCES applications (id), name text NOT NULL,
  created_at_ms double precision NOT NULL, PRIMARY KEY (application_id, name));

CREATE TABLE provider_configs (
  id text PRIMARY KEY, organization_id text NOT NULL REFERENCES organizations (id),
  type text NOT NULL CHECK (type IN ('openai_compatible', 'anthropic')),
  base_url text NOT NULL, model text NOT NULL,
  serves_tier text NOT NULL CHECK (serves_tier IN ('economy', 'standard', 'premium')),
  role text NOT NULL CHECK (role IN ('primary', 'fallback')),
  enabled integer NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
  platform_enabled integer NOT NULL DEFAULT 1 CHECK (platform_enabled IN (0, 1)),
  price_input_per_mtok double precision, price_output_per_mtok double precision,
  capabilities text NOT NULL DEFAULT '{}', first_token_budget_ms double precision,
  created_at_ms double precision NOT NULL,
  UNIQUE (organization_id, type, base_url, model));
CREATE INDEX provider_configs_by_organization ON provider_configs (organization_id);

CREATE TABLE provider_keys (
  provider_id text PRIMARY KEY REFERENCES provider_configs (id), key_id text NOT NULL,
  nonce bytea NOT NULL, ciphertext bytea NOT NULL, last4 text,
  created_at_ms double precision NOT NULL, updated_at_ms double precision NOT NULL);

CREATE TABLE modes (
  scope_type text NOT NULL CHECK (scope_type IN ('organization', 'application')),
  scope_id text NOT NULL,
  mode text NOT NULL CHECK (mode IN
    ('local_only', 'local_with_hosted_fallback', 'hosted_primary_with_local_fallback')),
  updated_at_ms double precision NOT NULL, PRIMARY KEY (scope_type, scope_id));
"""

# Version 2 (docs/37): budgets, Postgres only (the SQLite ledger keeps its totals in memory).
# A counter per application and period; a reservation per request in flight; the ids of the
# reservations settled lately.
PG_BUDGETS = """
CREATE TABLE budget_counters (
  application_id text NOT NULL, period_start_ms double precision NOT NULL,
  committed_tokens bigint NOT NULL CHECK (committed_tokens >= 0),
  reserved_tokens bigint NOT NULL CHECK (reserved_tokens >= 0),
  PRIMARY KEY (application_id, period_start_ms));

CREATE TABLE budget_reservations (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  application_id text NOT NULL, period_start_ms double precision NOT NULL,
  tokens bigint NOT NULL CHECK (tokens >= 0),
  expires_at_ms double precision NOT NULL);
CREATE INDEX budget_reservations_by_expiry ON budget_reservations (expires_at_ms);
CREATE INDEX budget_reservations_by_period
  ON budget_reservations (application_id, period_start_ms, expires_at_ms);

CREATE TABLE budget_settlements (
  reservation_id bigint PRIMARY KEY, settled_at_ms double precision NOT NULL);
"""

# Each version and what it adds, applied in order to a database that has an earlier one.
SCHEMA = ((1, PG_BASELINE), (2, PG_BUDGETS))

# The tables of PG_BUDGETS: Postgres only, never copied to or from SQLite.
BUDGET_TABLES = ("budget_counters", "budget_reservations", "budget_settlements")

# Every table of the baseline, in an order foreign keys allow to fill (and the reverse to empty).
TABLES = (
    "organizations",
    "applications",
    "api_keys",
    "usage_events",
    "users",
    "sessions",
    "audit_log",
    "role_assignments",
    "app_roles",
    "provider_configs",
    "provider_keys",
    "modes",
)


def lock_timeout_from(environ: Mapping[str, str]) -> float:
    """``GATEWAY_STATE_LOCK_TIMEOUT_MS``: how long a management write waits for the write lock
    (default 2000). The error names the variable."""
    raw = environ.get(LOCK_TIMEOUT_ENV)
    if raw is None:
        return DEFAULT_LOCK_TIMEOUT_MS
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{LOCK_TIMEOUT_ENV} must be a number of milliseconds") from None
    if not value > 0:
        raise ValueError(f"{LOCK_TIMEOUT_ENV} must be positive")
    return value


class PostgresPasswordMissingError(Exception):
    """The Postgres URL carries no password and the test setting is off. The message names
    neither the address nor anything else from the URL."""

    def __init__(self) -> None:
        super().__init__(
            f"the Postgres URL has no password; only a test database may go without one, with "
            f"{NO_PASSWORD_FOR_TESTS_ENV}=1"
        )


def _has_password(url: str) -> bool:
    """A non-empty password in the URL, after the user name or as the ``password`` parameter.
    Not ``PGPASSWORD`` or a password file: the URL is the one secret setting (docs/36)."""
    try:
        parts = urlsplit(url)
        given = parts.password
    except ValueError:
        return False
    return bool(given) or any(parse_qs(parts.query).get("password", []))


def check_postgres_url(url: str, env: Mapping[str, str]) -> None:
    """At start-up, before connecting: ``PostgresPasswordMissingError`` for a URL without a
    password, unless ``NO_PASSWORD_FOR_TESTS_ENV`` is ``1``."""
    if env.get(NO_PASSWORD_FOR_TESTS_ENV) == "1":
        return
    if not _has_password(url):
        raise PostgresPasswordMissingError()


class _NotOpen:
    """The pools of a database not opened yet: every connection is refused."""

    def acquire(self, *, timeout: float | None = None) -> _NotOpen:
        return self

    async def __aenter__(self) -> None:
        raise ConnectionRefusedError("not open yet")

    async def __aexit__(self, *_: object) -> None:
        return None

    async def close(self) -> None:
        return None


class PostgresDatabase:
    """One gateway instance's Postgres: two pools, the write lock, and the clock of the
    database's windows (it is a ``StoreClock``)."""

    def __init__(
        self,
        requests: asyncpg.Pool,
        management: asyncpg.Pool,
        clock: Clock,
        *,
        lock_timeout_ms: float = DEFAULT_LOCK_TIMEOUT_MS,
    ) -> None:
        self.requests = requests
        self.management = management
        self._clock = clock
        self._lock_timeout_ms = lock_timeout_ms
        self._in_transaction: contextvars.ContextVar[asyncpg.Connection | None] = (
            contextvars.ContextVar("gateway_pg_transaction", default=None)
        )

        self._opener: Callable[[], Awaitable[tuple[asyncpg.Pool, asyncpg.Pool]]] | None = None

    def __repr__(self) -> str:
        return "PostgresDatabase(<pools and address redacted>)"

    @classmethod
    def unopened(
        cls,
        url: str,
        clock: Clock,
        *,
        env: Mapping[str, str],
        schema: str | None = None,
        request_pool_size: int = 10,
        management_pool_size: int = 2,
        lock_timeout_ms: float = DEFAULT_LOCK_TIMEOUT_MS,
    ) -> PostgresDatabase:
        """A database whose pools are made by ``open`` (in the server's lifespan, on its event
        loop); until then every call is refused as unavailable. ``env`` is the process's
        environment: a URL without a password is refused here, before anything else
        (``check_postgres_url``)."""
        check_postgres_url(url, env)
        settings = {"search_path": schema} if schema is not None else None

        # A closure, so the address is in no attribute and in no repr.
        async def pools() -> tuple[asyncpg.Pool, asyncpg.Pool]:
            requests = await asyncpg.create_pool(
                url,
                min_size=1,
                max_size=request_pool_size,
                command_timeout=COMMAND_TIMEOUT_S,
                server_settings=settings,
            )
            try:
                management = await asyncpg.create_pool(
                    url,
                    min_size=1,
                    max_size=management_pool_size,
                    command_timeout=COMMAND_TIMEOUT_S,
                    server_settings=settings,
                )
            except BaseException:
                await requests.close()
                raise
            return requests, management

        closed = cast(asyncpg.Pool, _NotOpen())
        db = cls(closed, closed, clock, lock_timeout_ms=lock_timeout_ms)
        db._opener = pools
        return db

    async def open(self) -> None:
        """Both pools, or ``StateUnavailableError`` (never the address or the password)."""
        if self._opener is None:
            raise RuntimeError("this database was built on its pools: there is nothing to open")
        try:
            self.requests, self.management = await self._opener()
        except _UNAVAILABLE as exc:
            raise StateUnavailableError(type(exc).__name__) from None

    @classmethod
    async def connect(
        cls,
        url: str,
        clock: Clock,
        *,
        env: Mapping[str, str],
        schema: str | None = None,
        request_pool_size: int = 10,
        management_pool_size: int = 2,
        lock_timeout_ms: float = DEFAULT_LOCK_TIMEOUT_MS,
    ) -> PostgresDatabase:
        """``unopened`` and ``open``: both pools now."""
        db = cls.unopened(
            url,
            clock,
            env=env,
            schema=schema,
            request_pool_size=request_pool_size,
            management_pool_size=management_pool_size,
            lock_timeout_ms=lock_timeout_ms,
        )
        await db.open()
        return db

    async def aclose(self) -> None:
        await self.requests.close()
        await self.management.close()

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[asyncpg.Connection]:
        """The current write transaction's connection, or one from the request pool."""
        held = self._in_transaction.get()
        if held is not None:
            yield held
            return
        try:
            async with self.requests.acquire(timeout=COMMAND_TIMEOUT_S) as connection:
                yield connection
        except _UNAVAILABLE as exc:
            raise StateUnavailableError(type(exc).__name__) from None

    @asynccontextmanager
    async def write_transaction(self) -> AsyncIterator[None]:
        """One management write: a connection of the management pool, a transaction, and the
        write lock, or ``StateUnavailableError`` with nothing written (section 3.6)."""
        if self._in_transaction.get() is not None:
            raise RuntimeError("a write transaction is already open")
        try:
            async with (
                self.management.acquire(timeout=COMMAND_TIMEOUT_S) as connection,
                connection.transaction(),
            ):
                await connection.execute(
                    f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}; "
                    f"SET LOCAL idle_in_transaction_session_timeout = {IDLE_IN_TRANSACTION_MS}"
                )
                await self._take_write_lock(connection)
                token = self._in_transaction.set(connection)
                try:
                    yield
                finally:
                    self._in_transaction.reset(token)
        except _UNAVAILABLE as exc:
            raise StateUnavailableError(type(exc).__name__) from None

    @asynccontextmanager
    async def request_transaction(self) -> AsyncIterator[None]:
        """A request's own writes, together or not at all: a connection of the request pool and
        a transaction, without the write lock (section 3.6). Each statement in it that decides
        something locks its own row."""
        if self._in_transaction.get() is not None:
            raise RuntimeError("a transaction is already open")
        try:
            async with (
                self.requests.acquire(timeout=COMMAND_TIMEOUT_S) as connection,
                connection.transaction(),
            ):
                await connection.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
                token = self._in_transaction.set(connection)
                try:
                    yield
                finally:
                    self._in_transaction.reset(token)
        except _UNAVAILABLE as exc:
            raise StateUnavailableError(type(exc).__name__) from None

    async def _take_write_lock(self, connection: asyncpg.Connection) -> None:
        deadline = self._clock.now_ms() + self._lock_timeout_ms
        while not await connection.fetchval("SELECT pg_try_advisory_xact_lock($1)", WRITE_LOCK):
            if self._clock.now_ms() >= deadline:
                raise StateUnavailableError("lock_timeout")
            await self._clock.sleep_ms(LOCK_POLL_MS)

    async def now_ms(self) -> float:
        """``StoreClock``: the database's time (section 3.8)."""
        async with self.connection() as connection:
            return float(await connection.fetchval("SELECT gateway_now_ms()"))

    async def ready(self) -> bool:
        """``/readyz``: a connection of the request pool answers now (``Readiness``)."""
        async with self.connection() as connection:
            return bool(await connection.fetchval("SELECT true"))


async def ensure_schema(connection: asyncpg.Connection) -> bool:
    """In the caller's transaction: take the schema lock, so that instances starting together
    apply the schema once, and apply each version this database does not have yet, in order.
    Returns whether anything was applied. Refuses a database whose version is newer than this
    code knows (its rules would be unknown)."""
    await connection.execute("SELECT pg_advisory_xact_lock($1)", SCHEMA_LOCK)
    await connection.execute(
        "CREATE TABLE IF NOT EXISTS gateway_schema (version integer PRIMARY KEY,"
        " applied_at_ms double precision NOT NULL)"
    )
    found = await connection.fetchval("SELECT max(version) FROM gateway_schema")
    if found is not None and found > SCHEMA_VERSION:
        raise RuntimeError(
            f"the database's schema is version {found}; this gateway knows up to "
            f"{SCHEMA_VERSION}: upgrade the gateway first"
        )
    applied = False
    for version, sql in SCHEMA:
        if found is not None and version <= found:
            continue
        await connection.execute(sql)
        await connection.execute(
            "INSERT INTO gateway_schema VALUES ($1, gateway_now_ms())", version
        )
        applied = True
    return applied


async def apply_schema(db: PostgresDatabase) -> int:
    """Bring the schema to this code's version (``ensure_schema``), in one transaction.
    Returns the schema version."""
    try:
        async with (
            db.management.acquire(timeout=COMMAND_TIMEOUT_S) as connection,
            connection.transaction(),
        ):
            await ensure_schema(connection)
        return SCHEMA_VERSION
    except _UNAVAILABLE as exc:
        raise StateUnavailableError(type(exc).__name__) from None


# --- the stores ----------------------------------------------------------------------------


class _Store:
    def __init__(self, db: PostgresDatabase) -> None:
        self._db = db

    def write_transaction(self) -> AbstractAsyncContextManager[None]:
        return self._db.write_transaction()

    def request_transaction(self) -> AbstractAsyncContextManager[None]:
        return self._db.request_transaction()

    async def _fetch(self, sql: str, *args: Any) -> list[asyncpg.Record]:
        async with self._db.connection() as c:
            return list(await c.fetch(sql, *args))

    async def _row(self, sql: str, *args: Any) -> asyncpg.Record | None:
        async with self._db.connection() as c:
            return await c.fetchrow(sql, *args)

    async def _value(self, sql: str, *args: Any) -> Any:
        async with self._db.connection() as c:
            return await c.fetchval(sql, *args)

    async def _execute(self, sql: str, *args: Any) -> int:
        """Run a statement; returns how many rows it touched."""
        async with self._db.connection() as c:
            status = await c.execute(sql, *args)
        return int(status.rsplit(" ", 1)[-1]) if status[-1:].isdigit() else 0


def _tuple(row: asyncpg.Record | None) -> tuple[Any, ...]:
    assert row is not None
    return tuple(row.values())


def _placeholders(count: int) -> str:
    return ", ".join(f"${i}" for i in range(1, count + 1))


class PostgresUsageStore(_Store):
    async def append(self, event: UsageEvent) -> int:
        source = event.source
        values = (
            event.application_id,
            event.request_id,
            event.model,
            event.prompt_tokens,
            event.cached_tokens,
            event.completion_tokens,
            event.base_cost_usd,
            event.billed_cost_usd,
            event.occurred_at_ms,
            *(
                (
                    source.provider,
                    source.provider_class,
                    source.fallback_from,
                    source.fallback_reason,
                    int(source.usage_estimated),
                )
                if source is not None
                else (None, None, None, None, None)
            ),
        )
        seq = await self._value(
            f"INSERT INTO usage_events ({_USAGE_COLUMNS}) VALUES ({_placeholders(14)})"
            " RETURNING seq",
            *values,
        )
        return int(seq)

    async def usage_by_model(
        self, application_id: str, start_ms: float, end_ms: float
    ) -> dict[str, ModelUsage]:
        rows = await self._fetch(
            f"SELECT {_USAGE_COLUMNS} FROM usage_events WHERE application_id = $1"
            " AND occurred_at_ms >= $2 AND occurred_at_ms < $3 ORDER BY seq",
            application_id,
            start_ms,
            end_ms,
        )
        return _totals(_event(tuple(r.values())) for r in rows)

    async def usage_by_model_for(
        self, application_ids: Collection[str], start_ms: float, end_ms: float
    ) -> dict[str, ModelUsage]:
        ids = sorted(set(application_ids))
        if not ids:
            return {}
        rows = await self._fetch(
            f"SELECT {_USAGE_COLUMNS} FROM usage_events WHERE application_id = ANY($1::text[])"
            " AND occurred_at_ms >= $2 AND occurred_at_ms < $3 ORDER BY seq",
            ids,
            start_ms,
            end_ms,
        )
        return _totals(_event(tuple(r.values())) for r in rows)

    async def events(self, application_id: str | None = None) -> list[StoredUsageEvent]:
        sql = f"SELECT seq, {_USAGE_COLUMNS} FROM usage_events"
        rows = (
            await self._fetch(sql + " ORDER BY seq")
            if application_id is None
            else await self._fetch(sql + " WHERE application_id = $1 ORDER BY seq", application_id)
        )
        return [StoredUsageEvent(int(r[0]), _event(tuple(r.values())[1:])) for r in rows]

    async def prune(self, before_ms: float, limit: int | None = None) -> int:
        if limit is None:
            return await self._execute(
                "DELETE FROM usage_events WHERE occurred_at_ms < $1", before_ms
            )
        return await self._execute(
            "DELETE FROM usage_events WHERE seq IN (SELECT seq FROM usage_events"
            " WHERE occurred_at_ms < $1 ORDER BY occurred_at_ms LIMIT $2)",
            before_ms,
            limit,
        )


class PostgresApplicationStore(_Store):
    async def add_organization(self, organization: Organization) -> None:
        o = organization
        _checked_name(o.name)
        if await self.get_organization(o.id) is not None:
            raise ValueError(f"organization {o.id!r} exists")
        if await self.find_organization(o.name) is not None:
            raise ValueError(f"an organization is already named {o.name!r}")
        try:
            await self._execute(
                f"INSERT INTO organizations ({_ORG_COLUMNS}) VALUES ($1, $2, $3, $4, $5)",
                o.id,
                o.name,
                o.kind.value,
                o.created_at_ms,
                int(o.disabled),
            )
        except asyncpg.exceptions.UniqueViolationError as exc:
            raise ValueError(f"organization {o.id!r} or its name exists") from exc

    async def get_organization(self, organization_id: str) -> Organization | None:
        row = await self._row(
            f"SELECT {_ORG_COLUMNS} FROM organizations WHERE id = $1", organization_id
        )
        return _organization(tuple(row.values())) if row is not None else None

    async def find_organization(self, name: str) -> Organization | None:
        row = await self._row(
            f"SELECT {_ORG_COLUMNS} FROM organizations WHERE name = $1", normalize_name(name)
        )
        return _organization(tuple(row.values())) if row is not None else None

    async def list_organizations(self) -> list[Organization]:
        rows = await self._fetch(f"SELECT {_ORG_COLUMNS} FROM organizations ORDER BY ord")
        return [_organization(tuple(r.values())) for r in rows]

    async def set_organization_disabled(self, organization_id: str, disabled: bool) -> None:
        touched = await self._execute(
            "UPDATE organizations SET disabled = $1 WHERE id = $2", int(disabled), organization_id
        )
        if touched != 1:
            raise KeyError(organization_id)

    async def add_application(self, application: Application) -> None:
        a = application
        _checked_name(a.name)
        if await self.get_application(a.id) is not None:
            raise ValueError(f"application {a.id!r} exists")
        _check_organization(await self.get_organization(a.organization_id), a)
        try:
            await self._execute(
                f"INSERT INTO applications ({_APP_COLUMNS}) VALUES ({_placeholders(10)})",
                a.id,
                a.name,
                a.plan.name,
                json.dumps(list(a.plan.models)),
                a.plan.price_multiplier,
                a.plan.monthly_token_budget,
                a.max_tokens,
                a.requests_per_minute,
                a.created_at_ms,
                a.organization_id,
            )
        except asyncpg.exceptions.UniqueViolationError as exc:
            raise _name_taken(a) from exc

    async def get_application(self, application_id: str) -> Application | None:
        row = await self._row(
            f"SELECT {_APP_COLUMNS} FROM applications WHERE id = $1", application_id
        )
        return _application(tuple(row.values())) if row is not None else None

    async def list_applications(self, organization_id: str | None = None) -> list[Application]:
        if organization_id is None:
            rows = await self._fetch(f"SELECT {_APP_COLUMNS} FROM applications ORDER BY ord")
        else:
            rows = await self._fetch(
                f"SELECT {_APP_COLUMNS} FROM applications WHERE organization_id = $1 ORDER BY ord",
                organization_id,
            )
        return [_application(tuple(r.values())) for r in rows]

    async def page_applications(
        self, organization_id: str, after: str | None, limit: int
    ) -> list[Application]:
        rows = await self._fetch(
            f"SELECT {_APP_COLUMNS} FROM applications WHERE organization_id = $1 AND id > $2"
            " ORDER BY id LIMIT $3",
            organization_id,
            after or "",
            limit,
        )
        return [_application(tuple(r.values())) for r in rows]

    async def add_key(self, record: ApiKeyRecord) -> None:
        r = record
        try:
            await self._execute(
                f"INSERT INTO api_keys ({_KEY_COLUMNS}) VALUES ({_placeholders(8)})",
                r.key_id,
                r.application_id,
                r.label,
                r.key_hmac,
                r.created_at_ms,
                r.expires_at_ms,
                int(r.revoked),
                r.last_used_at_ms,
            )
        except (
            asyncpg.exceptions.UniqueViolationError,
            asyncpg.exceptions.ForeignKeyViolationError,
        ) as exc:
            raise ValueError("key id exists, or no such application") from exc

    async def get_key(self, key_id: str) -> ApiKeyRecord | None:
        row = await self._row(f"SELECT {_KEY_COLUMNS} FROM api_keys WHERE key_id = $1", key_id)
        return _key(tuple(row.values())) if row is not None else None

    async def lookup_key(self, key_id: str) -> KeyLookup | None:
        key_columns = ", ".join(f"k.{c.strip()}" for c in _KEY_COLUMNS.split(","))
        app_columns = ", ".join(f"a.{c.strip()}" for c in _APP_COLUMNS.split(","))
        # The database's time in the same query: the key check decides expiry on it and costs
        # no second round trip (section 3.8).
        row = await self._row(
            f"SELECT {key_columns}, {app_columns}, o.disabled, gateway_now_ms() FROM api_keys AS k"
            " LEFT JOIN applications AS a ON a.id = k.application_id"
            " LEFT JOIN organizations AS o ON o.id = a.organization_id WHERE k.key_id = $1",
            key_id,
        )
        if row is None:
            return None
        values = tuple(row.values())
        width = len(_KEY_COLUMNS.split(","))
        application_row = values[width:-2]
        application = _application(application_row) if application_row[0] is not None else None
        return KeyLookup(_key(values[:width]), application, values[-2] == 0, float(values[-1]))

    async def list_keys(self, application_id: str) -> list[ApiKeyRecord]:
        rows = await self._fetch(
            f"SELECT {_KEY_COLUMNS} FROM api_keys WHERE application_id = $1 ORDER BY ord",
            application_id,
        )
        return [_key(tuple(r.values())) for r in rows]

    async def page_keys(
        self, application_id: str, after: str | None, limit: int
    ) -> list[ApiKeyRecord]:
        rows = await self._fetch(
            f"SELECT {_KEY_COLUMNS} FROM api_keys WHERE application_id = $1 AND key_id > $2"
            " ORDER BY key_id LIMIT $3",
            application_id,
            after or "",
            limit,
        )
        return [_key(tuple(r.values())) for r in rows]

    async def set_key_expiry(self, key_id: str, expires_at_ms: float) -> None:
        if (
            await self._execute(
                "UPDATE api_keys SET expires_at_ms = $1 WHERE key_id = $2", expires_at_ms, key_id
            )
            != 1
        ):
            raise KeyError(key_id)

    async def revoke_key(self, key_id: str) -> None:
        if await self._execute("UPDATE api_keys SET revoked = 1 WHERE key_id = $1", key_id) != 1:
            raise KeyError(key_id)

    async def record_last_used(self, times_ms: Mapping[str, float]) -> None:
        if not times_ms:
            return
        async with self._db.connection() as c:
            # Never backwards, whichever instance's flush lands last.
            await c.executemany(
                "UPDATE api_keys SET last_used_at_ms = $1"
                " WHERE key_id = $2 AND (last_used_at_ms IS NULL OR last_used_at_ms < $1)",
                [(at_ms, key_id) for key_id, at_ms in times_ms.items()],
            )


class PostgresAccountStore(_Store):
    async def add_user(self, user: User) -> None:
        u = user
        try:
            await self._execute(
                f"INSERT INTO users ({_USER_COLUMNS}) VALUES ({_placeholders(11)})",
                u.id,
                u.username,
                u.password_hash,
                u.organization_id,
                u.created_at_ms,
                int(u.disabled),
                int(u.must_change_password),
                int(u.bootstrap),
                u.failed_logins,
                u.first_failure_at_ms,
                u.locked_until_ms,
            )
        except (
            asyncpg.exceptions.UniqueViolationError,
            asyncpg.exceptions.ForeignKeyViolationError,
        ) as exc:
            raise ValueError(f"username {u.username!r} is taken, or no such organization") from exc

    async def get_user(self, user_id: str) -> User | None:
        row = await self._row(f"SELECT {_USER_COLUMNS} FROM users WHERE id = $1", user_id)
        return _user(tuple(row.values())) if row is not None else None

    async def find_user(self, username: str) -> User | None:
        row = await self._row(f"SELECT {_USER_COLUMNS} FROM users WHERE username = $1", username)
        return _user(tuple(row.values())) if row is not None else None

    async def page_users(self, organization_id: str, after: str | None, limit: int) -> list[User]:
        rows = await self._fetch(
            f"SELECT {_USER_COLUMNS} FROM users WHERE organization_id = $1 AND username > $2"
            " ORDER BY username LIMIT $3",
            organization_id,
            after or "",
            limit,
        )
        return [_user(tuple(r.values())) for r in rows]

    async def count_members(self, organization_id: str) -> int:
        return int(
            await self._value(
                "SELECT count(*) FROM users WHERE organization_id = $1", organization_id
            )
        )

    async def _set(self, user_id: str, sql: str, *params: Any) -> None:
        n = len(params)
        if (
            await self._execute(f"UPDATE users SET {sql} WHERE id = ${n + 1}", *params, user_id)
            != 1
        ):
            raise KeyError(user_id)

    async def set_login_state(
        self,
        user_id: str,
        failed_logins: int,
        first_failure_at_ms: float | None,
        locked_until_ms: float | None,
    ) -> None:
        await self._set(
            user_id,
            "failed_logins = $1, first_failure_at_ms = $2, locked_until_ms = $3",
            failed_logins,
            first_failure_at_ms,
            locked_until_ms,
        )

    async def count_login_failure(
        self, user_id: str, now_ms: float, *, window_ms: float, lock_after: int, lock_ms: float
    ) -> FailureCount:
        """One statement (section 3.3), on the database's clock: ``now_ms`` is not used. The row
        is locked and counted as it is when counted, whichever instance counts it."""
        row = await self._row(
            # The time is read once (the CTE is evaluated once), then used for every decision.
            "WITH clock AS (SELECT gateway_now_ms() AS t),"
            " counted AS ("
            " SELECT u.id, clock.t,"
            "  CASE WHEN u.first_failure_at_ms IS NULL OR clock.t - u.first_failure_at_ms >= $2"
            "   THEN 1 ELSE u.failed_logins + 1 END AS failures,"
            "  CASE WHEN u.first_failure_at_ms IS NULL OR clock.t - u.first_failure_at_ms >= $2"
            "   THEN clock.t ELSE u.first_failure_at_ms END AS first"
            " FROM users AS u CROSS JOIN clock WHERE u.id = $1 FOR UPDATE OF u)"
            " UPDATE users SET"
            "  failed_logins = CASE WHEN c.failures >= $3 THEN 0 ELSE c.failures END,"
            "  first_failure_at_ms = CASE WHEN c.failures >= $3 THEN NULL ELSE c.first END,"
            "  locked_until_ms = CASE WHEN c.failures >= $3 THEN c.t + $4"
            "   ELSE users.locked_until_ms END"
            " FROM counted AS c WHERE users.id = c.id"
            " RETURNING c.failures, CASE WHEN c.failures >= $3 THEN c.t + $4 END",
            user_id,
            window_ms,
            lock_after,
            lock_ms,
        )
        if row is None:
            raise KeyError(user_id)
        return FailureCount(int(row[0]), float(row[1]) if row[1] is not None else None)

    async def set_password(
        self, user_id: str, password_hash: str, *, must_change: bool, bootstrap: Bootstrap
    ) -> None:
        await self._set(
            user_id,
            "password_hash = $1, must_change_password = $2, bootstrap = $3",
            password_hash,
            int(must_change),
            int(bootstrap),
        )

    async def set_password_hash(self, user_id: str, password_hash: str, *, replacing: str) -> bool:
        changed = await self._execute(
            "UPDATE users SET password_hash = $1 WHERE id = $2 AND password_hash = $3",
            password_hash,
            user_id,
            replacing,
        )
        return changed == 1

    async def set_bootstrap(self, user_id: str, bootstrap: Bootstrap) -> None:
        await self._set(user_id, "bootstrap = $1", int(bootstrap))

    async def use_bootstrap(self, user_id: str) -> bool:
        changed = await self._execute(
            "UPDATE users SET bootstrap = $1 WHERE id = $2 AND bootstrap = $3",
            int(Bootstrap.USED),
            user_id,
            int(Bootstrap.UNUSED),
        )
        return changed == 1

    async def set_disabled(self, user_id: str, disabled: bool) -> None:
        await self._set(user_id, "disabled = $1", int(disabled))

    async def add_session(self, session: Session) -> None:
        s = session
        await self._execute(
            f"INSERT INTO sessions ({_SESSION_COLUMNS}) VALUES ({_placeholders(9)})",
            s.id_hash,
            s.handle,
            s.user_id,
            s.csrf_hash,
            s.created_at_ms,
            s.last_seen_at_ms,
            s.expires_at_ms,
            int(s.revoked),
            s.step_up_until_ms,
        )

    async def get_session(self, id_hash: str) -> Session | None:
        row = await self._row(
            f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE id_hash = $1", id_hash
        )
        return _session(tuple(row.values())) if row is not None else None

    async def touch_session(self, id_hash: str, at_ms: float) -> None:
        # Never backwards, whichever instance touches it last.
        await self._execute(
            "UPDATE sessions SET last_seen_at_ms = GREATEST(last_seen_at_ms, $1)"
            " WHERE id_hash = $2",
            at_ms,
            id_hash,
        )

    async def revoke_session(self, id_hash: str) -> bool:
        return (
            await self._execute(
                "UPDATE sessions SET revoked = 1, step_up_until_ms = NULL WHERE id_hash = $1"
                " AND revoked = 0",
                id_hash,
            )
            == 1
        )

    async def revoke_sessions(self, user_id: str, *, keep_id_hash: str | None = None) -> int:
        return await self._execute(
            "UPDATE sessions SET revoked = 1, step_up_until_ms = NULL WHERE user_id = $1"
            " AND revoked = 0 AND id_hash IS DISTINCT FROM $2",
            user_id,
            keep_id_hash,
        )

    async def set_step_up(self, id_hash: str, until_ms: float | None) -> None:
        await self._execute(
            "UPDATE sessions SET step_up_until_ms = $1 WHERE id_hash = $2", until_ms, id_hash
        )

    async def claim_step_up(self, id_hash: str, now_ms: float) -> float | None:
        """One statement (section 3.2), on the database's clock: ``now_ms`` is not used. Two
        instances claiming one window: the second waits on the row and finds it closed."""
        row = await self._row(
            "UPDATE sessions AS s SET step_up_until_ms = NULL"
            " FROM (SELECT id_hash, step_up_until_ms FROM sessions WHERE id_hash = $1"
            "  AND revoked = 0 AND step_up_until_ms > gateway_now_ms() FOR UPDATE) AS old"
            " WHERE s.id_hash = old.id_hash RETURNING old.step_up_until_ms",
            id_hash,
        )
        return float(row[0]) if row is not None else None

    async def restore_step_up(self, id_hash: str, until_ms: float) -> bool:
        return (
            await self._execute(
                "UPDATE sessions SET step_up_until_ms = $1 WHERE id_hash = $2 AND revoked = 0"
                " AND step_up_until_ms IS NULL",
                until_ms,
                id_hash,
            )
            == 1
        )

    async def clear_step_up(self, user_id: str) -> int:
        return await self._execute(
            "UPDATE sessions SET step_up_until_ms = NULL WHERE user_id = $1"
            " AND step_up_until_ms IS NOT NULL",
            user_id,
        )

    async def list_sessions(self, user_id: str) -> list[Session]:
        rows = await self._fetch(
            f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE user_id = $1 AND revoked = 0"
            " ORDER BY created_at_ms, ord",
            user_id,
        )
        return [_session(tuple(r.values())) for r in rows]

    async def prune_sessions(self, now_ms: float, idle_ms: float, limit: int | None = None) -> int:
        where = "revoked = 1 OR expires_at_ms <= $1 OR last_seen_at_ms <= $2"
        if limit is None:
            return await self._execute(
                f"DELETE FROM sessions WHERE {where}", now_ms, now_ms - idle_ms
            )
        return await self._execute(
            f"DELETE FROM sessions WHERE id_hash IN (SELECT id_hash FROM sessions WHERE {where}"
            " LIMIT $3)",
            now_ms,
            now_ms - idle_ms,
            limit,
        )


class PostgresRoleStore(_Store):
    async def assignments_for(self, user_id: str) -> tuple[Assignment, ...]:
        rows = await self._fetch(
            "SELECT role, scope_type, scope_id FROM role_assignments WHERE user_id = $1"
            " ORDER BY ord",
            user_id,
        )
        return tuple(Assignment(r[0], ScopeType(r[1]), r[2]) for r in rows)

    async def grant(
        self, user_id: str, assignment: Assignment, granted_by: str | None, at_ms: float
    ) -> None:
        try:
            await self._execute(
                "INSERT INTO role_assignments (user_id, role, scope_type, scope_id, granted_by,"
                " granted_at_ms) VALUES ($1, $2, $3, $4, $5, $6)",
                user_id,
                assignment.role,
                assignment.scope_type.value,
                assignment.scope_id,
                granted_by,
                at_ms,
            )
        except asyncpg.exceptions.UniqueViolationError as exc:
            raise ValueError("the user already holds that role in that scope") from exc

    async def revoke(self, user_id: str, assignment: Assignment) -> bool:
        return (
            await self._execute(
                "DELETE FROM role_assignments WHERE user_id = $1 AND role = $2"
                " AND scope_type = $3 AND scope_id = $4",
                user_id,
                assignment.role,
                assignment.scope_type.value,
                assignment.scope_id,
            )
            == 1
        )

    async def enabled_holders(self, assignment: Assignment) -> list[str]:
        rows = await self._fetch(
            "SELECT r.user_id FROM role_assignments AS r JOIN users AS u ON u.id = r.user_id"
            " WHERE r.role = $1 AND r.scope_type = $2 AND r.scope_id = $3 AND u.disabled = 0"
            " ORDER BY r.ord",
            assignment.role,
            assignment.scope_type.value,
            assignment.scope_id,
        )
        return [r[0] for r in rows]

    async def platform_admin_exists(self) -> bool:
        return (
            await self._row(
                "SELECT 1 FROM role_assignments WHERE role = $1 AND scope_type = $2 LIMIT 1",
                Role.PLATFORM_ADMIN.value,
                ScopeType.PLATFORM.value,
            )
            is not None
        )

    async def define_app_role(self, application_id: str, name: str, at_ms: float) -> None:
        try:
            await self._execute(
                "INSERT INTO app_roles (application_id, name, created_at_ms) VALUES ($1, $2, $3)",
                application_id,
                check_app_role_name(name),
                at_ms,
            )
        except (
            asyncpg.exceptions.UniqueViolationError,
            asyncpg.exceptions.ForeignKeyViolationError,
        ) as exc:
            raise ValueError("that role exists, or there is no such application") from exc

    async def app_roles(self, application_id: str) -> list[str]:
        rows = await self._fetch(
            "SELECT name FROM app_roles WHERE application_id = $1 ORDER BY ord", application_id
        )
        return [r[0] for r in rows]


class PostgresAuditStore(_Store):
    async def insert(self, event: AuditEvent, at_ms: float) -> None:
        await self._execute(
            "INSERT INTO audit_log (at_ms, actor_user_id, action, target_type, target_id,"
            " outcome, details) VALUES ($1, $2, $3, $4, $5, $6, $7)",
            at_ms,
            event.actor_user_id,
            event.action.value,
            event.target_type.value,
            event.target_id,
            event.outcome.value,
            json.dumps(dict(event.details), sort_keys=True),
        )

    async def entries(self) -> list[AuditEntry]:
        rows = await self._fetch(
            "SELECT seq, at_ms, actor_user_id, action, target_type, target_id, outcome, details"
            " FROM audit_log ORDER BY seq"
        )
        return [_entry(tuple(r.values())) for r in rows]

    async def prune(self, before_ms: float, limit: int | None = None) -> int:
        if limit is None:
            return await self._execute("DELETE FROM audit_log WHERE at_ms < $1", before_ms)
        return await self._execute(
            "DELETE FROM audit_log WHERE seq IN (SELECT seq FROM audit_log WHERE at_ms < $1"
            " ORDER BY at_ms LIMIT $2)",
            before_ms,
            limit,
        )


class PostgresProviderStore(_Store):
    async def add(self, config: ProviderConfig) -> None:
        c = config
        try:
            await self._execute(
                f"INSERT INTO provider_configs ({_PROVIDER_COLUMNS}) VALUES ({_placeholders(14)})",
                c.id,
                c.organization_id,
                c.type,
                c.base_url,
                c.model,
                c.serves_tier,
                c.role,
                int(c.enabled),
                int(c.platform_enabled),
                c.price_input_per_mtok,
                c.price_output_per_mtok,
                json.dumps(dict(c.capabilities)),
                c.first_token_budget_ms,
                c.created_at_ms,
            )
        except asyncpg.exceptions.UniqueViolationError as exc:
            raise ManagementRefusal(
                Reason.PROVIDER_EXISTS,
                "this organization already has a provider of that type, URL and model",
            ) from exc

    async def get(self, provider_id: str) -> ProviderConfig | None:
        row = await self._row(
            f"SELECT {_PROVIDER_COLUMNS} FROM provider_configs WHERE id = $1", provider_id
        )
        return _config(tuple(row.values())) if row is not None else None

    async def list_for(self, organization_id: str) -> list[ProviderConfig]:
        rows = await self._fetch(
            f"SELECT {_PROVIDER_COLUMNS} FROM provider_configs WHERE organization_id = $1"
            " ORDER BY id",
            organization_id,
        )
        return [_config(tuple(r.values())) for r in rows]

    async def list_all(self) -> list[ProviderConfig]:
        rows = await self._fetch(f"SELECT {_PROVIDER_COLUMNS} FROM provider_configs ORDER BY id")
        return [_config(tuple(r.values())) for r in rows]

    async def replace(self, config: ProviderConfig) -> None:
        c = config
        try:
            await self._execute(
                "UPDATE provider_configs SET type = $1, base_url = $2, model = $3,"
                " serves_tier = $4, role = $5, enabled = $6, platform_enabled = $7,"
                " price_input_per_mtok = $8, price_output_per_mtok = $9, capabilities = $10,"
                " first_token_budget_ms = $11 WHERE id = $12",
                c.type,
                c.base_url,
                c.model,
                c.serves_tier,
                c.role,
                int(c.enabled),
                int(c.platform_enabled),
                c.price_input_per_mtok,
                c.price_output_per_mtok,
                json.dumps(dict(c.capabilities)),
                c.first_token_budget_ms,
                c.id,
            )
        except asyncpg.exceptions.UniqueViolationError as exc:
            raise ManagementRefusal(
                Reason.PROVIDER_EXISTS,
                "this organization already has a provider of that type, URL and model",
            ) from exc

    async def delete(self, provider_id: str) -> None:
        await self._execute("DELETE FROM provider_keys WHERE provider_id = $1", provider_id)
        await self._execute("DELETE FROM provider_configs WHERE id = $1", provider_id)

    async def get_key(self, provider_id: str) -> StoredKey | None:
        row = await self._row(
            "SELECT provider_id, key_id, nonce, ciphertext, last4, created_at_ms, updated_at_ms"
            " FROM provider_keys WHERE provider_id = $1",
            provider_id,
        )
        if row is None:
            return None
        pid, key_id, nonce, ciphertext, last4, created, updated = tuple(row.values())
        return StoredKey(
            pid, SealedKey(key_id, bytes(nonce), bytes(ciphertext), last4), created, updated
        )

    async def put_key(self, provider_id: str, sealed: SealedKey, at_ms: float) -> None:
        await self._execute(
            "INSERT INTO provider_keys (provider_id, key_id, nonce, ciphertext, last4,"
            " created_at_ms, updated_at_ms) VALUES ($1, $2, $3, $4, $5, $6, $6)"
            " ON CONFLICT (provider_id) DO UPDATE SET key_id = excluded.key_id,"
            " nonce = excluded.nonce, ciphertext = excluded.ciphertext, last4 = excluded.last4,"
            " updated_at_ms = excluded.updated_at_ms",
            provider_id,
            sealed.key_id,
            sealed.nonce,
            sealed.ciphertext,
            sealed.last4,
            at_ms,
        )

    async def delete_key(self, provider_id: str) -> bool:
        return (
            await self._execute("DELETE FROM provider_keys WHERE provider_id = $1", provider_id)
            == 1
        )

    async def all_keys(self) -> list[StoredKey]:
        ids = [
            r[0]
            for r in await self._fetch("SELECT provider_id FROM provider_keys ORDER BY provider_id")
        ]
        return [k for k in [await self.get_key(i) for i in ids] if k is not None]


class PostgresModeStore(_Store):
    async def _get(self, scope_type: str, scope_id: str) -> Mode:
        found = await self._value(
            "SELECT mode FROM modes WHERE scope_type = $1 AND scope_id = $2", scope_type, scope_id
        )
        return Mode(found) if found is not None else DEFAULT_MODE

    async def _set(self, scope_type: str, scope_id: str, mode: Mode, at_ms: float) -> None:
        await self._execute(
            "INSERT INTO modes (scope_type, scope_id, mode, updated_at_ms) VALUES ($1, $2, $3, $4)"
            " ON CONFLICT (scope_type, scope_id)"
            " DO UPDATE SET mode = excluded.mode, updated_at_ms = excluded.updated_at_ms",
            scope_type,
            scope_id,
            Mode(mode).value,
            at_ms,
        )

    async def organization_mode(self, organization_id: str) -> Mode:
        return await self._get("organization", organization_id)

    async def application_mode(self, application_id: str) -> Mode:
        return await self._get("application", application_id)

    async def set_organization_mode(self, organization_id: str, mode: Mode, at_ms: float) -> None:
        await self._set("organization", organization_id, mode, at_ms)

    async def set_application_mode(self, application_id: str, mode: Mode, at_ms: float) -> None:
        await self._set("application", application_id, mode, at_ms)


# --- budgets (docs/37) ---------------------------------------------------------------------

# The tokens of the period's usage events, for a counter seen for the first time: billed tokens,
# as ``ModelUsage.billed_tokens`` counts them.
_SEED = (
    "INSERT INTO budget_counters"
    " (application_id, period_start_ms, committed_tokens, reserved_tokens)"
    " SELECT $1, $2, COALESCE(sum(prompt_tokens - cached_tokens + completion_tokens), 0), 0"
    " FROM usage_events WHERE application_id = $1 AND occurred_at_ms >= $2"
    " AND occurred_at_ms < $3 ON CONFLICT DO NOTHING"
)
# Q1: admitted while what is committed and what is in flight are below the budget. The row lock
# of this UPDATE is what orders two instances: the second sees the first's reservation.
_ADMIT = (
    "UPDATE budget_counters SET reserved_tokens = reserved_tokens + $3"
    " WHERE application_id = $1 AND period_start_ms = $2"
    " AND committed_tokens + reserved_tokens < $4 RETURNING committed_tokens"
)
_UNRESERVE = (
    "UPDATE budget_counters SET reserved_tokens = reserved_tokens - $3"
    " WHERE application_id = $1 AND period_start_ms = $2"
)
# A reservation deleted and taken off its counter in one statement; nothing if it is gone.
_TAKE_OFF = (
    "WITH held AS (DELETE FROM budget_reservations WHERE id = $1"
    " RETURNING application_id, period_start_ms, tokens)"
    " UPDATE budget_counters c SET reserved_tokens = c.reserved_tokens - held.tokens FROM held"
    " WHERE c.application_id = held.application_id AND c.period_start_ms = held.period_start_ms"
)
_COMMIT = (
    "UPDATE budget_counters SET committed_tokens = committed_tokens + $3"
    " WHERE application_id = $1 AND period_start_ms = $2"
)


class PostgresBudgets:
    """Budgets every instance shares (``SharedBudgets``), on the database's clock: the period
    and every expiry are decided on ``gateway_now_ms()``, read once per call (section 3.8).

    - ``reserve``: the request's bound is added to the counter's reserved tokens if committed
      and reserved are below the budget (Q1), and a reservation row is kept until
      ``hold_ms`` past now. Otherwise ``BudgetExceededError``.
    - ``settle``: the reservation is deleted and, if it was still there, taken off the
      counter; the usage event is appended and its billed tokens committed. Once per
      reservation: a settle tried again after an answer that was lost is not charged twice.
    - ``release``: the reservation is deleted and taken off the counter.
    - ``sweep``: every reservation past its expiry is released (an instance that died holding
      one); it returns each one's application and tokens.

    A settle or release the database does not take is kept and tried again on ``clock``; while
    any is kept, ``healthy`` is False and ``reserve`` refuses (decision 8 of the plan).
    """

    def __init__(
        self,
        db: PostgresDatabase,
        clock: Clock,
        *,
        periods: BudgetPeriods | None = None,
        retention_ms: float | None = None,
    ) -> None:
        if retention_ms is not None and retention_ms <= 0:
            raise ValueError("retention_ms must be positive")
        self._db = db
        self._clock = clock
        self._usage = PostgresUsageStore(db)
        self._store = _Store(db)
        self._periods = periods if periods is not None else CalendarMonthsUTC()
        self._retention_ms = retention_ms
        # Settles and releases not written yet, oldest first, by reservation id.
        self._waiting: list[tuple[int, Callable[[], Awaitable[None]]]] = []
        self._retrying: asyncio.Task[None] | None = None

    def __repr__(self) -> str:
        return "PostgresBudgets(<database redacted>)"

    @property
    def healthy(self) -> bool:
        return not self._waiting

    @property
    def retention_ms(self) -> float | None:
        return self._retention_ms

    async def _now(self) -> float:
        return float(await self._store._value("SELECT gateway_now_ms()"))

    async def reserve(
        self, application_id: str, budget_tokens: int, tokens: int, *, hold_ms: float
    ) -> Reservation:
        if self._waiting:
            raise StateUnavailableError("settle_waiting")
        s = self._store
        async with self._db.request_transaction():
            now = await self._now()
            start, end = self._periods.bounds(now)
            admitted = await s._row(_ADMIT, application_id, start, tokens, budget_tokens)
            if admitted is None:
                # The first request of the period here: its counter, from the period's events.
                await s._execute(_SEED, application_id, start, end)
                admitted = await s._row(_ADMIT, application_id, start, tokens, budget_tokens)
            if admitted is None:
                raise await self._refusal(application_id, budget_tokens, start, end, now)
            reservation_id = await s._value(
                "INSERT INTO budget_reservations"
                " (application_id, period_start_ms, tokens, expires_at_ms)"
                " VALUES ($1, $2, $3, $4) RETURNING id",
                application_id,
                start,
                tokens,
                now + hold_ms,
            )
        return Reservation(application_id, tokens, int(reservation_id))

    async def _refusal(
        self, application_id: str, budget_tokens: int, start: float, end: float, now: float
    ) -> BudgetExceededError:
        row = await self._store._row(
            "SELECT committed_tokens, (SELECT min(expires_at_ms) FROM budget_reservations"
            " WHERE application_id = $1 AND period_start_ms = $2)"
            " FROM budget_counters WHERE application_id = $1 AND period_start_ms = $2",
            application_id,
            start,
        )
        committed, earliest = _tuple(row)
        # B3: refused only because of what is in flight, room comes back when the earliest
        # reservation ends, at the latest at its expiry; otherwise at the end of the period.
        if committed < budget_tokens and earliest is not None:
            retry_after_ms = max(0.0, float(earliest) - now)
        else:
            retry_after_ms = end - now
        return BudgetExceededError(
            application_id, int(committed), budget_tokens, retry_after_ms=retry_after_ms
        )

    async def settle(
        self,
        reservation: Reservation,
        result: RequestResult,
        *,
        model: str,
        price_multiplier: float,
        source: ChatSource | None = None,
    ) -> float:
        billed_cost_usd = result.cost_usd * price_multiplier

        async def write() -> None:
            await self._settle(reservation, result, model, billed_cost_usd, source)

        await self._write_or_keep(reservation, write)
        return billed_cost_usd

    async def _settle(
        self,
        reservation: Reservation,
        result: RequestResult,
        model: str,
        billed_cost_usd: float,
        source: ChatSource | None,
    ) -> None:
        s = self._store
        async with self._db.request_transaction():
            now = await self._now()
            if reservation.id is not None:
                first = await s._value(
                    "INSERT INTO budget_settlements VALUES ($1, $2)"
                    " ON CONFLICT DO NOTHING RETURNING reservation_id",
                    reservation.id,
                    now,
                )
                if first is None:
                    return  # written by an earlier try whose answer was lost
                await self._take_off(reservation.id)
            # Charged to the period it finished in.
            start, end = self._periods.bounds(now)
            event = UsageEvent.of(
                reservation.application_id,
                result,
                model=model,
                billed_cost_usd=billed_cost_usd,
                occurred_at_ms=now,
                source=source,
            )
            await self._usage.append(event)
            billed = event.prompt_tokens - event.cached_tokens + event.completion_tokens
            app = reservation.application_id
            if await s._execute(_COMMIT, app, start, billed) == 0:
                # No counter for this period yet: seeded from the events, this one included. If
                # another instance seeded it meanwhile (without this event, not committed yet),
                # nothing is inserted and this event is added to that counter.
                if await s._execute(_SEED, app, start, end) == 0:
                    await s._execute(_COMMIT, app, start, billed)

    async def _take_off(self, reservation_id: int) -> None:
        """Delete a reservation and take it off its counter, if it is still there (a sweep may
        have released it already)."""
        await self._store._execute(_TAKE_OFF, reservation_id)

    async def release(self, reservation: Reservation) -> None:
        if reservation.id is None:
            return
        reservation_id = reservation.id

        async def write() -> None:
            async with self._db.request_transaction():
                await self._take_off(reservation_id)

        await self._write_or_keep(reservation, write)

    async def _write_or_keep(
        self, reservation: Reservation, write: Callable[[], Awaitable[None]]
    ) -> None:
        try:
            await write()
        except StateUnavailableError as exc:
            assert reservation.id is not None
            LOG.warning(
                "a budget write could not be made (%s); kept and tried again (reservation %d)",
                exc,
                reservation.id,
            )
            self._waiting.append((reservation.id, write))
            if self._retrying is None or self._retrying.done():
                self._retrying = asyncio.ensure_future(self._retry())

    async def _retry(self) -> None:
        wait_ms = RETRY_FIRST_MS
        while self._waiting:
            await self._clock.sleep_ms(wait_ms)
            _, write = self._waiting[0]
            try:
                await write()
            except StateUnavailableError:
                wait_ms = min(2 * wait_ms, RETRY_MOST_MS)
                continue
            self._waiting.pop(0)
            wait_ms = RETRY_FIRST_MS

    async def sweep(self) -> list[Expired]:
        s = self._store
        async with self._db.request_transaction():
            now = await self._now()
            expired = await s._fetch(
                "DELETE FROM budget_reservations WHERE expires_at_ms <= $1"
                " RETURNING application_id, period_start_ms, tokens",
                now,
            )
            for row in expired:
                await s._execute(_UNRESERVE, *_tuple(row))
            await s._execute(
                "DELETE FROM budget_settlements WHERE settled_at_ms < $1", now - SETTLED_KEEP_MS
            )
        return [Expired(str(row[0]), int(row[2])) for row in expired]

    async def usage(self, application_id: str) -> ApplicationUsage:
        """The period's usage, from its events: what every instance has charged."""
        start, end = self._periods.bounds(await self._now())
        by_model = await self._usage.usage_by_model(application_id, start, end)
        total = ModelUsage()
        for usage in by_model.values():
            total = ModelUsage(
                requests=total.requests + usage.requests,
                prompt_tokens=total.prompt_tokens + usage.prompt_tokens,
                cached_tokens=total.cached_tokens + usage.cached_tokens,
                completion_tokens=total.completion_tokens + usage.completion_tokens,
                base_cost_usd=total.base_cost_usd + usage.base_cost_usd,
                billed_cost_usd=total.billed_cost_usd + usage.billed_cost_usd,
            )
        return ApplicationUsage(application_id, start, end, total, by_model)

    async def prune(self, limit: int | None = None) -> int:
        """Usage events older than the retention, never the current period's (``CostLedger``)."""
        if self._retention_ms is None:
            return 0
        now = await self._now()
        period_start, _ = self._periods.bounds(now)
        return await self._usage.prune(min(now - self._retention_ms, period_start), limit)

    async def aclose(self) -> None:
        """Stop trying. Each write still kept is logged by its reservation id alone; the sweep
        of another instance releases the reservation, and the request's usage is lost."""
        if self._retrying is not None:
            self._retrying.cancel()
        for reservation_id, _ in self._waiting:
            LOG.error(
                "stopping with a budget write not made (reservation %d): the sweep releases it, "
                "and that request's usage is not charged",
                reservation_id,
            )


def postgres_state(db: PostgresDatabase) -> StateBackend:
    """Every store over one Postgres database; windows on its clock."""
    return StateBackend(
        usage=PostgresUsageStore(db),
        applications=PostgresApplicationStore(db),
        accounts=PostgresAccountStore(db),
        roles=PostgresRoleStore(db),
        audit=PostgresAuditStore(db),
        providers=PostgresProviderStore(db),
        modes=PostgresModeStore(db),
        clock=db,
    )
