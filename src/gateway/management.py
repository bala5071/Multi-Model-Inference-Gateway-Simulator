"""Applications and their keys as the management endpoints and the CLI change them, the
resolvers that find what a management path names, and the refusals they share.

See docs/30-management-endpoints.md and docs/identity-plan.md, section 7.

- **Resolvers** answer a path's organization, user, application or key with the resource
  ``authorize`` checks, or None when it does not exist **or lies outside the organization the
  path names**: a user of ``south`` reached through ``/orgs/north/...`` is missing, and missing
  is answered exactly as forbidden.
- **``ManagementService``** creates applications from a plan template (the id generated
  unless the CLI names one) and issues, rotates and revokes keys, re-checking ``authorize``
  against the record itself when an actor is given, and writing an audit entry for each change
  (the key's id, never the key). With ``limits`` (the HTTP path) every key expires and an
  application holds at most so many active keys; without them (the operator at the CLI) the
  behaviour is as before.
- **``ManagementRefusal``** is a refusal for a conflict or a cap, carrying the reason that is
  both its answer's error code and its audit entry's reason.

Store calls are coroutines that never suspend, for the reason given in ``gateway.usage``.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from gateway.apikeys import ApiKeyService, IssuedKey
from gateway.applications import ApiKeyRecord, Application, ApplicationStore
from gateway.applications import application_from_template as _from_template
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
from gateway.organizations import normalize_name
from gateway.permissions import Actor, Resource, authorize
from gateway.routing import PlansConfig
from gateway.users import AccountStore, normalize_username

T = TypeVar("T")
HOUR_MS = 60 * 60 * 1000.0
DAY_MS = 24 * HOUR_MS
APPLICATION_ID_PREFIX = "app_"
APPLICATION_ID_BYTES = 8
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
MAX_ACTIVE_KEYS = 10
DEFAULT_KEY_DAYS = 90.0
MAX_KEY_DAYS = 365.0
DEFAULT_GRACE_HOURS = 24.0
MAX_GRACE_HOURS = 168.0
# A key's label: 1 to 64 of these, with no space at either end.
LABEL_PATTERN = re.compile(r"[A-Za-z0-9._-](?:[A-Za-z0-9 ._-]{0,62}[A-Za-z0-9._-])?")


class ManagementRefusal(ValueError):
    """A management change refused for a conflict (an answer of 409) or a cap (429). Its
    ``reason`` is the answer's error code and the audit entry's reason; the message says what
    to do, and never repeats a password or a key."""

    def __init__(self, reason: Reason, message: str) -> None:
        self.reason = reason
        super().__init__(message)


class UnknownPlanError(ValueError):
    """An application asked for a plan template the plans file does not have."""


class NotPermittedError(Exception):
    """The actor may not do this."""


def _require(actor: Actor | None, action: str, resource: Resource) -> None:
    if actor is None:
        return  # the operator at the CLI
    if not authorize(actor, action, resource).allowed:
        raise NotPermittedError(f"not permitted: {action}")


def _who(actor: Actor | None) -> tuple[str | None, dict[str, object]]:
    """The audit entry's actor, and ``acting_as`` for the operator acting as a user (docs/28)."""
    if actor is None:
        return None, {}
    if actor.via_operator:
        return None, {"acting_as": actor.user_id}
    return actor.user_id, {}


def _id_form(value: str) -> str | None:
    """``value`` if an audit entry may store it (an id), else None: an application made at
    the CLI may have an id in another form."""
    return value if ORGANIZATION_ID_PATTERN.fullmatch(value) else None


# --- resolvers ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Stores:
    """What the resolvers read: organizations, applications and keys, and users."""

    organizations: ApplicationStore
    users: AccountStore


async def resolve_organization(stores: Stores, organization_id: str) -> Resource | None:
    found = await stores.organizations.get_organization(organization_id)
    return Resource.organization(found.id) if found is not None else None


async def resolve_member(stores: Stores, organization_id: str, username: str) -> Resource | None:
    """The organization, as the resource a change to one of its users acts on, if the user
    exists **and belongs to it**."""
    if await resolve_organization(stores, organization_id) is None:
        return None
    try:
        name = normalize_username(username)
    except ValueError:
        return None
    user = await stores.users.find_user(name)
    if user is None or user.organization_id != organization_id:
        return None
    return Resource.organization(organization_id)


async def resolve_application(
    stores: Stores, organization_id: str, application_id: str
) -> Resource | None:
    """The application, if it exists **and belongs to the organization**."""
    if await resolve_organization(stores, organization_id) is None:
        return None
    application = await stores.organizations.get_application(application_id)
    if application is None or application.organization_id != organization_id:
        return None
    return Resource.application(organization_id, application.id)


async def resolve_key(
    stores: Stores, organization_id: str, application_id: str, key_id: str
) -> Resource | None:
    """The key's application, if the key exists **and belongs to that application**, which
    belongs to the organization."""
    resource = await resolve_application(stores, organization_id, application_id)
    if resource is None:
        return None
    record = await stores.organizations.get_key(key_id)
    if record is None or record.application_id != application_id:
        return None
    return resource


# --- applications and keys ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KeyLimits:
    """The HTTP path's limits on keys: every key expires within ``max_days``, and an
    application has at most ``max_active`` active keys."""

    max_active: int = MAX_ACTIVE_KEYS
    max_days: float = MAX_KEY_DAYS


@dataclass(frozen=True, slots=True)
class Page(Generic[T]):
    """One page of a list, and the cursor of the next (None on the last)."""

    items: tuple[T, ...]
    next: str | None


def check_page_size(limit: int) -> int:
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError(f"limit must be from 1 to {MAX_PAGE_SIZE}")
    return limit


class ManagementService:
    """Applications and keys, below HTTP and the CLI.

    Methods take an ``actor``: the user acting, checked with ``authorize`` against the record
    itself; or None for the operator at the CLI."""

    def __init__(
        self,
        keys: ApiKeyService,
        templates: PlansConfig,
        audit: AuditLog,
        epoch: EpochSource,
        *,
        limits: KeyLimits | None = None,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        self.keys = keys
        self.templates = templates
        self.audit = audit
        self.limits = limits
        self._epoch = epoch
        self._random_bytes = random_bytes

    def __repr__(self) -> str:
        return "ManagementService(<keys redacted>)"

    @property
    def store(self) -> ApplicationStore:
        return self.keys.store

    async def _now(self) -> float:
        # Keys' expiry is decided on the store's clock (section 3.8), as the key check does.
        return await self.keys.store_clock.now_ms()

    async def _record(self, event: AuditEvent) -> None:
        await self.audit.write(event, await self._now())

    async def _application(self, application_id: str) -> Application:
        application = await self.store.get_application(application_id)
        if application is None:
            raise ValueError(f"no application {application_id!r}")
        return application

    async def _key(self, key_id: str) -> tuple[ApiKeyRecord, Application]:
        record = await self.store.get_key(key_id)
        if record is None:
            raise ValueError(f"no key {key_id!r}")
        return record, await self._application(record.application_id)

    # --- applications --------------------------------------------------------------------

    async def create_application(
        self,
        organization_id: str,
        name: str,
        plan: str,
        *,
        actor: Actor | None = None,
        application_id: str | None = None,
    ) -> Application:
        """Create an application on a plan template, in an enabled organization, with a name
        not used there yet. Its id is generated unless one is given (the CLI)."""
        _require(actor, "applications.create", Resource.organization(organization_id))
        # The checks and the insert in one transaction: the organization cannot be disabled,
        # nor the name taken, between them (docs/36).
        async with self.store.write_transaction():
            organization = await self.store.get_organization(organization_id)
            if organization is None:
                raise ValueError(f"no organization {organization_id!r}")
            if plan not in self.templates.plans:
                plans = ", ".join(self.templates.plans)
                raise UnknownPlanError(f"unknown plan {plan!r}; plans are: {plans}")
            if organization.disabled:
                raise ManagementRefusal(
                    Reason.ORGANIZATION_DISABLED,
                    f"organization {organization_id!r} is disabled; no application can be created",
                )
            normalized = normalize_name(name)
            if any(
                a.name == normalized for a in await self.store.list_applications(organization_id)
            ):
                raise ManagementRefusal(
                    Reason.APPLICATION_NAME_TAKEN,
                    f"organization {organization_id!r} already has an application named "
                    f"{normalized!r}",
                )
            if application_id is None:
                application_id = (
                    APPLICATION_ID_PREFIX + self._random_bytes(APPLICATION_ID_BYTES).hex()
                )
            application = _from_template(
                application_id,
                normalized,
                self.templates,
                plan,
                organization_id=organization_id,
                created_at_ms=await self._now(),
            )
            await self.store.add_application(application)
            who, acting = _who(actor)
            details: dict[str, object] = {
                **acting,
                "organization_id": organization_id,
                "plan": plan,
            }
            if _id_form(plan) is None:
                del details["plan"]
            if _id_form(organization_id) is None:
                del details["organization_id"]
            await self._record(
                AuditEvent(
                    AuditAction.APPLICATION_CREATED,
                    Outcome.SUCCESS,
                    TargetType.APPLICATION,
                    _id_form(application.id),
                    who,
                    details,
                )
            )
            return application

    async def page_applications(
        self, organization_id: str, after: str | None, limit: int, *, actor: Actor | None = None
    ) -> Page[Application]:
        _require(actor, "applications.list", Resource.organization(organization_id))
        found = await self.store.page_applications(
            organization_id, after, check_page_size(limit) + 1
        )
        return page_of(found, limit, lambda a: a.id)

    async def read_application(
        self, application_id: str, *, actor: Actor | None = None
    ) -> Application:
        application = await self._application(application_id)
        resource = Resource.application(application.organization_id, application.id)
        _require(actor, "applications.read", resource)
        return application

    # --- keys ----------------------------------------------------------------------------

    def _active(self, record: ApiKeyRecord, now: float) -> bool:
        return not record.revoked and (record.expires_at_ms is None or now < record.expires_at_ms)

    def _expiry(self, expires_in_days: float | None, now: float) -> float | None:
        if self.limits is None:
            return now + expires_in_days * DAY_MS if expires_in_days is not None else None
        if expires_in_days is None or not 1 <= expires_in_days <= self.limits.max_days:
            raise ValueError(f"a key expires within 1 to {self.limits.max_days:g} days")
        return now + expires_in_days * DAY_MS

    async def _check_room(
        self, application: Application, now: float, *, replacing: str | None
    ) -> None:
        """At most ``limits.max_active`` active keys, a key being replaced not counted."""
        if self.limits is None:
            return
        active = [
            k
            for k in await self.store.list_keys(application.id)
            if self._active(k, now) and k.key_id != replacing
        ]
        if len(active) >= self.limits.max_active:
            raise ManagementRefusal(
                Reason.KEY_LIMIT,
                f"application {application.id!r} has {len(active)} active keys, the most it may "
                "have: revoke one first",
            )

    def _key_event(
        self,
        action: AuditAction,
        record: ApiKeyRecord,
        application: Application,
        actor: Actor | None,
        **extra: object,
    ) -> AuditEvent:
        who, acting = _who(actor)
        details: dict[str, object] = {**acting, **extra}
        for field_name, value in (
            ("application_id", application.id),
            ("organization_id", application.organization_id),
        ):
            if _id_form(value) is not None:
                details[field_name] = value
        return AuditEvent(action, Outcome.SUCCESS, TargetType.API_KEY, record.key_id, who, details)

    async def issue_key(
        self,
        application_id: str,
        label: str,
        *,
        actor: Actor | None = None,
        expires_in_days: float | None = None,
    ) -> IssuedKey:
        """A new key, shown once by the caller and never again."""
        application = await self._application(application_id)
        resource = Resource.application(application.organization_id, application.id)
        _require(actor, "application_keys.issue", resource)
        now = await self._now()
        expires = self._expiry(expires_in_days, now)
        extra: dict[str, object] = {}
        if expires is not None:
            extra["expires_at_ms"] = expires
        # The count and the new key together: no other writer can take the last free place
        # between them (docs/scale-out-plan.md, section 3.6).
        async with self.store.write_transaction():
            await self._check_room(application, now, replacing=None)
            issued = await self.keys.issue(application.id, label, expires_at_ms=expires)
            await self._record(
                self._key_event(AuditAction.KEY_ISSUED, issued.record, application, actor, **extra)
            )
        return issued

    async def rotate_key(
        self,
        key_id: str,
        *,
        grace_hours: float = DEFAULT_GRACE_HOURS,
        actor: Actor | None = None,
        expires_in_days: float | None = None,
    ) -> IssuedKey:
        """A replacement key; the old one keeps working for ``grace_hours``."""
        old, application = await self._key(key_id)
        resource = Resource.application(application.organization_id, application.id)
        _require(actor, "application_keys.rotate", resource)
        now = await self._now()
        if self.limits is not None and not 0 <= grace_hours <= MAX_GRACE_HOURS:
            raise ValueError(f"grace_hours must be from 0 to {MAX_GRACE_HOURS:g}")
        if not self._active(old, now):
            raise ManagementRefusal(
                Reason.KEY_NOT_ACTIVE, f"key {key_id} is revoked or expired: issue a new one"
            )
        expires = self._expiry(expires_in_days, now)
        grace_ms = grace_hours * HOUR_MS
        async with self.store.write_transaction():  # the count and the new key together
            # The key checked again: a revocation since the check above wins (docs/36).
            current = await self.store.get_key(key_id)
            if current is None or not self._active(current, now):
                raise ManagementRefusal(
                    Reason.KEY_NOT_ACTIVE, f"key {key_id} is revoked or expired: issue a new one"
                )
            await self._check_room(application, now, replacing=old.key_id)
            issued = await self.keys.rotate(key_id, grace_ms=grace_ms, expires_at_ms=expires)
            extra: dict[str, object] = {"key_id": issued.record.key_id, "grace_ms": grace_ms}
            if expires is not None:
                extra["expires_at_ms"] = expires
            await self._record(
                self._key_event(AuditAction.KEY_ROTATED, old, application, actor, **extra)
            )
        return issued

    async def revoke_key(self, key_id: str, *, actor: Actor | None = None) -> None:
        record, application = await self._key(key_id)
        resource = Resource.application(application.organization_id, application.id)
        _require(actor, "application_keys.revoke", resource)
        async with self.store.write_transaction():  # ordered against a rotation of the key
            await self.keys.revoke(key_id)
            await self._record(self._key_event(AuditAction.KEY_REVOKED, record, application, actor))

    async def page_keys(
        self, application_id: str, after: str | None, limit: int, *, actor: Actor | None = None
    ) -> Page[ApiKeyRecord]:
        application = await self._application(application_id)
        resource = Resource.application(application.organization_id, application.id)
        _require(actor, "application_keys.list", resource)
        found = await self.store.page_keys(application.id, after, check_page_size(limit) + 1)
        return page_of(found, limit, lambda k: k.key_id)

    def key_state(self, record: ApiKeyRecord, now_ms: float) -> str:
        """``now_ms``: the request's store time (``SessionContext.now_ms``)."""
        if record.revoked:
            return "revoked"
        return "active" if self._active(record, now_ms) else "expired"


def page_of(found: list[T], limit: int, cursor: Callable[[T], str]) -> Page[T]:
    """``found`` holds up to ``limit + 1`` items: the extra one says there is a next page."""
    items = found[:limit]
    more = len(found) > limit
    return Page(tuple(items), cursor(items[-1]) if more and items else None)
