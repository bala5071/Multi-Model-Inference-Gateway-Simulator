"""Usage events, one per settled request, and the stores that keep them.

See docs/19-usage-store.md.

An event holds token counts and costs only. No prompt text, response text or token ids
ever reach a store (CLAUDE.md hard rule 7); ``UsageEvent`` has no field that could hold
them.

Every store method is synchronous, and callers must never move one onto a thread or
await it: on a ``SimClock`` an event loop that looks idle lets simulated time jump.
"""

from __future__ import annotations

import itertools
import sqlite3
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from gateway.schema import RequestResult

# How many events an in-memory store keeps, newest first out last. The ledger never needs
# older ones (see InMemoryUsageStore), so this only bounds what tests can inspect.
DEFAULT_MAX_EVENTS = 10_000


@dataclass(frozen=True, slots=True)
class ModelUsage:
    requests: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    base_cost_usd: float = 0.0
    billed_cost_usd: float = 0.0

    @property
    def billed_tokens(self) -> int:
        """Tokens that count against the budget: uncached prompt tokens plus completion."""
        return self.prompt_tokens - self.cached_tokens + self.completion_tokens

    def plus(self, result: RequestResult, billed_cost_usd: float) -> ModelUsage:
        return ModelUsage(
            requests=self.requests + 1,
            prompt_tokens=self.prompt_tokens + result.tokens_in,
            cached_tokens=self.cached_tokens + result.cached_tokens,
            completion_tokens=self.completion_tokens + result.tokens_out,
            base_cost_usd=self.base_cost_usd + result.cost_usd,
            billed_cost_usd=self.billed_cost_usd + billed_cost_usd,
        )

    def plus_event(self, event: UsageEvent) -> ModelUsage:
        return ModelUsage(
            requests=self.requests + 1,
            prompt_tokens=self.prompt_tokens + event.prompt_tokens,
            cached_tokens=self.cached_tokens + event.cached_tokens,
            completion_tokens=self.completion_tokens + event.completion_tokens,
            base_cost_usd=self.base_cost_usd + event.base_cost_usd,
            billed_cost_usd=self.billed_cost_usd + event.billed_cost_usd,
        )


@dataclass(frozen=True, slots=True)
class UsageEvent:
    """What one settled request used. ``occurred_at_ms`` is epoch time (``EpochSource``)."""

    application_id: str
    # The response id (req-N). Repeats across restarts; the store's seq is the unique key.
    request_id: str
    model: str
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int
    base_cost_usd: float
    billed_cost_usd: float
    occurred_at_ms: float

    @classmethod
    def of(
        cls,
        application_id: str,
        result: RequestResult,
        *,
        model: str,
        billed_cost_usd: float,
        occurred_at_ms: float,
    ) -> UsageEvent:
        return cls(
            application_id=application_id,
            request_id=result.request_id,
            model=model,
            prompt_tokens=result.tokens_in,
            cached_tokens=result.cached_tokens,
            completion_tokens=result.tokens_out,
            base_cost_usd=result.cost_usd,
            billed_cost_usd=billed_cost_usd,
            occurred_at_ms=occurred_at_ms,
        )


@dataclass(frozen=True, slots=True)
class StoredUsageEvent:
    # The store's own key: unique, increasing, never reused.
    seq: int
    event: UsageEvent


class UsageStore(Protocol):
    def append(self, event: UsageEvent) -> int:
        """Store one event and return its ``seq``."""
        ...

    def usage_by_model(
        self, application_id: str, start_ms: float, end_ms: float
    ) -> dict[str, ModelUsage]:
        """Totals per model of the application's events with ``start_ms <= occurred_at_ms <
        end_ms``, summed in ``seq`` order."""
        ...

    def events(self, application_id: str | None = None) -> list[StoredUsageEvent]:
        """Stored events in ``seq`` order, for one application or all of them."""
        ...


def _totals(events: Iterable[UsageEvent]) -> dict[str, ModelUsage]:
    by_model: dict[str, ModelUsage] = {}
    for event in events:
        by_model[event.model] = by_model.get(event.model, ModelUsage()).plus_event(event)
    return by_model


class InMemoryUsageStore:
    """Events in a bounded in-memory ring: the default for tests and simulated runs.

    Nothing survives the process. The ring only drops events the ledger can no longer
    ask for: the ledger asks a store for a period's totals once, when it first sees an
    application in that period, and in one process no event of that application in that
    period can exist yet. So ``max_events`` bounds memory without changing any total.
    """

    def __init__(self, max_events: int = DEFAULT_MAX_EVENTS) -> None:
        if max_events < 1:
            raise ValueError("max_events must be at least 1")
        self._events: deque[StoredUsageEvent] = deque(maxlen=max_events)
        self._seq = itertools.count(1)

    def append(self, event: UsageEvent) -> int:
        seq = next(self._seq)
        self._events.append(StoredUsageEvent(seq, event))
        return seq

    def usage_by_model(
        self, application_id: str, start_ms: float, end_ms: float
    ) -> dict[str, ModelUsage]:
        return _totals(
            s.event
            for s in self._events
            if s.event.application_id == application_id
            and start_ms <= s.event.occurred_at_ms < end_ms
        )

    def events(self, application_id: str | None = None) -> list[StoredUsageEvent]:
        return [
            s
            for s in self._events
            if application_id is None or s.event.application_id == application_id
        ]


# In the order of UsageEvent's fields.
_COLUMNS = (
    "application_id, request_id, model, prompt_tokens, cached_tokens, completion_tokens, "
    "base_cost_usd, billed_cost_usd, occurred_at_ms"
)


def _event(row: Sequence[Any]) -> UsageEvent:
    return UsageEvent(*row)


class SqliteUsageStore:
    """Events in the ``usage_events`` table (migration 1).

    The connection comes from ``gateway.database.open_database`` and must already be
    migrated. Each append is one short autocommit transaction; it blocks the event loop
    for as long as the write takes (measured in docs/19-usage-store.md).
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        found = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'usage_events'"
        ).fetchone()
        if found is None:
            raise RuntimeError("usage_events does not exist; run the migrations first")
        self._connection = connection

    def append(self, event: UsageEvent) -> int:
        cursor = self._connection.execute(
            f"INSERT INTO usage_events ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.application_id,
                event.request_id,
                event.model,
                event.prompt_tokens,
                event.cached_tokens,
                event.completion_tokens,
                event.base_cost_usd,
                event.billed_cost_usd,
                event.occurred_at_ms,
            ),
        )
        seq = cursor.lastrowid
        assert seq is not None
        return seq

    def usage_by_model(
        self, application_id: str, start_ms: float, end_ms: float
    ) -> dict[str, ModelUsage]:
        rows = self._connection.execute(
            f"SELECT {_COLUMNS} FROM usage_events"
            " WHERE application_id = ? AND occurred_at_ms >= ? AND occurred_at_ms < ?"
            " ORDER BY seq",
            (application_id, start_ms, end_ms),
        )
        # Summed in Python, in seq order, so the floating-point totals are the ones the
        # ledger built up request by request.
        return _totals(_event(row) for row in rows)

    def events(self, application_id: str | None = None) -> list[StoredUsageEvent]:
        query = f"SELECT seq, {_COLUMNS} FROM usage_events"
        params: tuple[str, ...] = ()
        if application_id is not None:
            query += " WHERE application_id = ?"
            params = (application_id,)
        rows = self._connection.execute(query + " ORDER BY seq", params).fetchall()
        return [StoredUsageEvent(int(row[0]), _event(row[1:])) for row in rows]
