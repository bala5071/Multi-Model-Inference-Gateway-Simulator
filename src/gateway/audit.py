"""The audit log: who did what, to what, when, and with what outcome.

See docs/27-users-and-sessions.md and docs/identity-plan.md, section 9.

Every field passes through an allowlist. An event's ``details`` may carry only the keys in
``DETAIL_FIELDS``, each with a fixed type, and string values only from fixed sets or in the
form of an id; anything else raises ``ValueError`` before anything is written. So a password,
a session id, a CSRF token or a hash cannot be stored, even by mistake. A failed login for a
username that does not exist is recorded with no identifier at all: nothing of what was typed
is kept.

Rows for unknown-user failures are capped per minute (of epoch time); the ones over the cap
are not written, and how many were dropped is written to the log itself once that minute is
over. Entries older than the retention are pruned in batches, beside the server. Store calls
are synchronous, for the reason given in ``gateway.usage``.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from gateway.permissions import PERMISSIONS

MINUTE_MS = 60_000.0
DEFAULT_UNKNOWN_USER_ROWS_PER_MINUTE = 60
# User ids and session handles: what an entry may name.
ID_PATTERN = re.compile(r"(usr|ses)_[0-9a-f]{16}")
# Organization ids are chosen by an operator; an entry stores them only in this form.
ORGANIZATION_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}")
# An API key's id: the visible part of the key, never the key (gateway.apikeys).
KEY_ID_PATTERN = re.compile(r"[0-9a-f]{16}")
# A provider's id (docs/32).
PROVIDER_ID_PATTERN = re.compile(r"prv_[0-9a-f]{16}")
SCOPE_TYPES = frozenset({"platform", "organization", "application"})


class AuditAction(StrEnum):
    LOGIN = "login"
    LOGIN_FAILED = "login_failed"
    LOGIN_LOCKED = "login_locked"
    LOGOUT = "logout"
    USER_CREATED = "user_created"
    USER_DISABLED = "user_disabled"
    USER_ENABLED = "user_enabled"
    USER_UNLOCKED = "user_unlocked"
    PASSWORD_CHANGED = "password_changed"
    PASSWORD_CHANGE_FAILED = "password_change_failed"
    PASSWORD_RESET = "password_reset"
    SESSIONS_REVOKED = "sessions_revoked"
    ROWS_DROPPED = "audit_rows_dropped"
    ROLE_GRANTED = "role_granted"
    ROLE_REVOKED = "role_revoked"
    STEP_UP_GRANTED = "step_up_granted"
    STEP_UP_FAILED = "step_up_failed"
    STEP_UP_REQUIRED = "step_up_required"
    APPLICATION_CREATED = "application_created"
    KEY_ISSUED = "key_issued"
    KEY_ROTATED = "key_rotated"
    KEY_REVOKED = "key_revoked"
    # A management request refused for a conflict or a cap (docs/30): its reason and action.
    MANAGEMENT_REFUSED = "management_refused"
    # An organization's model providers and their keys (docs/32): never the key or last4.
    PROVIDER_CREATED = "provider_created"
    PROVIDER_UPDATED = "provider_updated"
    PROVIDER_DISABLED = "provider_disabled"
    PROVIDER_ENABLED = "provider_enabled"
    PROVIDER_DELETED = "provider_deleted"
    PROVIDER_KEY_SET = "provider_key_set"
    PROVIDER_KEY_DELETED = "provider_key_deleted"
    PROVIDER_TESTED = "provider_tested"
    # A fallback mode set on an organization or an application (docs/33).
    MODE_CHANGED = "mode_changed"


class Outcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    INFO = "info"


class TargetType(StrEnum):
    USER = "user"
    SESSION = "session"
    UNKNOWN_USER = "unknown_user"
    AUDIT_LOG = "audit_log"
    ORGANIZATION = "organization"
    APPLICATION = "application"
    API_KEY = "api_key"
    PROVIDER = "provider"


class Reason(StrEnum):
    WRONG_PASSWORD = "wrong_password"
    UNKNOWN_USER = "unknown_user"
    DISABLED = "disabled"
    LOCKED = "locked"
    BOOTSTRAP_USED = "bootstrap_used"
    RATE_LIMITED = "rate_limited"
    LOGOUT = "logout"
    PASSWORD_CHANGED = "password_changed"
    PASSWORD_RESET = "password_reset"
    USER_DISABLED = "user_disabled"
    BOOTSTRAP = "bootstrap"
    CLI = "cli"
    UNKNOWN_USER_CAP = "unknown_user_cap"
    SESSION_CAP = "session_cap"
    MUST_CHANGE_PASSWORD = "must_change_password"
    # A user created over HTTP with a password the server made (docs/30).
    TEMPORARY_PASSWORD = "temporary_password"
    # Management refusals (docs/30): each is the error code of its answer.
    USERNAME_TAKEN = "username_taken"
    ORGANIZATION_FULL = "organization_full"
    ORGANIZATION_DISABLED = "organization_disabled"
    APPLICATION_NAME_TAKEN = "application_name_taken"
    LAST_ADMIN = "last_admin"
    OWN_ACCOUNT = "own_account"
    KEY_LIMIT = "key_limit"
    KEY_NOT_ACTIVE = "key_not_active"
    USER_CREATION_LIMITED = "user_creation_limited"
    PROVIDER_EXISTS = "provider_exists"
    NO_KEY = "no_key"
    KEY_UNREADABLE = "key_unreadable"
    TEST_RATE_LIMITED = "test_rate_limited"
    MODE_LOOSER_THAN_ORGANIZATION = "mode_looser_than_organization"


# The only keys ``details`` may have, and the type of each value.
DETAIL_FIELDS: Mapping[str, type] = {
    "reason": Reason,
    "sessions_revoked": int,
    "failures": int,
    "locked_until_ms": float,
    "organization_id": str,
    "platform_admin": bool,
    "password_rehashed": bool,
    "dropped": int,
    "minute_start_ms": float,
    "must_change_password": bool,
    "role": str,
    "scope_type": str,
    "scope_id": str,
    "acting_as": str,
    "action": str,
    "step_up_until_ms": float,
    "application_id": str,
    "plan": str,
    "key_id": str,
    "expires_at_ms": float,
    "grace_ms": float,
    "provider_type": str,
    "outcome_code": str,
    "outcome_detail": str,
    "switch": str,
    "step_up_consumed": bool,
    "key_deleted": bool,
    "old_mode": str,
    "new_mode": str,
}
# gateway.egress.refusal's reasons for an address.
_ADDRESS_REFUSALS = (
    "metadata",
    "loopback",
    "link_local",
    "private",
    "multicast",
    "reserved",
    "not_global",
)
# gateway.modes.Mode's values (docs/33).
_MODES = frozenset(
    {"local_only", "local_with_hosted_fallback", "hosted_primary_with_local_fallback"}
)
# The string details that take only these values.
FIXED_VALUES: Mapping[str, frozenset[str]] = {
    "provider_type": frozenset({"openai_compatible", "anthropic"}),
    "switch": frozenset({"organization", "platform"}),
    "old_mode": _MODES,
    "new_mode": _MODES,
    # "ok", or the code a provider failure or refusal maps to (gateway.chat, docs/32).
    "outcome_code": frozenset(
        {
            "ok",
            "no_key",
            "key_unreadable",
            "provider_auth_failed",
            "upstream_rate_limited",
            "upstream_unavailable",
            "upstream_timeout",
            "upstream_malformed",
            "upstream_rejected",
            "tools_not_supported",
            "egress_refused",
        }
    ),
    # Why a test-connection did not reach its host (gateway.egress, gateway.adapters.http):
    # fixed words, never anything the host sent.
    "outcome_detail": frozenset(
        {
            "connect",
            "connection_lost",
            "dns_failure",
            "probe",
            "unresolved",
            "bad_address",
            "unix_socket",
            "not_https",
            "local_only",
            *_ADDRESS_REFUSALS,
            *(f"embedded_{reason}" for reason in _ADDRESS_REFUSALS),
        }
    ),
}


def _checked_details(details: Mapping[str, object]) -> dict[str, object]:
    checked: dict[str, object] = {}
    for key, value in details.items():
        kind = DETAIL_FIELDS.get(key)
        if kind is None:
            raise ValueError(f"audit details may not have a {key!r} field")
        if kind is Reason:
            checked[key] = Reason(str(value)).value  # only the fixed reasons
        elif kind is bool:
            if not isinstance(value, bool):
                raise ValueError(f"audit detail {key!r} must be a bool")
            checked[key] = value
        elif kind is int:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"audit detail {key!r} must be a count")
            checked[key] = value
        elif kind is float:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"audit detail {key!r} must be a number")
            if not math.isfinite(value):
                raise ValueError(f"audit detail {key!r} must be finite")
            checked[key] = float(value)
        elif key == "scope_type":
            if value not in SCOPE_TYPES:
                raise ValueError("audit detail 'scope_type' must be a scope type")
            checked[key] = value
        elif key == "action":
            if value not in PERMISSIONS:
                raise ValueError("audit detail 'action' must be an action of the permission table")
            checked[key] = value
        elif key == "acting_as":
            if not isinstance(value, str) or not re.fullmatch(r"usr_[0-9a-f]{16}", value):
                raise ValueError("audit detail 'acting_as' must be a user id")
            checked[key] = value
        elif key in FIXED_VALUES:
            if value not in FIXED_VALUES[key]:
                raise ValueError(f"audit detail {key!r} must be one of its fixed values")
            checked[key] = value
        elif key == "key_id":
            if not isinstance(value, str) or not KEY_ID_PATTERN.fullmatch(value):
                raise ValueError("audit detail 'key_id' must be a key id")
            checked[key] = value
        else:  # organization_id, application_id, plan, role, scope_id: ids, never free text
            ok = isinstance(value, str) and (
                ORGANIZATION_ID_PATTERN.fullmatch(value) is not None
                or (key == "scope_id" and value == "*")
            )
            if not ok:
                raise ValueError(f"audit detail {key!r} must be in the form of an id")
            checked[key] = value
    return checked


def _checked_id(value: str | None, what: str) -> str | None:
    if value is not None and not ID_PATTERN.fullmatch(value):
        raise ValueError(f"audit {what} must be a user id or a session handle")
    return value


# What each kind of target may be named by: an id in its own form, never free text.
_TARGET_PATTERNS: Mapping[TargetType, re.Pattern[str]] = {
    TargetType.ORGANIZATION: ORGANIZATION_ID_PATTERN,
    TargetType.APPLICATION: ORGANIZATION_ID_PATTERN,
    TargetType.API_KEY: KEY_ID_PATTERN,
    TargetType.PROVIDER: PROVIDER_ID_PATTERN,
}


def _checked_target(target_type: TargetType, value: str | None) -> str | None:
    pattern = _TARGET_PATTERNS.get(target_type)
    if pattern is None:
        return _checked_id(value, "target_id")
    if value is not None and not pattern.fullmatch(value):
        raise ValueError(f"audit target_id of a {target_type.value} must be in the form of its id")
    return value


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """One entry. Constructing it checks every field against the allowlist."""

    action: AuditAction
    outcome: Outcome
    target_type: TargetType
    target_id: str | None = None
    actor_user_id: str | None = None
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        AuditAction(self.action)
        Outcome(self.outcome)
        _checked_target(TargetType(self.target_type), self.target_id)
        _checked_id(self.actor_user_id, "actor_user_id")
        if self.target_type is TargetType.UNKNOWN_USER and self.target_id is not None:
            raise ValueError("an unknown user has no id to record")
        object.__setattr__(self, "details", _checked_details(self.details))


@dataclass(frozen=True, slots=True)
class AuditEntry:
    seq: int
    at_ms: float
    event: AuditEvent


class AuditLog:
    """The audit log in the table of migration 7."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        unknown_user_rows_per_minute: int = DEFAULT_UNKNOWN_USER_ROWS_PER_MINUTE,
    ) -> None:
        found = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'audit_log'"
        ).fetchone()
        if found is None:
            raise RuntimeError("audit_log does not exist; run the migrations first")
        if unknown_user_rows_per_minute < 1:
            raise ValueError("unknown_user_rows_per_minute must be at least 1")
        self._connection = connection
        self._cap = unknown_user_rows_per_minute
        self._minute: int | None = None
        self._written_this_minute = 0
        self._dropped = 0

    def write(self, event: AuditEvent, at_ms: float) -> bool:
        """Append ``event`` at epoch time ``at_ms``. Returns False if it was an unknown-user
        row over this minute's cap (counted, and reported once the minute is over)."""
        self.flush_dropped(at_ms)
        if event.target_type is TargetType.UNKNOWN_USER:
            minute = math.floor(at_ms / MINUTE_MS)
            if minute != self._minute:
                self._minute, self._written_this_minute = minute, 0
            if self._written_this_minute >= self._cap:
                self._dropped += 1
                return False
            self._written_this_minute += 1
        self._insert(event, at_ms)
        return True

    def flush_dropped(self, now_ms: float) -> int:
        """Once the minute that dropped rows is over, write how many. Returns that count."""
        if self._dropped == 0 or self._minute is None:
            return 0
        if math.floor(now_ms / MINUTE_MS) == self._minute:
            return 0
        dropped, self._dropped = self._dropped, 0
        self._insert(
            AuditEvent(
                AuditAction.ROWS_DROPPED,
                Outcome.INFO,
                TargetType.AUDIT_LOG,
                details={
                    "reason": Reason.UNKNOWN_USER_CAP,
                    "dropped": dropped,
                    "minute_start_ms": self._minute * MINUTE_MS,
                },
            ),
            (self._minute + 1) * MINUTE_MS,
        )
        return dropped

    def _insert(self, event: AuditEvent, at_ms: float) -> None:
        self._connection.execute(
            "INSERT INTO audit_log (at_ms, actor_user_id, action, target_type, target_id,"
            " outcome, details) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                at_ms,
                event.actor_user_id,
                event.action.value,
                event.target_type.value,
                event.target_id,
                event.outcome.value,
                json.dumps(dict(event.details), sort_keys=True),
            ),
        )

    def entries(self) -> list[AuditEntry]:
        rows = self._connection.execute(
            "SELECT seq, at_ms, actor_user_id, action, target_type, target_id, outcome, details"
            " FROM audit_log ORDER BY seq"
        ).fetchall()
        return [_entry(row) for row in rows]

    def prune(self, before_ms: float, limit: int | None = None) -> int:
        """Delete entries with ``at_ms < before_ms``, oldest first, at most ``limit``."""
        if limit is None:
            cursor = self._connection.execute("DELETE FROM audit_log WHERE at_ms < ?", (before_ms,))
        else:
            cursor = self._connection.execute(
                "DELETE FROM audit_log WHERE seq IN (SELECT seq FROM audit_log"
                " WHERE at_ms < ? ORDER BY at_ms LIMIT ?)",
                (before_ms, limit),
            )
        return cursor.rowcount


def _entry(row: tuple[Any, ...]) -> AuditEntry:
    seq, at_ms, actor, action, target_type, target_id, outcome, details = row
    event = AuditEvent(
        AuditAction(action),
        Outcome(outcome),
        TargetType(target_type),
        target_id,
        actor,
        json.loads(details),
    )
    return AuditEntry(int(seq), at_ms, event)
