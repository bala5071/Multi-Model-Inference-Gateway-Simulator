"""Applications, the organizations that own them, the records of their API keys, and the
stores that keep all three.

See docs/20-applications-api-keys.md and docs/26-organizations.md.

An application is who a request is for: it has its own copy of the settings of the plan it
was created from, and belongs to one organization. Application ids are unique everywhere;
names are unique within an organization. Nothing here assumes there is one organization:
both stores start with the ``default`` organization (migration 4 creates it), which is data.

A key record holds the key's HMAC, never the key. Store methods are coroutines that never
suspend, for the reason given in ``gateway.usage``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, replace
from typing import Any, Protocol

from gateway.database import write_transaction
from gateway.organizations import (
    DEFAULT_ORGANIZATION_ID,
    DEFAULT_ORGANIZATION_NAME,
    Organization,
    OrganizationKind,
    normalize_name,
)
from gateway.routing import Plan, PlansConfig


@dataclass(frozen=True, slots=True)
class Application:
    id: str
    name: str
    # The application's own plan settings, copied from a template when it was created.
    plan: Plan
    # The largest max_tokens a request may ask for, also used when it asks for none.
    max_tokens: int | None
    requests_per_minute: int | None
    # Epoch time. Budget periods will be counted from here once usage is stored (step 3).
    created_at_ms: float
    # The organization it belongs to; fixed when it is created.
    organization_id: str


def application_from_template(
    application_id: str,
    name: str,
    templates: PlansConfig,
    plan_name: str,
    *,
    organization_id: str,
    created_at_ms: float,
) -> Application:
    if not application_id:
        raise ValueError("an application needs an id")
    if plan_name not in templates.plans:
        raise ValueError(f"unknown plan {plan_name!r}; plans are: {', '.join(templates.plans)}")
    limits = templates.limits_for(plan_name)
    return Application(
        id=application_id,
        name=normalize_name(name),
        plan=templates.plans[plan_name],
        max_tokens=limits.max_tokens,
        requests_per_minute=limits.requests_per_minute,
        created_at_ms=created_at_ms,
        organization_id=organization_id,
    )


@dataclass(frozen=True, slots=True)
class ApiKeyRecord:
    """What is stored about a key. The key itself is never stored."""

    # The lookup id, which is also the visible part of the key after its prefix.
    key_id: str
    application_id: str
    label: str
    # HMAC-SHA256 of the whole key with the pepper, as hex.
    key_hmac: str
    created_at_ms: float
    expires_at_ms: float | None = None
    revoked: bool = False
    last_used_at_ms: float | None = None


@dataclass(frozen=True, slots=True)
class KeyLookup:
    """What a key check needs, found in one lookup: the key's record, its application (None
    if it is gone) and whether that application's organization is enabled."""

    record: ApiKeyRecord
    application: Application | None
    organization_enabled: bool
    # The store's time, read in the same query, where the store has its own clock
    # (docs/scale-out-plan.md, section 3.8); None where the injected epoch decides.
    now_ms: float | None = None


class ApplicationStore(Protocol):
    def write_transaction(self) -> AbstractAsyncContextManager[None]:
        """One write transaction: whatever the block writes, together or not at all, and no
        other writer between its reads and its writes."""
        ...

    async def add_organization(self, organization: Organization) -> None:
        """Raise ``ValueError`` if the id or the name is taken."""
        ...

    async def get_organization(self, organization_id: str) -> Organization | None: ...

    async def find_organization(self, name: str) -> Organization | None:
        """The organization with exactly this name (after NFC normalization), if any."""
        ...

    async def list_organizations(self) -> list[Organization]:
        """Every organization, in the order they were added."""
        ...

    async def set_organization_disabled(self, organization_id: str, disabled: bool) -> None:
        """Raise ``KeyError`` if there is no such organization."""
        ...

    async def add_application(self, application: Application) -> None:
        """Raise ``ValueError`` if an application with that id exists, the name is used in
        its organization, or the organization does not exist or is disabled."""
        ...

    async def get_application(self, application_id: str) -> Application | None: ...

    async def list_applications(self, organization_id: str | None = None) -> list[Application]:
        """Every application (or every one of an organization), in the order they were
        added."""
        ...

    async def page_applications(
        self, organization_id: str, after: str | None, limit: int
    ) -> list[Application]:
        """Up to ``limit`` applications of an organization by id, after ``after`` (compared,
        never looked up)."""
        ...

    async def add_key(self, record: ApiKeyRecord) -> None:
        """Raise ``ValueError`` if the key id exists or the application does not."""
        ...

    async def get_key(self, key_id: str) -> ApiKeyRecord | None: ...

    async def lookup_key(self, key_id: str) -> KeyLookup | None:
        """The key's record with its application and organization state, in one lookup."""
        ...

    async def list_keys(self, application_id: str) -> list[ApiKeyRecord]: ...

    async def page_keys(
        self, application_id: str, after: str | None, limit: int
    ) -> list[ApiKeyRecord]:
        """Up to ``limit`` key records of an application by key id, after ``after``."""
        ...

    async def set_key_expiry(self, key_id: str, expires_at_ms: float) -> None: ...

    async def revoke_key(self, key_id: str) -> None: ...

    async def record_last_used(self, times_ms: Mapping[str, float]) -> None:
        """Set ``last_used_at_ms`` for several keys at once (never moving it backwards)."""
        ...


def _default_organization(created_at_ms: float) -> Organization:
    return Organization(
        DEFAULT_ORGANIZATION_ID, DEFAULT_ORGANIZATION_NAME, OrganizationKind.BUSINESS, created_at_ms
    )


def _checked_name(name: str) -> None:
    if normalize_name(name) != name:
        raise ValueError(f"name {name!r} is not in normalized (NFC) form")


class InMemoryApplicationStore:
    """Organizations, applications and key records in memory: tests and simulated live mode.

    Starts with the ``default`` organization, as a migrated database does, created at
    ``created_at_ms``."""

    def __init__(self, created_at_ms: float = 0.0) -> None:
        default = _default_organization(created_at_ms)
        self._organizations: dict[str, Organization] = {default.id: default}
        self._applications: dict[str, Application] = {}
        self._keys: dict[str, ApiKeyRecord] = {}

    @asynccontextmanager
    async def write_transaction(self) -> AsyncIterator[None]:
        # One process and no await that suspends inside a block: nothing can come between.
        yield

    async def add_organization(self, organization: Organization) -> None:
        _checked_name(organization.name)
        if organization.id in self._organizations:
            raise ValueError(f"organization {organization.id!r} exists")
        if await self.find_organization(organization.name) is not None:
            raise ValueError(f"an organization is already named {organization.name!r}")
        self._organizations[organization.id] = organization

    async def get_organization(self, organization_id: str) -> Organization | None:
        return self._organizations.get(organization_id)

    async def find_organization(self, name: str) -> Organization | None:
        normalized = normalize_name(name)
        return next((o for o in self._organizations.values() if o.name == normalized), None)

    async def list_organizations(self) -> list[Organization]:
        return list(self._organizations.values())

    async def set_organization_disabled(self, organization_id: str, disabled: bool) -> None:
        organization = self._organizations.get(organization_id)
        if organization is None:
            raise KeyError(organization_id)
        self._organizations[organization_id] = replace(organization, disabled=disabled)

    async def add_application(self, application: Application) -> None:
        _checked_name(application.name)
        if application.id in self._applications:
            raise ValueError(f"application {application.id!r} exists")
        _check_organization(self._organizations.get(application.organization_id), application)
        if any(
            a.organization_id == application.organization_id and a.name == application.name
            for a in self._applications.values()
        ):
            raise _name_taken(application)
        self._applications[application.id] = application

    async def get_application(self, application_id: str) -> Application | None:
        return self._applications.get(application_id)

    async def list_applications(self, organization_id: str | None = None) -> list[Application]:
        return [
            a
            for a in self._applications.values()
            if organization_id is None or a.organization_id == organization_id
        ]

    async def page_applications(
        self, organization_id: str, after: str | None, limit: int
    ) -> list[Application]:
        found = sorted(
            (a for a in await self.list_applications(organization_id) if a.id > (after or "")),
            key=lambda a: a.id,
        )
        return found[:limit]

    async def add_key(self, record: ApiKeyRecord) -> None:
        if record.key_id in self._keys:
            raise ValueError("key id exists")
        if record.application_id not in self._applications:
            raise ValueError(f"no application {record.application_id!r}")
        self._keys[record.key_id] = record

    async def get_key(self, key_id: str) -> ApiKeyRecord | None:
        return self._keys.get(key_id)

    async def lookup_key(self, key_id: str) -> KeyLookup | None:
        record = self._keys.get(key_id)
        if record is None:
            return None
        application = self._applications.get(record.application_id)
        organization = self._organizations.get(application.organization_id) if application else None
        enabled = organization is not None and not organization.disabled
        return KeyLookup(record, application, enabled)

    async def list_keys(self, application_id: str) -> list[ApiKeyRecord]:
        return [k for k in self._keys.values() if k.application_id == application_id]

    async def page_keys(
        self, application_id: str, after: str | None, limit: int
    ) -> list[ApiKeyRecord]:
        found = sorted(
            (k for k in await self.list_keys(application_id) if k.key_id > (after or "")),
            key=lambda k: k.key_id,
        )
        return found[:limit]

    def _update(self, key_id: str, **changes: Any) -> None:
        record = self._keys.get(key_id)
        if record is None:
            raise KeyError(key_id)
        self._keys[key_id] = replace(record, **changes)

    async def set_key_expiry(self, key_id: str, expires_at_ms: float) -> None:
        self._update(key_id, expires_at_ms=expires_at_ms)

    async def revoke_key(self, key_id: str) -> None:
        self._update(key_id, revoked=True)

    async def record_last_used(self, times_ms: Mapping[str, float]) -> None:
        for key_id, at_ms in times_ms.items():
            record = self._keys.get(key_id)
            if record is not None and (record.last_used_at_ms or 0.0) < at_ms:
                self._keys[key_id] = replace(record, last_used_at_ms=at_ms)


def _check_organization(organization: Organization | None, application: Application) -> None:
    if organization is None:
        raise ValueError(f"no organization {application.organization_id!r}")
    if organization.disabled:
        raise ValueError(
            f"organization {organization.id!r} is disabled; no application can be created in it"
        )


def _name_taken(application: Application) -> ValueError:
    return ValueError(
        f"organization {application.organization_id!r} already has an application named "
        f"{application.name!r}"
    )


_APP_COLUMNS = (
    "id, name, plan_name, models, price_multiplier, monthly_token_budget, max_tokens, "
    "requests_per_minute, created_at_ms, organization_id"
)
_KEY_COLUMNS = (
    "key_id, application_id, label, key_hmac, created_at_ms, expires_at_ms, revoked, "
    "last_used_at_ms"
)
_ORG_COLUMNS = "id, name, kind, created_at_ms, disabled"


def _application(row: tuple[Any, ...]) -> Application:
    app_id, name, plan_name, models, multiplier, budget, max_tokens, rpm, created, org = row
    return Application(
        id=app_id,
        name=name,
        plan=Plan(plan_name, tuple(json.loads(models)), multiplier, budget),
        max_tokens=max_tokens,
        requests_per_minute=rpm,
        created_at_ms=created,
        organization_id=org,
    )


def _key(row: tuple[Any, ...]) -> ApiKeyRecord:
    key_id, app_id, label, key_hmac, created, expires, revoked, last_used = row
    return ApiKeyRecord(key_id, app_id, label, key_hmac, created, expires, bool(revoked), last_used)


def _organization(row: tuple[Any, ...]) -> Organization:
    org_id, name, kind, created, disabled = row
    return Organization(org_id, name, OrganizationKind(kind), created, bool(disabled))


class SqliteApplicationStore:
    """Organizations, applications and key records in the tables of migrations 2 and 4."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        found = connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
            " AND name IN ('organizations', 'applications', 'api_keys')"
        ).fetchone()
        if found[0] != 3:
            raise RuntimeError(
                "organizations, applications and api_keys do not exist; run the migrations first"
            )
        self._connection = connection

    def write_transaction(self) -> AbstractAsyncContextManager[None]:
        return write_transaction(self._connection)

    async def add_organization(self, organization: Organization) -> None:
        o = organization
        _checked_name(o.name)
        if await self.get_organization(o.id) is not None:
            raise ValueError(f"organization {o.id!r} exists")
        if await self.find_organization(o.name) is not None:
            raise ValueError(f"an organization is already named {o.name!r}")
        self._connection.execute(
            f"INSERT INTO organizations ({_ORG_COLUMNS}) VALUES (?, ?, ?, ?, ?)",
            (o.id, o.name, o.kind.value, o.created_at_ms, int(o.disabled)),
        )

    async def get_organization(self, organization_id: str) -> Organization | None:
        row = self._connection.execute(
            f"SELECT {_ORG_COLUMNS} FROM organizations WHERE id = ?", (organization_id,)
        ).fetchone()
        return _organization(row) if row is not None else None

    async def find_organization(self, name: str) -> Organization | None:
        row = self._connection.execute(
            f"SELECT {_ORG_COLUMNS} FROM organizations WHERE name = ?", (normalize_name(name),)
        ).fetchone()
        return _organization(row) if row is not None else None

    async def list_organizations(self) -> list[Organization]:
        rows = self._connection.execute(
            f"SELECT {_ORG_COLUMNS} FROM organizations ORDER BY rowid"
        ).fetchall()
        return [_organization(row) for row in rows]

    async def set_organization_disabled(self, organization_id: str, disabled: bool) -> None:
        cursor = self._connection.execute(
            "UPDATE organizations SET disabled = ? WHERE id = ?", (int(disabled), organization_id)
        )
        if cursor.rowcount != 1:
            raise KeyError(organization_id)

    async def add_application(self, application: Application) -> None:
        a = application
        _checked_name(a.name)
        if await self.get_application(a.id) is not None:
            raise ValueError(f"application {a.id!r} exists")
        _check_organization(await self.get_organization(a.organization_id), a)
        taken = self._connection.execute(
            "SELECT 1 FROM applications WHERE organization_id = ? AND name = ?",
            (a.organization_id, a.name),
        ).fetchone()
        if taken is not None:
            raise _name_taken(a)
        self._connection.execute(
            f"INSERT INTO applications ({_APP_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                a.id,
                a.name,
                a.plan.name,
                json.dumps(list(a.plan.models)),
                a.plan.price_multiplier,
                a.plan.monthly_token_budget,
                a.max_tokens,
                a.requests_per_minute,
                a.created_at_ms,
                a.organization_id,
            ),
        )

    async def get_application(self, application_id: str) -> Application | None:
        row = self._connection.execute(
            f"SELECT {_APP_COLUMNS} FROM applications WHERE id = ?", (application_id,)
        ).fetchone()
        return _application(row) if row is not None else None

    async def list_applications(self, organization_id: str | None = None) -> list[Application]:
        if organization_id is None:
            rows = self._connection.execute(
                f"SELECT {_APP_COLUMNS} FROM applications ORDER BY rowid"
            ).fetchall()
        else:
            rows = self._connection.execute(
                f"SELECT {_APP_COLUMNS} FROM applications WHERE organization_id = ? ORDER BY rowid",
                (organization_id,),
            ).fetchall()
        return [_application(row) for row in rows]

    async def page_applications(
        self, organization_id: str, after: str | None, limit: int
    ) -> list[Application]:
        rows = self._connection.execute(
            f"SELECT {_APP_COLUMNS} FROM applications WHERE organization_id = ? AND id > ?"
            " ORDER BY id LIMIT ?",
            (organization_id, after or "", limit),
        ).fetchall()
        return [_application(row) for row in rows]

    async def add_key(self, record: ApiKeyRecord) -> None:
        r = record
        try:
            self._connection.execute(
                f"INSERT INTO api_keys ({_KEY_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    r.key_id,
                    r.application_id,
                    r.label,
                    r.key_hmac,
                    r.created_at_ms,
                    r.expires_at_ms,
                    int(r.revoked),
                    r.last_used_at_ms,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("key id exists, or no such application") from exc

    async def get_key(self, key_id: str) -> ApiKeyRecord | None:
        row = self._connection.execute(
            f"SELECT {_KEY_COLUMNS} FROM api_keys WHERE key_id = ?", (key_id,)
        ).fetchone()
        return _key(row) if row is not None else None

    async def lookup_key(self, key_id: str) -> KeyLookup | None:
        key_columns = ", ".join(f"k.{c.strip()}" for c in _KEY_COLUMNS.split(","))
        app_columns = ", ".join(f"a.{c.strip()}" for c in _APP_COLUMNS.split(","))
        row = self._connection.execute(
            f"SELECT {key_columns}, {app_columns}, o.disabled FROM api_keys AS k"
            " LEFT JOIN applications AS a ON a.id = k.application_id"
            " LEFT JOIN organizations AS o ON o.id = a.organization_id"
            " WHERE k.key_id = ?",
            (key_id,),
        ).fetchone()
        if row is None:
            return None
        key_width = len(_KEY_COLUMNS.split(","))
        application_row = row[key_width:-1]
        application = _application(application_row) if application_row[0] is not None else None
        return KeyLookup(_key(row[:key_width]), application, row[-1] == 0)

    async def list_keys(self, application_id: str) -> list[ApiKeyRecord]:
        rows = self._connection.execute(
            f"SELECT {_KEY_COLUMNS} FROM api_keys WHERE application_id = ? ORDER BY rowid",
            (application_id,),
        ).fetchall()
        return [_key(row) for row in rows]

    async def page_keys(
        self, application_id: str, after: str | None, limit: int
    ) -> list[ApiKeyRecord]:
        rows = self._connection.execute(
            f"SELECT {_KEY_COLUMNS} FROM api_keys WHERE application_id = ? AND key_id > ?"
            " ORDER BY key_id LIMIT ?",
            (application_id, after or "", limit),
        ).fetchall()
        return [_key(row) for row in rows]

    def _update(self, sql: str, params: tuple[Any, ...]) -> None:
        if self._connection.execute(sql, params).rowcount != 1:
            raise KeyError(params[-1])

    async def set_key_expiry(self, key_id: str, expires_at_ms: float) -> None:
        self._update(
            "UPDATE api_keys SET expires_at_ms = ? WHERE key_id = ?", (expires_at_ms, key_id)
        )

    async def revoke_key(self, key_id: str) -> None:
        self._update("UPDATE api_keys SET revoked = 1 WHERE key_id = ?", (key_id,))

    async def record_last_used(self, times_ms: Mapping[str, float]) -> None:
        if not times_ms:
            return
        connection = self._connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.executemany(
                "UPDATE api_keys SET last_used_at_ms = ?"
                " WHERE key_id = ? AND (last_used_at_ms IS NULL OR last_used_at_ms < ?)",
                [(at_ms, key_id, at_ms) for key_id, at_ms in times_ms.items()],
            )
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")


async def seed_demo_applications(
    store: ApplicationStore,
    templates: PlansConfig,
    *,
    created_at_ms: float,
    organization_id: str = DEFAULT_ORGANIZATION_ID,
) -> list[Application]:
    """Create the plans file's demo applications that do not exist yet, in
    ``organization_id`` (the ``default`` organization unless another is named); returns
    those made."""
    made = []
    for app_id, demo in templates.applications.items():
        if await store.get_application(app_id) is None:
            application = application_from_template(
                app_id,
                demo.name,
                templates,
                demo.plan,
                organization_id=organization_id,
                created_at_ms=created_at_ms,
            )
            await store.add_application(application)
            made.append(application)
    return made
