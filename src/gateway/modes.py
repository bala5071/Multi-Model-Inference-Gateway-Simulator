"""Fallback modes: where an organization's and an application's chat requests may be served.

See docs/33-fallback-modes.md.

- ``local_only`` (the default): the gateway's own tiers and the operator's local destinations.
- ``local_with_hosted_fallback``: local first, then the organization's ``fallback`` providers.
- ``hosted_primary_with_local_fallback``: the organization's ``primary`` providers first, then
  local. Every prompt goes to a third party.

An application may be as strict as its organization or stricter, never looser.
``effective_mode`` is the only function that decides which mode applies to a request: the
stricter of the two. Nothing else compares modes.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from gateway.audit import (
    ORGANIZATION_ID_PATTERN,
    AuditAction,
    AuditEvent,
    AuditLog,
    Outcome,
    Reason,
    TargetType,
)
from gateway.clock import EpochSource
from gateway.management import ManagementRefusal
from gateway.permissions import Actor


class Mode(StrEnum):
    LOCAL_ONLY = "local_only"
    LOCAL_WITH_HOSTED_FALLBACK = "local_with_hosted_fallback"
    HOSTED_PRIMARY_WITH_LOCAL_FALLBACK = "hosted_primary_with_local_fallback"


DEFAULT_MODE = Mode.LOCAL_ONLY
# Strictest first.
_STRICTNESS = (
    Mode.LOCAL_ONLY,
    Mode.LOCAL_WITH_HOSTED_FALLBACK,
    Mode.HOSTED_PRIMARY_WITH_LOCAL_FALLBACK,
)


def effective_mode(organization: Mode, application: Mode) -> Mode:
    """The mode a request of this application is served under: the stricter of the two."""
    return min(organization, application, key=_STRICTNESS.index)


def looser_than(mode: Mode, bound: Mode) -> bool:
    """Whether ``mode`` would let prompts go further than ``bound`` allows."""
    return _STRICTNESS.index(mode) > _STRICTNESS.index(bound)


def may_reach_hosted(mode: Mode) -> bool:
    return mode is not Mode.LOCAL_ONLY


class ModeStore(Protocol):
    def immediate(self) -> contextlib.AbstractContextManager[None]: ...

    def organization_mode(self, organization_id: str) -> Mode: ...

    def application_mode(self, application_id: str) -> Mode: ...

    def set_organization_mode(self, organization_id: str, mode: Mode, at_ms: float) -> None: ...

    def set_application_mode(self, application_id: str, mode: Mode, at_ms: float) -> None: ...


class InMemoryModeStore:
    """Modes in memory, for tests and simulated runs. Absent means ``local_only``."""

    def __init__(self) -> None:
        self._modes: dict[tuple[str, str], Mode] = {}

    @contextmanager
    def immediate(self) -> Iterator[None]:
        yield

    def organization_mode(self, organization_id: str) -> Mode:
        return self._modes.get(("organization", organization_id), DEFAULT_MODE)

    def application_mode(self, application_id: str) -> Mode:
        return self._modes.get(("application", application_id), DEFAULT_MODE)

    def set_organization_mode(self, organization_id: str, mode: Mode, at_ms: float) -> None:
        self._modes[("organization", organization_id)] = Mode(mode)

    def set_application_mode(self, application_id: str, mode: Mode, at_ms: float) -> None:
        self._modes[("application", application_id)] = Mode(mode)


class SqliteModeStore:
    """Modes in the ``modes`` table (migration 12). Absent means ``local_only``."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    @contextmanager
    def immediate(self) -> Iterator[None]:
        """One write transaction: the mode and its audit entry together, or neither."""
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        self._connection.execute("COMMIT")

    def _get(self, scope_type: str, scope_id: str) -> Mode:
        row = self._connection.execute(
            "SELECT mode FROM modes WHERE scope_type = ? AND scope_id = ?", (scope_type, scope_id)
        ).fetchone()
        return Mode(row[0]) if row is not None else DEFAULT_MODE

    def _set(self, scope_type: str, scope_id: str, mode: Mode, at_ms: float) -> None:
        self._connection.execute(
            "INSERT INTO modes (scope_type, scope_id, mode, updated_at_ms) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (scope_type, scope_id)"
            " DO UPDATE SET mode = excluded.mode, updated_at_ms = excluded.updated_at_ms",
            (scope_type, scope_id, Mode(mode).value, at_ms),
        )

    def organization_mode(self, organization_id: str) -> Mode:
        return self._get("organization", organization_id)

    def application_mode(self, application_id: str) -> Mode:
        return self._get("application", application_id)

    def set_organization_mode(self, organization_id: str, mode: Mode, at_ms: float) -> None:
        self._set("organization", organization_id, mode, at_ms)

    def set_application_mode(self, application_id: str, mode: Mode, at_ms: float) -> None:
        self._set("application", application_id, mode, at_ms)


class ChangeMarker(Protocol):
    """``requires``' consume-on-use token: told once the change is made (docs/32)."""

    def changed(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ModeChange:
    previous: Mode
    mode: Mode


class ModeService:
    """Setting modes, through ``requires`` with consume-on-use step-up, each change audited
    with the old and the new mode in the same transaction (docs/33)."""

    def __init__(self, store: ModeStore, audit: AuditLog, epoch: EpochSource) -> None:
        self.store = store
        self._audit = audit
        self._epoch = epoch

    def _record(
        self, target: TargetType, target_id: str, organization_id: str, change: ModeChange,
        actor: Actor,
    ) -> None:  # fmt: skip
        details: dict[str, object] = {
            "old_mode": change.previous.value,
            "new_mode": change.mode.value,
            "step_up_consumed": True,
        }
        if ORGANIZATION_ID_PATTERN.fullmatch(organization_id):
            details["organization_id"] = organization_id
        self._audit.write(
            AuditEvent(
                AuditAction.MODE_CHANGED,
                Outcome.SUCCESS,
                target,
                target_id,
                actor.user_id,
                details,
            ),
            self._epoch.epoch_ms(),
        )

    def set_organization(
        self, organization_id: str, mode: Mode, *, actor: Actor, marker: ChangeMarker
    ) -> ModeChange:
        """An organization's mode. Its applications follow it when it is stricter than theirs:
        ``effective_mode`` takes the stricter, without their stored values changing."""
        with self.store.immediate():
            change = ModeChange(self.store.organization_mode(organization_id), mode)
            self.store.set_organization_mode(organization_id, mode, self._epoch.epoch_ms())
            self._record(TargetType.ORGANIZATION, organization_id, organization_id, change, actor)
            marker.changed()
        return change

    def set_application(
        self,
        organization_id: str,
        application_id: str,
        mode: Mode,
        *,
        actor: Actor,
        marker: ChangeMarker,
    ) -> ModeChange:
        """An application's mode: as strict as its organization's or stricter, else 409
        ``mode_looser_than_organization`` (refused before any change, so the step-up window is
        given back)."""
        with self.store.immediate():
            # Read in the same transaction as the write: no organization change slips between.
            bound = self.store.organization_mode(organization_id)
            if looser_than(mode, bound):
                raise ManagementRefusal(
                    Reason.MODE_LOOSER_THAN_ORGANIZATION,
                    f"the organization's mode is {bound.value}; an application may be as "
                    "strict or stricter, never looser",
                )
            change = ModeChange(self.store.application_mode(application_id), mode)
            self.store.set_application_mode(application_id, mode, self._epoch.epoch_ms())
            self._record(TargetType.APPLICATION, application_id, organization_id, change, actor)
            marker.changed()
        return change
