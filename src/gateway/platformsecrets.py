"""The operator's own secrets, sealed in the vault's platform scope (docs/48): the SMTP password.

One row per kind (``platform_secrets``, migration 18 and Postgres schema 7), with no
organization column: it belongs to the platform. Each row keeps what its ciphertext is bound to
(the SMTP host, port and username) beside it, so the binding can be rebuilt to open it, and a
password set for another server does not open for this one. No last four characters are kept.

``platform_secrets`` is read by the platform secret stores and nothing else (a test greps every
module's SQL); those stores are used only by the SMTP sender and the operator's CLI.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from gateway.database import write_transaction
from gateway.vault import PlatformBinding, SealedKey

SMTP_PASSWORD = "smtp_password"


@dataclass(frozen=True, slots=True)
class StoredPlatformSecret:
    """A platform secret as stored: sealed, never the value."""

    kind: str
    sealed: SealedKey
    host: str
    port: int
    username: str
    updated_at_ms: float

    @property
    def binding(self) -> PlatformBinding:
        return PlatformBinding(self.kind, self.host, self.port, self.username)

    def __repr__(self) -> str:
        return f"StoredPlatformSecret(kind={self.kind!r}, key_id={self.sealed.key_id!r}, <sealed>)"


COLUMNS = "kind, key_id, nonce, ciphertext, host, port, username, updated_at_ms"


def secret_row(s: StoredPlatformSecret) -> tuple[Any, ...]:
    return (
        s.kind, s.sealed.key_id, s.sealed.nonce, s.sealed.ciphertext, s.host, s.port, s.username,
        s.updated_at_ms,
    )  # fmt: skip


def secret_from_row(row: Sequence[Any]) -> StoredPlatformSecret:
    return StoredPlatformSecret(
        row[0], SealedKey(row[1], bytes(row[2]), bytes(row[3]), None), row[4], int(row[5]), row[6],
        float(row[7]),
    )  # fmt: skip


class PlatformSecretStore(Protocol):
    def write_transaction(self) -> contextlib.AbstractAsyncContextManager[None]: ...

    async def get(self, kind: str) -> StoredPlatformSecret | None: ...

    async def put(self, secret: StoredPlatformSecret) -> None:
        """Insert or replace the row of its kind."""
        ...

    async def all(self) -> list[StoredPlatformSecret]:
        """Every platform secret (``rotate-vault-key``), by kind."""
        ...


class InMemoryPlatformSecretStore:
    def __init__(self) -> None:
        self._rows: dict[str, StoredPlatformSecret] = {}

    @contextlib.asynccontextmanager
    async def write_transaction(self) -> AsyncIterator[None]:
        yield

    async def get(self, kind: str) -> StoredPlatformSecret | None:
        return self._rows.get(kind)

    async def put(self, secret: StoredPlatformSecret) -> None:
        self._rows[secret.kind] = secret

    async def all(self) -> list[StoredPlatformSecret]:
        return [self._rows[k] for k in sorted(self._rows)]


class SqlitePlatformSecretStore:
    """Over migration 18's ``platform_secrets``. Coroutines that never suspend (docs/34)."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection

    def write_transaction(self) -> contextlib.AbstractAsyncContextManager[None]:
        return write_transaction(self._db)

    async def get(self, kind: str) -> StoredPlatformSecret | None:
        row = self._db.execute(
            f"SELECT {COLUMNS} FROM platform_secrets WHERE kind = ?", (kind,)
        ).fetchone()
        return secret_from_row(row) if row is not None else None

    async def put(self, secret: StoredPlatformSecret) -> None:
        self._db.execute(
            f"INSERT OR REPLACE INTO platform_secrets ({COLUMNS}) VALUES ({', '.join('?' * 8)})",
            secret_row(secret),
        )

    async def all(self) -> list[StoredPlatformSecret]:
        rows = self._db.execute(f"SELECT {COLUMNS} FROM platform_secrets ORDER BY kind").fetchall()
        return [secret_from_row(r) for r in rows]
