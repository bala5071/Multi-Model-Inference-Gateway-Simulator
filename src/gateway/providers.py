"""An organization's model providers, their sealed keys, and test-connection.

See docs/32-provider-settings-and-vault.md and docs/provider-plan.md, sections 3 and 4.

- **A provider belongs to an organization**: type (``openai_compatible`` or ``anthropic``; an
  organization never configures Ollama, which is the operator's), an ``https`` base URL checked
  by the egress guard, a model, the plan tier it can serve, a role, prices it entered, and two
  switches: the organization's and the platform's. It is enabled when both are on.
- **Its key** is sealed by the vault, bound to the organization, provider, type and URL origin.
  Changing the type or URL deletes the key in the same transaction; changing the model resets
  the probed capabilities.
- **Test-connection** runs only when an org_admin asks, spends tokens on the organization's
  account, is rate-limited per provider, and goes through the egress guard. It records each
  capability with its source, when it was probed and the model it was probed with, and answers
  only with them or a mapped error code: never the provider's text or error body.
- **A key the loaded master keys cannot read** (``key_unreadable``) is shown as such, logged as a
  warning once per provider at startup and once at first use, and never tried with another key.

Store calls are coroutines that never suspend, for the reason given in ``gateway.usage``.
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
from collections import deque
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from typing import Any, Protocol

import httpx

from gateway.adapters import AnthropicAdapter, OpenAICompatibleAdapter
from gateway.adapters.base import HttpAdapter
from gateway.audit import (
    ORGANIZATION_ID_PATTERN,
    AuditAction,
    AuditEvent,
    AuditLog,
    Outcome,
    Reason,
    TargetType,
)
from gateway.chat import (
    ChatDelta,
    ChatMessage,
    ChatRequest,
    ProviderError,
    ProviderErrorCode,
    Source,
    ToolSpec,
)
from gateway.clock import Clock, EpochSource
from gateway.database import write_transaction
from gateway.egress import always_hosted, check_base_url, egress_scope
from gateway.management import ManagementRefusal, Stores
from gateway.overload import RequestTimeoutError, run_with_timeout
from gateway.permissions import Actor, Resource
from gateway.redaction import Redactor
from gateway.vault import Binding, KeyUnreadableError, SealedKey, Vault, VaultIntegrityError

PROVIDER_TYPES = ("openai_compatible", "anthropic")
TIERS = ("economy", "standard", "premium")
ROLES = ("primary", "fallback")
PROVIDER_ID_BYTES = 8
KEY_MIN_LENGTH = 8
KEY_MAX_LENGTH = 512
TESTS_PER_HOUR = 5
TESTS_PER_ORGANIZATION_HOUR = 20
HOUR_MS = 60 * 60 * 1000.0
PROBE_TIMEOUT_MS = 30_000.0
PING = ToolSpec("ping", "Answers pong.", {"type": "object", "properties": {}})
LOG = logging.getLogger("gateway.providers")
# The failures that mean "the host was not reached": refused by the guard, no such name, no
# connection, the connection lost, no answer in time. Test-connection answers them all the
# same way, so it cannot be used to map which addresses and ports exist (docs/32); which one
# it was goes to the audit log only.
UNREACHED_DETAILS = frozenset({"connect", "connection_lost", "dns_failure"})


def unreached(exc: ProviderError) -> bool:
    """Whether a provider failure happened before any HTTP answer came back."""
    if exc.code in (ProviderErrorCode.EGRESS_REFUSED, ProviderErrorCode.UPSTREAM_TIMEOUT):
        return True
    return exc.code is ProviderErrorCode.UPSTREAM_UNAVAILABLE and exc.detail in UNREACHED_DETAILS


class ProviderUnreachedError(Exception):
    """Test-connection's one answer for every failure to reach the host."""


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    id: str
    organization_id: str
    type: str
    base_url: str
    model: str
    serves_tier: str
    role: str
    enabled: bool
    platform_enabled: bool
    price_input_per_mtok: float | None
    price_output_per_mtok: float | None
    # Each capability: {"value", "source", "probed_at_ms", "model", "reason"}.
    capabilities: Mapping[str, Mapping[str, Any]]
    first_token_budget_ms: float | None
    created_at_ms: float

    @property
    def is_enabled(self) -> bool:
        return self.enabled and self.platform_enabled

    @property
    def binding(self) -> Binding:
        return Binding(self.organization_id, self.id, self.type, self.base_url)


@dataclass(frozen=True, slots=True)
class StoredKey:
    provider_id: str
    sealed: SealedKey
    created_at_ms: float
    updated_at_ms: float


_COLUMNS = (
    "id, organization_id, type, base_url, model, serves_tier, role, enabled, platform_enabled, "
    "price_input_per_mtok, price_output_per_mtok, capabilities, first_token_budget_ms, "
    "created_at_ms"
)


def _config(row: tuple[Any, ...]) -> ProviderConfig:
    (pid, org, kind, url, model, tier, role, on, platform_on, p_in, p_out, caps, budget, at) = row
    return ProviderConfig(
        pid, org, kind, url, model, tier, role, bool(on), bool(platform_on), p_in, p_out,
        json.loads(caps), budget, at,
    )  # fmt: skip


class ProviderStore(Protocol):
    """Providers and their sealed keys (docs/34-state-interfaces.md)."""

    def write_transaction(self) -> AbstractAsyncContextManager[None]: ...

    async def add(self, config: ProviderConfig) -> None:
        """Raise ``ManagementRefusal`` (``provider_exists``) for a duplicate."""
        ...

    async def get(self, provider_id: str) -> ProviderConfig | None: ...

    async def list_for(self, organization_id: str) -> list[ProviderConfig]: ...

    async def list_all(self) -> list[ProviderConfig]: ...

    async def replace(self, config: ProviderConfig) -> None: ...

    async def delete(self, provider_id: str) -> None: ...

    async def get_key(self, provider_id: str) -> StoredKey | None: ...

    async def put_key(self, provider_id: str, sealed: SealedKey, at_ms: float) -> None: ...

    async def delete_key(self, provider_id: str) -> bool: ...

    async def all_keys(self) -> list[StoredKey]: ...


class SqliteProviderStore:
    """The tables of migrations 10 and 11."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        found = connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
            " AND name IN ('provider_configs', 'provider_keys')"
        ).fetchone()
        if found[0] != 2:
            raise RuntimeError("provider_configs and provider_keys do not exist; run migrations")
        self._connection = connection

    def write_transaction(self) -> AbstractAsyncContextManager[None]:
        return write_transaction(self._connection)

    async def add(self, config: ProviderConfig) -> None:
        c = config
        try:
            self._connection.execute(
                f"INSERT INTO provider_configs ({_COLUMNS})"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    c.id, c.organization_id, c.type, c.base_url, c.model, c.serves_tier, c.role,
                    int(c.enabled), int(c.platform_enabled), c.price_input_per_mtok,
                    c.price_output_per_mtok, json.dumps(dict(c.capabilities)),
                    c.first_token_budget_ms, c.created_at_ms,
                ),
            )  # fmt: skip
        except sqlite3.IntegrityError as exc:
            raise ManagementRefusal(
                Reason.PROVIDER_EXISTS,
                "this organization already has a provider of that type, URL and model",
            ) from exc

    async def get(self, provider_id: str) -> ProviderConfig | None:
        row = self._connection.execute(
            f"SELECT {_COLUMNS} FROM provider_configs WHERE id = ?", (provider_id,)
        ).fetchone()
        return _config(row) if row is not None else None

    async def list_for(self, organization_id: str) -> list[ProviderConfig]:
        rows = self._connection.execute(
            f"SELECT {_COLUMNS} FROM provider_configs WHERE organization_id = ? ORDER BY id",
            (organization_id,),
        ).fetchall()
        return [_config(row) for row in rows]

    async def list_all(self) -> list[ProviderConfig]:
        rows = self._connection.execute(
            f"SELECT {_COLUMNS} FROM provider_configs ORDER BY id"
        ).fetchall()
        return [_config(row) for row in rows]

    async def replace(self, config: ProviderConfig) -> None:
        c = config
        try:
            self._connection.execute(
                "UPDATE provider_configs SET type = ?, base_url = ?, model = ?, serves_tier = ?,"
                " role = ?, enabled = ?, platform_enabled = ?, price_input_per_mtok = ?,"
                " price_output_per_mtok = ?, capabilities = ?, first_token_budget_ms = ?"
                " WHERE id = ?",
                (
                    c.type, c.base_url, c.model, c.serves_tier, c.role, int(c.enabled),
                    int(c.platform_enabled), c.price_input_per_mtok, c.price_output_per_mtok,
                    json.dumps(dict(c.capabilities)), c.first_token_budget_ms, c.id,
                ),
            )  # fmt: skip
        except sqlite3.IntegrityError as exc:
            raise ManagementRefusal(
                Reason.PROVIDER_EXISTS,
                "this organization already has a provider of that type, URL and model",
            ) from exc

    async def delete(self, provider_id: str) -> None:
        self._connection.execute("DELETE FROM provider_keys WHERE provider_id = ?", (provider_id,))
        self._connection.execute("DELETE FROM provider_configs WHERE id = ?", (provider_id,))

    async def get_key(self, provider_id: str) -> StoredKey | None:
        row = self._connection.execute(
            "SELECT provider_id, key_id, nonce, ciphertext, last4, created_at_ms, updated_at_ms"
            " FROM provider_keys WHERE provider_id = ?",
            (provider_id,),
        ).fetchone()
        if row is None:
            return None
        pid, key_id, nonce, ciphertext, last4, created, updated = row
        return StoredKey(
            pid, SealedKey(key_id, bytes(nonce), bytes(ciphertext), last4), created, updated
        )

    async def put_key(self, provider_id: str, sealed: SealedKey, at_ms: float) -> None:
        """Insert or overwrite the key (in the caller's transaction, if any)."""
        self._connection.execute(
            "INSERT INTO provider_keys (provider_id, key_id, nonce, ciphertext, last4,"
            " created_at_ms, updated_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (provider_id) DO UPDATE SET key_id = excluded.key_id,"
            " nonce = excluded.nonce, ciphertext = excluded.ciphertext, last4 = excluded.last4,"
            " updated_at_ms = excluded.updated_at_ms",
            (
                provider_id,
                sealed.key_id,
                sealed.nonce,
                sealed.ciphertext,
                sealed.last4,
                at_ms,
                at_ms,
            ),
        )

    async def delete_key(self, provider_id: str) -> bool:
        cursor = self._connection.execute(
            "DELETE FROM provider_keys WHERE provider_id = ?", (provider_id,)
        )
        return cursor.rowcount == 1

    async def all_keys(self) -> list[StoredKey]:
        ids = [r[0] for r in self._connection.execute("SELECT provider_id FROM provider_keys")]
        return [k for k in [await self.get_key(i) for i in ids] if k is not None]


async def resolve_provider(
    stores: Stores, providers: ProviderStore, organization_id: str, provider_id: str
) -> Resource | None:
    """The organization, as the resource a provider action acts on, if the provider exists
    **and belongs to it**."""
    if await stores.organizations.get_organization(organization_id) is None:
        return None
    found = await providers.get(provider_id)
    if found is None or found.organization_id != organization_id:
        return None
    return Resource.organization(organization_id)


class ConnectionTestCounter(Protocol):
    """How many test-connections a provider and an organization have had in the last hour
    (docs/34-state-interfaces.md)."""

    async def count(self, provider_id: str, organization_id: str) -> None:
        """Count one test of the provider, or raise ``ManagementRefusal``
        (``test_rate_limited``) counting nothing: both windows or neither."""
        ...


class InMemoryConnectionTestCounter:
    """At most so many tests of one provider an hour, and of all an organization's providers
    together (one provider per address would get round the first), in this process's memory,
    on the injected clock."""

    def __init__(
        self,
        clock: Clock,
        tests_per_hour: int = TESTS_PER_HOUR,
        tests_per_organization_hour: int = TESTS_PER_ORGANIZATION_HOUR,
    ) -> None:
        self._clock = clock
        self._tests_per_hour = tests_per_hour
        self._tests_per_organization_hour = tests_per_organization_hour
        self._tests: dict[str, deque[float]] = {}
        self._organization_tests: dict[str, deque[float]] = {}

    async def count(self, provider_id: str, organization_id: str) -> None:
        now = self._clock.now_ms()
        windows = (
            (self._tests.setdefault(provider_id, deque()), self._tests_per_hour, "a provider"),
            (
                self._organization_tests.setdefault(organization_id, deque()),
                self._tests_per_organization_hour,
                "an organization's providers",
            ),
        )
        for times, limit, what in windows:
            while times and now - times[0] >= HOUR_MS:
                times.popleft()
            if len(times) >= limit:
                raise ManagementRefusal(
                    Reason.TEST_RATE_LIMITED,
                    f"at most {limit} tests of {what} an hour; try again later",
                )
        for times, _, _ in windows:
            times.append(now)


class ChangeMarker(Protocol):
    """What a consume-on-use handler is given to mark that it changed something (docs/32):
    ``requires``' token. Marked in the transaction that makes the change."""

    def changed(self) -> None: ...


@dataclass(frozen=True, slots=True)
class KeyState:
    present: bool
    readable: bool
    last4: str | None
    updated_at_ms: float | None


class ProviderService:
    """Providers below HTTP. ``client_factory`` makes the HTTP client test-connection uses: the
    egress guard's (``gateway.egress.organization_client``) in the server."""

    def __init__(
        self,
        store: ProviderStore,
        vault: Vault,
        audit: AuditLog,
        clock: Clock,
        epoch: EpochSource,
        redactor: Redactor,
        client_factory: Callable[[], httpx.AsyncClient],
        *,
        tests_per_hour: int = TESTS_PER_HOUR,
        tests_per_organization_hour: int = TESTS_PER_ORGANIZATION_HOUR,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
        test_counter: ConnectionTestCounter | None = None,
    ) -> None:
        self.store = store
        self.vault = vault
        self.audit = audit
        self.redactor = redactor
        self._clock = clock
        self._epoch = epoch
        self._client_factory = client_factory
        self._random_bytes = random_bytes
        self._tests: ConnectionTestCounter = (
            test_counter
            if test_counter is not None
            else InMemoryConnectionTestCounter(clock, tests_per_hour, tests_per_organization_hour)
        )
        self._warned_at_use: set[str] = set()

    def __repr__(self) -> str:
        return "ProviderService(<vault redacted>)"

    @property
    def client_factory(self) -> Callable[[], httpx.AsyncClient]:
        """A new guarded client for each call to an organization's provider (docs/33)."""
        return self._client_factory

    def _now(self) -> float:
        return self._epoch.epoch_ms()

    async def _record(
        self,
        action: AuditAction,
        config: ProviderConfig,
        actor: Actor,
        outcome: Outcome = Outcome.SUCCESS,
        **details: object,
    ) -> None:
        org = config.organization_id
        extra: dict[str, object] = {"provider_type": config.type, **details}
        if ORGANIZATION_ID_PATTERN.fullmatch(org):
            extra["organization_id"] = org
        await self.audit.write(
            AuditEvent(action, outcome, TargetType.PROVIDER, config.id, actor.user_id, extra),
            self._now(),
        )

    # --- reading ---------------------------------------------------------------------------

    async def key_state(self, config: ProviderConfig) -> KeyState:
        stored = await self.store.get_key(config.id)
        if stored is None:
            return KeyState(False, False, None, None)
        return KeyState(
            True, self.vault.readable(stored.sealed), stored.sealed.last4, stored.updated_at_ms
        )

    async def warn_unreadable(self) -> int:
        """At startup: a warning for each provider whose key no loaded master key can read (the
        provider id and the missing key id, nothing else). Returns how many."""
        count = 0
        for stored in await self.store.all_keys():
            if not self.vault.readable(stored.sealed):
                LOG.warning(
                    "provider %s: its key was sealed under master key %s, which is not loaded; "
                    "the key is unreadable until it is entered again (docs/32)",
                    stored.provider_id,
                    stored.sealed.key_id,
                )
                count += 1
        return count

    # --- changing --------------------------------------------------------------------------

    async def create(
        self,
        organization_id: str,
        *,
        type: str,
        base_url: str,
        model: str,
        serves_tier: str,
        role: str,
        price_input_per_mtok: float | None,
        price_output_per_mtok: float | None,
        first_token_budget_ms: float | None,
        actor: Actor,
    ) -> ProviderConfig:
        if type not in PROVIDER_TYPES:
            # Ollama is the operator's, configured in backends.yaml, never an organization's.
            raise ValueError(f"type must be one of {', '.join(PROVIDER_TYPES)}")
        config = ProviderConfig(
            id="prv_" + self._random_bytes(PROVIDER_ID_BYTES).hex(),
            organization_id=organization_id,
            type=type,
            base_url=check_base_url(base_url),
            model=model,
            serves_tier=serves_tier,
            role=role,
            enabled=True,
            platform_enabled=True,
            price_input_per_mtok=price_input_per_mtok,
            price_output_per_mtok=price_output_per_mtok,
            capabilities={},
            first_token_budget_ms=first_token_budget_ms,
            created_at_ms=self._now(),
        )
        await self.store.add(config)
        await self._record(AuditAction.PROVIDER_CREATED, config, actor)
        return config

    async def update(
        self, config: ProviderConfig, changes: Mapping[str, Any], *, actor: Actor
    ) -> tuple[ProviderConfig, bool]:
        """Apply ``changes``; returns the provider and whether its key was deleted (a type or
        URL change: the key is bound to them and could not be used anyway)."""
        if "type" in changes and changes["type"] not in PROVIDER_TYPES:
            raise ValueError(f"type must be one of {', '.join(PROVIDER_TYPES)}")
        if "base_url" in changes:
            changes = {**changes, "base_url": check_base_url(changes["base_url"])}
        updated = replace(config, **changes)
        rebound = (updated.type, updated.base_url) != (config.type, config.base_url)
        if updated.model != config.model or rebound:
            updated = replace(updated, capabilities={})  # probed with another model: unknown
        async with self.store.write_transaction():
            await self.store.replace(updated)
            key_deleted = await self.store.delete_key(config.id) if rebound else False
            await self._record(
                AuditAction.PROVIDER_UPDATED, updated, actor, key_deleted=key_deleted
            )
        return updated, key_deleted

    async def set_switch(
        self, config: ProviderConfig, on: bool, *, platform: bool, actor: Actor
    ) -> ProviderConfig:
        updated = replace(config, platform_enabled=on) if platform else replace(config, enabled=on)
        await self.store.replace(updated)
        await self._record(
            AuditAction.PROVIDER_ENABLED if on else AuditAction.PROVIDER_DISABLED,
            updated,
            actor,
            switch="platform" if platform else "organization",
        )
        return updated

    async def delete(self, config: ProviderConfig, *, actor: Actor, marker: ChangeMarker) -> None:
        async with self.store.write_transaction():
            await self.store.delete(config.id)
            await self._record(AuditAction.PROVIDER_DELETED, config, actor, step_up_consumed=True)
            marker.changed()

    async def set_key(
        self, config: ProviderConfig, key: str, *, actor: Actor, marker: ChangeMarker
    ) -> KeyState:
        """Seal and store ``key`` as it arrived; the API never returns it."""
        if (
            not KEY_MIN_LENGTH <= len(key) <= KEY_MAX_LENGTH
            or not key.isprintable()
            or not key.isascii()
        ):
            raise ValueError(
                f"a key is {KEY_MIN_LENGTH} to {KEY_MAX_LENGTH} printable ASCII characters"
            )
        with self.redactor.watching(key):
            sealed = self.vault.seal(key, config.binding)
        async with self.store.write_transaction():
            await self.store.put_key(config.id, sealed, self._now())
            await self._record(AuditAction.PROVIDER_KEY_SET, config, actor, step_up_consumed=True)
            marker.changed()
        return await self.key_state(config)

    async def delete_key(
        self, config: ProviderConfig, *, actor: Actor, marker: ChangeMarker
    ) -> None:
        async with self.store.write_transaction():
            if not await self.store.delete_key(config.id):
                raise ManagementRefusal(Reason.NO_KEY, "this provider has no key to delete")
            await self._record(
                AuditAction.PROVIDER_KEY_DELETED, config, actor, step_up_consumed=True
            )
            marker.changed()

    # --- test-connection -------------------------------------------------------------------

    def adapter(self, config: ProviderConfig, client: httpx.AsyncClient) -> HttpAdapter:
        kind = AnthropicAdapter if config.type == "anthropic" else OpenAICompatibleAdapter
        return kind(name=config.id, base_url=config.base_url, model=config.model, client=client)

    async def test(self, config: ProviderConfig, *, actor: Actor) -> dict[str, Any]:
        """Probe the provider with its key: one short answer (streaming), one tool round trip.
        Spends tokens on the organization's account. Returns the capabilities recorded, or
        raises ``ProviderUnreachedError`` when the host was not reached (which way goes to the
        audit only), ``ProviderError`` (mapped, never the provider's text) when it answered, or
        ``ManagementRefusal``."""
        await self._tests.count(config.id, config.organization_id)
        stored = await self.store.get_key(config.id)
        if stored is None:
            await self._record(
                AuditAction.PROVIDER_TESTED, config, actor, Outcome.FAILURE, outcome_code="no_key"
            )
            raise ManagementRefusal(Reason.NO_KEY, "this provider has no key: set one first")
        if not self.vault.readable(stored.sealed):
            if config.id not in self._warned_at_use:
                self._warned_at_use.add(config.id)
                LOG.warning(
                    "provider %s: its key is unreadable (master key %s not loaded); not used",
                    config.id,
                    stored.sealed.key_id,
                )
            await self._record(
                AuditAction.PROVIDER_TESTED,
                config,
                actor,
                Outcome.FAILURE,
                outcome_code="key_unreadable",
            )
            raise ManagementRefusal(
                Reason.KEY_UNREADABLE, "this provider's key cannot be read: enter it again"
            )
        try:
            capabilities = await self._probe(config, stored)
        except (KeyUnreadableError, VaultIntegrityError):
            await self._record(
                AuditAction.PROVIDER_TESTED,
                config,
                actor,
                Outcome.FAILURE,
                outcome_code="key_unreadable",
            )
            raise ManagementRefusal(
                Reason.KEY_UNREADABLE, "this provider's key cannot be read: enter it again"
            ) from None
        except ProviderError as exc:
            detail = {"outcome_detail": exc.detail} if unreached(exc) and exc.detail else {}
            await self._record(
                AuditAction.PROVIDER_TESTED,
                config,
                actor,
                Outcome.FAILURE,
                outcome_code=exc.code.value,
                **detail,
            )
            if unreached(exc):
                raise ProviderUnreachedError from None
            raise
        current = await self.store.get(config.id)
        if current is not None and current.model == config.model:
            await self.store.replace(replace(current, capabilities=capabilities))
        await self._record(AuditAction.PROVIDER_TESTED, config, actor, outcome_code="ok")
        return capabilities

    async def _probe(self, config: ProviderConfig, stored: StoredKey) -> dict[str, Any]:
        now = self._now()

        def flag(value: object, reason: str | None = None) -> dict[str, Any]:
            return {
                "value": value,
                "source": Source.PROBED.value if value is not None else Source.UNKNOWN.value,
                "probed_at_ms": now,
                "model": config.model,
                "reason": reason,
            }

        # An org_admin asked for this probe of a hosted provider: fixed prompts, audited,
        # whatever the organization's mode (docs/33). The guard's other rules still apply.
        with egress_scope(always_hosted):
            return await self._probe_under_scope(config, stored, flag)

    async def _probe_under_scope(
        self, config: ProviderConfig, stored: StoredKey, flag: Callable[..., dict[str, Any]]
    ) -> dict[str, Any]:
        async with self._client_factory() as client:
            adapter = self.adapter(config, client)
            with self.vault.lease(stored.sealed, config.binding, self.redactor) as key:
                deltas: list[ChatDelta] = []
                ask = ChatRequest(
                    "probe-1", config.serves_tier,
                    (ChatMessage("user", "Reply with the word OK."),), max_tokens=8,
                )  # fmt: skip
                await self._timed(adapter.chat(ask, credential=key, on_delta=deltas.append))
                streaming = flag(sum(1 for d in deltas if d.text) >= 1)
                tools: dict[str, Any]
                try:
                    called = await self._timed(
                        adapter.chat(
                            ChatRequest(
                                "probe-2", config.serves_tier,
                                (ChatMessage("user", "Call the ping tool."),),
                                tools=(PING,), max_tokens=16,
                            ),
                            credential=key,
                        )
                    )  # fmt: skip
                    if not called.tool_calls:
                        tools = flag(None, "no_tool_call_in_probe")
                    else:
                        call = called.tool_calls[0]
                        await self._timed(
                            adapter.chat(
                                ChatRequest(
                                    "probe-3", config.serves_tier,
                                    (
                                        ChatMessage("user", "Call the ping tool."),
                                        ChatMessage("assistant", None, (call,)),
                                        ChatMessage("tool", "pong", tool_call_id=call.id),
                                    ),
                                    tools=(PING,), max_tokens=8,
                                ),
                                credential=key,
                            )
                        )  # fmt: skip
                        tools = flag(True)
                except ProviderError as exc:
                    if exc.code is not ProviderErrorCode.TOOLS_NOT_SUPPORTED:
                        raise
                    tools = flag(False, exc.detail or "tools_not_supported")
        return {
            "supports_streaming": streaming,
            "supports_tools": tools,
            "context_window": flag(None, "not_reported_by_provider"),
        }

    async def _timed(self, work: Any) -> Any:
        try:
            return await run_with_timeout(self._clock, work, PROBE_TIMEOUT_MS)
        except RequestTimeoutError:
            raise ProviderError(ProviderErrorCode.UPSTREAM_TIMEOUT, detail="probe") from None
