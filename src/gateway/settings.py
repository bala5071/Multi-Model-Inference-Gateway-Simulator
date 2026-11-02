"""Per-application settings under the platform's, the plan's and the organization's caps.

See docs/39-settings.md and docs/settings-and-alerts-plan.md, sections 1 to 5.

- **The catalogue** (S1 to S11): which settings exist, each one's type and range, which level
  may set it (an organization caps it for all its applications, an application sets its own),
  and which direction is stricter. Anything else in a document is refused.
- **Four levels**, read from the most general: the platform (the operator's values), the plan
  copy (the application's own copy of its plan), the organization, the application.
  ``effective_settings`` is the only function that decides a value: the application's value
  (or the default) clamped by every cap above it in the stricter direction. Each value carries
  its source and, when a cap lowered it, which.
- **Documents** are canonical JSON: only the settings that are set, sorted keys, no
  whitespace. A row is judged when it is read: a missing row is the defaults; a row that does
  not parse, fails validation, has a schema version this code does not know, or names a
  setting it does not have, is ``SettingsUnreadableError`` (answered 503, never a looser
  value).
- **The stores** keep the current document of each scope and every version with who, when, the
  old and the new document. A store's ``write`` is called inside its ``write_transaction()``.

Nothing here reads a clock or a store on its own: the request path gets the rows with the key
(``lookup_key``) and decides from them.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
from collections import OrderedDict
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

from gateway.database import write_transaction
from gateway.routing import Quality

if TYPE_CHECKING:
    from gateway.applications import Application

# The version of the document format. A row with a newer one was written by newer code: this
# code refuses to judge it (and an instance refuses to start on a database that has one).
SETTINGS_SCHEMA_VERSION = 1
# The versions kept per scope; older ones are dropped by the write that makes a newer one.
VERSIONS_KEPT = 200
# Parsed documents kept per instance (a version never changes, so none can be stale).
DOCUMENT_CACHE_SIZE = 4096


class Scope(StrEnum):
    ORGANIZATION = "organization"
    APPLICATION = "application"


class Source(StrEnum):
    """Where an effective value came from."""

    DEFAULT = "default"
    PLATFORM = "platform"
    PLAN = "plan"
    ORGANIZATION = "organization"
    APPLICATION = "application"


class Kind(StrEnum):
    TIERS = "tiers"
    ALIASES = "aliases"
    QUALITY = "quality"
    BOOL = "bool"
    INT = "int"


@dataclass(frozen=True, slots=True)
class Setting:
    name: str
    kind: Kind
    # Which levels may set it: an organization's value caps every application of it.
    levels: frozenset[Scope]
    minimum: int = 0
    maximum: int = 0


_BOTH = frozenset({Scope.ORGANIZATION, Scope.APPLICATION})
_APP = frozenset({Scope.APPLICATION})
MAX_ALIASES = 16
ALIAS_PATTERN = re.compile(r"[a-z0-9._-]{1,32}")
TIER_PATTERN = re.compile(r"[a-z0-9._-]{1,32}")

# S1 to S11 of the plan, in its order. S12 (the mode) has its own table; S13 to S16 come with
# PR S3; key policy is out of scope.
CATALOGUE: Mapping[str, Setting] = MappingProxyType(
    {
        s.name: s
        for s in (
            Setting("allowed_tiers", Kind.TIERS, _BOTH),
            Setting("model_aliases", Kind.ALIASES, _APP),
            Setting("default_quality", Kind.QUALITY, _APP),
            Setting("allow_downgrade_default", Kind.BOOL, _APP),
            Setting("requests_per_minute", Kind.INT, _BOTH, 1, 1_000_000),
            Setting("monthly_token_budget", Kind.INT, _BOTH, 0, 10**15),
            Setting("max_tokens", Kind.INT, _BOTH, 1, 10_000_000),
            Setting("default_max_tokens", Kind.INT, _APP, 1, 10_000_000),
            Setting("generate_timeout_ms", Kind.INT, _BOTH, 1_000, 3_600_000),
            Setting("chat_first_token_ms", Kind.INT, _BOTH, 1_000, 3_600_000),
            Setting("chat_total_timeout_ms", Kind.INT, _BOTH, 1_000, 3_600_000),
        )
    }
)

Document = Mapping[str, Any]


class SettingsInvalidError(ValueError):
    """A document that a write may not store: the setting and what is wrong, never more."""

    def __init__(self, setting: str, message: str) -> None:
        self.setting = setting
        super().__init__(f"{setting}: {message}")


class SettingsUnreadableError(Exception):
    """A stored row that cannot be judged. Its requests are refused (503), never served with a
    looser value. Says which scope, which version and which check, never the document."""

    def __init__(self, scope: Scope, scope_id: str, version: int, check: str) -> None:
        self.scope = scope
        self.scope_id = scope_id
        self.version = version
        self.check = check
        super().__init__(f"settings of {scope.value} {scope_id!r} version {version}: {check}")


def _check_value(setting: Setting, value: object) -> object:
    """``value`` in its canonical form, or ``SettingsInvalidError``."""
    name = setting.name
    if setting.kind is Kind.INT:
        if isinstance(value, bool) or not isinstance(value, int):
            raise SettingsInvalidError(name, "must be an integer")
        if not setting.minimum <= value <= setting.maximum:
            raise SettingsInvalidError(name, f"must be from {setting.minimum} to {setting.maximum}")
        return value
    if setting.kind is Kind.BOOL:
        if not isinstance(value, bool):
            raise SettingsInvalidError(name, "must be true or false")
        return value
    if setting.kind is Kind.QUALITY:
        if not isinstance(value, str) or value not in {q.value for q in Quality}:
            raise SettingsInvalidError(name, "must be cheapest, balanced or best")
        return value
    if setting.kind is Kind.TIERS:
        if not isinstance(value, list) or not value:
            raise SettingsInvalidError(name, "must be a non-empty list of tiers")
        if not all(isinstance(t, str) and TIER_PATTERN.fullmatch(t) for t in value):
            raise SettingsInvalidError(name, "must list tier names")
        if len(set(value)) != len(value):
            raise SettingsInvalidError(name, "lists a tier twice")
        return list(value)
    # Kind.ALIASES
    if not isinstance(value, dict) or not value:
        raise SettingsInvalidError(name, "must be a non-empty map of alias to tier")
    if len(value) > MAX_ALIASES:
        raise SettingsInvalidError(name, f"may have at most {MAX_ALIASES} aliases")
    for alias, target in value.items():
        if not isinstance(alias, str) or not ALIAS_PATTERN.fullmatch(alias):
            raise SettingsInvalidError(name, "an alias is 1 to 32 of a-z, 0-9, '.', '_', '-'")
        if not isinstance(target, str) or not TIER_PATTERN.fullmatch(target):
            raise SettingsInvalidError(name, "an alias names a tier")
    return dict(value)


def validate_document(scope: Scope, document: Mapping[str, object]) -> dict[str, object]:
    """Every field known, settable at ``scope``, of its type and in its range: the document's
    canonical values, or ``SettingsInvalidError``. Caps are checked separately (``refusals``)."""
    checked: dict[str, object] = {}
    for name, value in document.items():
        setting = CATALOGUE.get(name)
        if setting is None:
            raise SettingsInvalidError(name, "is not a setting")
        if scope not in setting.levels:
            raise SettingsInvalidError(name, f"cannot be set on an {scope.value}")
        checked[name] = _check_value(setting, value)
    chat_first = checked.get("chat_first_token_ms")
    chat_total = checked.get("chat_total_timeout_ms")
    if isinstance(chat_first, int) and isinstance(chat_total, int) and chat_first > chat_total:
        raise SettingsInvalidError("chat_first_token_ms", "must not exceed chat_total_timeout_ms")
    return checked


def canonical(document: Mapping[str, object]) -> str:
    """The stored text: sorted keys, no whitespace, ASCII."""
    return json.dumps(dict(document), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


# --- rows -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SettingsRow:
    """One scope's current document, as stored."""

    scope: Scope
    scope_id: str
    version: int
    document: str
    schema_version: int
    updated_at_ms: float


@dataclass(frozen=True, slots=True)
class StoredSettings:
    """The two rows a request of an application is decided on, read with its key: None where
    a scope has never been set."""

    organization: SettingsRow | None
    application: SettingsRow | None


NO_SETTINGS = StoredSettings(None, None)


@dataclass(frozen=True, slots=True)
class SettingsVersion:
    scope: Scope
    scope_id: str
    version: int
    document: str
    previous_document: str | None
    changed_fields: tuple[str, ...]
    # None for the operator's CLI.
    changed_by_user_id: str | None
    # ``cli``, ``http``, ``revert:<n>``.
    via: str
    changed_at_ms: float


def parse_row(row: SettingsRow, expected: Scope) -> Document:
    """A stored row's document, judged: ``SettingsUnreadableError`` naming the check that failed
    (``schema_version``, ``json``, ``scope``, ``validation``), never the document."""
    if row.scope is not expected:
        raise SettingsUnreadableError(row.scope, row.scope_id, row.version, "scope")
    if row.schema_version != SETTINGS_SCHEMA_VERSION:
        raise SettingsUnreadableError(row.scope, row.scope_id, row.version, "schema_version")
    try:
        parsed = json.loads(row.document)
    except (ValueError, TypeError):
        raise SettingsUnreadableError(row.scope, row.scope_id, row.version, "json") from None
    if not isinstance(parsed, dict):
        raise SettingsUnreadableError(row.scope, row.scope_id, row.version, "json")
    try:
        checked = validate_document(expected, parsed)
    except SettingsInvalidError:
        raise SettingsUnreadableError(row.scope, row.scope_id, row.version, "validation") from None
    if canonical(checked) != row.document:
        # Valid values, but not as this code writes them: written by hand or by other code.
        raise SettingsUnreadableError(row.scope, row.scope_id, row.version, "canonical")
    return MappingProxyType(checked)


class DocumentCache:
    """Parsed documents of one instance, keyed by scope, version and the text's digest: a
    version never changes, and a row rewritten in place has another digest, so nothing kept
    here can be stale. Only documents that passed are kept. Bounded, least recently used out."""

    def __init__(self, size: int = DOCUMENT_CACHE_SIZE) -> None:
        self._size = size
        self._kept: OrderedDict[tuple[str, str, int, str], Document] = OrderedDict()

    def parsed(self, row: SettingsRow, expected: Scope) -> Document:
        digest = hashlib.sha256(row.document.encode()).hexdigest()
        key = (row.scope.value, row.scope_id, row.version, digest)
        found = self._kept.get(key)
        if found is not None and row.schema_version == SETTINGS_SCHEMA_VERSION:
            self._kept.move_to_end(key)
            return found
        document = parse_row(row, expected)
        self._kept[key] = document
        if len(self._kept) > self._size:
            self._kept.popitem(last=False)
        return document

    def __len__(self) -> int:
        return len(self._kept)


# --- the effective values ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlatformSettings:
    """The operator's values on this instance: the top level, which no customer can loosen."""

    # The registered tiers, cheapest first.
    tiers: tuple[str, ...]
    # Names an alias may not take: the tiers and the registry's own aliases.
    reserved_model_names: frozenset[str]
    # None: this instance has no timeout on generate requests.
    generate_timeout_ms: float | None
    chat_first_token_ms: float
    chat_total_timeout_ms: float


@dataclass(frozen=True, slots=True)
class Effective:
    value: object
    source: Source
    # The level whose cap lowered the value below what was asked for (or the default).
    capped_by: Source | None = None


@dataclass(frozen=True, slots=True)
class EffectiveSettings:
    allowed_tiers: tuple[str, ...]
    model_aliases: Mapping[str, str]
    default_quality: Quality | None
    allow_downgrade_default: bool
    requests_per_minute: int | None
    monthly_token_budget: int
    max_tokens: int | None
    # Applied as the request's max_tokens when it names none (Q4); None: no limit applies.
    default_max_tokens: int | None
    generate_timeout_ms: float | None
    chat_first_token_ms: float
    chat_total_timeout_ms: float
    explain: Mapping[str, Effective] = field(default_factory=dict)
    # Aliases kept in the application's document but not in force: their target is not an
    # allowed tier, or the name is the registry's.
    ignored_aliases: tuple[str, ...] = ()


def _lowest(
    default: tuple[int | float | None, Source],
    caps: Sequence[tuple[int | float | None, Source]],
    own: int | None,
) -> Effective:
    """A "lower is stricter" setting: the application's value (else the default) lowered to
    every cap in force. None means no limit, the loosest of all."""
    if own is not None:
        value: int | float | None = own
        source = Source.APPLICATION
    else:
        value, source = default
    capped_by: Source | None = None
    for cap, level in caps:
        if cap is not None and (value is None or cap < value):
            value = cap
            if own is None:
                source = level
            else:
                capped_by = level
    return Effective(value, source, capped_by)


def effective_settings(
    platform: PlatformSettings,
    application: Application,
    organization: Document,
    own: Document,
) -> EffectiveSettings:
    """The values a request of ``application`` is served with: the only function that decides
    one. ``organization`` and ``own`` are the two documents, already judged (``parse_row``);
    an empty one is the defaults."""
    plan = application.plan
    explain: dict[str, Effective] = {}

    tiers = list(plan.models)
    tiers_source = Source.PLAN
    tiers_capped: Source | None = None
    asked = own.get("allowed_tiers")
    if asked is not None:
        tiers = [t for t in tiers if t in asked]
        tiers_source = Source.APPLICATION
        if len(tiers) < len(asked):
            tiers_capped = Source.PLAN
    capped = organization.get("allowed_tiers")
    if capped is not None:
        kept = [t for t in tiers if t in capped]
        if len(kept) < len(tiers):
            if asked is None:
                tiers_source = Source.ORGANIZATION
            else:
                tiers_capped = Source.ORGANIZATION
        tiers = kept
    allowed = tuple(tiers)
    explain["allowed_tiers"] = Effective(allowed, tiers_source, tiers_capped)

    aliases: dict[str, str] = {}
    ignored: list[str] = []
    for alias, target in dict(own.get("model_aliases") or {}).items():
        if target in allowed and alias not in platform.reserved_model_names:
            aliases[alias] = target
        else:
            ignored.append(alias)
    explain["model_aliases"] = Effective(
        MappingProxyType(aliases),
        Source.APPLICATION if "model_aliases" in own else Source.DEFAULT,
    )

    quality = own.get("default_quality")
    explain["default_quality"] = Effective(
        quality, Source.APPLICATION if quality is not None else Source.DEFAULT
    )
    downgrade = bool(own.get("allow_downgrade_default", False))
    explain["allow_downgrade_default"] = Effective(
        downgrade, Source.APPLICATION if "allow_downgrade_default" in own else Source.DEFAULT
    )

    def default_of(value: int | float | None, level: Source) -> tuple[int | float | None, Source]:
        return (value, level if value is not None else Source.DEFAULT)

    rpm = _lowest(
        default_of(application.requests_per_minute, Source.PLAN),
        [
            (application.requests_per_minute, Source.PLAN),
            (organization.get("requests_per_minute"), Source.ORGANIZATION),
        ],
        own.get("requests_per_minute"),
    )
    budget = _lowest(
        (plan.monthly_token_budget, Source.PLAN),
        [
            (plan.monthly_token_budget, Source.PLAN),
            (organization.get("monthly_token_budget"), Source.ORGANIZATION),
        ],
        own.get("monthly_token_budget"),
    )
    max_tokens = _lowest(
        default_of(application.max_tokens, Source.PLAN),
        [
            (application.max_tokens, Source.PLAN),
            (organization.get("max_tokens"), Source.ORGANIZATION),
        ],
        own.get("max_tokens"),
    )
    default_max = _lowest(
        (max_tokens.value, max_tokens.source),  # type: ignore[arg-type]
        [(max_tokens.value, max_tokens.source)],  # type: ignore[list-item]
        own.get("default_max_tokens"),
    )
    generate = _lowest(
        default_of(platform.generate_timeout_ms, Source.PLATFORM),
        [
            (platform.generate_timeout_ms, Source.PLATFORM),
            (organization.get("generate_timeout_ms"), Source.ORGANIZATION),
        ],
        own.get("generate_timeout_ms"),
    )
    total = _lowest(
        (platform.chat_total_timeout_ms, Source.PLATFORM),
        [
            (platform.chat_total_timeout_ms, Source.PLATFORM),
            (organization.get("chat_total_timeout_ms"), Source.ORGANIZATION),
        ],
        own.get("chat_total_timeout_ms"),
    )
    first = _lowest(
        (platform.chat_first_token_ms, Source.PLATFORM),
        [
            (platform.chat_first_token_ms, Source.PLATFORM),
            (organization.get("chat_first_token_ms"), Source.ORGANIZATION),
            # The first token never waits longer than the whole answer may.
            (total.value, total.source),  # type: ignore[list-item]
        ],
        own.get("chat_first_token_ms"),
    )
    explain.update(
        requests_per_minute=rpm,
        monthly_token_budget=budget,
        max_tokens=max_tokens,
        default_max_tokens=default_max,
        generate_timeout_ms=generate,
        chat_first_token_ms=first,
        chat_total_timeout_ms=total,
    )
    assert total.value is not None
    assert first.value is not None
    assert budget.value is not None
    return EffectiveSettings(
        allowed_tiers=allowed,
        model_aliases=MappingProxyType(aliases),
        default_quality=Quality(quality) if quality is not None else None,
        allow_downgrade_default=downgrade,
        requests_per_minute=_int_or_none(rpm.value),
        monthly_token_budget=int(budget.value),  # type: ignore[call-overload]
        max_tokens=_int_or_none(max_tokens.value),
        default_max_tokens=_int_or_none(default_max.value),
        generate_timeout_ms=_float_or_none(generate.value),
        chat_first_token_ms=float(first.value),  # type: ignore[arg-type]
        chat_total_timeout_ms=float(total.value),  # type: ignore[arg-type]
        explain=MappingProxyType(explain),
        ignored_aliases=tuple(ignored),
    )


def _int_or_none(value: object) -> int | None:
    return None if value is None else int(value)  # type: ignore[call-overload]


def _float_or_none(value: object) -> float | None:
    return None if value is None else float(value)  # type: ignore[arg-type]


def effective_application(application: Application, effective: EffectiveSettings) -> Application:
    """``application`` as a request sees it: its plan's models, budget and limits replaced by the
    effective values, so the router, the rate limit and the budget use them unchanged."""
    plan = replace(
        application.plan,
        models=effective.allowed_tiers,
        monthly_token_budget=effective.monthly_token_budget,
    )
    return replace(
        application,
        plan=plan,
        max_tokens=effective.max_tokens,
        requests_per_minute=effective.requests_per_minute,
    )


@dataclass(frozen=True, slots=True)
class Refusal:
    """A setting a write would make looser than a cap in force, and the level of that cap."""

    setting: str
    level: Source


def _above(value: object, cap: object) -> bool:
    return value is not None and cap is not None and value > cap  # type: ignore[operator]


def refusals(
    platform: PlatformSettings,
    scope: Scope,
    document: Document,
    *,
    application: Application | None = None,
    organization: Document | None = None,
) -> list[Refusal]:
    """Every setting of ``document`` (already validated) looser than a cap in force. For an
    organization's document: the platform's caps. For an application's: the platform's, its
    plan copy's and its organization's (``organization``, already judged)."""
    found: list[Refusal] = []
    org = organization if organization is not None else {}
    caps: dict[str, list[tuple[object, Source]]] = {
        "generate_timeout_ms": [(platform.generate_timeout_ms, Source.PLATFORM)],
        "chat_first_token_ms": [(platform.chat_first_token_ms, Source.PLATFORM)],
        "chat_total_timeout_ms": [(platform.chat_total_timeout_ms, Source.PLATFORM)],
    }
    if scope is Scope.ORGANIZATION:
        tiers = document.get("allowed_tiers")
        if tiers is not None and any(t not in platform.tiers for t in tiers):
            found.append(Refusal("allowed_tiers", Source.PLATFORM))
    else:
        assert application is not None
        plan = application.plan
        caps["requests_per_minute"] = [(application.requests_per_minute, Source.PLAN)]
        caps["monthly_token_budget"] = [(plan.monthly_token_budget, Source.PLAN)]
        caps["max_tokens"] = [(application.max_tokens, Source.PLAN)]
        for name, levels in caps.items():
            if name in org:
                levels.append((org[name], Source.ORGANIZATION))
        tiers = document.get("allowed_tiers")
        if tiers is not None:
            if any(t not in plan.models for t in tiers):
                found.append(Refusal("allowed_tiers", Source.PLAN))
            elif "allowed_tiers" in org and any(t not in org["allowed_tiers"] for t in tiers):
                found.append(Refusal("allowed_tiers", Source.ORGANIZATION))
        # Bounds that follow other settings: the effective values with this document in force.
        effective = effective_settings(platform, application, org, document)
        default_max = document.get("default_max_tokens")
        if _above(default_max, effective.max_tokens):
            level = effective.explain["max_tokens"].source
            found.append(Refusal("default_max_tokens", level))
        first = document.get("chat_first_token_ms")
        if first is not None and first > effective.chat_total_timeout_ms:
            found.append(
                Refusal("chat_first_token_ms", effective.explain["chat_total_timeout_ms"].source)
            )
        for alias, target in dict(document.get("model_aliases") or {}).items():
            if alias in platform.reserved_model_names:
                found.append(Refusal("model_aliases", Source.PLATFORM))
                break
            if target not in effective.allowed_tiers:
                found.append(Refusal("model_aliases", effective.explain["allowed_tiers"].source))
                break
    for name, levels in caps.items():
        value = document.get(name)
        for cap, level in levels:
            if _above(value, cap):
                found.append(Refusal(name, level))
                break
    seen: set[str] = set()
    unique = []
    for refusal in found:
        if refusal.setting not in seen:
            seen.add(refusal.setting)
            unique.append(refusal)
    return sorted(unique, key=lambda r: list(CATALOGUE).index(r.setting))


def changed_fields(before: Document, after: Document) -> tuple[str, ...]:
    """The names of the settings whose value differs, in the catalogue's order."""
    return tuple(n for n in CATALOGUE if before.get(n) != after.get(n))


def resolve(
    platform: PlatformSettings,
    application: Application,
    stored: StoredSettings | None,
    cache: DocumentCache,
) -> EffectiveSettings:
    """A request's settings, from the rows read with its key (``settings_from_rows`` of the
    plan): a missing row is the defaults; a row that cannot be judged raises
    ``SettingsUnreadableError``. ``stored`` None means the rows were not read at all, which is
    refused the same way, never taken as "no settings"."""
    if stored is None:
        raise SettingsUnreadableError(Scope.APPLICATION, application.id, 0, "not_read")
    organization: Document = {}
    own: Document = {}
    if stored.organization is not None:
        organization = cache.parsed(stored.organization, Scope.ORGANIZATION)
    if stored.application is not None:
        own = cache.parsed(stored.application, Scope.APPLICATION)
    return effective_settings(platform, application, organization, own)


# --- the stores ----------------------------------------------------------------------------------


class SettingsVersionConflictError(Exception):
    """The scope's current version is not the one the write expected."""

    def __init__(self, current: int) -> None:
        self.current = current
        super().__init__(f"the current version is {current}")


class SettingsStore(Protocol):
    def write_transaction(self) -> contextlib.AbstractAsyncContextManager[None]: ...

    async def current(self, scope: Scope, scope_id: str) -> SettingsRow | None: ...

    async def versions(self, scope: Scope, scope_id: str, limit: int) -> list[SettingsVersion]:
        """The latest ``limit`` versions, newest first."""
        ...

    async def version(
        self, scope: Scope, scope_id: str, version: int
    ) -> SettingsVersion | None: ...

    async def write(
        self,
        scope: Scope,
        scope_id: str,
        *,
        expected_version: int,
        document: str,
        changed: Sequence[str],
        changed_by_user_id: str | None,
        via: str,
        at_ms: float,
    ) -> int:
        """Store ``document`` as the scope's next version, with its version row, if the current
        version is ``expected_version`` (0 for a scope never set); else
        ``SettingsVersionConflictError`` and nothing written. Returns the new version. Called
        inside ``write_transaction()``. Keeps the latest ``VERSIONS_KEPT`` versions."""
        ...


class InMemorySettingsStore:
    """Settings in memory, for tests and simulated runs. The in-memory application store holds
    one, so that its key lookup can return the rows as the databases' join does."""

    def __init__(self) -> None:
        self._current: dict[tuple[Scope, str], SettingsRow] = {}
        self._versions: dict[tuple[Scope, str], list[SettingsVersion]] = {}

    @asynccontextmanager
    async def write_transaction(self) -> AsyncIterator[None]:
        yield

    def stored(self, organization_id: str, application_id: str) -> StoredSettings:
        """For the key lookup: not a coroutine, since the lookup it is part of is one call."""
        return StoredSettings(
            self._current.get((Scope.ORGANIZATION, organization_id)),
            self._current.get((Scope.APPLICATION, application_id)),
        )

    async def current(self, scope: Scope, scope_id: str) -> SettingsRow | None:
        return self._current.get((Scope(scope), scope_id))

    async def versions(self, scope: Scope, scope_id: str, limit: int) -> list[SettingsVersion]:
        return list(reversed(self._versions.get((Scope(scope), scope_id), [])))[:limit]

    async def version(self, scope: Scope, scope_id: str, version: int) -> SettingsVersion | None:
        found = self._versions.get((Scope(scope), scope_id), [])
        return next((v for v in found if v.version == version), None)

    async def write(
        self,
        scope: Scope,
        scope_id: str,
        *,
        expected_version: int,
        document: str,
        changed: Sequence[str],
        changed_by_user_id: str | None,
        via: str,
        at_ms: float,
    ) -> int:
        key = (Scope(scope), scope_id)
        row = self._current.get(key)
        current = row.version if row is not None else 0
        if current != expected_version:
            raise SettingsVersionConflictError(current)
        version = current + 1
        self._current[key] = SettingsRow(
            key[0], scope_id, version, document, SETTINGS_SCHEMA_VERSION, at_ms
        )
        kept = self._versions.setdefault(key, [])
        kept.append(
            SettingsVersion(
                key[0],
                scope_id,
                version,
                document,
                row.document if row is not None else None,
                tuple(changed),
                changed_by_user_id,
                via,
                at_ms,
            )
        )
        del kept[:-VERSIONS_KEPT]
        return version

    def corrupt(self, scope: Scope, scope_id: str, **changes: object) -> None:
        """Tests only: rewrite a stored row in place, as a hand edit of a database would."""
        key = (Scope(scope), scope_id)
        self._current[key] = replace(self._current[key], **changes)  # type: ignore[arg-type]


_ROW_COLUMNS = "scope_type, scope_id, version, document, schema_version, updated_at_ms"
_VERSION_COLUMNS = (
    "scope_type, scope_id, version, document, previous_document, changed_fields,"
    " changed_by_user_id, via, changed_at_ms"
)


def settings_row(values: Sequence[Any]) -> SettingsRow:
    scope, scope_id, version, document, schema_version, updated = values
    return SettingsRow(Scope(scope), scope_id, int(version), document, int(schema_version), updated)


def settings_version(values: Sequence[Any]) -> SettingsVersion:
    scope, scope_id, version, document, previous, changed, by, via, at = values
    return SettingsVersion(
        Scope(scope), scope_id, int(version), document, previous, tuple(json.loads(changed)), by,
        via, at,
    )  # fmt: skip


def joined_settings(values: Sequence[Any], organization_id: str, application_id: str) -> (
    StoredSettings
):  # fmt: skip
    """The two rows of a key lookup's join: four columns each (version, document, schema
    version, updated), all None where the scope has no row."""
    org, app = values[:4], values[4:8]
    return StoredSettings(
        SettingsRow(Scope.ORGANIZATION, organization_id, int(org[0]), org[1], int(org[2]), org[3])
        if org[0] is not None
        else None,
        SettingsRow(Scope.APPLICATION, application_id, int(app[0]), app[1], int(app[2]), app[3])
        if app[0] is not None
        else None,
    )


# The key lookup's join: the organization's and the application's rows, in the same statement.
JOIN_COLUMNS = (
    "so.version, so.document, so.schema_version, so.updated_at_ms,"
    " sa.version, sa.document, sa.schema_version, sa.updated_at_ms"
)
JOIN = (
    " LEFT JOIN settings_current AS so"
    " ON so.scope_type = 'organization' AND so.scope_id = a.organization_id"
    " LEFT JOIN settings_current AS sa"
    " ON sa.scope_type = 'application' AND sa.scope_id = a.id"
)


class SqliteSettingsStore:
    """Settings in the tables of migration 14."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def write_transaction(self) -> contextlib.AbstractAsyncContextManager[None]:
        return write_transaction(self._connection)

    async def current(self, scope: Scope, scope_id: str) -> SettingsRow | None:
        row = self._connection.execute(
            f"SELECT {_ROW_COLUMNS} FROM settings_current WHERE scope_type = ? AND scope_id = ?",
            (Scope(scope).value, scope_id),
        ).fetchone()
        return settings_row(row) if row is not None else None

    async def versions(self, scope: Scope, scope_id: str, limit: int) -> list[SettingsVersion]:
        rows = self._connection.execute(
            f"SELECT {_VERSION_COLUMNS} FROM settings_versions"
            " WHERE scope_type = ? AND scope_id = ? ORDER BY version DESC LIMIT ?",
            (Scope(scope).value, scope_id, limit),
        ).fetchall()
        return [settings_version(r) for r in rows]

    async def version(self, scope: Scope, scope_id: str, version: int) -> SettingsVersion | None:
        row = self._connection.execute(
            f"SELECT {_VERSION_COLUMNS} FROM settings_versions"
            " WHERE scope_type = ? AND scope_id = ? AND version = ?",
            (Scope(scope).value, scope_id, version),
        ).fetchone()
        return settings_version(row) if row is not None else None

    async def write(
        self,
        scope: Scope,
        scope_id: str,
        *,
        expected_version: int,
        document: str,
        changed: Sequence[str],
        changed_by_user_id: str | None,
        via: str,
        at_ms: float,
    ) -> int:
        connection = self._connection
        scope_type = Scope(scope).value
        found = connection.execute(
            "SELECT version, document FROM settings_current WHERE scope_type = ? AND scope_id = ?",
            (scope_type, scope_id),
        ).fetchone()
        current = int(found[0]) if found is not None else 0
        if current != expected_version:
            raise SettingsVersionConflictError(current)
        version = current + 1
        connection.execute(
            f"INSERT INTO settings_current ({_ROW_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (scope_type, scope_id) DO UPDATE SET version = excluded.version,"
            " document = excluded.document, schema_version = excluded.schema_version,"
            " updated_at_ms = excluded.updated_at_ms",
            (scope_type, scope_id, version, document, SETTINGS_SCHEMA_VERSION, at_ms),
        )
        connection.execute(
            f"INSERT INTO settings_versions ({_VERSION_COLUMNS})"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                scope_type,
                scope_id,
                version,
                document,
                found[1] if found is not None else None,
                json.dumps(list(changed)),
                changed_by_user_id,
                via,
                at_ms,
            ),
        )
        connection.execute(
            "DELETE FROM settings_versions WHERE scope_type = ? AND scope_id = ? AND version <= ?",
            (scope_type, scope_id, version - VERSIONS_KEPT),
        )
        return version


def reserved_names(tiers: Iterable[str], aliases: Iterable[str]) -> frozenset[str]:
    return frozenset((*tiers, *aliases))
