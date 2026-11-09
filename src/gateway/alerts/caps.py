"""The notification caps and the instances' agreement on them (docs/46, the A4 plan's section 7).

**The caps.** Notifications, not deliveries, are counted: one to three channels count once.
Per rule an hour, per organization an hour, and test sends per channel and per organization an
hour. Each is one environment variable of the instance, a whole number in its range; anything
else refuses the start, naming the variable (``caps_from``). Each range starts at 1, so no cap
can be turned off, and each default is at most a sixth of its maximum (the plan's section 21.5).

**The ceilings row** (the main plan's decision 19, for the four caps: the reviewer's decision of
2026-11-08). Every instance writes its four values and their sha256 to ``instance_ceilings`` on
each lease attempt (every 10 s) and reads the other **live** instances' rows: those written in
the last ``LIVE_MS`` (a dead instance's row stops counting then, and is deleted after
``KEEP_MS``). A difference is **reported, never refused**: an ``alert_caps_differ`` event naming
the caps that differ (never a value of anything else) and a warning log line, once for each new
difference. The row and the event hold only the four numbers and their names.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, fields
from typing import Protocol

LOG = logging.getLogger("gateway.alerts")

# An instance whose row is older than this is not live: its values no longer count.
LIVE_MS = 60_000.0
# A row older than this is deleted (an instance that stopped long ago).
KEEP_MS = 60 * 60 * 1000.0


@dataclass(frozen=True, slots=True)
class AlertCaps:
    per_rule: int = 6
    per_organization: int = 60
    test_sends_per_channel: int = 10
    test_sends_per_organization: int = 30


# Each cap: its variable, and its range (the plan's section 7).
CAP_VARIABLES: Mapping[str, tuple[str, int, int]] = {
    "per_rule": ("GATEWAY_ALERT_CAP_PER_RULE", 1, 60),
    "per_organization": ("GATEWAY_ALERT_CAP_PER_ORGANIZATION", 1, 600),
    "test_sends_per_channel": ("GATEWAY_ALERT_TEST_SENDS_PER_CHANNEL", 1, 60),
    "test_sends_per_organization": ("GATEWAY_ALERT_TEST_SENDS_PER_ORGANIZATION", 1, 300),
}


def caps_from(environ: Mapping[str, str]) -> AlertCaps:
    """The caps this instance applies: each variable's value, or its default. A value that is
    not a whole number in its range (0, a negative number and an empty value among them)
    refuses the start, naming the variable (A4-17)."""
    found: dict[str, int] = {}
    for name, (variable, low, high) in CAP_VARIABLES.items():
        raw = environ.get(variable)
        if raw is None:
            continue
        text = raw.strip()
        if not text.isdigit() or not low <= int(text) <= high:
            raise ValueError(f"{variable} must be a whole number from {low} to {high}")
        found[name] = int(text)
    return AlertCaps(**found)


def ceilings_text(caps: AlertCaps) -> str:
    """The four values, by name, as stored: JSON with sorted keys."""
    return json.dumps(asdict(caps), sort_keys=True, separators=(",", ":"))


def digest(caps: AlertCaps) -> str:
    return hashlib.sha256(ceilings_text(caps).encode("ascii")).hexdigest()


def differing(mine: AlertCaps, others: Mapping[str, str]) -> list[str]:
    """The names of the caps on which a live instance differs from this one (``others``: each
    instance's stored values). A row that cannot be read differs on every cap."""
    names = [f.name for f in fields(AlertCaps)]
    own = asdict(mine)
    found: set[str] = set()
    for text in others.values():
        try:
            theirs = json.loads(text)
        except ValueError:
            theirs = {}
        if not isinstance(theirs, dict):
            theirs = {}
        found.update(n for n in names if theirs.get(n) != own[n])
    return [n for n in names if n in found]


class CeilingsStore(Protocol):
    async def report(
        self, instance_id: str, digest: str, ceilings: str, live_ms: float, keep_ms: float
    ) -> dict[str, str]:
        """Write this instance's row (its time the store's own), delete rows older than
        ``keep_ms``, and return the other instances' stored values whose row is younger than
        ``live_ms`` and whose digest differs, by instance id."""
        ...


class InMemoryCeilingsStore:
    """One process's, or several sharing it in a test: the store's time from ``now``."""

    def __init__(self, now: Callable[[], float]) -> None:
        self._now = now
        self._rows: dict[str, tuple[str, str, float]] = {}

    async def report(
        self, instance_id: str, digest: str, ceilings: str, live_ms: float, keep_ms: float
    ) -> dict[str, str]:
        now = self._now()
        self._rows[instance_id] = (digest, ceilings, now)
        for key in [k for k, (_, _, at) in self._rows.items() if at < now - keep_ms]:
            del self._rows[key]
        return {
            k: text
            for k, (d, text, at) in sorted(self._rows.items())
            if k != instance_id and d != digest and at > now - live_ms
        }


class SqliteCeilingsStore:
    """Over migration 17's ``instance_ceilings``: one process per database, so its row alone,
    unless an earlier process left one (it stops counting after ``LIVE_MS``)."""

    def __init__(self, connection: sqlite3.Connection, now: Callable[[], float]) -> None:
        self._db = connection
        self._now = now

    async def report(
        self, instance_id: str, digest: str, ceilings: str, live_ms: float, keep_ms: float
    ) -> dict[str, str]:
        now = self._now()
        self._db.execute(
            "INSERT INTO instance_ceilings (instance_id, digest, ceilings, seen_at_ms)"
            " VALUES (?, ?, ?, ?) ON CONFLICT (instance_id) DO UPDATE SET"
            " digest = excluded.digest, ceilings = excluded.ceilings,"
            " seen_at_ms = excluded.seen_at_ms",
            (instance_id, digest, ceilings, now),
        )
        self._db.execute("DELETE FROM instance_ceilings WHERE seen_at_ms < ?", (now - keep_ms,))
        rows = self._db.execute(
            "SELECT instance_id, ceilings FROM instance_ceilings WHERE instance_id <> ?"
            " AND digest <> ? AND seen_at_ms > ? ORDER BY instance_id",
            (instance_id, digest, now - live_ms),
        ).fetchall()
        return {r[0]: r[1] for r in rows}


class CeilingsWatch:
    """This instance's side of the comparison: ``check`` on each lease attempt. Reports each new
    difference once (an event and a warning), and nothing while every live instance agrees."""

    def __init__(
        self,
        store: CeilingsStore,
        caps: AlertCaps,
        instance_id: str,
        *,
        on_differ: Callable[[list[str]], None] | None = None,
    ) -> None:
        self._store = store
        self.caps = caps
        self._instance = instance_id
        self._digest = digest(caps)
        self._text = ceilings_text(caps)
        self._on_differ = on_differ
        self.differ: list[str] = []

    async def check(self) -> list[str]:
        """The caps a live instance differs on (empty: all agree)."""
        others = await self._store.report(self._instance, self._digest, self._text, LIVE_MS,
                                          KEEP_MS)  # fmt: skip
        now = differing(self.caps, others)
        if now and now != self.differ:
            LOG.warning(
                "alert caps differ between live instances: %s (reported, not refused)",
                ", ".join(now),
            )
            if self._on_differ is not None:
                self._on_differ(now)
        self.differ = now
        return now
