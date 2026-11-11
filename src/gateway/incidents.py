"""Incidents, the notifications recorded on them, and the evaluator's lease (docs/45, the plan's
sections 8 and 11).

**An incident** is one series of one rule (the series key is empty for an application-wide
signal, a provider id for ``circuit_open``, a key id for ``key_expires_in``) from pending to
resolved. At most one is open per (rule, series key): a partial unique index where the state is
not ``resolved``, on SQLite and Postgres alike.

**Every change is fenced and compared.** An evaluator writes only while it holds the lease at
the epoch it took it with (``Fence``), checked in the statement that writes; and an incident is
changed only from the state and revision it was read at, so a change made twice (two
evaluators, a retried cycle) is made once. A notification is recorded in the same transaction
as the change it belongs to: one rolled back records nothing.

**Notifications are delivered from A4** (docs/46). They are recorded on the incident
(``recorded``, or ``held`` in quiet hours and ``released`` when their summary is recorded), and a
recorded one's deliveries are queued in the outbox **in the same transaction** (``apply``'s
``then``). **An incident's first channels** (``notify_channels``) are the channel ids its first
delivered notification went to: NULL until then, written once by the statement that records it
(``WHERE notify_channels IS NULL``), and never by the compare-and-set update, which leaves the
column out. Its later notifications go to those ids.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field, fields, replace
from enum import StrEnum
from typing import Any, Protocol

from gateway.clock import EpochSource
from gateway.database import write_transaction

# The lease: held 30 s, renewed every 10 s by its holder (the plan's section 11.2).
LEASE_MS = 30_000.0
INCIDENT_ID_PREFIX = "inc_"
NOTIFICATION_ID_PREFIX = "ntf_"


class IncidentState(StrEnum):
    PENDING = "pending"
    FIRING = "firing"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


OPEN_STATES = (IncidentState.PENDING, IncidentState.FIRING, IncidentState.ACKNOWLEDGED)


class ResolveReason(StrEnum):
    CLEARED = "cleared"  # the condition false at two consecutive evaluations
    PENDING_CLEARED = "pending_cleared"  # it stopped holding before `for` passed
    NO_DATA = "no_data"  # 60 minutes with nothing judgeable while firing
    RULE_DISABLED = "rule_disabled"
    RULE_DELETED = "rule_deleted"
    RULE_CHANGED = "rule_changed"
    # The built-in channel_delivery_failing (docs/46): the channel delivered, or was deleted.
    CHANNEL_DELIVERED = "channel_delivered"
    CHANNEL_DELETED = "channel_deleted"


class NotificationKind(StrEnum):
    FIRING = "firing"
    FIRING_AGAIN = "firing_again"
    REMINDER = "reminder"
    RESOLVED = "resolved"
    QUIET_HOURS_SUMMARY = "quiet_hours_summary"


class NotificationState(StrEnum):
    RECORDED = "recorded"  # to be delivered to the rule's channels (A4)
    HELD = "held"  # in the rule's quiet hours
    RELEASED = "released"  # held, then covered by a quiet-hours summary


@dataclass(frozen=True, slots=True)
class Incident:
    id: str
    organization_id: str
    application_id: str
    rule_id: str
    rule_version: int
    series_key: str
    state: IncidentState
    severity: str
    opened_at_ms: float
    pending_since_ms: float | None = None
    fired_at_ms: float | None = None
    acknowledged_at_ms: float | None = None
    acknowledged_by: str | None = None
    resolved_at_ms: float | None = None
    resolve_reason: ResolveReason | None = None
    # The value that fired (or, while pending, the last value that held).
    value: float | None = None
    # Consecutive evaluations at which the condition was false while firing.
    false_count: int = 0
    no_data_since_ms: float | None = None
    # The last firing or firing-again notification, for the cooldown.
    last_notified_at_ms: float | None = None
    last_reminder_at_ms: float | None = None
    # Whether a firing notification was recorded since the incident last (re)opened: a resolved
    # one is recorded only then.
    firing_sent: bool = False
    # Fired again inside the cooldown: one firing-again notification once the cooldown ends.
    firing_again_due: bool = False
    reopened: int = 0
    revision: int = 1
    updated_at_ms: float = 0.0
    # The channel ids the first delivered notification went to (None: none yet). Set once;
    # the compare-and-set update never writes it (A4-8).
    notify_channels: tuple[str, ...] | None = None

    @property
    def open(self) -> bool:
        return self.state is not IncidentState.RESOLVED


@dataclass(frozen=True, slots=True)
class Notification:
    id: str
    incident_id: str
    application_id: str
    kind: NotificationKind
    state: NotificationState
    at_ms: float
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class Fence:
    """Who writes: the lease's holder, at the epoch it took the lease with."""

    holder: str
    epoch: int


@dataclass(frozen=True, slots=True)
class LeaseView:
    """The lease row as one attempt read it, with the store's time of that read."""

    holder: str | None
    epoch: int
    expires_at_ms: float
    last_cycle_completed_at_ms: float | None
    last_cycle_epoch: int | None
    now_ms: float

    def held_by(self, holder: str) -> bool:
        return self.holder == holder and self.expires_at_ms > self.now_ms


@dataclass(frozen=True, slots=True)
class Change:
    """One incident change: the incident as it is to be, the state and revision it was read at
    (None, None for a new one), and the notifications recorded with it."""

    incident: Incident
    expected_state: IncidentState | None
    expected_revision: int | None
    notifications: tuple[Notification, ...] = field(default_factory=tuple)
    # Held notifications of the incident covered by this change's summary.
    release_held: bool = False


class IncidentStore(Protocol):
    def write_transaction(self) -> contextlib.AbstractAsyncContextManager[None]: ...

    async def attempt_lease(self, holder: str) -> LeaseView:
        """Take the lease if it is free or expired (a new epoch), renew it if ``holder`` has it;
        either way return the row as it is now, with the store's time."""
        ...

    async def complete_cycle(self, fence: Fence) -> bool:
        """The heartbeat: the cycle's end, written with the renewal in one conditional
        statement, only while ``fence`` still holds the lease."""
        ...

    async def apply(self, fence: Fence, change: Change, then: Then | None = None) -> bool:
        """Make ``change`` if ``fence`` holds the lease and the incident is still at the state
        and revision it was read at (or, new, none is open for its rule and series): the change,
        its notifications, the incident's first channels (if it has none yet) and ``then`` (the
        outbox: the notifications' deliveries and their caps, M22) in one transaction. Whether
        it was made; ``then`` runs only if it was, and if it raises nothing is made."""
        ...

    async def acknowledge(
        self, application_id: str, incident_id: str, user_id: str, at_ms: float
    ) -> Incident | None:
        """Firing to acknowledged (a user, not the evaluator: no fence); None if it is not
        firing."""
        ...

    async def open_incidents(self) -> list[Incident]: ...

    async def recent_resolved(self, rule_id: str, series_key: str) -> Incident | None:
        """The most recently resolved incident of a series, for the cooldown."""
        ...

    async def recent_resolved_many(
        self, series: Collection[tuple[str, str]], since_ms: float
    ) -> dict[tuple[str, str], Incident]:
        """``recent_resolved`` of each (rule id, series key), in one read, among the incidents
        resolved at or after ``since_ms`` (an older one is past every cooldown: the evaluator
        passes its page's longest); a series with none is not in the answer."""
        ...

    async def get_incident(self, application_id: str, incident_id: str) -> Incident | None: ...

    async def list_incidents(
        self,
        organization_id: str,
        *,
        application_id: str | None,
        state: IncidentState | None,
        limit: int,
        after: str | None,
    ) -> list[Incident]: ...

    async def open_counts(self, organization_id: str) -> dict[str, dict[str, int]]:
        """How many of the organization's incidents are open, by application and state
        (docs/51): ``{application id: {state: count}}``, only the states with one, and only
        applications with one. The built-in incidents kept under the empty application id are
        not counted: they belong to no application."""
        ...

    async def notifications(self, incident_id: str) -> list[Notification]: ...

    async def held_notifications(self) -> list[Notification]:
        """Every notification held for quiet hours, to be summarised when they end."""
        ...


# The open incidents of one organization by application and state (docs/51): one statement on
# ``incidents_by_organization``, the same text for SQLite (``?``) and Postgres (``$1``). The empty
# application id is left out by ``count_open``, which every store's answer goes through.
OPEN_COUNTS_SQL = (
    "SELECT application_id, state, COUNT(*) FROM incidents"
    " WHERE organization_id = $1 AND state <> 'resolved'"
    " GROUP BY application_id, state"
)


def count_open(found: Iterable[tuple[str, str, int]]) -> dict[str, dict[str, int]]:
    """``open_counts``' answer from (application id, state, count) triples, the empty
    application id left out."""
    counts: dict[str, dict[str, int]] = {}
    for application_id, state, n in found:
        if application_id == "":
            continue
        per_state = counts.setdefault(application_id, {})
        per_state[state] = per_state.get(state, 0) + n
    return counts


# What runs inside ``apply``'s transaction once the incident is written: the outbox (M22).
Then = Callable[[], Awaitable[None]]
INCIDENT_COLUMNS = tuple(f.name for f in fields(Incident))
# What the compare-and-set update writes: every column but the id and the first channels, which
# are written once (A4-8).
UPDATE_COLUMNS = tuple(c for c in INCIDENT_COLUMNS[1:] if c != "notify_channels")
NOTIFICATION_COLUMNS = tuple(f.name for f in fields(Notification))


def incident_row(incident: Incident) -> tuple[Any, ...]:
    values = []
    for name in INCIDENT_COLUMNS:
        value = getattr(incident, name)
        if isinstance(value, bool):
            value = int(value)
        elif isinstance(value, StrEnum):
            value = value.value
        elif name == "notify_channels" and value is not None:
            value = channels_text(value)
        values.append(value)
    return tuple(values)


def update_row(incident: Incident) -> tuple[Any, ...]:
    """The values of ``UPDATE_COLUMNS``, in their order."""
    row = dict(zip(INCIDENT_COLUMNS, incident_row(incident), strict=True))
    return tuple(row[c] for c in UPDATE_COLUMNS)


def channels_text(channels: Sequence[str]) -> str:
    return json.dumps(list(channels), separators=(",", ":"))


def channels_of(text: str | None) -> tuple[str, ...] | None:
    if text is None:
        return None
    found = json.loads(text)
    if not isinstance(found, list) or not all(isinstance(c, str) for c in found):
        raise ValueError("an incident's first channels are a list of ids")
    return tuple(found)


def incident_from_row(row: Sequence[Any]) -> Incident:
    found = dict(zip(INCIDENT_COLUMNS, row, strict=True))
    found["state"] = IncidentState(found["state"])
    found["resolve_reason"] = (
        ResolveReason(found["resolve_reason"]) if found["resolve_reason"] is not None else None
    )
    for name in ("firing_sent", "firing_again_due"):
        found[name] = bool(found[name])
    for name in ("rule_version", "false_count", "reopened", "revision"):
        found[name] = int(found[name])
    found["notify_channels"] = channels_of(found["notify_channels"])
    return Incident(**found)


def latest_resolved(incidents: Iterable[Incident]) -> dict[tuple[str, str], Incident]:
    """The most recently resolved of each series (``recent_resolved``'s order: the resolution
    time, then the id)."""
    found: dict[tuple[str, str], Incident] = {}
    for i in incidents:
        key = (i.rule_id, i.series_key)
        best = found.get(key)
        if best is None or (i.resolved_at_ms or 0.0, i.id) > (best.resolved_at_ms or 0.0, best.id):
            found[key] = i
    return found


def notification_row(notification: Notification) -> tuple[Any, ...]:
    return tuple(
        v.value if isinstance(v, StrEnum) else v
        for v in (getattr(notification, n) for n in NOTIFICATION_COLUMNS)
    )


def notification_from_row(row: Sequence[Any]) -> Notification:
    found = dict(zip(NOTIFICATION_COLUMNS, row, strict=True))
    found["kind"] = NotificationKind(found["kind"])
    found["state"] = NotificationState(found["state"])
    return Notification(**found)


class InMemoryIncidentStore:
    """Incidents and the lease in memory, for tests and one process: the same rules as the
    databases (the fence, the compare, one open incident a series), on ``epoch``'s time. Two
    evaluators sharing one instance behave as two instances sharing a database."""

    def __init__(self, epoch: EpochSource) -> None:
        self._epoch = epoch
        self._incidents: dict[str, Incident] = {}
        self._notifications: list[Notification] = []
        self._lease = LeaseView(None, 0, 0.0, None, None, 0.0)

    @contextlib.asynccontextmanager
    async def write_transaction(self) -> AsyncIterator[None]:
        yield

    def _fenced(self, fence: Fence, now: float) -> bool:
        lease = self._lease
        return (
            lease.holder == fence.holder
            and lease.epoch == fence.epoch
            and lease.expires_at_ms > now
        )

    async def attempt_lease(self, holder: str) -> LeaseView:
        now = self._epoch.epoch_ms()
        lease = self._lease
        if lease.expires_at_ms <= now or lease.holder == holder:
            epoch = lease.epoch if lease.holder == holder else lease.epoch + 1
            lease = replace(lease, holder=holder, epoch=epoch, expires_at_ms=now + LEASE_MS)
        self._lease = replace(lease, now_ms=now)
        return self._lease

    async def complete_cycle(self, fence: Fence) -> bool:
        now = self._epoch.epoch_ms()
        if not self._fenced(fence, now):
            return False
        self._lease = replace(
            self._lease,
            last_cycle_completed_at_ms=now,
            last_cycle_epoch=fence.epoch,
            expires_at_ms=now + LEASE_MS,
        )
        return True

    async def apply(self, fence: Fence, change: Change, then: Then | None = None) -> bool:
        if not self._fenced(fence, self._epoch.epoch_ms()):
            return False
        new = change.incident
        found = self._incidents.get(new.id)
        if found is not None and found.notify_channels is not None:
            # Set once: the update leaves them as they are (A4-8).
            new = replace(new, notify_channels=found.notify_channels)
        if change.expected_state is None:
            if found is not None or any(
                i.open and i.rule_id == new.rule_id and i.series_key == new.series_key
                for i in self._incidents.values()
            ):
                return False
        elif (
            found is None
            or found.state is not change.expected_state
            or found.revision != change.expected_revision
        ):
            return False
        if then is not None:
            await then()  # before anything is kept: if it raises, nothing is made
        self._incidents[new.id] = new
        if change.release_held:
            self._notifications = [
                replace(n, state=NotificationState.RELEASED)
                if n.incident_id == new.id and n.state is NotificationState.HELD
                else n
                for n in self._notifications
            ]
        self._notifications.extend(change.notifications)
        return True

    async def acknowledge(
        self, application_id: str, incident_id: str, user_id: str, at_ms: float
    ) -> Incident | None:
        found = await self.get_incident(application_id, incident_id)
        if found is None or found.state is not IncidentState.FIRING:
            return None
        done = replace(
            found,
            state=IncidentState.ACKNOWLEDGED,
            acknowledged_at_ms=at_ms,
            acknowledged_by=user_id,
            revision=found.revision + 1,
            updated_at_ms=at_ms,
        )
        self._incidents[incident_id] = done
        return done

    async def open_incidents(self) -> list[Incident]:
        return sorted((i for i in self._incidents.values() if i.open), key=lambda i: i.id)

    async def recent_resolved(self, rule_id: str, series_key: str) -> Incident | None:
        resolved = [
            i
            for i in self._incidents.values()
            if not i.open and i.rule_id == rule_id and i.series_key == series_key
        ]
        return max(resolved, key=lambda i: (i.resolved_at_ms or 0.0, i.id), default=None)

    async def recent_resolved_many(
        self, series: Collection[tuple[str, str]], since_ms: float
    ) -> dict[tuple[str, str], Incident]:
        wanted = set(series)
        return latest_resolved(
            i
            for i in self._incidents.values()
            if not i.open
            and (i.rule_id, i.series_key) in wanted
            and (i.resolved_at_ms or 0.0) >= since_ms
        )

    async def get_incident(self, application_id: str, incident_id: str) -> Incident | None:
        found = self._incidents.get(incident_id)
        return found if found is not None and found.application_id == application_id else None

    async def list_incidents(
        self,
        organization_id: str,
        *,
        application_id: str | None,
        state: IncidentState | None,
        limit: int,
        after: str | None,
    ) -> list[Incident]:
        found = sorted(
            (
                i
                for i in self._incidents.values()
                if i.organization_id == organization_id
                and (application_id is None or i.application_id == application_id)
                and (state is None or i.state is state)
                and (after is None or i.id > after)
            ),
            key=lambda i: i.id,
        )
        return found[:limit]

    async def open_counts(self, organization_id: str) -> dict[str, dict[str, int]]:
        return count_open(
            (i.application_id, i.state.value, 1)
            for i in self._incidents.values()
            if i.organization_id == organization_id and i.open
        )

    async def notifications(self, incident_id: str) -> list[Notification]:
        return [n for n in self._notifications if n.incident_id == incident_id]

    async def held_notifications(self) -> list[Notification]:
        return [n for n in self._notifications if n.state is NotificationState.HELD]


_FENCE_SQLITE = (
    "EXISTS (SELECT 1 FROM alert_lease WHERE id = 1 AND holder = ? AND epoch = ?"
    " AND expires_at_ms > ?)"
)


class SqliteIncidentStore:
    """Over migration 16's tables, on ``epoch``'s time (the one process's). Coroutines that
    never suspend (docs/34)."""

    def __init__(self, connection: sqlite3.Connection, epoch: EpochSource) -> None:
        self._db = connection
        self._epoch = epoch

    def write_transaction(self) -> contextlib.AbstractAsyncContextManager[None]:
        return write_transaction(self._db)

    def _lease(self, now: float) -> LeaseView:
        row = self._db.execute(
            "SELECT holder, epoch, expires_at_ms, last_cycle_completed_at_ms, last_cycle_epoch"
            " FROM alert_lease WHERE id = 1"
        ).fetchone()
        return LeaseView(row[0], int(row[1]), float(row[2]), row[3], row[4], now)

    async def attempt_lease(self, holder: str) -> LeaseView:
        now = self._epoch.epoch_ms()
        async with self.write_transaction():
            # The one row, made by the first attempt (a fresh database has no rows).
            self._db.execute(
                "INSERT OR IGNORE INTO alert_lease (id, epoch, expires_at_ms) VALUES (1, 0, 0)"
            )
            self._db.execute(
                "UPDATE alert_lease SET"
                " epoch = CASE WHEN holder IS ? THEN epoch ELSE epoch + 1 END,"
                " holder = ?, expires_at_ms = ?"
                " WHERE id = 1 AND (expires_at_ms <= ? OR holder IS ?)",
                (holder, holder, now + LEASE_MS, now, holder),
            )
            return self._lease(now)

    async def complete_cycle(self, fence: Fence) -> bool:
        now = self._epoch.epoch_ms()
        done = self._db.execute(
            "UPDATE alert_lease SET last_cycle_completed_at_ms = ?, last_cycle_epoch = ?,"
            " expires_at_ms = ? WHERE id = 1 AND holder = ? AND epoch = ? AND expires_at_ms > ?",
            (now, fence.epoch, now + LEASE_MS, fence.holder, fence.epoch, now),
        )
        return done.rowcount == 1

    async def apply(self, fence: Fence, change: Change, then: Then | None = None) -> bool:
        now = self._epoch.epoch_ms()
        new = change.incident
        row = incident_row(new)
        async with self.write_transaction():
            if change.expected_state is None:
                try:
                    done = self._db.execute(
                        f"INSERT INTO incidents ({', '.join(INCIDENT_COLUMNS)})"
                        f" SELECT {', '.join('?' * len(row))} WHERE {_FENCE_SQLITE}",
                        (*row, fence.holder, fence.epoch, now),
                    )
                except sqlite3.IntegrityError:  # one open incident a series, by the index
                    return False
            else:
                done = self._db.execute(
                    f"UPDATE incidents SET {', '.join(f'{c} = ?' for c in UPDATE_COLUMNS)}"
                    f" WHERE id = ? AND state = ? AND revision = ? AND {_FENCE_SQLITE}",
                    (
                        *update_row(new), new.id, change.expected_state.value,
                        change.expected_revision, fence.holder, fence.epoch, now,
                    ),
                )  # fmt: skip
            if done.rowcount != 1:
                return False
            if change.expected_state is not None and new.notify_channels is not None:
                # The first channels, once: only while the incident has none.
                self._db.execute(
                    "UPDATE incidents SET notify_channels = ?"
                    " WHERE id = ? AND notify_channels IS NULL",
                    (channels_text(new.notify_channels), new.id),
                )
            if change.release_held:
                self._db.execute(
                    "UPDATE incident_notifications SET state = 'released'"
                    " WHERE incident_id = ? AND state = 'held'",
                    (new.id,),
                )
            for notification in change.notifications:
                self._db.execute(
                    f"INSERT INTO incident_notifications ({', '.join(NOTIFICATION_COLUMNS)})"
                    f" VALUES ({', '.join('?' * len(NOTIFICATION_COLUMNS))})",
                    notification_row(notification),
                )
            if then is not None:
                await then()
        return True

    async def acknowledge(
        self, application_id: str, incident_id: str, user_id: str, at_ms: float
    ) -> Incident | None:
        done = self._db.execute(
            "UPDATE incidents SET state = 'acknowledged', acknowledged_at_ms = ?,"
            " acknowledged_by = ?, revision = revision + 1, updated_at_ms = ?"
            " WHERE id = ? AND application_id = ? AND state = 'firing'",
            (at_ms, user_id, at_ms, incident_id, application_id),
        )
        if done.rowcount != 1:
            return None
        return await self.get_incident(application_id, incident_id)

    def _incidents(self, where: str, args: Sequence[Any]) -> list[Incident]:
        rows = self._db.execute(
            f"SELECT {', '.join(INCIDENT_COLUMNS)} FROM incidents WHERE {where}", tuple(args)
        ).fetchall()
        return [incident_from_row(r) for r in rows]

    async def open_incidents(self) -> list[Incident]:
        return self._incidents("state <> 'resolved' ORDER BY id", ())

    async def recent_resolved(self, rule_id: str, series_key: str) -> Incident | None:
        found = self._incidents(
            "rule_id = ? AND series_key = ? AND state = 'resolved'"
            " ORDER BY resolved_at_ms DESC, id DESC LIMIT 1",
            (rule_id, series_key),
        )
        return found[0] if found else None

    async def recent_resolved_many(
        self, series: Collection[tuple[str, str]], since_ms: float
    ) -> dict[tuple[str, str], Incident]:
        wanted = set(series)
        rules = sorted({rule_id for rule_id, _ in wanted})
        if not rules:
            return {}
        found = self._incidents(
            f"rule_id IN ({', '.join('?' * len(rules))}) AND state = 'resolved'"
            " AND resolved_at_ms >= ?",
            (*rules, since_ms),
        )
        return latest_resolved(i for i in found if (i.rule_id, i.series_key) in wanted)

    async def get_incident(self, application_id: str, incident_id: str) -> Incident | None:
        found = self._incidents("id = ? AND application_id = ?", (incident_id, application_id))
        return found[0] if found else None

    async def list_incidents(
        self,
        organization_id: str,
        *,
        application_id: str | None,
        state: IncidentState | None,
        limit: int,
        after: str | None,
    ) -> list[Incident]:
        where, args = ["organization_id = ?", "id > ?"], [organization_id, after or ""]
        if application_id is not None:
            where.append("application_id = ?")
            args.append(application_id)
        if state is not None:
            where.append("state = ?")
            args.append(state.value)
        return self._incidents(" AND ".join(where) + " ORDER BY id LIMIT ?", (*args, limit))

    async def open_counts(self, organization_id: str) -> dict[str, dict[str, int]]:
        rows = self._db.execute(OPEN_COUNTS_SQL.replace("$1", "?"), (organization_id,)).fetchall()
        return count_open((str(r[0]), str(r[1]), int(r[2])) for r in rows)

    async def notifications(self, incident_id: str) -> list[Notification]:
        rows = self._db.execute(
            f"SELECT {', '.join(NOTIFICATION_COLUMNS)} FROM incident_notifications"
            " WHERE incident_id = ? ORDER BY at_ms, id",
            (incident_id,),
        ).fetchall()
        return [notification_from_row(r) for r in rows]

    async def held_notifications(self) -> list[Notification]:
        rows = self._db.execute(
            f"SELECT {', '.join(NOTIFICATION_COLUMNS)} FROM incident_notifications"
            " WHERE state = 'held' ORDER BY incident_id, at_ms, id"
        ).fetchall()
        return [notification_from_row(r) for r in rows]
