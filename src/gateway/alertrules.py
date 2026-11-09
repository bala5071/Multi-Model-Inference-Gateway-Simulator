"""Alert rules: what they are, the default ones, where they are kept, and how they change
(docs/44, the plan's section 7).

A rule belongs to one application and names one signal of ``gateway.signals``, a comparison,
a threshold in the signal's unit, a window, how long the condition must hold, a severity, its
channels, a cooldown, an optional reminder and quiet hours, and whether it is enabled. Nothing
evaluates rules yet (A3) and nothing is sent (A4).

**Channels** (A4, docs/46): ``[]``, ``["organization defaults"]``, or up to ``MAX_RULE_CHANNELS``
channel ids. The ids are checked at write to be channels **of the rule's own organization**: an
id of another organization's channel is refused exactly as one that does not exist (M15).

**A write that makes an enabled rule fire less** (``gateway.alerts.weakening``) queues a
``rule_weakened`` notice to the rule's channels as they were before it, in its transaction.

**Default rules are real rows,** created with each application and, for the applications that
already exist, by migration 15's data step (Postgres schema 4). Their ids are derived from the
application and the default's key, so the data step, a new application and
``restore-defaults`` agree on them.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Protocol

from gateway.alertrecorder import EDGES, MINUTE_MS, snap
from gateway.alertrollups import AlertRollups
from gateway.audit import AuditAction, AuditEvent, AuditLog, Outcome, Reason, TargetType
from gateway.clock import EpochSource
from gateway.database import write_transaction
from gateway.management import ManagementRefusal, Stores, resolve_application
from gateway.permissions import Resource
from gateway.signals import FROM_ROLLUPS, SIGNALS, evaluate

if TYPE_CHECKING:
    from gateway.alerts.notices import Notices

RULE_ID_PREFIX = "rul_"
RULE_ID = re.compile(r"rul_[0-9a-f]{16}")
MAX_RULES = 50
ORGANIZATION_DEFAULTS = "organization defaults"
MAX_RULE_CHANNELS = 5
CHANNEL_ID = re.compile(r"chn_[0-9a-f]{16}")
# One message for a channel id that does not exist and one of another organization (M15).
CHANNELS_MESSAGE = (
    f'must be [], ["{ORGANIZATION_DEFAULTS}"], or 1 to {MAX_RULE_CHANNELS} different channel ids '
    "of this organization"
)
COMPARISONS = (">", ">=", "<", "<=")
SEVERITIES = ("info", "warning", "critical")
NAME = re.compile(r"[A-Za-z0-9 ._-]{1,64}")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
OFFSET = re.compile(r"[+-](0\d|1[0-4]):[0-5]\d")
CLOCK = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
# A minute is complete 25 s after it ends (the plan's section 11.2): not judged before.
COMPLETE_AFTER_MS = 25_000.0
PREVIEW_MINUTES = 60

_UNIT_RANGE: Mapping[str, tuple[float, float]] = {
    "%": (0.0, 100.0),
    "ms": (0.0, EDGES[-1]),
    "count": (0.0, 1e9),
    "days": (0.0, 3650.0),
    "min": (0.0, 1440.0),
}


class RuleInvalidError(ValueError):
    """A rule body that is not valid: names the field."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field


@dataclass(frozen=True, slots=True)
class QuietHours:
    utc_offset: str
    start: str
    end: str
    days: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RuleSpec:
    """What a rule says, checked."""

    name: str
    signal: str
    parameter: str | None
    comparison: str
    threshold: float
    window: int | None
    for_minutes: int
    severity: str
    channels: tuple[str, ...]
    cooldown_minutes: int = 30
    repeat_every_minutes: int | None = None
    quiet_hours: QuietHours | None = None
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class AlertRule:
    id: str
    application_id: str
    spec: RuleSpec
    version: int
    default_key: str | None
    created_at_ms: float
    updated_at_ms: float


def _int(body: Mapping[str, Any], field: str, low: int, high: int, default: int | None) -> Any:
    value = body.get(field, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise RuleInvalidError(field, f"must be a whole number from {low} to {high}")
    return value


def validate_rule(body: Mapping[str, Any]) -> RuleSpec:
    """A rule body checked field by field (the plan's section 7); a latency threshold snapped
    to the nearest histogram edge. ``RuleInvalidError`` names the first field wrong."""
    known = {
        "name", "signal", "parameter", "comparison", "threshold", "window", "for_minutes",
        "severity", "channels", "cooldown_minutes", "repeat_every_minutes", "quiet_hours",
        "enabled",
    }  # fmt: skip
    for field in body:
        if field not in known:
            raise RuleInvalidError(field, "is not a rule field")
    name = body.get("name")
    if not isinstance(name, str) or not NAME.fullmatch(name) or name != name.strip():
        raise RuleInvalidError("name", "1 to 64 of A-Z a-z 0-9 space . _ -, no space at an end")
    named = body.get("signal")
    signal = SIGNALS.get(named) if isinstance(named, str) else None
    if signal is None:
        raise RuleInvalidError("signal", "must be one of the signals")
    parameter = body.get("parameter")
    if signal.parameters:
        if parameter not in signal.parameters:
            raise RuleInvalidError("parameter", f"must be one of {', '.join(signal.parameters)}")
    elif parameter is not None:
        raise RuleInvalidError("parameter", f"{signal.name} takes none")
    comparison = body.get("comparison")
    if comparison not in COMPARISONS:
        raise RuleInvalidError("comparison", "must be >, >=, < or <=")
    threshold = body.get("threshold")
    low, high = _UNIT_RANGE[signal.unit]
    if isinstance(threshold, bool) or not isinstance(threshold, int | float):
        raise RuleInvalidError("threshold", "must be a number")
    if not low <= float(threshold) <= high:
        raise RuleInvalidError("threshold", f"must be from {low:g} to {high:g} {signal.unit}")
    value = snap(float(threshold)) if signal.unit == "ms" else float(threshold)
    window = body.get("window")
    if signal.windows:
        if window not in signal.windows:
            choices = ", ".join(str(w) for w in signal.windows)
            raise RuleInvalidError("window", f"must be one of {choices} minutes")
    elif window is not None:
        raise RuleInvalidError("window", f"{signal.name} has no window")
    for_minutes = _int(body, "for_minutes", 0, 60, 0)
    severity = body.get("severity")
    if severity not in SEVERITIES:
        raise RuleInvalidError("severity", "must be info, warning or critical")
    channels = body.get("channels", [ORGANIZATION_DEFAULTS])
    if not isinstance(channels, list) or not (
        channels in ([], [ORGANIZATION_DEFAULTS])
        or (
            1 <= len(channels) <= MAX_RULE_CHANNELS
            and len(set(channels)) == len(channels)
            and all(isinstance(c, str) and CHANNEL_ID.fullmatch(c) for c in channels)
        )
    ):
        raise RuleInvalidError("channels", CHANNELS_MESSAGE)
    cooldown = _int(body, "cooldown_minutes", 5, 1440, 30)
    repeat = _int(body, "repeat_every_minutes", 60, 1440, None)
    quiet = _quiet_hours(body.get("quiet_hours"))
    enabled = body.get("enabled", True)
    if not isinstance(enabled, bool):
        raise RuleInvalidError("enabled", "must be true or false")
    return RuleSpec(
        name, signal.name, parameter, comparison, value, window, for_minutes, severity,
        tuple(channels), cooldown, repeat, quiet, enabled,
    )  # fmt: skip


def _quiet_hours(value: Any) -> QuietHours | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"utc_offset", "start", "end", "days"}:
        raise RuleInvalidError("quiet_hours", "needs utc_offset, start, end and days")
    offset, start, end, days = value["utc_offset"], value["start"], value["end"], value["days"]
    if not isinstance(offset, str) or not OFFSET.fullmatch(offset):
        raise RuleInvalidError("quiet_hours", "utc_offset is -12:00 to +14:00")
    hours, minutes = int(offset[1:3]), int(offset[4:6])
    if (offset[0] == "-" and (hours, minutes) > (12, 0)) or (hours, minutes) > (14, 0):
        raise RuleInvalidError("quiet_hours", "utc_offset is -12:00 to +14:00")
    for clock in (start, end):
        if not isinstance(clock, str) or not CLOCK.fullmatch(clock):
            raise RuleInvalidError("quiet_hours", "start and end are HH:MM")
    if (
        not isinstance(days, list)
        or not days
        or len(set(days)) != len(days)
        or not set(days) <= set(DAYS)
    ):
        raise RuleInvalidError("quiet_hours", f"days is a set of {', '.join(DAYS)}")
    return QuietHours(offset, start, end, tuple(d for d in DAYS if d in days))


def spec_body(spec: RuleSpec) -> dict[str, Any]:
    """A rule as the endpoints show it (and as a body that validates to the same rule)."""
    quiet = spec.quiet_hours
    return {
        "name": spec.name,
        "signal": spec.signal,
        "parameter": spec.parameter,
        "comparison": spec.comparison,
        "threshold": spec.threshold,
        "window": spec.window,
        "for_minutes": spec.for_minutes,
        "severity": spec.severity,
        "channels": list(spec.channels),
        "cooldown_minutes": spec.cooldown_minutes,
        "repeat_every_minutes": spec.repeat_every_minutes,
        "quiet_hours": None
        if quiet is None
        else {
            "utc_offset": quiet.utc_offset,
            "start": quiet.start,
            "end": quiet.end,
            "days": list(quiet.days),
        },
        "enabled": spec.enabled,
    }


def _default(key: str, **body: Any) -> tuple[str, RuleSpec]:
    return key, validate_rule({"channels": [ORGANIZATION_DEFAULTS], **body})


# The default rules of the plan's section 7, every number a chosen default, not a measurement.
# Not here: latency_total_p99 (no threshold can be chosen for the customer, so it is not a row;
# a rule the customer writes) and channel_delivery_failing (A4, with channels).
DEFAULT_RULES: Mapping[str, RuleSpec] = dict(
    (
        _default("budget_used", name="Budget 80 percent used", signal="budget_used",
                 comparison=">=", threshold=80, severity="warning"),
        _default("budget_exhausted", name="Requests refused for budget", signal="rejections",
                 parameter="budget_exhausted", comparison=">", threshold=0, window=5,
                 severity="critical"),
        _default("error_rate", name="Error rate above 5 percent", signal="error_rate",
                 comparison=">", threshold=5, window=5, for_minutes=5, severity="critical"),
        _default("circuit_open", name="Provider skipped", signal="circuit_open", comparison=">=",
                 threshold=2, severity="warning"),
        _default("key_expires", name="Key expires within 14 days", signal="key_expires_in",
                 comparison="<=", threshold=14, severity="warning"),
        _default("hosted_share", name="Answered by a hosted provider", signal="hosted_share",
                 comparison=">", threshold=0, window=60, severity="info", enabled=False),
        _default("rate_limited", name="Requests rate limited", signal="rejections",
                 parameter="rate_limited", comparison=">", threshold=0, window=5,
                 severity="warning", enabled=False),
        _default("reservation_expired", name="Budget reservations expired",
                 signal="reservation_expired", comparison=">", threshold=0, window=60,
                 severity="warning", enabled=False),
    )
)  # fmt: skip


def default_rule_id(application_id: str, key: str) -> str:
    """The same id from the data step, a new application and restore-defaults."""
    digest = hashlib.sha256(f"{application_id}:{key}".encode()).hexdigest()
    return RULE_ID_PREFIX + digest[:16]


def default_rules(application_id: str, at_ms: float) -> list[AlertRule]:
    return [
        AlertRule(default_rule_id(application_id, key), application_id, spec, 1, key, at_ms, at_ms)
        for key, spec in DEFAULT_RULES.items()
    ]


# --- the stores ---------------------------------------------------------------------------------


class RuleVersionConflictError(Exception):
    def __init__(self, current: int | None) -> None:
        super().__init__(f"the rule is at version {current}")
        self.current = current


class RuleStore(Protocol):
    def write_transaction(self) -> contextlib.AbstractAsyncContextManager[None]: ...

    async def list_rules(
        self, application_id: str, limit: int, after: str | None = None
    ) -> list[AlertRule]:
        """A page of the application's rules, by id, after ``after``."""
        ...

    async def get_rule(self, application_id: str, rule_id: str) -> AlertRule | None: ...

    async def list_enabled_rules(self, limit: int, after: str | None = None) -> list[AlertRule]:
        """A page of every application's enabled rules, by id, after ``after``: the
        evaluator's read (docs/45)."""
        ...

    async def count_rules(self, application_id: str) -> int: ...

    async def insert_rule(self, rule: AlertRule) -> None:
        """Add a rule whose id is new; a rule of that id already there is replaced only by
        ``put_rule``."""
        ...

    async def put_rule(self, rule: AlertRule, expected_version: int | None) -> None:
        """Write ``rule`` over the one of its id at ``expected_version`` (None: whatever is
        there, or none); ``RuleVersionConflictError`` otherwise, nothing written."""
        ...

    async def delete_rule(
        self, application_id: str, rule_id: str, expected_version: int
    ) -> None: ...


COLUMNS = (
    "id, application_id, name, signal, parameter, comparison, threshold, window_minutes,"
    " for_minutes, severity, channels, cooldown_minutes, repeat_every_minutes, quiet_hours,"
    " enabled, version, default_key, created_at_ms, updated_at_ms"
)


def rule_row(rule: AlertRule) -> tuple[Any, ...]:
    s = rule.spec
    quiet = (
        None
        if s.quiet_hours is None
        else json.dumps(spec_body(s)["quiet_hours"], sort_keys=True, separators=(",", ":"))
    )
    return (
        rule.id, rule.application_id, s.name, s.signal, s.parameter, s.comparison, s.threshold,
        s.window, s.for_minutes, s.severity, json.dumps(list(s.channels)), s.cooldown_minutes,
        s.repeat_every_minutes, quiet, int(s.enabled), rule.version, rule.default_key,
        rule.created_at_ms, rule.updated_at_ms,
    )  # fmt: skip


def rule_from_row(row: Sequence[Any]) -> AlertRule:
    quiet = json.loads(row[13]) if row[13] is not None else None
    spec = RuleSpec(
        row[2], row[3], row[4], row[5], float(row[6]), row[7], int(row[8]), row[9],
        tuple(json.loads(row[10])), int(row[11]), row[12],
        QuietHours(quiet["utc_offset"], quiet["start"], quiet["end"], tuple(quiet["days"]))
        if quiet is not None
        else None,
        bool(row[14]),
    )  # fmt: skip
    return AlertRule(row[0], row[1], spec, int(row[15]), row[16], float(row[17]), float(row[18]))


class InMemoryRuleStore:
    def __init__(self) -> None:
        self._rules: dict[str, AlertRule] = {}

    @contextlib.asynccontextmanager
    async def write_transaction(self) -> AsyncIterator[None]:
        yield

    async def list_rules(
        self, application_id: str, limit: int, after: str | None = None
    ) -> list[AlertRule]:
        found = sorted(
            (r for r in self._rules.values() if r.application_id == application_id),
            key=lambda r: r.id,
        )
        return [r for r in found if after is None or r.id > after][:limit]

    async def get_rule(self, application_id: str, rule_id: str) -> AlertRule | None:
        rule = self._rules.get(rule_id)
        return rule if rule is not None and rule.application_id == application_id else None

    async def list_enabled_rules(self, limit: int, after: str | None = None) -> list[AlertRule]:
        found = sorted((r for r in self._rules.values() if r.spec.enabled), key=lambda r: r.id)
        return [r for r in found if after is None or r.id > after][:limit]

    async def count_rules(self, application_id: str) -> int:
        return sum(1 for r in self._rules.values() if r.application_id == application_id)

    async def insert_rule(self, rule: AlertRule) -> None:
        if rule.id in self._rules:
            raise RuleVersionConflictError(self._rules[rule.id].version)
        self._rules[rule.id] = rule

    async def put_rule(self, rule: AlertRule, expected_version: int | None) -> None:
        found = self._rules.get(rule.id)
        if expected_version is not None and (found is None or found.version != expected_version):
            raise RuleVersionConflictError(found.version if found is not None else None)
        self._rules[rule.id] = rule

    async def delete_rule(self, application_id: str, rule_id: str, expected_version: int) -> None:
        found = await self.get_rule(application_id, rule_id)
        if found is None or found.version != expected_version:
            raise RuleVersionConflictError(found.version if found is not None else None)
        del self._rules[rule_id]


class SqliteRuleStore:
    """Over migration 15's ``alert_rules``. Coroutines that never suspend (docs/34)."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection

    def write_transaction(self) -> contextlib.AbstractAsyncContextManager[None]:
        return write_transaction(self._db)

    async def list_rules(
        self, application_id: str, limit: int, after: str | None = None
    ) -> list[AlertRule]:
        rows = self._db.execute(
            f"SELECT {COLUMNS} FROM alert_rules WHERE application_id = ? AND id > ?"
            " ORDER BY id LIMIT ?",
            (application_id, after or "", limit),
        ).fetchall()
        return [rule_from_row(r) for r in rows]

    async def get_rule(self, application_id: str, rule_id: str) -> AlertRule | None:
        row = self._db.execute(
            f"SELECT {COLUMNS} FROM alert_rules WHERE application_id = ? AND id = ?",
            (application_id, rule_id),
        ).fetchone()
        return rule_from_row(row) if row is not None else None

    async def list_enabled_rules(self, limit: int, after: str | None = None) -> list[AlertRule]:
        rows = self._db.execute(
            f"SELECT {COLUMNS} FROM alert_rules WHERE enabled = 1 AND id > ? ORDER BY id LIMIT ?",
            (after or "", limit),
        ).fetchall()
        return [rule_from_row(r) for r in rows]

    async def count_rules(self, application_id: str) -> int:
        row = self._db.execute(
            "SELECT count(*) FROM alert_rules WHERE application_id = ?", (application_id,)
        ).fetchone()
        return int(row[0])

    async def insert_rule(self, rule: AlertRule) -> None:
        try:
            self._db.execute(
                f"INSERT INTO alert_rules ({COLUMNS}) VALUES ({', '.join('?' * 19)})",
                rule_row(rule),
            )
        except sqlite3.IntegrityError:
            raise RuleVersionConflictError(None) from None

    async def put_rule(self, rule: AlertRule, expected_version: int | None) -> None:
        if expected_version is None:
            self._db.execute(
                f"INSERT OR REPLACE INTO alert_rules ({COLUMNS}) VALUES ({', '.join('?' * 19)})",
                rule_row(rule),
            )
            return
        row = rule_row(rule)
        done = self._db.execute(
            "UPDATE alert_rules SET name = ?, signal = ?, parameter = ?, comparison = ?,"
            " threshold = ?, window_minutes = ?, for_minutes = ?, severity = ?, channels = ?,"
            " cooldown_minutes = ?, repeat_every_minutes = ?, quiet_hours = ?, enabled = ?,"
            " version = ?, default_key = ?, updated_at_ms = ?"
            " WHERE id = ? AND application_id = ? AND version = ?",
            (*row[2:17], row[18], rule.id, rule.application_id, expected_version),
        )
        if done.rowcount != 1:
            raise RuleVersionConflictError(None)

    async def delete_rule(self, application_id: str, rule_id: str, expected_version: int) -> None:
        done = self._db.execute(
            "DELETE FROM alert_rules WHERE id = ? AND application_id = ? AND version = ?",
            (rule_id, application_id, expected_version),
        )
        if done.rowcount != 1:
            raise RuleVersionConflictError(None)


# --- the service --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PreviewMinute:
    minute: int
    value: float | None
    why_not: str | None
    met: bool


def _met(comparison: str, value: float, threshold: float) -> bool:
    return {
        ">": value > threshold,
        ">=": value >= threshold,
        "<": value < threshold,
        "<=": value <= threshold,
    }[comparison]


class RuleService:
    """Rules of applications, below HTTP: each change a version, checked, audited by rule id
    and the names of the fields changed (never a threshold's history: the row has it)."""

    def __init__(
        self,
        store: RuleStore,
        audit: AuditLog,
        epoch: EpochSource,
        rollups: AlertRollups | None,
        random_bytes: Callable[[int], bytes],
        *,
        notices: Notices | None = None,
    ) -> None:
        """``notices`` (docs/46) checks a rule's channel ids against the organization's channels
        and queues the ``rule_weakened`` notice; without it (the CLI, which only creates default
        rules) a rule may name no channel id, and no rule has channels to tell."""
        self.store = store
        self._audit = audit
        self._epoch = epoch
        self._rollups = rollups
        self._random_bytes = random_bytes
        self._notices = notices

    async def check_channels(self, application_id: str, spec: RuleSpec) -> None:
        """A rule's channel ids are channels of its application's organization, or
        ``RuleInvalidError`` with one message whether an id is missing or another
        organization's (M15). In the caller's transaction when it writes."""
        ids = [c for c in spec.channels if c != ORGANIZATION_DEFAULTS]
        if not ids:
            return
        notices = self._notices
        if notices is None:
            raise RuleInvalidError("channels", CHANNELS_MESSAGE)
        organization_id = await notices.organization_of(application_id)
        for channel_id in ids:
            found = await notices.channels.get_channel(channel_id)
            if found is None or found.organization_id != organization_id:
                raise RuleInvalidError("channels", CHANNELS_MESSAGE)

    async def _weakened(
        self, application_id: str, before: AlertRule, after: RuleSpec | None
    ) -> None:
        """The ``rule_weakened`` notice, if ``after`` (None: deleted) makes the enabled rule
        ``before`` fire less: to its channels as they were, in the caller's transaction."""
        from gateway.alerts.weakening import weakened_fields

        fields = weakened_fields(before.spec, after)
        if fields and self._notices is not None:
            await self._notices.rule_weakened(application_id, before, fields, deleted=after is None)

    async def _record(
        self,
        action: AuditAction,
        rule_id: str,
        application_id: str,
        actor_user_id: str | None,
        changed: Sequence[str] = (),
    ) -> None:
        details: dict[str, object] = {"application_id": application_id}
        if changed:
            details["rule_fields"] = list(changed)
        await self._audit.write(
            AuditEvent(
                action, Outcome.SUCCESS, TargetType.ALERT_RULE, rule_id, actor_user_id, details
            ),
            self._epoch.epoch_ms(),
        )

    async def create_defaults(self, application_id: str) -> None:
        """The default rules of a new application, in the caller's transaction."""
        for rule in default_rules(application_id, self._epoch.epoch_ms()):
            await self.store.insert_rule(rule)

    async def create(
        self, application_id: str, body: Mapping[str, Any], actor_user_id: str | None
    ) -> AlertRule:
        spec = validate_rule(body)
        async with self.store.write_transaction():
            await self.check_channels(application_id, spec)
            if await self.store.count_rules(application_id) >= MAX_RULES:
                raise ManagementRefusal(
                    Reason.RULE_LIMIT, f"an application has at most {MAX_RULES} rules"
                )
            now = self._epoch.epoch_ms()
            rule = AlertRule(
                RULE_ID_PREFIX + self._random_bytes(8).hex(), application_id, spec, 1, None,
                now, now,
            )  # fmt: skip
            await self.store.insert_rule(rule)
            await self._record(AuditAction.ALERT_RULE_CREATED, rule.id, application_id,
                               actor_user_id)  # fmt: skip
        return rule

    async def _change(
        self,
        application_id: str,
        rule_id: str,
        expected_version: int,
        spec: RuleSpec,
        action: AuditAction,
        actor_user_id: str | None,
    ) -> AlertRule:
        async with self.store.write_transaction():
            found = await self.store.get_rule(application_id, rule_id)
            if found is None:
                raise KeyError(rule_id)  # the resolver found it: deleted meanwhile
            if found.version != expected_version:
                raise _conflict(found.version, expected_version)
            before, after = spec_body(found.spec), spec_body(spec)
            changed = [field for field in after if after[field] != before[field]]
            if not changed:
                return found
            if "channels" in changed:
                await self.check_channels(application_id, spec)
            await self._weakened(application_id, found, spec)
            rule = replace(found, spec=spec, version=found.version + 1,
                           updated_at_ms=self._epoch.epoch_ms())  # fmt: skip
            try:
                await self.store.put_rule(rule, expected_version)
            except RuleVersionConflictError as exc:
                raise _conflict(exc.current, expected_version) from None
            await self._record(action, rule_id, application_id, actor_user_id, changed)
        return rule

    async def update(
        self,
        application_id: str,
        rule_id: str,
        expected_version: int,
        body: Mapping[str, Any],
        actor_user_id: str | None,
    ) -> AlertRule:
        return await self._change(
            application_id, rule_id, expected_version, validate_rule(body),
            AuditAction.ALERT_RULE_CHANGED, actor_user_id,
        )  # fmt: skip

    async def set_enabled(
        self,
        application_id: str,
        rule_id: str,
        expected_version: int,
        enabled: bool,
        actor_user_id: str | None,
    ) -> AlertRule:
        found = await self.store.get_rule(application_id, rule_id)
        if found is None:
            raise KeyError(rule_id)
        action = AuditAction.ALERT_RULE_ENABLED if enabled else AuditAction.ALERT_RULE_DISABLED
        return await self._change(
            application_id, rule_id, expected_version, replace(found.spec, enabled=enabled),
            action, actor_user_id,
        )  # fmt: skip

    async def delete(
        self, application_id: str, rule_id: str, expected_version: int, actor_user_id: str | None
    ) -> None:
        async with self.store.write_transaction():
            before = await self.store.get_rule(application_id, rule_id)
            try:
                await self.store.delete_rule(application_id, rule_id, expected_version)
            except RuleVersionConflictError:
                found = await self.store.get_rule(application_id, rule_id)
                raise _conflict(found.version if found else None, expected_version) from None
            assert before is not None  # deleted at its version: it was there
            await self._weakened(application_id, before, None)
            await self._record(AuditAction.ALERT_RULE_DELETED, rule_id, application_id,
                               actor_user_id)  # fmt: skip

    async def restore_defaults(self, application_id: str, actor_user_id: str | None) -> int:
        """Every default rule back as it ships: a deleted one recreated, an edited one reset (a
        new version), as one audited change. Returns how many rules it wrote."""
        written = 0
        async with self.store.write_transaction():
            now = self._epoch.epoch_ms()
            for rule in default_rules(application_id, now):
                found = await self.store.get_rule(application_id, rule.id)
                if found is not None and found.spec == rule.spec:
                    continue
                if found is not None:
                    rule = replace(rule, version=found.version + 1,
                                   created_at_ms=found.created_at_ms)  # fmt: skip
                    # Restoring can make an edited rule fire less: told, as any write is.
                    await self._weakened(application_id, found, rule.spec)
                await self.store.put_rule(rule, None)
                written += 1
            await self._audit.write(
                AuditEvent(AuditAction.ALERT_RULES_RESTORED, Outcome.SUCCESS,
                           TargetType.APPLICATION, application_id, actor_user_id, {}),
                now,
            )  # fmt: skip
        return written

    async def preview(self, application_id: str, spec: RuleSpec) -> list[PreviewMinute] | None:
        """What ``spec`` would have been over the last hour of complete minutes, from the
        rollups: the signal's value (or why there is none) ending at each minute, and whether
        the condition held. None for a signal not computed from rollups."""
        if spec.signal not in FROM_ROLLUPS or self._rollups is None or spec.window is None:
            return None
        last = int((self._epoch.epoch_ms() - COMPLETE_AFTER_MS) // MINUTE_MS) - 1
        first = last - PREVIEW_MINUTES + 1
        read = await self._rollups.read(application_id, range(first - spec.window + 1, last + 1))
        minutes = []
        for end in range(first, last + 1):
            window = [r for r in read if end - spec.window < r.minute <= end]
            found = evaluate(spec.signal, application_id, window, window=spec.window,
                             parameter=spec.parameter)  # fmt: skip
            met = found.value is not None and _met(spec.comparison, found.value, spec.threshold)
            minutes.append(PreviewMinute(end, found.value, found.why_not, met))
        return minutes


async def resolve_rule(
    stores: Stores, rules: RuleStore, organization_id: str, application_id: str, rule_id: str
) -> Resource | None:
    """The rule's application, if the application belongs to the organization **and** the rule
    to the application; anything else is missing (answered as forbidden)."""
    resource = await resolve_application(stores, organization_id, application_id)
    if resource is None or not RULE_ID.fullmatch(rule_id):
        return None
    return resource if await rules.get_rule(application_id, rule_id) is not None else None


def _conflict(current: int | None, expected: int) -> ManagementRefusal:
    return ManagementRefusal(
        Reason.RULE_VERSION_CONFLICT,
        f"the rule is at version {current}, not {expected}: read it again and make the change "
        "from there",
    )
