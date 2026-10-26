"""Routing one chat request among its candidates, with fallback. See docs/33-fallback-modes.md.

The candidates for a request are the gateway's own tier it was routed to (``gateway``: of class
local, or hosted when the operator configured that tier at a destination not on its local
list) and the organization's providers **for that same tier** that are enabled and have a key,
in the order the effective mode gives:

- ``local_only``: the tier, if it is local;
- ``local_with_hosted_fallback``: the tier, then the ``fallback`` providers;
- ``hosted_primary_with_local_fallback``: the ``primary`` providers, then the tier.

A candidate that cannot take tools is left out of a request with tools. The router moves to
the next candidate only on the five triggers (``trigger``): a connection error, a 5xx, the
first token missing its budget, an open circuit breaker (skipped, never tried), and tool-call
arguments that are not a JSON object (one retry on the same candidate first). An unreadable
key also skips its provider. **Never after output has started**: the first delta passed on
closes fallback for the request, streaming or not, and a failure after it is the request's
failure. **Never another tier, never outside the plan**: only providers serving the routed
tier are candidates, and the request is passed on unchanged.

The mode is asked again before every candidate and, through the egress scope, at every request
and connection (``gateway.egress``): a mode made stricter mid-request stops the next attempt.
``gateway.modes.effective_mode`` is the only place it is decided.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum

import httpx

from gateway.adapters.base import HttpAdapter
from gateway.breaker import Breakers
from gateway.chat import (
    ChatDelta,
    ChatDeltaCallback,
    ChatRequest,
    ProviderError,
    ProviderErrorCode,
)
from gateway.chatservice import GATEWAY_PROVIDER, ChatOutcome, ChatService
from gateway.egress import LocalDestinations, egress_scope
from gateway.modes import Mode, ModeStore, effective_mode, may_reach_hosted
from gateway.providers import ProviderConfig, ProviderService
from gateway.usage import ChatSource

LOCAL = "local"
HOSTED = "hosted"
DEFAULT_HOSTED_FIRST_TOKEN_MS = 10_000.0
# Connection failures: the request never reached an answering server.
_CONNECT_DETAILS = frozenset({"connect", "connection_lost", "dns_failure"})


class FallbackReason(StrEnum):
    CONNECT_ERROR = "connect_error"
    UPSTREAM_5XX = "upstream_5xx"
    FIRST_TOKEN_BUDGET = "first_token_budget"
    CIRCUIT_OPEN = "circuit_open"
    INVALID_TOOL_JSON = "invalid_tool_json"
    KEY_UNREADABLE = "key_unreadable"


def trigger(exc: BaseException) -> FallbackReason | None:
    """The fallback trigger a failure is, or None when it is not one (an authentication
    failure, a rate limit, a 4xx, anything else): those are the request's answer."""
    if not isinstance(exc, ProviderError):
        return None
    code, detail = exc.code, exc.detail
    if code is ProviderErrorCode.EGRESS_REFUSED:
        return FallbackReason.CONNECT_ERROR
    if code is ProviderErrorCode.UPSTREAM_UNAVAILABLE:
        if detail in _CONNECT_DETAILS:
            return FallbackReason.CONNECT_ERROR
        if detail == "server_error":
            return FallbackReason.UPSTREAM_5XX
        return None
    if code is ProviderErrorCode.UPSTREAM_TIMEOUT:
        if detail == "connect":
            return FallbackReason.CONNECT_ERROR
        if detail == "first_token":
            return FallbackReason.FIRST_TOKEN_BUDGET
        return None
    if code is ProviderErrorCode.UPSTREAM_MALFORMED and detail == "tool_arguments":
        return FallbackReason.INVALID_TOOL_JSON
    return None


# The triggers that count against a provider's breaker (an open breaker is not a failure).
_BREAKER_FAILURES = frozenset(
    {
        FallbackReason.CONNECT_ERROR,
        FallbackReason.UPSTREAM_5XX,
        FallbackReason.FIRST_TOKEN_BUDGET,
        FallbackReason.INVALID_TOOL_JSON,
    }
)


@dataclass(frozen=True, slots=True)
class Candidate:
    provider: str
    provider_class: str
    config: ProviderConfig | None = None


@dataclass(slots=True)
class RouteTrace:
    """Where the request is being answered from, known once its first output is passed on.
    The streaming response reads it for its headers."""

    provider: str | None = None
    provider_class: str | None = None
    fallback_from: str | None = None
    fallback_reason: str | None = None

    def source(self, usage_estimated: bool) -> ChatSource:
        assert self.provider is not None
        assert self.provider_class is not None
        return ChatSource(
            self.provider,
            self.provider_class,
            self.fallback_from,
            self.fallback_reason,
            usage_estimated,
        )


@dataclass(frozen=True, slots=True)
class RoutedOutcome:
    outcome: ChatOutcome
    source: ChatSource


class NoCandidateError(Exception):
    """No candidate may serve this request: ``no_local_provider`` (the mode allows only local
    and the tier is not local) or ``tools_not_supported``."""

    def __init__(self, code: str, reason: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.reason = reason


class ChatRouter:
    def __init__(
        self,
        chat: ChatService,
        modes: ModeStore,
        breakers: Breakers,
        local: LocalDestinations,
        providers: ProviderService | None = None,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        if (providers is None) != (client_factory is None):
            raise ValueError("organization providers need their guarded client factory")
        self._chat = chat
        self._modes = modes
        self.breakers = breakers
        self._local = local
        self._providers = providers
        self._client_factory = client_factory

    # --- the mode, and who may serve ------------------------------------------------------

    def mode(self, organization_id: str, application_id: str) -> Mode:
        return effective_mode(
            self._modes.organization_mode(organization_id),
            self._modes.application_mode(application_id),
        )

    def tier_class(self, tier: str) -> str:
        """``local`` for the simulator and the operator's local destinations, else hosted."""
        adapter = self._chat.adapter_for(tier)
        if adapter is None:
            return LOCAL
        if isinstance(adapter, HttpAdapter) and self._local.contains_url(adapter.base_url):
            return LOCAL
        return HOSTED

    def _hosted(self, organization_id: str, tier: str, role: str) -> list[Candidate]:
        if self._providers is None:
            return []
        store = self._providers.store
        return [
            Candidate(config.id, HOSTED, config)
            for config in store.list_for(organization_id)
            if config.serves_tier == tier
            and config.role == role
            and config.is_enabled
            and store.get_key(config.id) is not None
        ]

    def _takes_tools(self, candidate: Candidate, tier: str) -> bool:
        if candidate.config is None:
            return self._chat.capabilities(tier).supports_tools is not False
        flag = candidate.config.capabilities.get("supports_tools", {})
        return flag.get("value") is not False

    def candidates(
        self, organization_id: str, application_id: str, tier: str, *, wants_tools: bool
    ) -> list[Candidate]:
        """The candidates for a request routed to ``tier``, in the order its mode gives.
        Raises ``NoCandidateError`` when none may serve it."""
        mode = self.mode(organization_id, application_id)
        local = Candidate(GATEWAY_PROVIDER, self.tier_class(tier))
        if mode is Mode.LOCAL_ONLY:
            order = [local]
        elif mode is Mode.LOCAL_WITH_HOSTED_FALLBACK:
            order = [local, *self._hosted(organization_id, tier, "fallback")]
        else:
            order = [*self._hosted(organization_id, tier, "primary"), local]
        allowed = [c for c in order if c.provider_class == LOCAL or may_reach_hosted(mode)]
        if not allowed:
            raise NoCandidateError("no_local_provider")
        if wants_tools:
            with_tools = [c for c in allowed if self._takes_tools(c, tier)]
            if not with_tools:
                reason = self._chat.capabilities(tier).reasons.get("supports_tools", "")
                raise NoCandidateError("tools_not_supported", reason)
            return with_tools
        return allowed

    # --- serving ---------------------------------------------------------------------------

    async def run(
        self,
        request: ChatRequest,
        organization_id: str,
        application_id: str,
        on_delta: ChatDeltaCallback | None = None,
        trace: RouteTrace | None = None,
    ) -> RoutedOutcome:
        trace = trace if trace is not None else RouteTrace()
        order = self.candidates(
            organization_id, application_id, request.model, wants_tools=bool(request.tools)
        )

        def hosted_now() -> bool:
            return may_reach_hosted(self.mode(organization_id, application_id))

        first = order[0].provider
        # Why the first candidate was left, then why each later one was: the first is reported.
        reasons: list[FallbackReason] = []
        current: list[Candidate] = []
        emitted = [False]

        def answered_by(candidate: Candidate) -> None:
            trace.provider = candidate.provider
            trace.provider_class = candidate.provider_class
            if candidate.provider != first:
                trace.fallback_from = first
                trace.fallback_reason = reasons[0].value if reasons else None

        def relay(delta: ChatDelta) -> None:
            if not emitted[0]:
                # The first output: from here on the request belongs to this candidate, and
                # the streaming answer's headers can say so.
                emitted[0] = True
                answered_by(current[-1])
            if on_delta is not None:
                on_delta(delta)

        failure: BaseException | None = None
        with egress_scope(hosted_now):
            for candidate in order:
                if candidate.provider_class == HOSTED and not hosted_now():
                    continue  # the mode was made stricter while this request was in flight
                if candidate.config is not None:
                    skipped = self._skip(organization_id, candidate.config)
                    if skipped is not None:
                        reasons.append(skipped)
                        continue
                current.append(candidate)
                tries = 2  # invalid tool JSON: one retry on the same candidate
                while True:
                    tries -= 1
                    try:
                        outcome = await self._attempt(candidate, request, relay)
                    except asyncio.CancelledError:
                        # The client went away: no verdict, and a half-open breaker's one
                        # probe is free again.
                        self._account(organization_id, candidate, None)
                        raise
                    except Exception as exc:
                        why = trigger(exc)
                        self._account(organization_id, candidate, why)
                        if emitted[0] or why is None:
                            raise  # output has started, or not a trigger: the answer
                        failure = exc
                        retry = why is FallbackReason.INVALID_TOOL_JSON and tries > 0
                        if retry and (
                            candidate.config is None
                            or self.breakers.admit(organization_id, candidate.provider)
                        ):
                            continue
                        reasons.append(why)
                        break
                    else:
                        self._account(organization_id, candidate, None, outcome)
                        answered_by(candidate)
                        source = trace.source(outcome.result.usage.estimated)
                        return RoutedOutcome(outcome, source)
        if failure is not None:
            raise failure
        # Every candidate was skipped (open breakers, unreadable keys, a mode change).
        raise ProviderError(ProviderErrorCode.UPSTREAM_UNAVAILABLE, detail="no_candidate")

    def _skip(self, organization_id: str, config: ProviderConfig) -> FallbackReason | None:
        assert self._providers is not None
        stored = self._providers.store.get_key(config.id)
        if stored is None or not self._providers.vault.readable(stored.sealed):
            return FallbackReason.KEY_UNREADABLE
        if not self.breakers.admit(organization_id, config.id):
            return FallbackReason.CIRCUIT_OPEN
        return None

    def _account(
        self,
        organization_id: str,
        candidate: Candidate,
        why: FallbackReason | None,
        outcome: ChatOutcome | None = None,
    ) -> None:
        """Tell the breaker and the slow-provider warning what happened (providers only)."""
        config = candidate.config
        if config is None:
            return
        if outcome is not None:
            budget = config.first_token_budget_ms or DEFAULT_HOSTED_FIRST_TOKEN_MS
            waited = outcome.timing.first_token_ms - outcome.timing.start_ms
            self.breakers.first_token(organization_id, config.id, missed=waited > budget)
            self.breakers.success(organization_id, config.id)
            return
        if why is FallbackReason.FIRST_TOKEN_BUDGET:
            self.breakers.first_token(organization_id, config.id, missed=True)
        if why in _BREAKER_FAILURES:
            self.breakers.failure(organization_id, config.id)
        else:
            self.breakers.release(organization_id, config.id)

    async def _attempt(
        self, candidate: Candidate, request: ChatRequest, relay: ChatDeltaCallback
    ) -> ChatOutcome:
        config = candidate.config
        if config is None:
            return await self._chat.run(request, relay)
        assert self._providers is not None
        assert self._client_factory is not None
        providers = self._providers
        stored = providers.store.get_key(config.id)
        if stored is None:
            raise ProviderError(ProviderErrorCode.UPSTREAM_UNAVAILABLE, detail="no_candidate")
        # A client of its own for every attempt: every request and connection goes through
        # the guard, none rides on a pooled connection (docs/33).
        async with self._client_factory() as client:
            adapter = providers.adapter(config, client)
            # The key is decrypted here and only for this one call (docs/32).
            with providers.vault.lease(stored.sealed, config.binding, providers.redactor) as key:
                outcome = await self._chat.attempt(
                    adapter,
                    request,
                    relay,
                    credential=key,
                    first_token_timeout_ms=config.first_token_budget_ms
                    or DEFAULT_HOSTED_FIRST_TOKEN_MS,
                    provider=config.id,
                )
        usage = outcome.result.usage
        cost = (
            usage.input_tokens * (config.price_input_per_mtok or 0.0)
            + usage.output_tokens * (config.price_output_per_mtok or 0.0)
        ) / 1_000_000
        return replace(outcome, timing=replace(outcome.timing, cost_usd=cost))
