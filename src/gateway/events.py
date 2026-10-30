"""An in-process event bus with a bounded log. See docs/15-monitoring-api.md."""

from __future__ import annotations

import itertools
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from gateway.clock import Clock

DEFAULT_MAX_EVENTS = 1000

# Every type the bus accepts. The per-type counters are keyed by these, so they are bounded.
EVENT_TYPES = (
    "state_change",
    "queue_full",
    "timeout",
    "downgrade",
    "budget_exhausted",
    # An organization provider's circuit breaker moved (docs/33).
    "circuit_breaker",
    # The shared counter store came back empty after a restart (docs/35).
    "state_store_restarted",
    # A shared budget reservation outlived its request and was released (docs/37).
    "reservation_expired",
)


@dataclass(frozen=True, slots=True)
class Event:
    # Counts up from 1 with no gaps, so a reader can tell when it has missed some.
    seq: int
    at_ms: float
    type: str
    model: str | None
    application_id: str | None
    data: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class EventPage:
    events: tuple[Event, ...]
    # Events after the reader's cursor that the log had already dropped.
    missed: int


class EventBus:
    """Publishes gateway events to listeners and keeps the newest ``max_events`` of them.

    Listeners are called synchronously, inside ``publish`` and in the order they
    subscribed; they must not raise. A reader that cannot keep a listener attached (the
    HTTP stream) polls ``since`` with the last sequence number it saw instead, so a slow
    reader costs no memory: it is told how many events it missed.

    Nothing here awaits, so on an event loop a publish is atomic with respect to other
    tasks. The bus is not safe to share between threads.
    """

    def __init__(self, clock: Clock, *, max_events: int = DEFAULT_MAX_EVENTS) -> None:
        if max_events < 1:
            raise ValueError("max_events must be at least 1")
        self._clock = clock
        self._log: deque[Event] = deque(maxlen=max_events)
        self._last_seq = 0
        self._counts = dict.fromkeys(EVENT_TYPES, 0)
        self._listeners: list[Callable[[Event], None]] = []

    @property
    def capacity(self) -> int:
        assert self._log.maxlen is not None
        return self._log.maxlen

    @property
    def retained(self) -> int:
        return len(self._log)

    @property
    def last_seq(self) -> int:
        """Sequence number of the newest event; also how many were ever published."""
        return self._last_seq

    @property
    def counts(self) -> dict[str, int]:
        """Events ever published, by type. Every type is present, at 0 if never seen."""
        return dict(self._counts)

    def publish(
        self,
        type: str,
        *,
        model: str | None = None,
        application_id: str | None = None,
        **data: object,
    ) -> Event:
        if type not in self._counts:
            raise ValueError(f"unknown event type {type!r}; types are: {', '.join(EVENT_TYPES)}")
        self._last_seq += 1
        event = Event(self._last_seq, self._clock.now_ms(), type, model, application_id, data)
        self._log.append(event)
        self._counts[type] += 1
        for listener in tuple(self._listeners):
            listener(event)
        return event

    def subscribe(self, listener: Callable[[Event], None]) -> Callable[[], None]:
        """Call ``listener`` with every event from now on. Returns the function that stops it."""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    def since(self, seq: int) -> EventPage:
        """The retained events newer than ``seq``, oldest first.

        A ``seq`` ahead of the newest event cannot have come from this bus (the process
        restarted, say), so it is treated as 0: everything retained is returned.
        """
        if seq < 0 or seq > self._last_seq:
            seq = 0
        oldest = self._last_seq - len(self._log) + 1
        missed = max(0, oldest - 1 - seq)
        skip = max(0, seq - oldest + 1)
        return EventPage(tuple(itertools.islice(self._log, skip, None)), missed)

    def recent(self, limit: int) -> tuple[Event, ...]:
        """The newest ``limit`` retained events, oldest first."""
        if limit <= 0:
            return ()
        return tuple(itertools.islice(self._log, max(0, len(self._log) - limit), None))
