"""Applications, the records of their API keys, and the stores that keep both.

See docs/20-applications-api-keys.md.

An application is who a request is for: it has its own copy of the settings of the plan it
was created from. Nothing here assumes one organization owns every application; A3 adds
organizations above applications with their own migration.

A key record holds the key's HMAC, never the key. Store methods are synchronous, for the
reason given in ``gateway.usage``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Protocol

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


def application_from_template(
    application_id: str,
    name: str,
    templates: PlansConfig,
    plan_name: str,
    *,
    created_at_ms: float,
) -> Application:
    if not application_id or not name:
        raise ValueError("an application needs an id and a name")
    if plan_name not in templates.plans:
        raise ValueError(f"unknown plan {plan_name!r}; plans are: {', '.join(templates.plans)}")
    limits = templates.limits_for(plan_name)
    return Application(
        id=application_id,
        name=name,
        plan=templates.plans[plan_name],
        max_tokens=limits.max_tokens,
        requests_per_minute=limits.requests_per_minute,
        created_at_ms=created_at_ms,
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


class ApplicationStore(Protocol):
    def add_application(self, application: Application) -> None:
        """Raise ``ValueError`` if an application with that id exists."""
        ...

    def get_application(self, application_id: str) -> Application | None: ...

    def list_applications(self) -> list[Application]:
        """Every application, in the order they were added."""
        ...

    def add_key(self, record: ApiKeyRecord) -> None:
        """Raise ``ValueError`` if the key id exists or the application does not."""
        ...

    def get_key(self, key_id: str) -> ApiKeyRecord | None: ...

    def list_keys(self, application_id: str) -> list[ApiKeyRecord]: ...

    def set_key_expiry(self, key_id: str, expires_at_ms: float) -> None: ...

    def revoke_key(self, key_id: str) -> None: ...

    def record_last_used(self, times_ms: Mapping[str, float]) -> None:
        """Set ``last_used_at_ms`` for several keys at once (never moving it backwards)."""
        ...


class InMemoryApplicationStore:
    """Applications and key records in memory: tests and simulated live mode."""

    def __init__(self) -> None:
        self._applications: dict[str, Application] = {}
        self._keys: dict[str, ApiKeyRecord] = {}

    def add_application(self, application: Application) -> None:
        if application.id in self._applications:
            raise ValueError(f"application {application.id!r} exists")
        self._applications[application.id] = application

    def get_application(self, application_id: str) -> Application | None:
        return self._applications.get(application_id)

    def list_applications(self) -> list[Application]:
        return list(self._applications.values())

    def add_key(self, record: ApiKeyRecord) -> None:
        if record.key_id in self._keys:
            raise ValueError("key id exists")
        if record.application_id not in self._applications:
            raise ValueError(f"no application {record.application_id!r}")
        self._keys[record.key_id] = record

    def get_key(self, key_id: str) -> ApiKeyRecord | None:
        return self._keys.get(key_id)

    def list_keys(self, application_id: str) -> list[ApiKeyRecord]:
        return [k for k in self._keys.values() if k.application_id == application_id]

    def _update(self, key_id: str, **changes: Any) -> None:
        record = self._keys.get(key_id)
        if record is None:
            raise KeyError(key_id)
        self._keys[key_id] = replace(record, **changes)

    def set_key_expiry(self, key_id: str, expires_at_ms: float) -> None:
        self._update(key_id, expires_at_ms=expires_at_ms)

    def revoke_key(self, key_id: str) -> None:
        self._update(key_id, revoked=True)

    def record_last_used(self, times_ms: Mapping[str, float]) -> None:
        for key_id, at_ms in times_ms.items():
            record = self._keys.get(key_id)
            if record is not None and (record.last_used_at_ms or 0.0) < at_ms:
                self._keys[key_id] = replace(record, last_used_at_ms=at_ms)


_APP_COLUMNS = (
    "id, name, plan_name, models, price_multiplier, monthly_token_budget, max_tokens, "
    "requests_per_minute, created_at_ms"
)
_KEY_COLUMNS = (
    "key_id, application_id, label, key_hmac, created_at_ms, expires_at_ms, revoked, "
    "last_used_at_ms"
)


def _application(row: tuple[Any, ...]) -> Application:
    app_id, name, plan_name, models, multiplier, budget, max_tokens, rpm, created = row
    return Application(
        id=app_id,
        name=name,
        plan=Plan(plan_name, tuple(json.loads(models)), multiplier, budget),
        max_tokens=max_tokens,
        requests_per_minute=rpm,
        created_at_ms=created,
    )


def _key(row: tuple[Any, ...]) -> ApiKeyRecord:
    key_id, app_id, label, key_hmac, created, expires, revoked, last_used = row
    return ApiKeyRecord(key_id, app_id, label, key_hmac, created, expires, bool(revoked), last_used)


class SqliteApplicationStore:
    """Applications and key records in the tables of migration 2."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        found = connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
            " AND name IN ('applications', 'api_keys')"
        ).fetchone()
        if found[0] != 2:
            raise RuntimeError("applications and api_keys do not exist; run the migrations first")
        self._connection = connection

    def add_application(self, application: Application) -> None:
        a = application
        try:
            self._connection.execute(
                f"INSERT INTO applications ({_APP_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"application {a.id!r} exists") from exc

    def get_application(self, application_id: str) -> Application | None:
        row = self._connection.execute(
            f"SELECT {_APP_COLUMNS} FROM applications WHERE id = ?", (application_id,)
        ).fetchone()
        return _application(row) if row is not None else None

    def list_applications(self) -> list[Application]:
        rows = self._connection.execute(
            f"SELECT {_APP_COLUMNS} FROM applications ORDER BY rowid"
        ).fetchall()
        return [_application(row) for row in rows]

    def add_key(self, record: ApiKeyRecord) -> None:
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

    def get_key(self, key_id: str) -> ApiKeyRecord | None:
        row = self._connection.execute(
            f"SELECT {_KEY_COLUMNS} FROM api_keys WHERE key_id = ?", (key_id,)
        ).fetchone()
        return _key(row) if row is not None else None

    def list_keys(self, application_id: str) -> list[ApiKeyRecord]:
        rows = self._connection.execute(
            f"SELECT {_KEY_COLUMNS} FROM api_keys WHERE application_id = ? ORDER BY rowid",
            (application_id,),
        ).fetchall()
        return [_key(row) for row in rows]

    def _update(self, sql: str, params: tuple[Any, ...]) -> None:
        if self._connection.execute(sql, params).rowcount != 1:
            raise KeyError(params[-1])

    def set_key_expiry(self, key_id: str, expires_at_ms: float) -> None:
        self._update(
            "UPDATE api_keys SET expires_at_ms = ? WHERE key_id = ?", (expires_at_ms, key_id)
        )

    def revoke_key(self, key_id: str) -> None:
        self._update("UPDATE api_keys SET revoked = 1 WHERE key_id = ?", (key_id,))

    def record_last_used(self, times_ms: Mapping[str, float]) -> None:
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


def seed_demo_applications(
    store: ApplicationStore, templates: PlansConfig, *, created_at_ms: float
) -> list[Application]:
    """Create the plans file's demo applications that do not exist yet; returns those made."""
    made = []
    for app_id, demo in templates.applications.items():
        if store.get_application(app_id) is None:
            application = application_from_template(
                app_id, demo.name, templates, demo.plan, created_at_ms=created_at_ms
            )
            store.add_application(application)
            made.append(application)
    return made
