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

MINUTE_MS = 60_000.0
DEFAULT_UNKNOWN_USER_ROWS_PER_MINUTE = 60
# User ids and session handles: what an entry may name.
ID_PATTERN = re.compile(r"(usr|ses)_[0-9a-f]{16}")
# Organization ids are chosen by an operator; an entry stores them only in this form.
ORGANIZATION_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}")
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


class Outcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    INFO = "info"


class TargetType(StrEnum):
    USER = "user"
    SESSION = "session"
    UNKNOWN_USER = "unknown_user"
    AUDIT_LOG = "audit_log"


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
        else:  # organization_id, role, scope_id: ids, never free text
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
        TargetType(self.target_type)
        _checked_id(self.target_id, "target_id")
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
