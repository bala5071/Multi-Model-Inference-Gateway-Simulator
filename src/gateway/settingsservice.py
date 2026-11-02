"""Changing settings: every change a version, checked against the caps in force, audited.

See docs/39-settings.md. The operator's CLI uses it in S1; the endpoints of PR S2 will too.

Each write runs in one ``write_transaction()`` (the global write lock on Postgres): the caps it
is checked against are read in the transaction that writes, so a cap tightened on another
instance in between either waits for the lock or is refused by its timeout, and an application
value looser than a cap in force is never stored (the eighth management invariant of the
scale-out plan, section 3.6). A change names the version it was made from: any other current
version is a conflict and nothing is written.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from gateway.applications import ApplicationStore
from gateway.audit import AuditAction, AuditEvent, AuditLog, Outcome, Reason, TargetType
from gateway.clock import EpochSource
from gateway.management import ManagementRefusal
from gateway.settings import (
    SETTINGS_SCHEMA_VERSION,
    Document,
    EffectiveSettings,
    PlatformSettings,
    Refusal,
    Scope,
    SettingsRow,
    SettingsStore,
    SettingsUnreadableError,
    SettingsVersionConflictError,
    canonical,
    changed_fields,
    effective_settings,
    parse_row,
    refusals,
    validate_document,
)


@dataclass(frozen=True, slots=True)
class SettingsChange:
    scope: Scope
    scope_id: str
    # The version now current; the same as before when nothing changed.
    version: int
    changed: tuple[str, ...]
    document: Document


@dataclass(frozen=True, slots=True)
class SettingsView:
    """One scope's settings: the current row (None if never set), its document and, for an
    application, the effective values with their sources."""

    row: SettingsRow | None
    document: Document
    effective: EffectiveSettings | None


def _refused(found: Sequence[Refusal]) -> ManagementRefusal:
    named = ", ".join(f"{r.setting} (capped by the {r.level.value})" for r in found)
    return ManagementRefusal(
        Reason.SETTING_LOOSER_THAN_CAP,
        f"looser than a cap in force: {named}. A setting may be as strict as its caps or "
        "stricter, never looser.",
    )


class SettingsService:
    def __init__(
        self,
        store: SettingsStore,
        applications: ApplicationStore,
        audit: AuditLog,
        epoch: EpochSource,
        platform: PlatformSettings,
    ) -> None:
        self.store = store
        self._applications = applications
        self._audit = audit
        self._epoch = epoch
        self.platform = platform

    async def _document(self, scope: Scope, scope_id: str) -> tuple[SettingsRow | None, Document]:
        row = await self.store.current(scope, scope_id)
        return row, (parse_row(row, scope) if row is not None else {})

    async def _check(
        self, scope: Scope, scope_id: str, document: Document, changed: Sequence[str]
    ) -> None:
        """Refuse ``document`` if a setting it changes is looser than a cap in force, read now.
        A value it does not change was checked when it was written, and a cap tightened since
        clamps it when it is read: it does not block an unrelated change."""
        if scope is Scope.ORGANIZATION:
            if await self._applications.get_organization(scope_id) is None:
                raise KeyError(f"no organization {scope_id!r}")
            found = refusals(self.platform, scope, document)
        else:
            application = await self._applications.get_application(scope_id)
            if application is None:
                raise KeyError(f"no application {scope_id!r}")
            _, organization = await self._document(Scope.ORGANIZATION, application.organization_id)
            found = refusals(
                self.platform, scope, document, application=application, organization=organization
            )
        found = [r for r in found if r.setting in changed]
        if found:
            raise _refused(found)

    async def view(self, scope: Scope, scope_id: str) -> SettingsView:
        row, document = await self._document(scope, scope_id)
        effective = None
        if scope is Scope.APPLICATION:
            application = await self._applications.get_application(scope_id)
            if application is None:
                raise KeyError(f"no application {scope_id!r}")
            _, organization = await self._document(Scope.ORGANIZATION, application.organization_id)
            effective = effective_settings(self.platform, application, organization, document)
        return SettingsView(row, document, effective)

    async def set(
        self,
        scope: Scope,
        scope_id: str,
        *,
        expected_version: int,
        values: Mapping[str, object] | None = None,
        unset: Sequence[str] = (),
        replace: bool = False,
        actor_user_id: str | None = None,
        via: str = "cli",
    ) -> SettingsChange:
        """Set ``values`` and remove ``unset`` from the scope's document (or, with ``replace``,
        make the document ``values`` alone: the way to write over a row that cannot be read).
        Refused, with nothing written: a document that is not valid
        (``SettingsInvalidError``), looser than a cap in force (``setting_looser_than_cap``), or
        made from another version (``settings_version_conflict``)."""
        scope = Scope(scope)
        async with self.store.write_transaction():
            if replace:
                row = await self.store.current(scope, scope_id)
                before: Document = {}
                if row is not None:
                    try:
                        before = parse_row(row, scope)
                    except SettingsUnreadableError:  # every field is reported as changed
                        before = {}
            else:
                row, before = await self._document(scope, scope_id)
            document = {} if replace else dict(before)
            document.update(values or {})
            for name in unset:
                document.pop(name, None)
            document = validate_document(scope, document)
            current = row.version if row is not None else 0
            if current != expected_version:
                raise ManagementRefusal(
                    Reason.SETTINGS_VERSION_CONFLICT,
                    f"the settings are at version {current}, not {expected_version}: read them "
                    "again and make the change from there",
                )
            changed = changed_fields(before, document)
            if not changed and not (replace and row is not None):
                return SettingsChange(scope, scope_id, current, (), document)
            await self._check(scope, scope_id, document, changed)
            version = await self._write(scope, scope_id, current, document, changed,
                                        actor_user_id, via)  # fmt: skip
            await self._record(AuditAction.SETTINGS_CHANGED, scope, scope_id, version, changed,
                               actor_user_id)  # fmt: skip
        return SettingsChange(scope, scope_id, version, changed, document)

    async def revert(
        self,
        scope: Scope,
        scope_id: str,
        *,
        expected_version: int,
        actor_user_id: str | None = None,
    ) -> SettingsChange:
        """One step back: version ``N + 1`` with version ``N - 1``'s document, checked against
        the caps in force like any write. Version 1 has nothing to go back to."""
        scope = Scope(scope)
        async with self.store.write_transaction():
            row, before = await self._document(scope, scope_id)
            current = row.version if row is not None else 0
            if current != expected_version:
                raise ManagementRefusal(
                    Reason.SETTINGS_VERSION_CONFLICT,
                    f"the settings are at version {current}, not {expected_version}",
                )
            previous = (
                await self.store.version(scope, scope_id, current - 1) if current > 1 else None
            )
            if previous is None:
                raise ManagementRefusal(
                    Reason.NO_PREVIOUS_VERSION,
                    f"version {current} has no previous version to go back to",
                )
            # A kept version is judged as a current row is: one that cannot be is refused.
            document = parse_row(
                SettingsRow(scope, scope_id, previous.version, previous.document,
                            SETTINGS_SCHEMA_VERSION, previous.changed_at_ms),
                scope,
            )  # fmt: skip
            changed = changed_fields(before, document)
            await self._check(scope, scope_id, document, changed)
            version = await self._write(scope, scope_id, current, document, changed,
                                        actor_user_id, f"revert:{previous.version}")  # fmt: skip
            await self._record(AuditAction.SETTINGS_REVERTED, scope, scope_id, version, changed,
                               actor_user_id)  # fmt: skip
        return SettingsChange(scope, scope_id, version, changed, document)

    async def _write(
        self,
        scope: Scope,
        scope_id: str,
        current: int,
        document: Document,
        changed: Sequence[str],
        actor_user_id: str | None,
        via: str,
    ) -> int:
        try:
            return await self.store.write(
                scope,
                scope_id,
                expected_version=current,
                document=canonical(document),
                changed=changed,
                changed_by_user_id=actor_user_id,
                via=via,
                at_ms=self._epoch.epoch_ms(),
            )
        except SettingsVersionConflictError as exc:
            raise ManagementRefusal(
                Reason.SETTINGS_VERSION_CONFLICT, f"the settings are at version {exc.current}"
            ) from None

    async def _record(
        self,
        action: AuditAction,
        scope: Scope,
        scope_id: str,
        version: int,
        changed: Sequence[str],
        actor_user_id: str | None,
    ) -> None:
        # The names of the fields changed, never their values: those are in the version row.
        await self._audit.write(
            AuditEvent(
                action,
                Outcome.SUCCESS,
                TargetType.ORGANIZATION if scope is Scope.ORGANIZATION else TargetType.APPLICATION,
                scope_id,
                actor_user_id,
                {"settings_version": version, "changed_fields": list(changed)},
            ),
            self._epoch.epoch_ms(),
        )
