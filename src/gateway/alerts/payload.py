"""What a notification carries: ``json-v1``, built from an allowlist (docs/46, the main plan's
section 10, the A4 plan's section 6).

``AlertPayload`` is a frozen dataclass with exactly the fields of ``json-v1``, each checked when
it is made (ids in their own forms, names in their charsets, kinds and units from fixed sets),
like ``AuditEvent``. It is built when a notification is queued and stored as built; a delivery
sends exactly what was stored. Its body is ``json.dumps(..., ensure_ascii=True)`` of a dict made
here, never string formatting, and at most ``MAX_PAYLOAD_BYTES``: one over is refused
(``PayloadTooLargeError``), never cut short.

**Never in it:** prompt or response text, token ids or hashed tokens, API keys or their HMACs,
provider keys, webhook URLs or their origins, signing keys, provider base URLs, usernames,
email addresses, IP addresses, session ids, the user who acknowledged, or dollars. Names are
the customer's own labels (the application's and the rule's).
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

MAX_PAYLOAD_BYTES = 8 * 1024
KINDS = frozenset(
    {
        "firing", "firing_again", "reminder", "resolved", "quiet_hours_summary", "rule_weakened",
        "channel_removed", "suppressed_summary", "test", "channel_delivery_failing",
    }
)  # fmt: skip
UNITS = frozenset({"%", "ms", "count", "days", "min"})
SEVERITIES = frozenset({"info", "warning", "critical"})
COMPARISONS = frozenset({">", ">=", "<", "<="})
INCIDENT_STATES = frozenset({"pending", "firing", "acknowledged", "resolved"})
RESOLVE_REASONS = frozenset(
    {"cleared", "pending_cleared", "no_data", "rule_disabled", "rule_deleted", "rule_changed",
     "channel_delivered", "channel_deleted"}
)  # fmt: skip
# A rule's fields (gateway.alertrules.spec_body), which a rule_weakened notice names.
RULE_FIELDS = frozenset(
    {
        "name", "signal", "parameter", "comparison", "threshold", "window", "for_minutes",
        "severity", "channels", "cooldown_minutes", "repeat_every_minutes", "quiet_hours",
        "enabled",
    }
)  # fmt: skip

_NOTIFICATION_ID = re.compile(r"ntf_[0-9a-f]{16}")
_ORGANIZATION_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
_RULE_ID = re.compile(r"rul_(cdf_)?[0-9a-f]{16}")
_INCIDENT_ID = re.compile(r"inc_[0-9a-f]{16}")
_CHANNEL_ID = re.compile(r"chn_[0-9a-f]{16}")
_RULE_NAME = re.compile(r"[A-Za-z0-9 ._-]{1,64}")
_SIGNAL = re.compile(r"[a-z_]{1,40}")
# A series: a provider id or an API key's id (or a channel id, for the built-in).
_SERIES = re.compile(r"(prv_|chn_)?[0-9a-f]{16}")


class PayloadInvalidError(ValueError):
    """A field outside the allowlist's forms: raised before anything is stored."""


class PayloadTooLargeError(ValueError):
    """A body over ``MAX_PAYLOAD_BYTES``: recorded as ``payload_too_large``, never truncated."""


def _check(pattern: re.Pattern[str], value: object, what: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise PayloadInvalidError(f"{what} is not in its form")
    return value


def _one_of(allowed: frozenset[str], value: object, what: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise PayloadInvalidError(f"{what} is not one of its fixed values")
    return value


def _application_name(value: object) -> str:
    # Application names are NFC, at most 100, no control or zero-width characters (note 26).
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 100
        or unicodedata.normalize("NFC", value) != value
        or any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in value)
    ):
        raise PayloadInvalidError("the application's name is not a name")
    return value


def _number(value: object, what: str, *, optional: bool = False) -> float | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or value != value:
        raise PayloadInvalidError(f"{what} is not a number")
    return float(value)


def iso(at_ms: float | None) -> str | None:
    """Epoch milliseconds as ``2026-11-02T10:00:00Z`` (whole seconds, UTC)."""
    if at_ms is None:
        return None
    return datetime.fromtimestamp(at_ms // 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True, slots=True)
class PayloadApplication:
    id: str
    name: str

    def __post_init__(self) -> None:
        _check(_ORGANIZATION_ID, self.id, "the application's id")
        _application_name(self.name)


@dataclass(frozen=True, slots=True)
class PayloadRule:
    id: str
    name: str
    signal: str
    comparison: str
    threshold: float
    unit: str
    window_s: int | None
    for_s: int
    severity: str

    def __post_init__(self) -> None:
        _check(_RULE_ID, self.id, "the rule's id")
        _check(_RULE_NAME, self.name, "the rule's name")
        _check(_SIGNAL, self.signal, "the signal")
        _one_of(COMPARISONS, self.comparison, "the comparison")
        _number(self.threshold, "the threshold")
        _one_of(UNITS, self.unit, "the unit")
        for value in (self.window_s, self.for_s):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise PayloadInvalidError("a duration is whole seconds")
        _one_of(SEVERITIES, self.severity, "the severity")


@dataclass(frozen=True, slots=True)
class PayloadIncident:
    id: str
    state: str
    series: str | None
    value: float | None
    fired_at: str | None
    resolved_at: str | None
    resolve_reason: str | None

    def __post_init__(self) -> None:
        _check(_INCIDENT_ID, self.id, "the incident's id")
        _one_of(INCIDENT_STATES, self.state, "the incident's state")
        if self.series is not None:
            _check(_SERIES, self.series, "the series")
        _number(self.value, "the value", optional=True)
        if self.resolve_reason is not None:
            _one_of(RESOLVE_REASONS, self.resolve_reason, "the resolve reason")


@dataclass(frozen=True, slots=True)
class PayloadDetails:
    """What some kinds carry beyond the incident, each field fixed in form: the rule fields a
    ``rule_weakened`` notice names (and whether the rule was deleted), the channel id a
    ``channel_removed`` notice is about, the counts of a summary and its hour."""

    fields: tuple[str, ...] | None = None
    deleted: bool | None = None
    channel: str | None = None
    notifications: int | None = None
    rules: int | None = None
    held: int | None = None
    period_start: str | None = None
    period_end: str | None = None

    def __post_init__(self) -> None:
        if self.fields is not None and not all(f in RULE_FIELDS for f in self.fields):
            raise PayloadInvalidError("a weakened field is not a rule field")
        if self.channel is not None:
            _check(_CHANNEL_ID, self.channel, "the channel's id")
        for count in (self.notifications, self.rules, self.held):
            if count is not None and (isinstance(count, bool) or not isinstance(count, int)):
                raise PayloadInvalidError("a count is a whole number")


@dataclass(frozen=True, slots=True)
class AlertPayload:
    delivery_id: str
    kind: str
    sent_at_ms: float
    organization_id: str
    application: PayloadApplication | None = None
    rule: PayloadRule | None = None
    incident: PayloadIncident | None = None
    console_path: str | None = None
    details: PayloadDetails | None = None
    version: int = field(default=1)

    def __post_init__(self) -> None:
        _check(_NOTIFICATION_ID, self.delivery_id, "the delivery id")
        _one_of(KINDS, self.kind, "the kind")
        _check(_ORGANIZATION_ID, self.organization_id, "the organization's id")
        if self.version != 1:
            raise PayloadInvalidError("json-v1 only")
        if self.console_path is not None and (
            not re.fullmatch(
                r"/v1/admin/orgs/[A-Za-z0-9._-]{1,64}(/[A-Za-z0-9._-]{1,64}){1,4}",
                self.console_path,
            )
            or any(part in (".", "..") for part in self.console_path.split("/"))
        ):
            raise PayloadInvalidError("the console path is not a path of ids")

    def as_dict(self) -> dict[str, Any]:
        a, r, i, d = self.application, self.rule, self.incident, self.details
        return {
            "version": self.version,
            "delivery_id": self.delivery_id,
            "kind": self.kind,
            "sent_at": iso(self.sent_at_ms),
            "organization": {"id": self.organization_id},
            "application": None if a is None else {"id": a.id, "name": a.name},
            "rule": None
            if r is None
            else {
                "id": r.id, "name": r.name, "signal": r.signal, "comparison": r.comparison,
                "threshold": r.threshold, "unit": r.unit, "window_s": r.window_s,
                "for_s": r.for_s, "severity": r.severity,
            },
            "incident": None
            if i is None
            else {
                "id": i.id, "state": i.state, "series": i.series, "value": i.value,
                "fired_at": i.fired_at, "resolved_at": i.resolved_at,
                "resolve_reason": i.resolve_reason,
            },
            "console_path": self.console_path,
            "details": None
            if d is None
            else {
                "fields": None if d.fields is None else list(d.fields), "deleted": d.deleted,
                "channel": d.channel, "notifications": d.notifications, "rules": d.rules,
                "held": d.held, "period_start": d.period_start, "period_end": d.period_end,
            },
        }  # fmt: skip

    def body(self) -> str:
        """The stored body: JSON, ASCII only, at most ``MAX_PAYLOAD_BYTES``."""
        text = json.dumps(self.as_dict(), ensure_ascii=True, separators=(",", ":"))
        if len(text.encode("ascii")) > MAX_PAYLOAD_BYTES:
            raise PayloadTooLargeError(
                f"a notification's body is at most {MAX_PAYLOAD_BYTES} bytes"
            )
        return text
