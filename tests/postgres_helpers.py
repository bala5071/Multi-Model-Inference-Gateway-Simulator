"""Building blocks for the Postgres tests (the tests marked ``postgres``): a schema of the test's
own on the test Postgres, any number of instances connected to it, the database's clock under
the test's control, and services built on those stores. Test values, not secrets."""

from __future__ import annotations

import os
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import asyncpg

from gateway.audit import AuditLog
from gateway.auth import AccountService
from gateway.clock import SystemEpoch, WallClock
from gateway.passwords import CHEAP, InlineHashRunner, PasswordHasher, Passwords
from gateway.postgres import (
    PostgresAccountStore,
    PostgresApplicationStore,
    PostgresAuditStore,
    PostgresDatabase,
    PostgresRoleStore,
    apply_schema,
)
from helpers import TEST_PEPPER

POSTGRES_URL_ENV = "GATEWAY_TEST_POSTGRES_URL"

# The database's clock in a test schema: the row the test sets, else the real one.
TEST_CLOCK = """
CREATE TABLE gateway_test_clock (now_ms double precision);
INSERT INTO gateway_test_clock VALUES (NULL);
CREATE OR REPLACE FUNCTION gateway_now_ms() RETURNS double precision LANGUAGE sql VOLATILE AS
  $$ SELECT COALESCE((SELECT now_ms FROM gateway_test_clock),
                     extract(epoch FROM clock_timestamp()) * 1000) $$;
"""


@dataclass
class PgWorld:
    schema: str
    instances: list[PostgresDatabase]
    admin: asyncpg.Connection

    async def set_time(self, now_ms: float | None) -> None:
        """The database's clock, for every instance; None gives it back its real time."""
        await self.admin.execute(f"UPDATE {self.schema}.gateway_test_clock SET now_ms = $1", now_ms)

    async def rows(self, sql: str, *args: Any) -> list[tuple[Any, ...]]:
        await self.admin.execute(f"SET search_path TO {self.schema}")
        return [tuple(r.values()) for r in await self.admin.fetch(sql, *args)]


@asynccontextmanager
async def pg_world(instances: int = 1, **connect: Any) -> AsyncIterator[PgWorld]:
    """``instances`` gateway instances' databases, on one schema of this test's own, with the
    schema applied and the clock under the test's control. The schema is dropped afterwards,
    never anything else."""
    url = os.environ[POSTGRES_URL_ENV]
    schema = f"gwtest_{random.SystemRandom().getrandbits(64):016x}"
    admin = await asyncpg.connect(url)
    await admin.execute(f"CREATE SCHEMA {schema}")
    dbs: list[PostgresDatabase] = []
    try:
        for _ in range(instances):
            dbs.append(await PostgresDatabase.connect(url, WallClock(), schema=schema, **connect))
        await apply_schema(dbs[0])
        await admin.execute(f"SET search_path TO {schema}")
        await admin.execute(TEST_CLOCK)
        yield PgWorld(schema, dbs, admin)
    finally:
        for db in dbs:
            await db.aclose()
        await admin.execute(f"DROP SCHEMA {schema} CASCADE")
        await admin.close()


def pg_accounts(db: PostgresDatabase, *, seed: int = 1, **kwargs: Any) -> AccountService:
    """An account service on Postgres stores, its windows on the database's clock."""
    return AccountService(
        PostgresAccountStore(db),
        AuditLog(PostgresAuditStore(db)),
        Passwords(PasswordHasher(CHEAP), InlineHashRunner()),
        roles=PostgresRoleStore(db),
        organizations=PostgresApplicationStore(db),
        clock=WallClock(),
        epoch=SystemEpoch(),
        secret=TEST_PEPPER,
        random_bytes=random.Random(seed).randbytes,
        store_clock=db,
        **kwargs,
    )
