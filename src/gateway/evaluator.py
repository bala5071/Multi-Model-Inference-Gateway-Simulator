"""The alert evaluator and its heartbeat (docs/45, the plan's sections 8 and 11).

**One evaluator at a time,** by the lease in the durable store: every instance attempts it every
10 s (``ATTEMPT_MS``); the one that holds it runs a cycle every 30 s (``CYCLE_MS``) and writes
only with its ``Fence``. Taking the lease over bumps its epoch, so an evaluator that paused past
its lease writes nothing, the heartbeat included.

**A cycle** reads the open incidents and the enabled rules, each rule's signal over its window
(the last complete minutes of the rollups, or a durable value), and makes each series'
transition, every one its own fenced compare-and-set (``IncidentStore.apply``). What each value
means for an incident is decided by one pure function, ``decide``.

**Its cost** (the plan's section 11.7): the applications with enabled rules are evaluated in
pages of ``APPLICATION_PAGE``, each page's durable values, rollups and recently resolved
incidents read once for the page (``BulkSignals``), so a cycle's store calls grow with the
pages. A cycle starts no application ``CYCLE_BUDGET_MS`` after it began (always after at least
one), and the next starts with the first one not reached (the fair resume). Between two
applications it gives the event loop back, so a store whose calls never suspend (SQLite) holds
a request up by one application at most.

**The heartbeat** is the lease row's ``last_cycle_completed_at_ms``, written with the renewal at
the end of each completed cycle. Every instance keeps the row its last attempt read, and
``Heartbeat.state`` judges it from that memory alone: health and metrics make no store call.
Monitoring decides nothing: the heartbeat never chooses who evaluates (the lease does).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Protocol

from gateway.alertrecorder import MINUTE_MS
from gateway.alertrollups import AlertRollups, MinuteRollup
from gateway.alertrules import AlertRule, QuietHours, RuleSpec, RuleStore
from gateway.clock import Clock
from gateway.incidents import (
    INCIDENT_ID_PREFIX,
    NOTIFICATION_ID_PREFIX,
    Change,
    Fence,
    Incident,
    IncidentState,
    IncidentStore,
    LeaseView,
    Notification,
    NotificationKind,
    NotificationState,
    ResolveReason,
)
from gateway.signals import FROM_ROLLUPS, INCOMPLETE, SignalValue, evaluate
from gateway.state import StateUnavailableError

LOG = logging.getLogger("gateway.alerts")

ATTEMPT_MS = 10_000.0
CYCLE_MS = 30_000.0
# A minute is complete 25 s after it ends (the 10 s flush, twice the skew bound, a margin).
COMPLETE_AFTER_MS = 25_000.0
# Firing resolves after the condition is false at two consecutive evaluations (one minute).
FALSE_TO_RESOLVE = 2
NO_DATA_MS = 60 * MINUTE_MS
# The heartbeat (the plan's section 11.6).
OK_WITHIN_MS = 90_000.0
STALE_IF_NONE_AFTER_MS = 120_000.0
UNKNOWN_AFTER_MS = 30_000.0
PAGE = 200
# The cycle's cost (section 11.7): applications a page, and when a cycle stops starting them.
APPLICATION_PAGE = 200
CYCLE_BUDGET_MS = 20_000.0
UNAVAILABLE = SignalValue(None, "unavailable")

_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


# --- what one value means for one series ---------------------------------------------------------


def _met(comparison: str, value: float, threshold: float) -> bool:
    return {
        ">": value > threshold,
        ">=": value >= threshold,
        "<": value < threshold,
        "<=": value <= threshold,
    }[comparison]


def _minutes_of(clock: str) -> int:
    return int(clock[:2]) * 60 + int(clock[3:])


def in_quiet_hours(quiet: QuietHours | None, at_ms: float) -> bool:
    """Whether ``at_ms`` (epoch ms) is in ``quiet``, at its fixed UTC offset: from ``start`` to
    ``end`` on its days, a span past midnight belonging to the day it starts on."""
    if quiet is None:
        return False
    sign = -1 if quiet.utc_offset[0] == "-" else 1
    offset = sign * (int(quiet.utc_offset[1:3]) * 60 + int(quiet.utc_offset[4:6]))
    local_minutes = int(at_ms // MINUTE_MS) + offset
    day, minute = divmod(local_minutes, 24 * 60)
    weekday = _WEEKDAYS[(day + 3) % 7]  # 1970-01-01 was a Thursday
    yesterday = _WEEKDAYS[(day + 2) % 7]
    start, end = _minutes_of(quiet.start), _minutes_of(quiet.end)
    if start == end:
        return False
    if start < end:
        return weekday in quiet.days and start <= minute < end
    return (weekday in quiet.days and minute >= start) or (yesterday in quiet.days and minute < end)


@dataclass(frozen=True, slots=True)
class Series:
    """One series of one rule at one evaluation: its key and value."""

    rule: AlertRule
    organization_id: str
    series_key: str
    value: SignalValue


IdMaker = Callable[[str], str]


def decide(
    series: Series,
    incident: Incident | None,
    recent: Incident | None,
    now_ms: float,
    new_id: IdMaker,
) -> Change | None:
    """The change ``series`` makes to its open ``incident`` (or, with none open, whether it opens
    one, or reopens ``recent``, the series' last resolved incident, inside the cooldown). None:
    nothing to write. The one place the states of the plan's section 8 are decided."""
    spec = series.rule.spec
    value = series.value
    met = (
        value.value is not None
        and value.judgeable
        and _met(spec.comparison, value.value, spec.threshold)
    )
    cooldown_ms = spec.cooldown_minutes * MINUTE_MS

    def note(incident: Incident, kind: NotificationKind, reason: str | None = None) -> Notification:
        held = spec.severity != "critical" and in_quiet_hours(spec.quiet_hours, now_ms)
        return Notification(
            new_id(NOTIFICATION_ID_PREFIX),
            incident.id,
            incident.application_id,
            kind,
            NotificationState.HELD if held else NotificationState.RECORDED,
            now_ms,
            reason,
        )

    def changed(
        before: Incident | None, after: Incident, notes: Sequence[Notification] = ()
    ) -> Change:
        if before is not None:
            after = replace(after, revision=before.revision + 1, updated_at_ms=now_ms)
        return Change(
            after,
            before.state if before is not None else None,
            before.revision if before is not None else None,
            tuple(notes),
        )

    def fire(before: Incident, opened: Incident) -> Change:
        # To firing: notified now, unless a firing notification of this series was recorded
        # inside the cooldown; then once the cooldown ends, as "firing again".
        fired = replace(
            opened,
            state=IncidentState.FIRING,
            fired_at_ms=now_ms,
            value=value.value,
            false_count=0,
            no_data_since_ms=None,
        )
        last = opened.last_notified_at_ms
        if last is not None and now_ms - last < cooldown_ms:
            return changed(before, replace(fired, firing_again_due=True))
        fired = replace(fired, last_notified_at_ms=now_ms, firing_sent=True, firing_again_due=False)
        kind = NotificationKind.FIRING if opened.reopened == 0 else NotificationKind.FIRING_AGAIN
        return changed(before, fired, [note(fired, kind)])

    def resolve(before: Incident, reason: ResolveReason) -> Change:
        done = replace(
            before,
            state=IncidentState.RESOLVED,
            resolved_at_ms=now_ms,
            resolve_reason=reason,
            firing_again_due=False,
        )
        notes = [note(done, NotificationKind.RESOLVED, reason.value)] if before.firing_sent else []
        return changed(before, done, notes)

    if incident is None:
        if not met:
            return None
        if (
            recent is not None
            and recent.last_notified_at_ms is not None
            and now_ms - recent.last_notified_at_ms < cooldown_ms
        ):
            # Fired again inside the cooldown: the resolved incident is reopened, not a new one.
            reopened = replace(
                recent,
                state=IncidentState.PENDING,
                rule_version=series.rule.version,
                pending_since_ms=now_ms,
                resolved_at_ms=None,
                resolve_reason=None,
                value=value.value,
                false_count=0,
                no_data_since_ms=None,
                firing_sent=False,
                reopened=recent.reopened + 1,
            )
            if spec.for_minutes == 0:
                return fire(recent, reopened)
            return changed(recent, reopened)
        opened = Incident(
            new_id(INCIDENT_ID_PREFIX),
            series.organization_id,
            series.rule.application_id,
            series.rule.id,
            series.rule.version,
            series.series_key,
            IncidentState.PENDING,
            spec.severity,
            now_ms,
            pending_since_ms=now_ms,
            value=value.value,
            updated_at_ms=now_ms,
        )
        if spec.for_minutes == 0:
            change = fire(opened, opened)
            return replace(change, expected_state=None, expected_revision=None)
        return Change(opened, None, None)

    if incident.state is IncidentState.PENDING:
        if not met:
            return resolve(incident, ResolveReason.PENDING_CLEARED)
        assert incident.pending_since_ms is not None
        if now_ms - incident.pending_since_ms >= spec.for_minutes * MINUTE_MS:
            return fire(incident, incident)
        return None

    # Firing or acknowledged.
    if not value.judgeable:
        # Too few to judge: it stays open, marked; after an hour of that it resolves, said so.
        since = incident.no_data_since_ms if incident.no_data_since_ms is not None else now_ms
        if now_ms - since >= NO_DATA_MS:
            return resolve(incident, ResolveReason.NO_DATA)
        if incident.no_data_since_ms is None:
            return changed(incident, replace(incident, no_data_since_ms=now_ms))
        return None
    if not met:
        false_count = incident.false_count + 1
        if false_count >= FALSE_TO_RESOLVE:
            return resolve(incident, ResolveReason.CLEARED)
        return changed(incident, replace(incident, false_count=false_count, no_data_since_ms=None))
    held = replace(incident, false_count=0, no_data_since_ms=None)
    notes: list[Notification] = []
    if (
        held.firing_again_due
        and held.last_notified_at_ms is not None
        and now_ms - held.last_notified_at_ms >= cooldown_ms
    ):
        held = replace(held, firing_again_due=False, last_notified_at_ms=now_ms, firing_sent=True)
        notes.append(note(held, NotificationKind.FIRING_AGAIN))
    elif (
        held.state is IncidentState.FIRING
        and spec.repeat_every_minutes is not None
        and held.firing_sent
    ):
        base = held.last_reminder_at_ms or held.last_notified_at_ms or now_ms
        if now_ms - base >= spec.repeat_every_minutes * MINUTE_MS:
            held = replace(held, last_reminder_at_ms=now_ms)
            notes.append(note(held, NotificationKind.REMINDER))
    if held == incident and not notes:
        return None
    return changed(incident, held, notes)


def rule_gone(
    incident: Incident, rule: AlertRule | None, now_ms: float, new_id: IdMaker
) -> Change | None:
    """An open incident whose rule was deleted, disabled or changed resolves, said so."""
    if rule is None:
        reason = ResolveReason.RULE_DELETED
    elif not rule.spec.enabled:
        reason = ResolveReason.RULE_DISABLED
    elif rule.version != incident.rule_version:
        reason = ResolveReason.RULE_CHANGED
    else:
        return None
    done = replace(
        incident,
        state=IncidentState.RESOLVED,
        resolved_at_ms=now_ms,
        resolve_reason=reason,
        firing_again_due=False,
        revision=incident.revision + 1,
        updated_at_ms=now_ms,
    )
    notes: tuple[Notification, ...] = ()
    if incident.firing_sent:
        notes = (
            Notification(
                new_id(NOTIFICATION_ID_PREFIX), incident.id, incident.application_id,
                NotificationKind.RESOLVED, NotificationState.RECORDED, now_ms, reason.value,
            ),
        )  # fmt: skip
    return Change(done, incident.state, incident.revision, notes)


def quiet_hours_over(
    incident: Incident, rule: AlertRule | None, held: int, now_ms: float, new_id: IdMaker
) -> Change | None:
    """At the end of a rule's quiet hours, one summary for an incident with held notifications,
    which covers them (they become ``released``)."""
    quiet = rule.spec.quiet_hours if rule is not None else None
    if held == 0 or in_quiet_hours(quiet, now_ms):
        return None
    summary = Notification(
        new_id(NOTIFICATION_ID_PREFIX), incident.id, incident.application_id,
        NotificationKind.QUIET_HOURS_SUMMARY, NotificationState.RECORDED, now_ms,
        f"{held} held",
    )  # fmt: skip
    after = replace(incident, revision=incident.revision + 1, updated_at_ms=now_ms)
    return Change(after, incident.state, incident.revision, (summary,), release_held=True)


# --- the durable signals, as the evaluator reads them ---------------------------------------------


class DurableSignals(Protocol):
    """The signals not in the rollups, read from the stores for one application (A3's wiring in
    ``app.py``): one value, or one per series."""

    async def budget_used(self, application_id: str) -> SignalValue: ...

    async def circuit_open(
        self, organization_id: str, application_id: str
    ) -> dict[str, SignalValue]: ...

    async def key_expires_in(self, application_id: str) -> dict[str, SignalValue]: ...

    async def reservation_expired(self, application_id: str) -> SignalValue: ...


@dataclass(frozen=True, slots=True)
class ApplicationSignals:
    """One application's durable values for one page of a cycle: its organization (None: the
    application is gone, and its rules are not judged) and the values its rules asked for."""

    organization_id: str | None
    budget_used: SignalValue = UNAVAILABLE
    circuit_open: Mapping[str, SignalValue] = field(default_factory=dict)
    key_expires_in: Mapping[str, SignalValue] = field(default_factory=dict)
    reservation_expired: SignalValue = UNAVAILABLE


class BulkSignals(Protocol):
    """The durable signals of a page of applications, each kind read once for the page (the
    server's, in ``app.py``)."""

    async def read_many(
        self, wanted: Mapping[str, frozenset[str]]
    ) -> Mapping[str, ApplicationSignals]:
        """For each application id, the durable signals (by name) its enabled rules use; every
        id asked is in the answer."""
        ...


class PerApplicationSignals:
    """``BulkSignals`` from one-application reads, one application at a time: what an
    evaluator given ``organization_of`` and ``DurableSignals`` reads through."""

    def __init__(
        self, organization_of: Callable[[str], Awaitable[str | None]], durable: DurableSignals
    ) -> None:
        self._organization_of = organization_of
        self._durable = durable

    async def read_many(
        self, wanted: Mapping[str, frozenset[str]]
    ) -> Mapping[str, ApplicationSignals]:
        found: dict[str, ApplicationSignals] = {}
        d = self._durable
        for application_id, signals in wanted.items():
            organization_id = await self._organization_of(application_id)
            if organization_id is None:
                found[application_id] = ApplicationSignals(None)
                continue
            budget = (
                await d.budget_used(application_id) if "budget_used" in signals else UNAVAILABLE
            )
            circuit = (
                await d.circuit_open(organization_id, application_id)
                if "circuit_open" in signals
                else {}
            )
            keys = await d.key_expires_in(application_id) if "key_expires_in" in signals else {}
            expired = (
                await d.reservation_expired(application_id)
                if "reservation_expired" in signals
                else UNAVAILABLE
            )
            found[application_id] = ApplicationSignals(
                organization_id, budget, circuit, keys, expired
            )
        return found


# --- the heartbeat --------------------------------------------------------------------------------


class HeartbeatState(StrEnum):
    OK = "ok"
    STALE = "stale"
    UNKNOWN = "unknown"
    DISABLED = "disabled"


class Heartbeat:
    """One instance's view of the evaluator's liveness: the lease row its last attempt read, and
    when (on this instance's clock). ``state`` is judged from that memory, never from a store."""

    def __init__(
        self,
        clock: Clock,
        holder: str,
        *,
        on_change: Callable[[HeartbeatState, HeartbeatState], None] | None = None,
    ) -> None:
        self._clock = clock
        self._holder = holder
        self._started_ms = clock.now_ms()
        self._read: tuple[LeaseView, float] | None = None
        self._on_change = on_change
        self._reported = HeartbeatState.OK

    def read(self, view: LeaseView) -> None:
        self._read = (view, self._clock.now_ms())
        self._notice()

    def failed(self) -> None:
        """An attempt could not read the row: the memory ages, nothing else changes."""
        self._notice()

    def _notice(self) -> None:
        state = self.state()
        if state is not self._reported and self._on_change is not None:
            self._on_change(self._reported, state)
        self._reported = state

    @property
    def holder(self) -> bool:
        return self._read is not None and self._read[0].held_by(self._holder)

    def last_cycle_age_ms(self) -> float | None:
        """The age of the last completed cycle, now; None while unknown or before any cycle."""
        if self._fresh() is None:
            return None
        view, at = self._read  # type: ignore[misc]
        if view.last_cycle_completed_at_ms is None:
            return None
        return view.now_ms - view.last_cycle_completed_at_ms + (self._clock.now_ms() - at)

    def _fresh(self) -> LeaseView | None:
        if self._read is None:
            return None
        view, at = self._read
        return view if self._clock.now_ms() - at < UNKNOWN_AFTER_MS else None

    def state(self) -> HeartbeatState:
        now = self._clock.now_ms()
        view = self._fresh()
        if view is None:
            if self._read is None and now - self._started_ms < UNKNOWN_AFTER_MS:
                return HeartbeatState.OK  # starting: no attempt has had time to answer
            return HeartbeatState.UNKNOWN
        age = self.last_cycle_age_ms()
        if age is None:
            # No cycle has ever completed: stale once an evaluator has had time to run one.
            return (
                HeartbeatState.STALE
                if now - self._started_ms >= STALE_IF_NONE_AFTER_MS
                else HeartbeatState.OK
            )
        return HeartbeatState.OK if age <= OK_WITHIN_MS else HeartbeatState.STALE


@dataclass(frozen=True, slots=True)
class EvaluatorStatus:
    """What health, metrics and the snapshot show (from memory)."""

    state: HeartbeatState
    last_cycle_age_ms: float | None
    holder: bool
    last_evaluation_ms: float | None
    failures: int


# --- the evaluator --------------------------------------------------------------------------------


class AlertEvaluator:
    def __init__(
        self,
        *,
        incidents: IncidentStore,
        rules: RuleStore,
        rollups: AlertRollups | None,
        organization_of: Callable[[str], Awaitable[str | None]] | None = None,
        durable: DurableSignals | None = None,
        signals: BulkSignals | None = None,
        clock: Clock,
        holder: str,
        random_bytes: Callable[[int], bytes],
        on_heartbeat_change: Callable[[HeartbeatState, HeartbeatState], None] | None = None,
    ) -> None:
        """The durable signals come from ``signals`` (a page at a time), or else one
        application at a time from ``organization_of`` and ``durable``."""
        if signals is None:
            if organization_of is None or durable is None:
                raise ValueError("give signals, or organization_of and durable")
            signals = PerApplicationSignals(organization_of, durable)
        self.incidents = incidents
        self._rules = rules
        self._rollups = rollups
        self._signals = signals
        self._clock = clock
        self.holder = holder
        self._random_bytes = random_bytes
        self.heartbeat = Heartbeat(clock, holder, on_change=on_heartbeat_change)
        self._last_cycle_local_ms: float | None = None
        self.last_evaluation_ms: float | None = None
        self.failures = 0
        # When this evaluator first saw each provider's breaker open: circuit_open's minutes.
        self._open_since: dict[tuple[str, str], float] = {}
        # The fair resume: the last application a cycle cut short reached (None: start at the
        # first). How many cycles the time box has cut.
        self._resume_after: str | None = None
        self.cut_short = 0

    def status(self) -> EvaluatorStatus:
        return EvaluatorStatus(
            self.heartbeat.state(),
            self.heartbeat.last_cycle_age_ms(),
            self.heartbeat.holder,
            self.last_evaluation_ms,
            self.failures,
        )

    def _new_id(self, prefix: str) -> str:
        return prefix + self._random_bytes(8).hex()

    async def attempt(self) -> LeaseView | None:
        """One lease attempt: the row read (and renewed or taken), or None if the store did not
        answer."""
        try:
            view = await self.incidents.attempt_lease(self.holder)
        except (StateUnavailableError, OSError, RuntimeError) as exc:
            LOG.warning("alert evaluator: the lease could not be read (%s)", type(exc).__name__)
            self.heartbeat.failed()
            return None
        self.heartbeat.read(view)
        return view

    async def step(self) -> bool:
        """What ``run`` does every 10 s: the lease attempt, and a cycle when this instance holds
        the lease and the last one is 30 s old. Whether a cycle completed."""
        view = await self.attempt()
        if view is None or not view.held_by(self.holder):
            return False
        local = self._clock.now_ms()
        if self._last_cycle_local_ms is not None and local - self._last_cycle_local_ms < CYCLE_MS:
            return False
        self._last_cycle_local_ms = local
        fence = Fence(self.holder, view.epoch)
        started = self._clock.now_ms()
        try:
            await self.cycle(fence, view.now_ms)
            completed = await self.incidents.complete_cycle(fence)
        except (StateUnavailableError, OSError, RuntimeError) as exc:
            self.failures += 1
            LOG.warning("alert evaluator: a cycle failed (%s)", type(exc).__name__)
            return False
        self.last_evaluation_ms = self._clock.now_ms() - started
        if completed:
            # Read again, so the heartbeat is this cycle's at once.
            await self.attempt()
        return completed

    async def run(self) -> None:
        """Every 10 s until cancelled."""
        while True:
            await self.step()
            await self._clock.sleep_ms(ATTEMPT_MS)

    async def _enabled_rules(self) -> list[AlertRule]:
        found: list[AlertRule] = []
        after: str | None = None
        while True:
            page = await self._rules.list_enabled_rules(PAGE, after)
            found += page
            if len(page) < PAGE:
                return found
            after = page[-1].id

    def _out_of_time(self, started: float) -> bool:
        return self._clock.now_ms() - started >= CYCLE_BUDGET_MS

    async def _windows(
        self, rules: Sequence[AlertRule], now_ms: float
    ) -> dict[str, list[MinuteRollup]] | None:
        """The rollups of each application with a rollup rule, read once over its longest
        window (each rule takes its own last minutes of it); None if they cannot be read."""
        longest: dict[str, int] = {}
        for rule in rules:
            assert rule.spec.window is not None
            longest[rule.application_id] = max(
                rule.spec.window, longest.get(rule.application_id, 0)
            )
        if self._rollups is None:
            return None
        if not longest:
            return {}
        last = int((now_ms - COMPLETE_AFTER_MS) // MINUTE_MS) - 1
        wanted = [(a, range(last - n + 1, last + 1)) for a, n in sorted(longest.items())]
        try:
            read = await self._rollups.read_many(wanted)
        except StateUnavailableError:
            return None  # Valkey down: those minutes are incomplete
        return {a: minutes for (a, _), minutes in zip(wanted, read, strict=True)}

    def _values(
        self,
        rule: AlertRule,
        signals: ApplicationSignals,
        windows: Mapping[str, list[MinuteRollup]] | None,
        now_ms: float,
    ) -> dict[str, SignalValue]:
        spec: RuleSpec = rule.spec
        if spec.signal in FROM_ROLLUPS:
            assert spec.window is not None
            if windows is None:
                return {"": INCOMPLETE}
            window = windows[rule.application_id][-spec.window :]
            return {
                "": evaluate(
                    spec.signal, rule.application_id, window, window=spec.window,
                    parameter=spec.parameter,
                )
            }  # fmt: skip
        if spec.signal == "budget_used":
            return {"": signals.budget_used}
        if spec.signal == "key_expires_in":
            return dict(signals.key_expires_in)
        if spec.signal == "reservation_expired":
            return {"": signals.reservation_expired}
        assert spec.signal == "circuit_open"
        assert signals.organization_id is not None
        values: dict[str, SignalValue] = {}
        for provider, open_now in signals.circuit_open.items():
            key = (signals.organization_id, provider)
            if open_now.value is None:
                values[provider] = open_now
            elif open_now.value > 0:
                since = self._open_since.setdefault(key, now_ms)
                values[provider] = SignalValue((now_ms - since) / MINUTE_MS)
            else:
                self._open_since.pop(key, None)
                values[provider] = SignalValue(0.0)
        return values

    def _order(self, application_ids: Sequence[str]) -> list[str]:
        """The applications in id order, starting after the last one a cut cycle reached."""
        ordered = sorted(application_ids)
        after = self._resume_after
        if after is None:
            return ordered
        return [a for a in ordered if a > after] + [a for a in ordered if a <= after]

    async def _read_page(
        self,
        page: Sequence[str],
        by_application: Mapping[str, list[AlertRule]],
        open_by_series: Mapping[tuple[str, str], Incident],
        now_ms: float,
    ) -> tuple[
        Mapping[str, ApplicationSignals],
        dict[str, dict[str, SignalValue]],
        Mapping[tuple[str, str], Incident],
    ]:
        """A page's reads, each once for the page: the durable values, the rollups, and the
        last resolved incident of each series with none open."""
        signals = await self._signals.read_many(
            {
                a: frozenset(
                    r.spec.signal for r in by_application[a] if r.spec.signal not in FROM_ROLLUPS
                )
                for a in page
            }
        )
        live = [
            r for a in page if signals[a].organization_id is not None for r in by_application[a]
        ]
        windows = await self._windows([r for r in live if r.spec.signal in FROM_ROLLUPS], now_ms)
        values: dict[str, dict[str, SignalValue]] = {}
        for rule in live:
            found = self._values(rule, signals[rule.application_id], windows, now_ms)
            # A series whose key is gone (a key revoked, a provider removed) is not judgeable.
            for key in [k for (r, k) in open_by_series if r == rule.id and k not in found]:
                found[key] = SignalValue(None, "too_few")
            values[rule.id] = found
        unopened = [
            (rule_id, key)
            for rule_id, found in values.items()
            for key in found
            if (rule_id, key) not in open_by_series
        ]
        if not unopened:
            return signals, values, {}
        # Resolved before the page's longest cooldown, an incident cannot be reopened (its last
        # notification is older still), so it is not read.
        longest = max(r.spec.cooldown_minutes for r in live)
        recent = await self.incidents.recent_resolved_many(unopened, now_ms - longest * MINUTE_MS)
        return signals, values, recent

    async def cycle(self, fence: Fence, now_ms: float) -> None:
        """One evaluation at the store's time ``now_ms``, every write fenced."""
        started = self._clock.now_ms()
        store = self.incidents
        rules = {r.id: r for r in await self._enabled_rules()}
        open_by_series: dict[tuple[str, str], Incident] = {}
        for incident in await store.open_incidents():
            rule = rules.get(incident.rule_id) or await self._rules.get_rule(
                incident.application_id, incident.rule_id
            )
            change = rule_gone(incident, rule, now_ms, self._new_id)
            if change is not None:
                await store.apply(fence, change)
            else:
                open_by_series[(incident.rule_id, incident.series_key)] = incident
        by_application: dict[str, list[AlertRule]] = {}
        for rule in rules.values():
            by_application.setdefault(rule.application_id, []).append(rule)
        order = self._order(list(by_application))
        reached: str | None = None  # the last application this cycle evaluated
        cut = False
        for start in range(0, len(order), APPLICATION_PAGE):
            page = order[start : start + APPLICATION_PAGE]
            signals, values, recent = await self._read_page(
                page, by_application, open_by_series, now_ms
            )
            for application_id in page:
                # The time box: no application started past it, but always one a cycle.
                if reached is not None and self._out_of_time(started):
                    cut = True
                    break
                organization_id = signals[application_id].organization_id
                if organization_id is not None:
                    for rule in by_application[application_id]:
                        for series_key, value in sorted(values[rule.id].items()):
                            current = open_by_series.get((rule.id, series_key))
                            change = decide(
                                Series(rule, organization_id, series_key, value), current,
                                recent.get((rule.id, series_key)) if current is None else None,
                                now_ms, self._new_id,
                            )  # fmt: skip
                            if change is not None:
                                await store.apply(fence, change)
                reached = application_id
                # The event loop back between two applications (section 11.7).
                await self._clock.sleep_ms(0.0)
            if cut:
                break
        # The fair resume: a cut cycle's successor starts after the last application it reached.
        self._resume_after = reached if cut else None
        if cut:
            self.cut_short += 1
            LOG.warning("alert evaluator: a cycle reached its time box; the next one resumes")
        # Quiet hours over: one summary for each incident with held notifications.
        for incident, held in (await self._held()).values():
            rule = rules.get(incident.rule_id) or await self._rules.get_rule(
                incident.application_id, incident.rule_id
            )
            change = quiet_hours_over(incident, rule, held, now_ms, self._new_id)
            if change is not None:
                await store.apply(fence, change)

    async def _held(self) -> Mapping[str, tuple[Incident, int]]:
        found: dict[str, tuple[Incident, int]] = {}
        for notification in await self.incidents.held_notifications():
            known = found.get(notification.incident_id)
            if known is not None:
                found[notification.incident_id] = (known[0], known[1] + 1)
                continue
            incident = await self.incidents.get_incident(
                notification.application_id, notification.incident_id
            )
            if incident is not None:
                found[incident.id] = (incident, 1)
        return found
