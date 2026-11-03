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

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

from gateway.applications import ApplicationStore
from gateway.audit import AuditAction, AuditEvent, AuditLog, Outcome, Reason, TargetType
from gateway.clock import EpochSource
from gateway.management import ManagementRefusal
from gateway.settings import (
    EGRESS_SETTINGS,
    PRESETS,
    SETTINGS_SCHEMA_VERSION,
    Document,
    EffectiveSettings,
    PlatformSettings,
    Refusal,
    Scope,
    SettingsInvalidError,
    SettingsRow,
    SettingsStore,
    SettingsUnreadableError,
    SettingsVersion,
    SettingsVersionConflictError,
    canonical,
    changed_fields,
    effective_settings,
    loosens_egress,
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
class _Prepared:
    row: SettingsRow | None
    current: int
    before: Document
    document: Document
    changed: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SettingsValidation:
    """What a write would do, from ``validate``: whether it would be stored; the refusal's
    code if not (``setting_looser_than_cap``, ``settings_version_conflict``, or None with
    ``invalid_setting`` for a document that is not valid); the fields it would change and the
    document it would store; for an application, the effective values it would have."""

    valid: bool
    code: str | None
    changed: tuple[str, ...]
    document: Document
    effective: EffectiveSettings | None
    refusals: tuple[Refusal, ...]
    invalid_setting: str | None
    message: str | None
    # The settings among S13 to S16 the change would loosen, for any application it affects:
    # any of them means the write needs step-up (docs/42).
    loosens: tuple[str, ...] = ()


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
        providers: Callable[[str], Awaitable[frozenset[str]]] | None = None,
    ) -> None:
        self.store = store
        self._applications = applications
        self._audit = audit
        self._epoch = epoch
        self.platform = platform
        # An organization's provider ids, which ``allowed_providers`` may name (S13).
        self._providers = providers

    async def _document(self, scope: Scope, scope_id: str) -> tuple[SettingsRow | None, Document]:
        row = await self.store.current(scope, scope_id)
        return row, (parse_row(row, scope) if row is not None else {})

    async def _refusals(
        self, scope: Scope, scope_id: str, document: Document, changed: Sequence[str]
    ) -> list[Refusal]:
        """The settings ``document`` changes that are looser than a cap in force, read now.
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
        return [r for r in found if r.setting in changed]

    async def _loosens(
        self, scope: Scope, scope_id: str, before: Document, after: Document
    ) -> tuple[str, ...]:
        """What the change loosens among S13 to S16, judged from the effective values of every
        application it affects, read now: the application itself, or every application of the
        organization whose caps change (docs/42)."""
        found: list[str] = []
        if scope is Scope.APPLICATION:
            application = await self._applications.get_application(scope_id)
            if application is None:
                raise KeyError(f"no application {scope_id!r}")
            _, organization = await self._document(Scope.ORGANIZATION, application.organization_id)
            found += loosens_egress(
                effective_settings(self.platform, application, organization, before),
                effective_settings(self.platform, application, organization, after),
            )
        else:
            for application in await self._applications.list_applications(scope_id):
                _, own = await self._document(Scope.APPLICATION, application.id)
                found += loosens_egress(
                    effective_settings(self.platform, application, before, own),
                    effective_settings(self.platform, application, after, own),
                )
        return tuple(name for name in EGRESS_SETTINGS if name in found)

    async def _check_providers(self, scope: Scope, scope_id: str, document: Document) -> None:
        """``allowed_providers`` names only providers of the application's organization; one of
        another organization and one that does not exist are refused alike."""
        named = document.get("allowed_providers")
        if scope is not Scope.APPLICATION or not isinstance(named, list) or not named:
            return
        application = await self._applications.get_application(scope_id)
        if application is None:
            raise KeyError(f"no application {scope_id!r}")
        known = (
            await self._providers(application.organization_id)
            if self._providers is not None
            else frozenset()
        )
        if any(provider not in known for provider in named):
            raise SettingsInvalidError(
                "allowed_providers", "names a provider the organization does not have"
            )

    async def _check(
        self, scope: Scope, scope_id: str, document: Document, changed: Sequence[str]
    ) -> None:
        found = await self._refusals(scope, scope_id, document, changed)
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

    async def _prepare(
        self,
        scope: Scope,
        scope_id: str,
        expected_version: int,
        values: Mapping[str, object] | None,
        unset: Sequence[str],
        replace: bool,
    ) -> _Prepared:
        """What a write would store, up to the point of storing it: the new document,
        validated, made from the version expected, and the fields it changes. The one path for
        ``set`` and ``validate``."""
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
        await self._check_providers(scope, scope_id, document)
        current = row.version if row is not None else 0
        if current != expected_version:
            raise ManagementRefusal(
                Reason.SETTINGS_VERSION_CONFLICT,
                f"the settings are at version {current}, not {expected_version}: read them "
                "again and make the change from there",
            )
        return _Prepared(row, current, before, document, changed_fields(before, document))

    async def validate(
        self,
        scope: Scope,
        scope_id: str,
        *,
        expected_version: int,
        values: Mapping[str, object] | None = None,
        unset: Sequence[str] = (),
    ) -> SettingsValidation:
        """A dry run of ``set``: every check it makes, on the same code, and what it would
        store and change, with every refusal. Writes nothing and takes no write lock."""
        scope = Scope(scope)
        try:
            prepared = await self._prepare(scope, scope_id, expected_version, values, unset, False)
        except SettingsInvalidError as exc:
            return SettingsValidation(False, None, (), {}, None, (), exc.setting, str(exc))
        except ManagementRefusal as exc:
            return SettingsValidation(False, exc.reason.value, (), {}, None, (), None, str(exc))
        found = await self._refusals(scope, scope_id, prepared.document, prepared.changed)
        loosens = await self._loosens(scope, scope_id, prepared.before, prepared.document)
        effective = None
        if scope is Scope.APPLICATION:
            application = await self._applications.get_application(scope_id)
            if application is None:
                raise KeyError(f"no application {scope_id!r}")
            _, organization = await self._document(Scope.ORGANIZATION, application.organization_id)
            effective = effective_settings(
                self.platform, application, organization, prepared.document
            )
        return SettingsValidation(
            not found,
            Reason.SETTING_LOOSER_THAN_CAP.value if found else None,
            prepared.changed,
            prepared.document,
            effective,
            tuple(found),
            None,
            str(_refused(found)) if found else None,
            loosens,
        )

    async def versions(
        self, scope: Scope, scope_id: str, *, limit: int, before: int | None = None
    ) -> list[SettingsVersion]:
        """A page of versions, newest first, older than ``before`` when given."""
        return await self.store.versions(Scope(scope), scope_id, limit, before=before)

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
        step_up: bool | None = None,
        action: AuditAction = AuditAction.SETTINGS_CHANGED,
        preset: str | None = None,
    ) -> SettingsChange:
        """Set ``values`` and remove ``unset`` from the scope's document (or, with ``replace``,
        make the document ``values`` alone: the way to write over a row that cannot be read).
        Refused, with nothing written: a document that is not valid
        (``SettingsInvalidError``), looser than a cap in force (``setting_looser_than_cap``), or
        made from another version (``settings_version_conflict``)."""
        scope = Scope(scope)
        async with self.store.write_transaction():
            prepared = await self._prepare(
                scope, scope_id, expected_version, values, unset, replace
            )
            current, document, changed = prepared.current, prepared.document, prepared.changed
            if not changed and not (replace and prepared.row is not None):
                return SettingsChange(scope, scope_id, current, (), document)
            await self._check(scope, scope_id, document, changed)
            await self._agree(scope, scope_id, prepared.before, document, step_up)
            version = await self._write(scope, scope_id, current, document, changed,
                                        actor_user_id, via)  # fmt: skip
            await self._record(
                action, scope, scope_id, version, changed, actor_user_id,
                step_up=step_up, preset=preset,
            )  # fmt: skip
        return SettingsChange(scope, scope_id, version, changed, document)

    async def _agree(
        self, scope: Scope, scope_id: str, before: Document, after: Document, step_up: bool | None
    ) -> None:
        """Inside the write's transaction: whether the change loosens S13 to S16 for any
        application it affects, judged again now, must be what the caller's step-up was decided
        on (``step_up``: whether one was claimed). A cap moved meanwhile is a conflict, and
        nothing is written. ``None`` is the operator's CLI, which needs no step-up."""
        if step_up is None:
            return
        if bool(await self._loosens(scope, scope_id, before, after)) != step_up:
            raise ManagementRefusal(
                Reason.SETTINGS_VERSION_CONFLICT,
                "what this change loosens moved while it was checked: check it again",
            )

    async def apply_preset(
        self,
        application_id: str,
        name: str,
        *,
        expected_version: int,
        actor_user_id: str | None = None,
        step_up: bool | None = None,
    ) -> SettingsChange:
        """A preset of the plan's section 4: its fields set as one version
        (``via: preset:<name>``), checked like any write, its step-up decided from the change
        it makes, never from its name."""
        return await self.set(
            Scope.APPLICATION,
            application_id,
            expected_version=expected_version,
            values=PRESETS[name],
            actor_user_id=actor_user_id,
            via=f"preset:{name}",
            step_up=step_up,
            action=AuditAction.SETTINGS_PRESET_APPLIED,
            preset=name,
        )

    async def revert(
        self,
        scope: Scope,
        scope_id: str,
        *,
        expected_version: int,
        actor_user_id: str | None = None,
        step_up: bool | None = None,
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
            await self._agree(scope, scope_id, before, document, step_up)
            version = await self._write(scope, scope_id, current, document, changed,
                                        actor_user_id, f"revert:{previous.version}")  # fmt: skip
            await self._record(AuditAction.SETTINGS_REVERTED, scope, scope_id, version, changed,
                               actor_user_id, step_up=step_up)  # fmt: skip
        return SettingsChange(scope, scope_id, version, changed, document)

    async def revert_loosens(
        self, scope: Scope, scope_id: str, *, expected_version: int
    ) -> tuple[str, ...]:
        """What ``revert`` would loosen among S13 to S16, read now, for the handler to decide
        step-up; ``revert`` judges it again inside its write. Nothing when there is nothing to
        revert (``revert`` then refuses)."""
        row, before = await self._document(scope, scope_id)
        current = row.version if row is not None else 0
        if current != expected_version or current < 2:
            return ()
        previous = await self.store.version(scope, scope_id, current - 1)
        if previous is None:
            return ()
        document = parse_row(
            SettingsRow(scope, scope_id, previous.version, previous.document,
                        SETTINGS_SCHEMA_VERSION, previous.changed_at_ms),
            scope,
        )  # fmt: skip
        return await self._loosens(scope, scope_id, before, document)

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
        *,
        step_up: bool | None = None,
        preset: str | None = None,
    ) -> None:
        # The names of the fields changed, never their values: those are in the version row.
        details: dict[str, object] = {"settings_version": version, "changed_fields": list(changed)}
        if step_up is not None:
            details["loosened_egress"] = step_up
        if preset is not None:
            details["preset"] = preset
        await self._audit.write(
            AuditEvent(
                action,
                Outcome.SUCCESS,
                TargetType.ORGANIZATION if scope is Scope.ORGANIZATION else TargetType.APPLICATION,
                scope_id,
                actor_user_id,
                details,
            ),
            self._epoch.epoch_ms(),
        )
