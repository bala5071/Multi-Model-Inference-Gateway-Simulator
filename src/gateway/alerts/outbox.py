"""The outbox: one row per (notification, channel), queued in the transaction of the change it
belongs to (docs/46, the A4 plan's section 5).

A row is ``queued`` (to be sent), ``sending`` (claimed by a delivery worker), ``delivered``,
``failed`` (with its code) or ``suppressed`` (over a cap, with ``rule_cap`` or
``organization_cap``). Its ``payload`` is the notification's ``json-v1`` body as it was built
when queued: a delivery sends exactly that. A unique index on (notification, channel) means a
notification queued twice for one channel is queued once.

Group 1 queues and lists; the delivery worker (group 2) claims and records outcomes.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Protocol

from gateway.database import write_transaction

DELIVERY_ID_PREFIX = "dlv_"


class DeliveryState(StrEnum):
    QUEUED = "queued"
    SENDING = "sending"
    DELIVERED = "delivered"
    FAILED = "failed"
    SUPPRESSED = "suppressed"


@dataclass(frozen=True, slots=True)
class Delivery:
    id: str
    notification_id: str
    channel_id: str
    organization_id: str
    state: DeliveryState
    attempts: int
    next_attempt_at_ms: float | None
    claimed_by: str | None
    claim_expires_at_ms: float | None
    last_code: str | None
    payload: str
    created_at_ms: float
    updated_at_ms: float

    def __repr__(self) -> str:
        # The payload is the notification's own body: kept out of reprs and tracebacks.
        return (
            f"Delivery(id={self.id!r}, channel_id={self.channel_id!r}, state={self.state.value!r},"
            f" attempts={self.attempts}, last_code={self.last_code!r})"
        )


COLUMNS = (
    "id, notification_id, channel_id, organization_id, state, attempts, next_attempt_at_ms,"
    " claimed_by, claim_expires_at_ms, last_code, payload, created_at_ms, updated_at_ms"
)


def delivery_row(d: Delivery) -> tuple[Any, ...]:
    return (
        d.id, d.notification_id, d.channel_id, d.organization_id, d.state.value, d.attempts,
        d.next_attempt_at_ms, d.claimed_by, d.claim_expires_at_ms, d.last_code, d.payload,
        d.created_at_ms, d.updated_at_ms,
    )  # fmt: skip


def delivery_from_row(row: Sequence[Any]) -> Delivery:
    return Delivery(
        row[0], row[1], row[2], row[3], DeliveryState(row[4]), int(row[5]), row[6], row[7],
        row[8], row[9], row[10], float(row[11]), float(row[12]),
    )  # fmt: skip


class OutboxStore(Protocol):
    def write_transaction(self) -> contextlib.AbstractAsyncContextManager[None]: ...

    async def queue(self, deliveries: Sequence[Delivery]) -> int:
        """Insert each delivery whose (notification, channel) is not queued yet, in the
        caller's transaction; how many were new."""
        ...

    async def channel_deliveries(
        self, channel_id: str, limit: int, after: str | None = None
    ) -> list[Delivery]:
        """A page of a channel's deliveries, by id."""
        ...

    async def notification_deliveries(self, notification_ids: Sequence[str]) -> list[Delivery]:
        """Every delivery of these notifications, by notification then channel."""
        ...

    async def fail_channel(self, channel_id: str, code: str, at_ms: float) -> int:
        """Every queued delivery of a channel failed with ``code`` (a deleted channel's:
        ``channel_gone``); how many."""
        ...

    async def count_notification(self, scope: str, scope_id: str, hour: int, cap: int) -> bool:
        """One more notification of a rule or an organization (``scope``) in ``hour``, if fewer
        than ``cap`` were counted: whether it was. One statement, ``INSERT ... ON CONFLICT DO
        UPDATE SET sent = sent + 1 WHERE sent < cap``, in the caller's transaction (M25)."""
        ...

    async def add_summary(
        self, organization_id: str, hour: int, notification_id: str, at_ms: float
    ) -> bool:
        """Record the organization's suppression summary of ``hour``: whether it is the first
        (a unique index on (organization, hour): one an hour, M26)."""
        ...


class InMemoryOutboxStore:
    def __init__(self) -> None:
        self._rows: dict[str, Delivery] = {}
        self._caps: dict[tuple[str, str, int], int] = {}
        self._summaries: dict[tuple[str, int], tuple[str, float]] = {}

    @contextlib.asynccontextmanager
    async def write_transaction(self) -> AsyncIterator[None]:
        yield

    async def queue(self, deliveries: Sequence[Delivery]) -> int:
        new = 0
        for d in deliveries:
            if any(
                r.notification_id == d.notification_id and r.channel_id == d.channel_id
                for r in self._rows.values()
            ):
                continue
            self._rows[d.id] = d
            new += 1
        return new

    async def channel_deliveries(
        self, channel_id: str, limit: int, after: str | None = None
    ) -> list[Delivery]:
        found = sorted(
            (r for r in self._rows.values() if r.channel_id == channel_id), key=lambda r: r.id
        )
        return [r for r in found if after is None or r.id > after][:limit]

    async def notification_deliveries(self, notification_ids: Sequence[str]) -> list[Delivery]:
        wanted = set(notification_ids)
        return sorted(
            (r for r in self._rows.values() if r.notification_id in wanted),
            key=lambda r: (r.notification_id, r.channel_id),
        )

    async def fail_channel(self, channel_id: str, code: str, at_ms: float) -> int:
        done = 0
        for key, r in list(self._rows.items()):
            if r.channel_id == channel_id and r.state is DeliveryState.QUEUED:
                self._rows[key] = replace(
                    r, state=DeliveryState.FAILED, last_code=code, next_attempt_at_ms=None,
                    updated_at_ms=at_ms,
                )  # fmt: skip
                done += 1
        return done

    async def count_notification(self, scope: str, scope_id: str, hour: int, cap: int) -> bool:
        key = (scope, scope_id, hour)
        sent = self._caps.get(key, 0)
        if sent >= cap:
            return False
        self._caps[key] = sent + 1
        return True

    async def add_summary(
        self, organization_id: str, hour: int, notification_id: str, at_ms: float
    ) -> bool:
        if (organization_id, hour) in self._summaries:
            return False
        self._summaries[(organization_id, hour)] = (notification_id, at_ms)
        return True


class SqliteOutboxStore:
    """Over migration 17's ``notification_deliveries``. Coroutines that never suspend."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection

    def write_transaction(self) -> contextlib.AbstractAsyncContextManager[None]:
        return write_transaction(self._db)

    async def queue(self, deliveries: Sequence[Delivery]) -> int:
        new = 0
        for d in deliveries:
            done = self._db.execute(
                f"INSERT OR IGNORE INTO notification_deliveries ({COLUMNS})"
                f" VALUES ({', '.join('?' * 13)})",
                delivery_row(d),
            )
            new += done.rowcount
        return new

    def _rows(self, where: str, args: Sequence[Any]) -> list[Delivery]:
        rows = self._db.execute(
            f"SELECT {COLUMNS} FROM notification_deliveries WHERE {where}", tuple(args)
        ).fetchall()
        return [delivery_from_row(r) for r in rows]

    async def channel_deliveries(
        self, channel_id: str, limit: int, after: str | None = None
    ) -> list[Delivery]:
        return self._rows(
            "channel_id = ? AND id > ? ORDER BY id LIMIT ?", (channel_id, after or "", limit)
        )

    async def notification_deliveries(self, notification_ids: Sequence[str]) -> list[Delivery]:
        ids = list(dict.fromkeys(notification_ids))
        if not ids:
            return []
        return self._rows(
            f"notification_id IN ({', '.join('?' * len(ids))})"
            " ORDER BY notification_id, channel_id",
            ids,
        )

    async def fail_channel(self, channel_id: str, code: str, at_ms: float) -> int:
        done = self._db.execute(
            "UPDATE notification_deliveries SET state = 'failed', last_code = ?,"
            " next_attempt_at_ms = NULL, updated_at_ms = ?"
            " WHERE channel_id = ? AND state = 'queued'",
            (code, at_ms, channel_id),
        )
        return done.rowcount

    async def count_notification(self, scope: str, scope_id: str, hour: int, cap: int) -> bool:
        row = self._db.execute(
            "INSERT INTO notification_caps (scope, scope_id, hour, sent) VALUES (?, ?, ?, 1)"
            " ON CONFLICT (scope, scope_id, hour) DO UPDATE SET sent = sent + 1"
            " WHERE sent < ? RETURNING sent",
            (scope, scope_id, hour, cap),
        ).fetchone()
        return row is not None

    async def add_summary(
        self, organization_id: str, hour: int, notification_id: str, at_ms: float
    ) -> bool:
        done = self._db.execute(
            "INSERT OR IGNORE INTO suppression_summaries"
            " (organization_id, hour, notification_id, created_at_ms) VALUES (?, ?, ?, ?)",
            (organization_id, hour, notification_id, at_ms),
        )
        return done.rowcount == 1
