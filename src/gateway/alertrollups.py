"""Per-minute rollups of every instance's recorders, and the flusher that sends them (docs/43).

``AlertRollups`` is a counter protocol with two implementations: ``InMemoryAlertRollups`` (the
recorder writes straight into it: one process, nothing to send) and ``ValkeyAlertRollups``
(``gateway.valkey``: every instance adds its deltas with ``HINCRBY``, so the minutes of several
instances add up exactly). Rollups are read by the evaluator (A3) and the validate preview
(A2); nothing on the request path reads them.

**A minute that cannot be trusted says so** (``incomplete``): one whose deltas an instance
dropped, and, in Valkey, one at or before a restart of the store (its counts were lost). A
signal over a window with an incomplete minute is "too few to judge".
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from gateway.alertrecorder import AlertRecorder, Counts, MinuteKey
from gateway.clock import Clock
from gateway.state import StateUnavailableError

FLUSH_EVERY_MS = 10_000.0
# Deltas an instance could not send are kept this long, then dropped and counted.
KEEP_UNSENT_MS = 10 * 60_000.0
# Minutes kept: the evaluator's hour and the plan's two hours of expiry in Valkey.
KEEP_MINUTES = 120


@dataclass(frozen=True, slots=True)
class MinuteRollup:
    application_id: str
    minute: int
    counts: Mapping[str, int] = field(default_factory=dict)
    incomplete: bool = False


class AlertRollups(Protocol):
    async def add(self, batch: str, deltas: Mapping[MinuteKey, Mapping[str, int]]) -> None:
        """Add counts, once per ``batch`` however often it is sent; raise
        ``StateUnavailableError`` if it may not have been taken."""
        ...

    async def mark_incomplete(self, keys: Sequence[MinuteKey]) -> None:
        """Say that these minutes lost counts."""
        ...

    async def read(self, application_id: str, minutes: Sequence[int]) -> list[MinuteRollup]:
        """One rollup per minute asked for, in order; a minute with nothing is empty."""
        ...

    async def read_many(
        self, wanted: Sequence[tuple[str, Sequence[int]]]
    ) -> list[list[MinuteRollup]]:
        """``read`` of each (application, minutes), in order, in one round trip (the
        evaluator's, a page at a time; docs/45)."""
        ...


class SharedAlertRollups(AlertRollups, Protocol):
    """Rollups in a store every instance shares, which can restart and lose them."""

    async def note_restart(self, minute: int) -> None:
        """The store restarted: every minute up to ``minute`` lost its counts."""
        ...


class InMemoryAlertRollups:
    """This process's rollups: the recorder absorbs into them at once (no I/O, no await), and
    minutes older than ``KEEP_MINUTES`` behind the newest are forgotten, so memory is bounded
    by the applications seen in that time."""

    def __init__(self, keep_minutes: int = KEEP_MINUTES) -> None:
        self._keep = keep_minutes
        self._minutes: dict[MinuteKey, Counts] = {}
        self._incomplete: set[MinuteKey] = set()
        self._newest = -1
        self._batches: dict[str, None] = {}

    def absorb(self, deltas: Mapping[MinuteKey, Mapping[str, int]]) -> None:
        for key, counts in deltas.items():
            into = self._minutes.setdefault(key, {})
            for name, n in counts.items():
                into[name] = into.get(name, 0) + n
            if key[1] > self._newest:
                self._newest = key[1]
                self._forget()

    def _forget(self) -> None:
        oldest = self._newest - self._keep
        for key in [k for k in self._minutes if k[1] <= oldest]:
            del self._minutes[key]
        self._incomplete = {k for k in self._incomplete if k[1] > oldest}

    def size(self) -> int:
        return len(self._minutes)

    async def add(self, batch: str, deltas: Mapping[MinuteKey, Mapping[str, int]]) -> None:
        if batch in self._batches:
            return
        self._batches[batch] = None
        while len(self._batches) > 1024:  # bounded: the oldest batch ids are forgotten
            del self._batches[next(iter(self._batches))]
        self.absorb(deltas)

    async def mark_incomplete(self, keys: Sequence[MinuteKey]) -> None:
        self._incomplete.update(keys)

    async def read(self, application_id: str, minutes: Sequence[int]) -> list[MinuteRollup]:
        return [
            MinuteRollup(
                application_id,
                minute,
                dict(self._minutes.get((application_id, minute), {})),
                (application_id, minute) in self._incomplete,
            )
            for minute in minutes
        ]

    async def read_many(
        self, wanted: Sequence[tuple[str, Sequence[int]]]
    ) -> list[list[MinuteRollup]]:
        return [await self.read(application_id, minutes) for application_id, minutes in wanted]


@dataclass
class _Batch:
    id: str
    deltas: dict[MinuteKey, Counts]
    since_ms: float


class RollupFlusher:
    """Every ``FLUSH_EVERY_MS`` (on the injected clock), off the request path: the recorder's
    deltas to the rollups, one batch per call. A batch is sent under its id until it is taken,
    and taken once however often it is sent (a call that timed out after Valkey applied it is
    not counted twice). New deltas go in a new batch. A batch not taken within
    ``KEEP_UNSENT_MS`` is dropped, counted (``on_dropped``, in (application, minute) entries),
    and its minutes are marked incomplete once the rollups answer again."""

    def __init__(
        self,
        recorder: AlertRecorder,
        rollups: AlertRollups,
        clock: Clock,
        instance: str,
        *,
        every_ms: float = FLUSH_EVERY_MS,
        keep_ms: float = KEEP_UNSENT_MS,
        on_dropped: Callable[[int], None] | None = None,
    ) -> None:
        self._recorder = recorder
        self._rollups = rollups
        self._clock = clock
        self._instance = instance
        self._every_ms = every_ms
        self._keep_ms = keep_ms
        self._on_dropped = on_dropped
        self._sequence = 0
        self._batches: list[_Batch] = []
        self._lost: set[MinuteKey] = set()
        self.dropped = 0

    def unsent(self) -> int:
        """(application, minute) entries waiting to be sent."""
        return sum(len(b.deltas) for b in self._batches)

    async def flush(self) -> bool:
        """Send what is waiting, oldest batch first; whether all of it was taken."""
        now = self._clock.now_ms()
        taken = self._recorder.take()
        if taken:
            self._sequence += 1
            self._batches.append(_Batch(f"{self._instance}:{self._sequence}", taken, now))
        try:
            while self._batches:
                await self._rollups.add(self._batches[0].id, self._batches[0].deltas)
                self._batches.pop(0)
            if self._lost:
                await self._rollups.mark_incomplete(sorted(self._lost))
                self._lost = set()
        except StateUnavailableError:
            kept = [b for b in self._batches if now - b.since_ms < self._keep_ms]
            expired = [b for b in self._batches if now - b.since_ms >= self._keep_ms]
            self._batches = kept
            entries = sum(len(b.deltas) for b in expired)
            for batch in expired:
                self._lost.update(batch.deltas)
            if entries:
                self.dropped += entries
                if self._on_dropped is not None:
                    self._on_dropped(entries)
            return False
        return True

    async def run(self) -> None:
        """Flush every ``every_ms`` until cancelled."""
        while True:
            await self._clock.sleep_ms(self._every_ms)
            await self.flush()
