"""The HTTP API. This is the only module that imports FastAPI and the only wiring point.

See docs/06-api.md, docs/09-routing.md, docs/10-overload.md,
docs/11-streaming-chunked-prefill.md, docs/12-real-models.md, docs/15-monitoring-api.md,
docs/16-dashboard.md, docs/17-playground-load-replay.md and
docs/20-applications-api-keys.md.
"""

from __future__ import annotations

import asyncio
import dataclasses
import itertools
import json
import logging
import math
import os
import random
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi import Path as Path_
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from gateway.adapters.base import HttpAdapter
from gateway.alertrecorder import MINUTE_MS, AlertRecorder
from gateway.alertrollups import (
    AlertRollups,
    InMemoryAlertRollups,
    MinuteRollup,
    RollupFlusher,
    SharedAlertRollups,
)
from gateway.alertrules import (
    DEFAULT_RULES,
    AlertRule,
    InMemoryRuleStore,
    RuleInvalidError,
    RuleService,
    RuleStore,
    resolve_rule,
    spec_body,
    validate_rule,
)
from gateway.alerts.caps import CeilingsWatch, caps_from
from gateway.alerts.channels import (
    WEBHOOK_PORTS_ENV,
    Channel,
    ChannelFieldError,
    ChannelGoneError,
    ChannelService,
    NothingChangedError,
    parse_webhook_ports,
    resolve_channel,
)
from gateway.alerts.delivery import (
    AttemptReport,
    DeliverySetup,
    DeliveryWorker,
    EmailSetup,
    TestSends,
)
from gateway.alerts.email import email_settings_from
from gateway.alerts.notices import Notices, builtin_rule_ids
from gateway.alerts.outbox import Delivery, OutboxStore
from gateway.alerts.recipients import (
    ConfirmationCaps,
    EmailUnavailableError,
    InstanceConfirmations,
    Mailer,
    Recipient,
    RecipientGoneError,
    RecipientService,
    resolve_recipient_in,
)
from gateway.alerts.smtp import RESOLVER_BUSY, SmtpMailer, SmtpSender
from gateway.alerts.webhook import DELIVERED
from gateway.apikeys import (
    INVALID_KEY_MESSAGE,
    ApiKeyRequiredError,
    ApiKeyService,
    ApplicationAccess,
    InvalidApiKeyError,
    IssuedKey,
    load_pepper,
)
from gateway.applications import (
    ApiKeyRecord,
    Application,
    ApplicationStore,
    InMemoryApplicationStore,
    seed_demo_applications,
)
from gateway.audit import (
    DEFAULT_UNKNOWN_USER_ROWS_PER_MINUTE,
    INCIDENT_ID_PATTERN,
    AuditAction,
    AuditEvent,
    AuditLog,
    Outcome,
    Reason,
    TargetType,
)
from gateway.auth import (
    ADMIN_PASSWORD_ENV,
    ADMIN_USERNAME_ENV,
    COOKIE_SECURE_ENV,
    CSRF_HEADER,
    LOGIN_FAILED_MESSAGE,
    SESSION_COOKIE,
    STEP_UP_FAILED_MESSAGE,
    STEP_UP_REQUIRED_MESSAGE,
    STEP_UP_SESSION_WINDOW_MS,
    USER_CREATE_WINDOW_MS,
    AccountAccess,
    AccountService,
    AccountWindows,
    BootstrapOutcome,
    CsrfError,
    CurrentPasswordIncorrectError,
    LoginFailedError,
    PasswordUnchangedError,
    SessionContext,
    SessionInvalidError,
    StepUpFailedError,
    StepUpRequiredError,
    audit_retention_ms,
    run_account_retention,
    session_cookie,
    step_up_settings,
    user_create_attempts,
)
from gateway.backend_config import (
    BackendConfig,
    TierBudget,
    build_chat_adapters,
    build_llamacpp_registry,
    load_backend_config,
)
from gateway.backends import Backend, BackendError
from gateway.breaker import Breakers, BreakerState, BreakerStore, SlowWarnings, Transition
from gateway.breakerreports import BreakerReports
from gateway.chat import (
    TOOL_NAME_PATTERN,
    ChatDelta,
    ChatInputError,
    ChatMessage,
    ChatRequest,
    NamedTool,
    ProviderError,
    ToolCall,
    ToolChoice,
    ToolSpec,
    json_object,
)
from gateway.chatrouter import (
    DEFAULT_MAX_HOSTED_ATTEMPTS,
    HOSTED,
    ChatRouter,
    HostedLimits,
    NoCandidateError,
    RoutedOutcome,
    RouteTrace,
    max_hosted_attempts_from,
)
from gateway.chatservice import (
    DEFAULT_FIRST_TOKEN_TIMEOUT_MS,
    DEFAULT_TOTAL_TIMEOUT_MS,
    GATEWAY_PROVIDER,
    ChatOutcome,
    ChatService,
    ChatTimeouts,
)
from gateway.clock import Clock, ClockEpoch, EpochSource, PacedSimClock, SystemEpoch, WallClock
from gateway.continuous import RequestTooLargeError, build_continuous_registry
from gateway.counters import SharedCounters
from gateway.database import ProcessLock, database_path, open_database
from gateway.egress import (
    LOCAL_ALLOW_ENV,
    LocalDestinations,
    operator_client,
    organization_client,
    provider_tls_context,
    system_resolver,
    webhook_client,
)
from gateway.evaluator import AlertEvaluator, ApplicationSignals, HeartbeatState
from gateway.events import EventBus
from gateway.incidents import (
    OPEN_STATES,
    Incident,
    IncidentState,
    IncidentStore,
    InMemoryIncidentStore,
)
from gateway.keyderivation import KeyPurpose, derive_key
from gateway.ledger import (
    ApplicationUsage,
    BudgetExceededError,
    Budgets,
    BulkUsage,
    CostLedger,
    Expired,
    ModelUsage,
    Reservation,
    SharedBudgets,
    run_retention,
    run_sweep,
)
from gateway.loadcontrol import PRESETS, LoadController, UnknownPresetError
from gateway.localonly import (
    STATE_CHANGING,
    LocalOnlyMiddleware,
    LocalOnlyPolicy,
    policy_from,
)
from gateway.management import (
    DEFAULT_GRACE_HOURS,
    DEFAULT_KEY_DAYS,
    DEFAULT_PAGE_SIZE,
    LABEL_PATTERN,
    MAX_GRACE_HOURS,
    MAX_KEY_DAYS,
    MAX_PAGE_SIZE,
    KeyLimits,
    ManagementRefusal,
    ManagementService,
    NotPermittedError,
    Stores,
    UnknownPlanError,
    resolve_application,
    resolve_application_settings_version,
    resolve_key,
    resolve_member,
    resolve_organization,
    resolve_organization_settings_version,
)
from gateway.metrics import Distribution, MetricsStore
from gateway.migrations import MIGRATIONS, MigrationError, MigrationRunner
from gateway.modes import (
    IN_FLIGHT,
    InMemoryModeStore,
    Mode,
    ModeService,
    ModeStore,
    effective_mode,
)
from gateway.monitoring import (
    DISABLED_EVALUATOR,
    AlertEvaluatorGroup,
    Monitor,
    Snapshot,
    render_prometheus,
)
from gateway.organizations import MAX_NAME_LENGTH
from gateway.overload import (
    OverloadPolicy,
    QueueFullError,
    RequestTimeoutError,
    SloMonitor,
    SloPolicy,
    pick_downgrade,
    run_with_timeout,
)
from gateway.passwords import (
    MAX_LENGTH,
    PRODUCTION,
    PasswordHasher,
    PasswordPolicyError,
    Passwords,
    ThreadHashRunner,
    parameters_from,
)
from gateway.permissions import (
    NAVIGATION_ACTIONS,
    PERMISSIONS,
    Actor,
    Level,
    Resource,
    Role,
    ScopeType,
)
from gateway.postgres import (
    PostgresBudgets,
    PostgresDatabase,
    apply_schema,
    lock_timeout_from,
    postgres_state,
)
from gateway.profiles import PROFILES, Tier, load_calibrated_profiles
from gateway.providers import (
    InMemoryConnectionTestCounter,
    ProviderConfig,
    ProviderGoneError,
    ProviderService,
    ProviderUnreachedError,
    resolve_provider,
)
from gateway.ratelimit import RateLimitedError, RateLimiter, RequestRateLimiter
from gateway.redaction import Redactor
from gateway.redaction import install as install_redaction
from gateway.registry import ModelRegistry, UnknownModelError
from gateway.routing import ModelNotAllowedError, Quality, Router, load_plans, pick_by_quality
from gateway.schema import GenerateRequest, RequestResult
from gateway.settings import (
    CATALOGUE,
    NO_SETTINGS,
    DocumentCache,
    EffectiveSettings,
    InMemorySettingsStore,
    PlatformSettings,
    Scope,
    SettingsInvalidError,
    SettingsRow,
    SettingsStore,
    SettingsUnreadableError,
    SettingsVersion,
    StoredSettings,
    caps_of,
    effective_application,
    parse_row,
    reserved_names,
    resolve,
)
from gateway.settings import PRESETS as SETTINGS_PRESETS
from gateway.settingsservice import SettingsChange, SettingsService, SettingsValidation
from gateway.signals import SignalValue
from gateway.signals import budget_used as signal_budget_used
from gateway.state import (
    POSTGRES_URL_ENV,
    STATE_ENV,
    SharedStateRefusedError,
    StateBackend,
    StateUnavailableError,
    check_state_setting,
    check_store_address,
    run_now,
    sqlite_state,
)
from gateway.timeseries import TimeSeriesStore
from gateway.usage import ChatSource, UsageStore
from gateway.usagefigures import WINDOWS as FIGURE_WINDOWS
from gateway.usagefigures import figures, window_for, window_view
from gateway.usagefigures import unavailable as unavailable_figures
from gateway.users import User
from gateway.valkey import ValkeyCounters, account_windows, shared_counters_from
from gateway.vault import Vault, load_master_keys
from gateway.workload import PlannedRequest

# How often /v1/monitor/stream pushes a snapshot, on the injected clock.
MONITOR_STREAM_INTERVAL_MS = 1000.0
PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
# The speeds the dashboard offers for simulated live mode.
SIMULATION_SPEEDS = (1.0, 5.0, 20.0)
# Where `npm run build` in frontend/ puts the dashboard. Not tracked; absent until built.
DASHBOARD_DIR = Path(__file__).resolve().parent / "dashboard"


class GenerateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Exactly one of model and quality must be given; the handler checks that.
    model: str | None = Field(default=None, min_length=1)
    quality: Quality | None = None
    prompt: str = Field(min_length=1)
    max_tokens: int | None = Field(default=None, ge=1)
    # Opt in to being served by a cheaper model when the chosen one is missing its SLO.
    allow_downgrade: bool = False
    # Answer with server-sent events, one per output token, instead of one JSON body.
    stream: bool = False


class Usage(BaseModel):
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int
    total_tokens: int


class Timing(BaseModel):
    queue_ms: float
    ttft_ms: float
    total_ms: float


class Cost(BaseModel):
    usd: float


class GenerateResponse(BaseModel):
    id: str
    text: str
    model: str
    usage: Usage
    timing: Timing
    cost: Cost


class LoadStartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preset: str = Field(min_length=1)
    # Without one, the gateway's own seed is used.
    seed: int | None = None


class SpeedBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    speed: float


class ModelInfo(BaseModel):
    id: str
    aliases: list[str]


class ModelList(BaseModel):
    models: list[ModelInfo]


class Health(BaseModel):
    status: str
    models: int
    uptime_ms: float
    # The alert evaluator's heartbeat, one word (docs/45): ok, stale, unknown or disabled.
    alert_evaluator: str


class PercentilePair(BaseModel):
    p50: float
    p99: float


class ModelMetrics(BaseModel):
    requests: int
    queue_ms: PercentilePair | None
    ttft_ms: PercentilePair | None
    total_ms: PercentilePair | None
    itl_ms: PercentilePair | None
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int
    cost_usd: float


class MetricsSummary(BaseModel):
    models: dict[str, ModelMetrics]


class DistributionStats(BaseModel):
    samples: int
    mean: float
    p50: float
    p90: float
    p99: float
    max: float


class WindowStats(BaseModel):
    window_ms: float
    span_ms: float
    requests: int
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int
    cost_usd: float
    truncated: bool
    itl_truncated: bool


class LifetimeStats(BaseModel):
    requests: int
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int
    cost_usd: float


class ModelMetricsDetail(BaseModel):
    lifetime: LifetimeStats
    window: WindowStats
    queue_ms: DistributionStats | None
    ttft_ms: DistributionStats | None
    total_ms: DistributionStats | None
    itl_ms: DistributionStats | None


class MetricsDetail(BaseModel):
    percentile_method: str
    models: dict[str, ModelMetricsDetail]


class UsageTotals(BaseModel):
    requests: int
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int
    billed_tokens: int
    base_cost_usd: float
    billed_cost_usd: float


class Period(BaseModel):
    start_ms: float
    end_ms: float


class ChatTextPart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["text"]
    text: str


class ChatFunctionCallIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=f"^{TOOL_NAME_PATTERN.pattern}$")
    arguments: str


class ChatToolCallIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    type: Literal["function"]
    function: ChatFunctionCallIn


class ChatMessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # "developer" is OpenAI's newer name for a system message.
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | list[ChatTextPart] | None = None
    tool_calls: list[ChatToolCallIn] | None = None
    tool_call_id: str | None = Field(default=None, min_length=1, max_length=128)


class ChatFunctionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=f"^{TOOL_NAME_PATTERN.pattern}$")
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})


class ChatToolIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["function"]
    function: ChatFunctionIn


class ChatToolChoiceFunction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str


class ChatToolChoiceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["function"]
    function: ChatToolChoiceFunction


class StreamOptionsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_usage: bool = False


class ChatCompletionBody(BaseModel):
    """``POST /v1/chat/completions`` in the OpenAI-compatible shape (docs/31). Any other field
    is refused by name."""

    model_config = ConfigDict(extra="forbid")

    # A tier or one of its aliases, as for /v1/generate; or a quality instead.
    model: str | None = Field(default=None, min_length=1)
    quality: Quality | None = None
    messages: list[ChatMessageIn] = Field(min_length=1)
    tools: list[ChatToolIn] | None = None
    tool_choice: Literal["auto", "none", "required"] | ChatToolChoiceIn | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    temperature: float | None = Field(default=None, ge=0, le=2, allow_inf_nan=False)
    stream: bool = False
    stream_options: StreamOptionsIn | None = None


def chat_request(body: ChatCompletionBody, request_id: str, tier: str) -> ChatRequest:
    """The request in the common chat format, or ``ChatInputError`` naming the field."""
    messages: list[ChatMessage] = []
    calls_so_far: set[str] = set()
    for i, raw in enumerate(body.messages):
        content = raw.content
        if isinstance(content, list):
            content = "".join(part.text for part in content)
        calls = tuple(
            ToolCall(c.id, c.function.name, c.function.arguments) for c in raw.tool_calls or ()
        )
        if calls and raw.role != "assistant":
            raise ChatInputError("Only an assistant message has tool calls.", f"messages[{i}]")
        for j, call in enumerate(calls):
            if json_object(call.arguments) is None:
                raise ChatInputError(
                    "Tool call arguments must be a JSON object.",
                    f"messages[{i}].tool_calls[{j}].function.arguments",
                )
        calls_so_far.update(c.id for c in calls)
        if raw.role == "tool":
            if raw.tool_call_id is None or raw.tool_call_id not in calls_so_far:
                raise ChatInputError(
                    "A tool message needs the tool_call_id of an earlier tool call.",
                    f"messages[{i}].tool_call_id",
                )
        elif raw.tool_call_id is not None:
            raise ChatInputError("Only a tool message has a tool_call_id.", f"messages[{i}]")
        role: Literal["system", "user", "assistant", "tool"] = (
            "system" if raw.role == "developer" else raw.role
        )
        messages.append(ChatMessage(role, content, calls, raw.tool_call_id))
    tools = []
    for i, tool in enumerate(body.tools or ()):
        parameters = tool.function.parameters
        if parameters.get("type") != "object":
            raise ChatInputError(
                'Tool parameters must be a JSON Schema with "type": "object".',
                f"tools[{i}].function.parameters",
            )
        tools.append(ToolSpec(tool.function.name, tool.function.description, parameters))
    choice: ToolChoice = "auto"
    if isinstance(body.tool_choice, ChatToolChoiceIn):
        if body.tool_choice.function.name not in {t.name for t in tools}:
            raise ChatInputError("tool_choice names a tool that is not given.", "tool_choice")
        choice = NamedTool(body.tool_choice.function.name)
    elif body.tool_choice is not None:
        choice = body.tool_choice
    if body.tool_choice is not None and not tools:
        raise ChatInputError("tool_choice needs tools.", "tool_choice")
    if body.stream_options is not None and not body.stream:
        raise ChatInputError("stream_options needs stream: true.", "stream_options")
    return ChatRequest(
        request_id,
        tier,
        tuple(messages),
        tuple(tools),
        choice,
        body.max_tokens,
        body.temperature,
    )


def _openai_error(
    status_code: int,
    code: str,
    message: str,
    *,
    kind: str = "invalid_request_error",
    param: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """An error in the OpenAI format (``{"error": {message, type, param, code}}``), so clients
    of that format raise their own typed errors."""
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": kind, "param": param, "code": code}},
        headers=headers,
    )


# How a chat request can fail with an answer the client should see; anything else is a bug.
CHAT_FAILURES = (ProviderError, ChatInputError, QueueFullError, RequestTimeoutError)


def _provider_error(exc: ProviderError) -> JSONResponse:
    headers = {"Retry-After": str(exc.retry_after_s)} if exc.retry_after_s is not None else None
    kind = {422: "invalid_request_error", 429: "rate_limit_error"}.get(exc.status, "api_error")
    message = exc.message + (f" ({exc.detail})" if exc.detail else "")
    return _openai_error(exc.status, exc.code.value, message, kind=kind, headers=headers)


class PlaygroundBody(GenerateBody):
    """A generate request from the dashboard's Playground in simulated live mode: the
    application it is for, instead of its key (which never leaves the server)."""

    application_id: str = Field(min_length=1)


class ApplicationUsageResponse(BaseModel):
    application_id: str
    name: str
    plan: str
    allowed_models: list[str]
    price_multiplier: float
    max_tokens: int | None
    requests_per_minute: int | None
    period: Period
    budget_tokens: int
    used_tokens: int
    remaining_tokens: int
    total: UsageTotals
    by_model: dict[str, UsageTotals]


class ModelOverload(BaseModel):
    degraded: bool
    slo_p99_ms: float | None
    recent_p99_ms: float | None
    samples: int
    waiting: int | None


class OverloadStatus(BaseModel):
    request_timeout_ms: float | None
    models: dict[str, ModelOverload]


def _pair(distribution: Distribution | None) -> PercentilePair | None:
    if distribution is None:
        return None
    return PercentilePair(p50=distribution.p50, p99=distribution.p99)


def _stats(distribution: Distribution | None) -> DistributionStats | None:
    if distribution is None:
        return None
    return DistributionStats(
        samples=distribution.samples,
        mean=distribution.mean,
        p50=distribution.p50,
        p90=distribution.p90,
        p99=distribution.p99,
        max=distribution.max,
    )


def _totals(usage: ModelUsage) -> UsageTotals:
    return UsageTotals(
        requests=usage.requests,
        prompt_tokens=usage.prompt_tokens,
        cached_tokens=usage.cached_tokens,
        completion_tokens=usage.completion_tokens,
        billed_tokens=usage.billed_tokens,
        base_cost_usd=usage.base_cost_usd,
        billed_cost_usd=usage.billed_cost_usd,
    )


def _sse(event: str, data: object, *, event_id: int | None = None) -> str:
    prefix = f"id: {event_id}\n" if event_id is not None else ""
    return f"{prefix}event: {event}\ndata: {json.dumps(data)}\n\n"


def _backend_failure(exc: BackendError) -> dict[str, object]:
    return {"code": exc.code, "message": str(exc), "backend": exc.backend, "url": exc.url}


def _error(
    status_code: int,
    code: str,
    message: str,
    *,
    headers: dict[str, str] | None = None,
    **extra: object,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, **extra}},
        headers=headers,
    )


# --- route declarations (docs/28) ---------------------------------------------------------

DECLARATION = "__gateway_declaration__"
Endpoint = TypeVar("Endpoint", bound=Callable[..., Any])
# Finds the resource a request acts on, or None if it does not exist (answered exactly as a
# forbidden one is).
Resolver = Callable[[Request], Awaitable["Resource | None"]]


@dataclass(frozen=True, slots=True)
class RouteDeclaration:
    """What a route declares: ``requires`` (checked by ``authorize`` on every request),
    ``local_only`` (an action the local-only guard enforces until A8) or ``public``."""

    kind: str
    action: str | None
    reason: str


def _declare(declaration: RouteDeclaration) -> Callable[[Endpoint], Endpoint]:
    def mark(endpoint: Endpoint) -> Endpoint:
        setattr(endpoint, DECLARATION, declaration)
        return endpoint

    return mark


def public_route(reason: str) -> Callable[[Endpoint], Endpoint]:
    """A route anyone may call, for the reason given (it may check a key or a session
    itself). Only a label: nothing runs per request."""
    return _declare(RouteDeclaration("public", None, reason))


def local_only_route(action: str) -> Callable[[Endpoint], Endpoint]:
    """A route of a declared action that the local-only guard enforces until A8 (docs/25)."""
    if action not in PERMISSIONS:
        raise ValueError(f"unknown action {action!r}")
    if PERMISSIONS[action].step_up:
        raise ValueError(f"{action} needs step-up: only requires() can enforce it (docs/29)")
    return _declare(RouteDeclaration("local_only", action, "local only until A8"))


class ForbiddenError(Exception):
    """Not permitted, or no such resource: the same answer for both."""


@dataclass(frozen=True, slots=True)
class Authorized:
    """What ``requires`` let through: who, with which session, for which action, on what;
    the role and relation that allowed it (``platform_admin@platform``, ...); and, for a
    consume-on-use action, the token its handler marks once it has changed something."""

    actor: Actor
    context: SessionContext
    action: str
    resource: Resource
    via: str = ""
    consumed: ConsumeToken | None = None


class ConsumeToken:
    """Given to the handler of a consume-on-use action (docs/32). The handler calls
    ``changed()`` in the transaction that makes its change; if the handler then fails, the
    step-up window it used stays used. If it fails before, the window is given back."""

    def __init__(self) -> None:
        self.was_changed = False

    def changed(self) -> None:
        self.was_changed = True


# The capabilities endpoint (docs/50), and how many of an organization's applications it names:
# the console's page of applications.
CAPABILITIES_PATH = "/v1/auth/capabilities"
CAPABILITY_APPLICATIONS = 100


def make_requires(service: AccountService) -> Callable[[str, Resolver], Any]:
    """``requires(action, resolver)``: a dependency that declares a route's action and
    enforces it, so a route cannot be declared without being checked. On every request it
    reads the session (401 without one), the CSRF token on a state-changing method, refuses a
    password that must be changed first, reads the actor's roles afresh, resolves the
    resource and calls ``authorize``; a resource that does not exist is refused exactly as a
    forbidden one. Then, for an action the table flags for step-up, it refuses unless this
    session's step-up window is open (docs/29): after authorization, so step-up never tells a
    forbidden resource from a missing one.

    ``authorize`` is imported here and nowhere else in this module: a test reads the source
    and fails on any other import or call of it, or any read of a session's step-up window."""
    from gateway.permissions import authorize

    def requires(action: str, resolver: Resolver) -> Any:
        if action not in PERMISSIONS:
            raise ValueError(f"unknown action {action!r}")

        consume = PERMISSIONS[action].consume

        async def check(
            request: Request, csrf: str | None = Header(default=None, alias=CSRF_HEADER)
        ) -> AsyncIterator[Authorized]:
            context = await service.authenticate(request.cookies.get(SESSION_COOKIE))
            if request.method.upper() in STATE_CHANGING:
                service.check_csrf(context, csrf)
            if context.user.must_change_password:
                raise PasswordChangeRequiredError()
            actor = await service.actor_for(context.user)
            resource = await resolver(request)
            decision = authorize(actor, action, resource) if resource is not None else None
            if resource is None or decision is None or not decision.allowed:
                raise ForbiddenError()
            if not consume:
                await service.check_step_up(context, action)
                authorized = Authorized(actor, context, action, resource, decision.reason)
                # For the refusal handler, which audits a conflict with who, what and where.
                request.state.authorized = authorized
                yield authorized
                return
            # Consume-on-use: the window is claimed (used up) before the handler, in one
            # transaction, so two requests cannot share it; it is given back only if the
            # handler fails before it has changed anything (docs/32).
            claimed = await service.claim_step_up(context, action)
            token = ConsumeToken()
            authorized = Authorized(actor, context, action, resource, decision.reason, token)
            request.state.authorized = authorized
            try:
                yield authorized
            except BaseException:
                if not token.was_changed:
                    await service.restore_step_up(context, claimed)
                raise

        setattr(check, DECLARATION, RouteDeclaration("requires", action, "authorize"))
        return Depends(check)

    @asynccontextmanager
    async def escalate(auth: Authorized, action: str) -> AsyncIterator[ConsumeToken]:
        """For a change that turns out to need more than its route's action (docs/42): the
        actor must also hold ``action`` on the same resource, and this session's step-up window
        is claimed (consume-on-use), given back if nothing is changed. The route declares it
        with ``escalates``."""
        if action not in PERMISSIONS or not PERMISSIONS[action].consume:
            raise ValueError(f"{action} is not a consume-on-use action")
        decision = authorize(auth.actor, action, auth.resource)
        if not decision.allowed:
            raise ForbiddenError()
        claimed = await service.claim_step_up(auth.context, action)
        token = ConsumeToken()
        try:
            yield token
        except BaseException:
            if not token.was_changed:
                await service.restore_step_up(auth.context, claimed)
            raise

    stores = Stores(service.organizations, service.store)

    async def capabilities(request: Request, organization_id: str | None) -> dict[str, object]:
        """What this session may do among ``NAVIGATION_ACTIONS`` (docs/50): for the platform, an
        organization (the session's own unless ``organization_id`` names another) and the
        applications in it the session can name, each action as ``authorize`` answers it. The
        same steps as ``requires``: the session (401 without one), a password that must be
        changed first refused, the actor's roles read now, the resource resolved; a missing
        organization is answered as one the session may do nothing in, byte for byte, which is
        why the organization asked for is not echoed."""
        context = await service.authenticate(request.cookies.get(SESSION_COOKIE))
        if context.user.must_change_password:
            raise PasswordChangeRequiredError()
        actor = await service.actor_for(context.user)
        asked = context.user.organization_id if organization_id is None else organization_id

        def allowed(level: Level, resource: Resource | None) -> dict[str, bool]:
            return {
                action: resource is not None and authorize(actor, action, resource).allowed
                for action in NAVIGATION_ACTIONS[level]
            }

        organization = await resolve_organization(stores, asked)
        in_organization = allowed(Level.ORGANIZATION, organization)
        named: list[Resource] = []
        more = False
        if organization is not None and in_organization["applications.list"]:
            # The organization's own page, the query the applications.list route makes.
            page = await service.organizations.page_applications(
                asked, None, CAPABILITY_APPLICATIONS + 1
            )
            more = len(page) > CAPABILITY_APPLICATIONS
            named = [Resource.application(asked, a.id) for a in page[:CAPABILITY_APPLICATIONS]]
        elif organization is not None:
            # The applications of the session's own application-scope assignments, each
            # resolved: one that is gone, or not in this organization, is not there.
            own = sorted(
                {a.scope_id for a in actor.assignments if a.scope_type is ScopeType.APPLICATION}
            )
            for application_id in own:
                found = await resolve_application(stores, asked, application_id)
                if found is not None:
                    named.append(found)
        applications = []
        for resource in named:
            actions = allowed(Level.APPLICATION, resource)
            if any(actions.values()):
                applications.append({"application_id": resource.application_id, "actions": actions})
        built_in = {role.value for role in Role}
        return {
            # The session's own built-in roles, for the header to show (decides nothing).
            "roles": [
                {"role": a.role, "scope_type": a.scope_type.value, "scope_id": a.scope_id}
                for a in actor.assignments
                if a.role in built_in
            ],
            "platform": allowed(Level.PLATFORM, Resource.platform()),
            "organization": in_organization,
            "applications": applications,
            "more_applications": more,
        }

    vars(requires)["escalate"] = escalate
    vars(requires)["capabilities"] = capabilities
    return requires


def escalates(action: str) -> Callable[[Endpoint], Endpoint]:
    """Declares the action a route escalates to when a change needs it (docs/42), beside its
    ``requires``: the route walk sees both."""
    if action not in PERMISSIONS or not PERMISSIONS[action].consume:
        raise ValueError(f"{action} is not a consume-on-use action")
    return _declare(RouteDeclaration("escalates", action, "a change that loosens egress"))


def declaration_of(route: object) -> list[RouteDeclaration]:
    """Every declaration a route carries: on its endpoint, and on its dependencies."""
    found = []
    endpoint = getattr(route, "endpoint", None)
    if hasattr(endpoint, DECLARATION):
        found.append(getattr(endpoint, DECLARATION))
    dependant = getattr(route, "dependant", None)
    for dependency in getattr(dependant, "dependencies", []):
        if hasattr(dependency.call, DECLARATION):
            found.append(getattr(dependency.call, DECLARATION))
    return found


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(max_length=256)
    password: str = Field(max_length=MAX_LENGTH)


class StepUpBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(max_length=MAX_LENGTH)


class ChangePasswordBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(max_length=MAX_LENGTH)
    new_password: str = Field(max_length=MAX_LENGTH)


class PasswordChangeRequiredError(Exception):
    """A session whose password must be changed first, on any other endpoint."""


# Answers carrying a session or its token must not be cached anywhere.
_NO_STORE = {"Cache-Control": "no-store"}
# The durable store's readiness check: whether it answers now (docs/36).
Readiness = Callable[[], Awaitable[bool]]
# A shared reservation outlives its request's own time by this much before the sweep releases
# it (docs/37).
RESERVATION_MARGIN_MS = 60_000.0


class Durable(Protocol):
    """A durable store that waits on a network (Postgres): opened by the server's lifespan
    before anything is served, closed after everything else."""

    async def open(self) -> None: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class Keyed:
    """A keyed request's caller: the application as the request sees it (its effective
    settings applied) and those settings, decided once from the rows read with the key."""

    application: Application
    settings: EffectiveSettings
    # The application as stored (its plan copy), for deciding the settings again (docs/42).
    stored: Application


class _Hold:
    """One admitted request's reservation (docs/37), ended exactly once: settled when the
    request bills, released by whatever ends it otherwise."""

    def __init__(self, budgets: Budgets, reservation: Reservation) -> None:
        self._budgets = budgets
        self._reservation = reservation
        self._ended = False
        # Set when a stream takes the request over: the stream ends the hold, not the handler.
        self.streaming = False

    async def settle(
        self,
        result: RequestResult,
        *,
        model: str,
        price_multiplier: float,
        source: ChatSource | None = None,
    ) -> float:
        self._ended = True
        return await self._budgets.settle(
            self._reservation,
            result,
            model=model,
            price_multiplier=price_multiplier,
            source=source,
        )

    async def release(self) -> None:
        if self._ended:
            return
        self._ended = True
        if self._reservation.id is None:
            return  # nothing is held (one process's ledger)
        # Shielded: a stream the client left is cancelled, and every await in it is cancelled
        # again; the release still runs to its end (else the sweep would release it later).
        await asyncio.shield(self._budgets.release(self._reservation))


def _auth_routes(app: FastAPI, accounts: AccountAccess) -> None:
    """Console login under /v1/auth (docs/27). Added only when ``create_app`` is given
    accounts; until the roles of step 5, a session reaches nothing else."""
    service = accounts.service

    def with_cookie(response: JSONResponse, value: str, *, clear: bool = False) -> JSONResponse:
        response.headers.append(
            "set-cookie", session_cookie(value, secure=accounts.cookie_secure, clear=clear)
        )
        return response

    async def session(request: Request) -> SessionContext:
        return await service.authenticate(request.cookies.get(SESSION_COOKIE))

    async def session_with_csrf(
        context: SessionContext = Depends(session),
        csrf: str | None = Header(default=None, alias=CSRF_HEADER),
    ) -> SessionContext:
        service.check_csrf(context, csrf)
        return context

    def user_view(context: SessionContext) -> dict[str, object]:
        user = context.user
        return {
            "user_id": user.id,
            "username": user.username,
            "organization_id": user.organization_id,
            "must_change_password": user.must_change_password,
        }

    app.state.requires = make_requires(service)

    @app.exception_handler(ForbiddenError)
    async def forbidden(_: Request, exc: ForbiddenError) -> JSONResponse:
        # One answer for "not permitted" and "does not exist", byte for byte (docs/28).
        return _error(403, "forbidden", "Not permitted, or not found.", headers=_NO_STORE)

    @app.exception_handler(NotPermittedError)
    async def not_permitted(_: Request, exc: NotPermittedError) -> JSONResponse:
        # A service's own check of the record, behind requires: the same answer (docs/30).
        return _error(403, "forbidden", "Not permitted, or not found.", headers=_NO_STORE)

    @app.exception_handler(ManagementRefusal)
    async def refused(request: Request, exc: ManagementRefusal) -> JSONResponse:
        # Every conflict and cap is audited: who, which action, which organization, why.
        authorized: Authorized | None = getattr(request.state, "authorized", None)
        if authorized is not None:
            await service.record_refusal(
                authorized.actor,
                authorized.action,
                authorized.resource.organization_id,
                exc.reason,
            )
        limited = (
            Reason.USER_CREATION_LIMITED, Reason.TEST_RATE_LIMITED, Reason.TEST_SEND_LIMITED,
            Reason.CONFIRMATION_LIMITED,
        )  # fmt: skip
        status = 429 if exc.reason in limited else 409
        return _error(status, exc.reason.value, str(exc), headers=_NO_STORE)

    @app.exception_handler(StepUpFailedError)
    async def step_up_failed(_: Request, exc: StepUpFailedError) -> JSONResponse:
        # One answer for every refused step-up, byte for byte (docs/29).
        return _error(403, "step_up_failed", STEP_UP_FAILED_MESSAGE, headers=_NO_STORE)

    @app.exception_handler(StepUpRequiredError)
    async def step_up_required(_: Request, exc: StepUpRequiredError) -> JSONResponse:
        return _error(403, "step_up_required", STEP_UP_REQUIRED_MESSAGE, headers=_NO_STORE)

    @app.exception_handler(LoginFailedError)
    async def login_failed(_: Request, exc: LoginFailedError) -> JSONResponse:
        # One answer, byte for byte, for every refused login (docs/27).
        return _error(401, "invalid_credentials", LOGIN_FAILED_MESSAGE, headers=_NO_STORE)

    @app.exception_handler(SessionInvalidError)
    async def session_invalid(_: Request, exc: SessionInvalidError) -> JSONResponse:
        return _error(401, "session_required", "Log in first.", headers=_NO_STORE)

    @app.exception_handler(CsrfError)
    async def csrf_failed(_: Request, exc: CsrfError) -> JSONResponse:
        return _error(
            403,
            "csrf_failed",
            f"Send the session's CSRF token in {CSRF_HEADER} (login and /v1/auth/me give it).",
        )

    @app.exception_handler(PasswordChangeRequiredError)
    async def change_required(_: Request, exc: PasswordChangeRequiredError) -> JSONResponse:
        return _error(
            403,
            "password_change_required",
            "Change the password first: POST /v1/auth/change-password.",
        )

    @app.exception_handler(CurrentPasswordIncorrectError)
    async def wrong_current(_: Request, exc: CurrentPasswordIncorrectError) -> JSONResponse:
        return _error(403, "current_password_incorrect", "The current password is not correct.")

    @app.exception_handler(PasswordPolicyError)
    async def rejected(_: Request, exc: PasswordPolicyError) -> JSONResponse:
        return _error(400, "password_rejected", str(exc), problem=exc.problem.value)

    @app.exception_handler(PasswordUnchangedError)
    async def unchanged(_: Request, exc: PasswordUnchangedError) -> JSONResponse:
        return _error(400, "password_rejected", str(exc), problem="same_as_current")

    @app.post("/v1/auth/login")
    @public_route("console login: the route checks the password")
    async def login(body: LoginBody) -> JSONResponse:
        # A session cookie sent with the request is ignored: every login makes a new session.
        result = await service.login(body.username, body.password)
        context = SessionContext(result.user, result.session, result.session_id)
        response = JSONResponse(
            {"user": user_view(context), "csrf_token": result.csrf_token}, headers=_NO_STORE
        )
        return with_cookie(response, result.session_id)

    @app.post("/v1/auth/logout")
    @public_route("the session's own endpoint: the route checks the session itself")
    async def logout(context: SessionContext = Depends(session_with_csrf)) -> JSONResponse:
        await service.logout(context)
        return with_cookie(JSONResponse({"logged_out": True}, headers=_NO_STORE), "", clear=True)

    @app.get("/v1/auth/me")
    @public_route("the session's own endpoint: the route checks the session itself")
    async def me(context: SessionContext = Depends(session)) -> JSONResponse:
        return JSONResponse(
            {
                "user": user_view(context),
                "csrf_token": service.csrf_token(context),
                "step_up_until_ms": service.step_up_until(context),
            },
            headers=_NO_STORE,
        )

    @app.get(CAPABILITIES_PATH)
    @public_route("the session's own endpoint: make_requires answers it with authorize")
    async def capabilities(
        request: Request, organization: str | None = Query(default=None, max_length=64)
    ) -> JSONResponse:
        # What the console's navigation shows (docs/50). Hiding is a convenience: every action
        # is still checked by its own route.
        answer = await app.state.requires.capabilities(request, organization)
        return JSONResponse(answer, headers=_NO_STORE)

    @app.post("/v1/auth/step-up")
    @public_route("the session's own endpoint: the route checks the session and the password")
    async def step_up(
        body: StepUpBody, context: SessionContext = Depends(session_with_csrf)
    ) -> JSONResponse:
        # The session and its CSRF token (and, before them, the Origin check of the
        # local-only guard) are checked before this runs: without them no password is verified.
        until = await service.step_up(context, body.password)
        return JSONResponse({"step_up_until_ms": until}, headers=_NO_STORE)

    @app.post("/v1/auth/change-password")
    @public_route("the session's own endpoint: the route checks the session itself")
    async def change_password(
        body: ChangePasswordBody, context: SessionContext = Depends(session_with_csrf)
    ) -> JSONResponse:
        revoked = await service.change_password(context, body.current_password, body.new_password)
        return JSONResponse({"changed": True, "sessions_revoked": revoked}, headers=_NO_STORE)

    @app.get("/v1/auth/sessions")
    @public_route("the session's own endpoint: the route checks the session itself")
    async def sessions(context: SessionContext = Depends(session)) -> JSONResponse:
        if context.user.must_change_password:
            raise PasswordChangeRequiredError()
        live = await service.live_sessions(context)
        return JSONResponse(
            {
                "sessions": [
                    {
                        "handle": s.handle,
                        "created_at_ms": s.created_at_ms,
                        "last_seen_at_ms": s.last_seen_at_ms,
                        "expires_at_ms": s.expires_at_ms,
                        "current": s.id_hash == context.session.id_hash,
                    }
                    for s in live
                ]
            },
            headers=_NO_STORE,
        )


class CreateUserBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # No password: the server makes a temporary one (docs/30).
    username: str = Field(pattern=r"^[A-Za-z0-9._-]{3,64}$")


class CreateApplicationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    # The name of a plan template in the plans file, never raw numbers.
    plan: str = Field(min_length=1, max_length=64)


class IssueKeyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(pattern=f"^{LABEL_PATTERN.pattern}$")
    expires_in_days: float = Field(
        default=DEFAULT_KEY_DAYS, ge=1, le=MAX_KEY_DAYS, allow_inf_nan=False
    )


class RotateKeyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grace_hours: float = Field(
        default=DEFAULT_GRACE_HOURS, ge=0, le=MAX_GRACE_HOURS, allow_inf_nan=False
    )
    expires_in_days: float = Field(
        default=DEFAULT_KEY_DAYS, ge=1, le=MAX_KEY_DAYS, allow_inf_nan=False
    )


ADMIN_PREFIX = "/v1/admin"
ORG_PREFIX = ADMIN_PREFIX + "/orgs/{org}"


class NoStoreMiddleware:
    """``Cache-Control: no-store`` on every answer under ``prefix``, errors and validation
    failures included (docs/30): an admin answer may carry a key or a temporary password, and
    every other one names users, applications and keys."""

    def __init__(self, app: Any, prefix: str = ADMIN_PREFIX) -> None:
        self.app = app
        self.prefix = prefix

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if not (path == self.prefix or path.startswith(self.prefix + "/")):
            await self.app(scope, receive, send)
            return

        async def no_store(message: Any) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"
                ]
                message = {**message, "headers": [*headers, (b"cache-control", b"no-store")]}
            await send(message)

        await self.app(scope, receive, no_store)


class _DurableSignals:
    """The signals the evaluator reads from the stores (docs/45), for one application: its
    budget used, each allowed provider's breaker, each active key's expiry. A store that cannot
    answer gives "unavailable", never a value."""

    def __init__(
        self,
        applications: ApplicationStore,
        usage: Callable[[str], Awaitable[ApplicationUsage]],
        settings_of: Callable[[Application], Awaitable[EffectiveSettings]],
        providers: ProviderService | None,
        breakers: BreakerStore,
        epoch: EpochSource,
        *,
        usage_many: Callable[[Sequence[str]], Awaitable[dict[str, ApplicationUsage]]] | None = None,
        settings_many: Callable[[Sequence[Application]], Awaitable[dict[str, EffectiveSettings]]]
        | None = None,
    ) -> None:
        """``usage_many`` and ``settings_many`` read a page at once; without them ``read_many``
        reads each application with ``usage`` and ``settings_of``."""
        self._applications = applications
        self._usage = usage
        self._settings_of = settings_of
        self._providers = providers
        self._breakers = breakers
        self._epoch = epoch
        self._usage_many_of = usage_many
        self._settings_many_of = settings_many

    async def budget_used(self, application_id: str) -> SignalValue:
        application = await self._applications.get_application(application_id)
        if application is None:
            return SignalValue(None, "unavailable")
        try:
            settings = await self._settings_of(application)
        except SettingsUnreadableError:
            return SignalValue(None, "unavailable")
        usage = await self._usage(application_id)
        return signal_budget_used(usage.total.billed_tokens, settings.monthly_token_budget)

    async def circuit_open(
        self, organization_id: str, application_id: str
    ) -> dict[str, SignalValue]:
        """1 for a breaker open or half-open, 0 for closed; the evaluator turns that into the
        minutes it has seen it so."""
        if self._providers is None:
            return {}
        application = await self._applications.get_application(application_id)
        if application is None:
            return {}
        try:
            allowed = (await self._settings_of(application)).allowed_providers
        except SettingsUnreadableError:
            allowed = None
        found: dict[str, SignalValue] = {}
        for config in await self._providers.store.list_for(organization_id):
            if not config.enabled or (allowed is not None and config.id not in allowed):
                continue
            try:
                state = await self._breakers.state(organization_id, config.id)
            except StateUnavailableError:
                found[config.id] = SignalValue(None, "unavailable")
                continue
            found[config.id] = SignalValue(0.0 if state is BreakerState.CLOSED else 1.0)
        return found

    async def key_expires_in(self, application_id: str) -> dict[str, SignalValue]:
        now = self._epoch.epoch_ms()
        return {
            key.key_id: SignalValue((key.expires_at_ms - now) / 86_400_000.0)
            for key in await self._applications.list_keys(application_id)
            if not key.revoked and key.expires_at_ms is not None
        }

    async def reservation_expired(self, application_id: str) -> SignalValue:
        # Counted by each instance's own sweep, so no instance knows the whole count: not
        # judged in A3 (docs/45); the default rule is off.
        return SignalValue(None, "unavailable")

    async def read_many(
        self, wanted: Mapping[str, frozenset[str]]
    ) -> dict[str, ApplicationSignals]:
        """``BulkSignals``: the values above for a page of applications, each kind read once for
        the page (the plan's section 11.7): the applications, both settings rows of each, the
        usage, the keys, and each organization's providers and their breakers once."""
        ids = list(wanted)
        applications = await self._applications.get_applications(ids)
        live = [a for a in ids if a in applications]

        def asking(signal: str) -> list[str]:
            return [a for a in live if signal in wanted[a]]

        budget_ids, circuit_ids, key_ids = (
            asking("budget_used"),
            asking("circuit_open") if self._providers is not None else [],
            asking("key_expires_in"),
        )
        settings = await self._settings_many(
            [applications[a] for a in dict.fromkeys(budget_ids + circuit_ids)]
        )
        usage = await self._usage_many(budget_ids) if budget_ids else {}
        keys = await self._applications.keys_of(key_ids) if key_ids else {}
        providers: dict[str, list[ProviderConfig]] = {}
        breakers: dict[tuple[str, str], SignalValue] = {}
        now = self._epoch.epoch_ms()
        found: dict[str, ApplicationSignals] = {}
        for application_id in ids:
            application = applications.get(application_id)
            if application is None:
                found[application_id] = ApplicationSignals(None)
                continue
            organization_id = application.organization_id
            effective = settings.get(application_id)
            budget = SignalValue(None, "unavailable")
            if application_id in usage and effective is not None:
                budget = signal_budget_used(
                    usage[application_id].total.billed_tokens, effective.monthly_token_budget
                )
            circuit: dict[str, SignalValue] = {}
            if application_id in circuit_ids:
                assert self._providers is not None
                allowed = effective.allowed_providers if effective is not None else None
                if organization_id not in providers:
                    providers[organization_id] = await self._providers.store.list_for(
                        organization_id
                    )
                for config in providers[organization_id]:
                    if not config.enabled or (allowed is not None and config.id not in allowed):
                        continue
                    if (organization_id, config.id) not in breakers:
                        breakers[(organization_id, config.id)] = await self._breaker(
                            organization_id, config.id
                        )
                    circuit[config.id] = breakers[(organization_id, config.id)]
            expiries = {
                key.key_id: SignalValue((key.expires_at_ms - now) / 86_400_000.0)
                for key in keys.get(application_id, [])
                if not key.revoked and key.expires_at_ms is not None
            }
            found[application_id] = ApplicationSignals(
                organization_id, budget, circuit, expiries, SignalValue(None, "unavailable")
            )
        return found

    async def _breaker(self, organization_id: str, provider_id: str) -> SignalValue:
        try:
            state = await self._breakers.state(organization_id, provider_id)
        except StateUnavailableError:
            return SignalValue(None, "unavailable")
        return SignalValue(0.0 if state is BreakerState.CLOSED else 1.0)

    async def _settings_many(
        self, applications: Sequence[Application]
    ) -> dict[str, EffectiveSettings]:
        """Each application's effective settings; one that cannot be judged is left out (its
        values are "unavailable", as ``budget_used`` and ``circuit_open`` say)."""
        if not applications:
            return {}
        if self._settings_many_of is not None:
            return await self._settings_many_of(applications)
        found: dict[str, EffectiveSettings] = {}
        for application in applications:
            with suppress(SettingsUnreadableError):
                found[application.id] = await self._settings_of(application)
        return found

    async def _usage_many(self, application_ids: Sequence[str]) -> dict[str, ApplicationUsage]:
        if self._usage_many_of is not None:
            return await self._usage_many_of(application_ids)
        return {a: await self._usage(a) for a in application_ids}


SettingsOf = Callable[[Application], Awaitable[EffectiveSettings]]
SettingsMany = Callable[[Sequence[Application]], Awaitable[dict[str, EffectiveSettings]]]


def alert_settings(
    store: SettingsStore | None, platform: PlatformSettings
) -> tuple[SettingsOf, SettingsMany]:
    """The alert evaluator's settings reads (docs/45): one application's effective settings,
    and a page's (both scopes' rows in two reads, one that cannot be judged left out)."""
    documents = DocumentCache()

    async def settings_of(application: Application) -> EffectiveSettings:
        if store is None:
            return resolve(platform, application, NO_SETTINGS, documents)
        stored = StoredSettings(
            await store.current(Scope.ORGANIZATION, application.organization_id),
            await store.current(Scope.APPLICATION, application.id),
        )
        return resolve(platform, application, stored, documents)

    async def settings_many(applications: Sequence[Application]) -> dict[str, EffectiveSettings]:
        organizations: dict[str, SettingsRow] = {}
        own: dict[str, SettingsRow] = {}
        if store is not None:
            organizations = await store.current_many(
                Scope.ORGANIZATION, {a.organization_id for a in applications}
            )
            own = await store.current_many(Scope.APPLICATION, [a.id for a in applications])
        found: dict[str, EffectiveSettings] = {}
        for application in applications:
            stored = StoredSettings(
                organizations.get(application.organization_id), own.get(application.id)
            )
            with suppress(SettingsUnreadableError):
                found[application.id] = resolve(platform, application, stored, documents)
        return found

    return settings_of, settings_many


@dataclass
class AlertNote:
    """What the handlers learn about one request for the alert recorder (docs/43): whose it
    is, how long it took, and for a chat request its route."""

    kind: str
    application_id: str | None = None
    ttft_ms: float | None = None
    total_ms: float | None = None
    downgraded: bool = False
    trace: RouteTrace | None = None


# The requests the alert recorder is told about, and their kind.
ALERT_PATHS = {"/v1/generate": "generate", "/v1/chat/completions": "chat"}
# Enough of an error body for its code: every error answer is small.
_ERROR_BODY_BYTES = 4096


def _error_code(body: bytes) -> str | None:
    try:
        found = json.loads(body)["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return None
    return found if isinstance(found, str) else None


class AlertOutcomeMiddleware:
    """Tells the alert recorder how each keyed request of ``ALERT_PATHS`` ended (docs/43): its
    status, the code of an error answer, and what the handlers noted. When the last of the
    answer is sent, so a stream is recorded at its end; a request whose client went away
    before the end is not. A request with no application (no key, an invalid key) is not
    recorded. The recorder does no I/O and never awaits."""

    def __init__(self, app: Any, recorder: AlertRecorder, note: ContextVar[AlertNote | None]):
        self.app = app
        self.recorder = recorder
        self.note = note

    def _record(self, note: AlertNote, status: int, body: bytes) -> None:
        if note.application_id is None:
            return
        trace = note.trace
        self.recorder.record(
            note.application_id,
            kind=note.kind,
            status=status,
            code=_error_code(body) if status >= 400 else None,
            ttft_ms=note.ttft_ms,
            total_ms=note.total_ms,
            fallbacks=(trace.fallback_reason or "",)
            if trace is not None and trace.fallback_from is not None
            else (),
            hosted_attempts=trace.hosted_attempts if trace is not None else 0,
            hosted=trace is not None and trace.provider_class == HOSTED,
            downgraded=note.downgraded,
        )

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        kind = (
            ALERT_PATHS.get(scope.get("path", ""))
            if scope["type"] == "http" and scope.get("method") == "POST"
            else None
        )
        if kind is None:
            await self.app(scope, receive, send)
            return
        note = AlertNote(kind)
        token = self.note.set(note)
        status = [500]
        body = bytearray()
        done = [False]

        async def watch(message: Any) -> None:
            if message["type"] == "http.response.start":
                status[0] = int(message["status"])
            elif message["type"] == "http.response.body":
                if status[0] >= 400 and len(body) < _ERROR_BODY_BYTES:
                    body.extend(message.get("body", b""))
                if not message.get("more_body", False) and not done[0]:
                    done[0] = True
                    self._record(note, status[0], bytes(body))
            await send(message)

        try:
            await self.app(scope, receive, watch)
        except Exception:
            if not done[0]:  # answered 500 by the server's own handler
                done[0] = True
                self._record(note, 500, b"")
            raise
        finally:
            self.note.reset(token)


def _page_body(name: str, items: list[dict[str, object]], next_cursor: str | None) -> JSONResponse:
    return JSONResponse({name: items, "next": next_cursor})


def _admin_routes(
    app: FastAPI,
    accounts: AccountAccess,
    management: ManagementService | None,
    modes: ModeStore | None = None,
) -> None:
    """The management endpoints under /v1/admin/orgs/{org} (docs/30): users, and with
    applications, applications and their keys. Every one through ``requires`` with a
    resolver that answers a resource outside the path's organization as missing."""
    service = accounts.service
    requires = app.state.requires
    stores = Stores(service.organizations, service.store)

    async def organization(request: Request) -> Resource | None:
        return await resolve_organization(stores, request.path_params["org"])

    async def member(request: Request) -> Resource | None:
        p = request.path_params
        return await resolve_member(stores, p["org"], p["username"])

    # Each route's declaration: its action and the resolver of what it acts on (docs/30).
    creating_users = requires("users.create", organization)
    listing_users = requires("users.list", organization)
    disabling_users = requires("users.disable", member)
    enabling_users = requires("users.enable", member)
    resetting_passwords = requires("users.reset_password", member)
    unlocking_users = requires("users.unlock", member)

    def user_view(user: User, now_ms: float) -> dict[str, object]:
        return {
            "user_id": user.id,
            "username": user.username,
            "organization_id": user.organization_id,
            "disabled": user.disabled,
            "must_change_password": user.must_change_password,
            "locked": service.is_locked(user, now_ms),
            "created_at_ms": user.created_at_ms,
        }

    @app.post(ORG_PREFIX + "/users")
    async def create_user(
        org: str, body: CreateUserBody, auth: Authorized = creating_users
    ) -> JSONResponse:
        await service.count_user_creation(auth.context)
        user, temporary = await service.create_user_with_temporary_password(
            body.username, org, actor=auth.actor
        )
        # The one place the temporary password is shown; never logged or stored.
        return JSONResponse(
            {"user": user_view(user, auth.context.now_ms), "temporary_password": temporary},
            status_code=201,
        )

    @app.get(ORG_PREFIX + "/users")
    async def list_users(
        org: str,
        limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
        after: str | None = Query(default=None, max_length=64),
        auth: Authorized = listing_users,
    ) -> JSONResponse:
        page = await service.page_users(org, after, limit, actor=auth.actor)
        return _page_body(
            "users", [user_view(u, auth.context.now_ms) for u in page.items], page.next
        )

    @app.post(ORG_PREFIX + "/users/{username}/disable")
    async def disable_user(username: str, auth: Authorized = disabling_users) -> JSONResponse:
        revoked = await service.disable_user(username, actor=auth.actor)
        return JSONResponse({"disabled": True, "sessions_revoked": revoked})

    @app.post(ORG_PREFIX + "/users/{username}/enable")
    async def enable_user(username: str, auth: Authorized = enabling_users) -> JSONResponse:
        await service.enable_user(username, actor=auth.actor)
        return JSONResponse({"enabled": True})

    @app.post(ORG_PREFIX + "/users/{username}/reset-password")
    async def reset_password(username: str, auth: Authorized = resetting_passwords) -> JSONResponse:
        revoked, temporary = await service.reset_to_temporary_password(username, actor=auth.actor)
        # The one place the temporary password is shown; never logged or stored.
        return JSONResponse({"temporary_password": temporary, "sessions_revoked": revoked})

    @app.post(ORG_PREFIX + "/users/{username}/unlock")
    async def unlock_user(username: str, auth: Authorized = unlocking_users) -> JSONResponse:
        await service.unlock_user(username, actor=auth.actor)
        return JSONResponse({"unlocked": True})

    if management is None:
        return
    keys = management

    async def application(request: Request) -> Resource | None:
        p = request.path_params
        return await resolve_application(stores, p["org"], p["app"])

    async def key(request: Request) -> Resource | None:
        p = request.path_params
        return await resolve_key(stores, p["org"], p["app"], p["key_id"])

    creating_applications = requires("applications.create", organization)
    # Who may create an application may see what it can be created from (docs/51).
    reading_plans = requires("applications.create", organization)
    listing_applications = requires("applications.list", organization)
    reading_applications = requires("applications.read", application)
    issuing_keys = requires("application_keys.issue", application)
    listing_keys = requires("application_keys.list", application)
    rotating_keys = requires("application_keys.rotate", key)
    revoking_keys = requires("application_keys.revoke", key)

    def application_view(a: Application) -> dict[str, object]:
        return {
            "application_id": a.id,
            "name": a.name,
            "organization_id": a.organization_id,
            "plan": a.plan.name,
            "allowed_models": list(a.plan.models),
            "max_tokens": a.max_tokens,
            "requests_per_minute": a.requests_per_minute,
            "created_at_ms": a.created_at_ms,
        }

    async def with_modes(found: Sequence[Application]) -> list[dict[str, object]]:
        """Each application's view with the mode in force for it (docs/51): the stricter of
        its organization's and its own, as ``effective_mode`` decides it for a request."""
        if not found:
            return []
        store = modes if modes is not None else InMemoryModeStore()
        organization_mode = await store.organization_mode(found[0].organization_id)
        return [
            {
                **application_view(a),
                "mode": effective_mode(organization_mode, await store.application_mode(a.id)).value,
            }
            for a in found
        ]

    def key_view(record: ApiKeyRecord, now_ms: float) -> dict[str, object]:
        # Never the key, never its HMAC.
        return {
            "key_id": record.key_id,
            "label": record.label,
            "state": keys.key_state(record, now_ms),
            "created_at_ms": record.created_at_ms,
            "expires_at_ms": record.expires_at_ms,
            "last_used_at_ms": record.last_used_at_ms,
        }

    def issued_view(issued: IssuedKey, now_ms: float) -> JSONResponse:
        # The one place a new key is shown; never logged or stored.
        return JSONResponse({"key": issued.key, **key_view(issued.record, now_ms)}, status_code=201)

    @app.post(ORG_PREFIX + "/applications")
    async def create_application(
        org: str,
        body: CreateApplicationBody,
        auth: Authorized = creating_applications,
    ) -> JSONResponse:
        try:
            made = await keys.create_application(org, body.name, body.plan, actor=auth.actor)
        except UnknownPlanError as exc:
            return _invalid("plan", str(exc))
        except ManagementRefusal:
            raise
        except ValueError as exc:  # the name: normalize_name says why, never echoing more
            return _invalid("name", str(exc))
        return JSONResponse((await with_modes([made]))[0], status_code=201)

    @app.get(ORG_PREFIX + "/plans")
    async def list_plans(org: str, auth: Authorized = reading_plans) -> JSONResponse:
        """The plan templates an application can be created from, for the Add application form
        (docs/51): names, models and limits; the plans file holds no key."""
        return JSONResponse(
            {
                "plans": [
                    {
                        "name": plan.name,
                        "allowed_models": list(plan.models),
                        "monthly_token_budget": plan.monthly_token_budget,
                        "max_tokens": limits.max_tokens,
                        "requests_per_minute": limits.requests_per_minute,
                    }
                    for plan, limits in keys.plan_templates(org, actor=auth.actor)
                ]
            }
        )

    @app.get(ORG_PREFIX + "/applications")
    async def list_applications(
        org: str,
        limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
        after: str | None = Query(default=None, max_length=64),
        auth: Authorized = listing_applications,
    ) -> JSONResponse:
        page = await keys.page_applications(org, after, limit, actor=auth.actor)
        return _page_body("applications", await with_modes(page.items), page.next)

    @app.get(ORG_PREFIX + "/applications/{app}")
    async def read_application(
        app_id: str = Path_(alias="app"),
        auth: Authorized = reading_applications,
    ) -> JSONResponse:
        found = await keys.read_application(app_id, actor=auth.actor)
        return JSONResponse((await with_modes([found]))[0])

    @app.post(ORG_PREFIX + "/applications/{app}/keys")
    async def issue_key(
        body: IssueKeyBody,
        app_id: str = Path_(alias="app"),
        auth: Authorized = issuing_keys,
    ) -> JSONResponse:
        issued = await keys.issue_key(
            app_id, body.label, actor=auth.actor, expires_in_days=body.expires_in_days
        )
        return issued_view(issued, auth.context.now_ms)

    @app.get(ORG_PREFIX + "/applications/{app}/keys")
    async def list_keys(
        app_id: str = Path_(alias="app"),
        limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
        after: str | None = Query(default=None, max_length=64),
        auth: Authorized = listing_keys,
    ) -> JSONResponse:
        page = await keys.page_keys(app_id, after, limit, actor=auth.actor)
        return _page_body("keys", [key_view(k, auth.context.now_ms) for k in page.items], page.next)

    @app.post(ORG_PREFIX + "/applications/{app}/keys/{key_id}/rotate")
    async def rotate_key(
        key_id: str,
        body: RotateKeyBody,
        auth: Authorized = rotating_keys,
    ) -> JSONResponse:
        issued = await keys.rotate_key(
            key_id,
            grace_hours=body.grace_hours,
            actor=auth.actor,
            expires_in_days=body.expires_in_days,
        )
        return issued_view(issued, auth.context.now_ms)

    @app.post(ORG_PREFIX + "/applications/{app}/keys/{key_id}/revoke")
    async def revoke_key(key_id: str, auth: Authorized = revoking_keys) -> JSONResponse:
        await keys.revoke_key(key_id, actor=auth.actor)
        return JSONResponse({"key_id": key_id, "state": "revoked"})


def _usage_routes(
    app: FastAPI,
    accounts: AccountAccess,
    management: ManagementService,
    rollups: AlertRollups,
    epoch: EpochSource,
    started_ms: float | None,
    clock: Clock,
) -> None:
    """The organization's usage figures for the console's Applications list (docs/51): one
    page of its applications, the list's own paging, each with its figures over the window from
    the alert recorder's rollups. Through ``requires`` (``usage.read_organization``); a missing
    organization is refused as a forbidden one. Ids, minutes and numbers only."""
    requires = app.state.requires
    service = accounts.service
    stores = Stores(service.organizations, service.store)

    async def organization(request: Request) -> Resource | None:
        return await resolve_organization(stores, request.path_params["org"])

    reading_usage = requires("usage.read_organization", organization)

    @app.get(ORG_PREFIX + "/metrics")
    async def organization_metrics(
        org: str,
        window: Literal["60m"] = Query(default="60m"),
        limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
        after: str | None = Query(default=None, max_length=64),
        auth: Authorized = reading_usage,
    ) -> JSONResponse:
        page = await management.page_for_usage(org, after, limit, actor=auth.actor)
        span = window_for(epoch.epoch_ms(), FIGURE_WINDOWS[window], started_ms)
        try:
            read: list[list[MinuteRollup]] | None = await rollups.read_many(
                [(a.id, span.minute_range) for a in page.items]
            )
        except StateUnavailableError:
            read = None  # Valkey down: every figure says so, none is made up
        rows = []
        for i, a in enumerate(page.items):
            found = unavailable_figures() if read is None else figures(a.id, read[i], span)
            rows.append({"application_id": a.id, **found})
            # The event loop back between two applications, as the evaluator does (docs/45):
            # a page's figures never hold up the requests being served.
            await clock.sleep_ms(0.0)
        return JSONResponse({"window": window_view(span), "applications": rows, "next": page.next})


class CreateProviderBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # openai_compatible or anthropic; Ollama is the operator's and is refused here (docs/32).
    type: str = Field(min_length=1, max_length=32)
    base_url: str = Field(min_length=1, max_length=2048)
    model: str = Field(min_length=1, max_length=200)
    serves_tier: Literal["economy", "standard", "premium"]
    role: Literal["primary", "fallback"] = "fallback"
    price_input_per_mtok: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    price_output_per_mtok: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    first_token_budget_ms: float | None = Field(default=None, gt=0, le=600_000, allow_inf_nan=False)


class UpdateProviderBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str | None = Field(default=None, min_length=1, max_length=32)
    base_url: str | None = Field(default=None, min_length=1, max_length=2048)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    serves_tier: Literal["economy", "standard", "premium"] | None = None
    role: Literal["primary", "fallback"] | None = None
    price_input_per_mtok: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    price_output_per_mtok: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    first_token_budget_ms: float | None = Field(default=None, gt=0, le=600_000, allow_inf_nan=False)


class SetProviderKeyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Checked by the service (length and characters), whose refusal never repeats it.
    key: str


class SetModeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Mode


def _mode_routes(app: FastAPI, accounts: AccountAccess, modes: ModeService) -> None:
    """``PUT .../mode`` for an organization and for one of its applications (docs/33), through
    ``requires`` with consume-on-use step-up: each change needs its own password."""
    service = accounts.service
    requires = app.state.requires
    stores = Stores(service.organizations, service.store)

    async def organization(request: Request) -> Resource | None:
        return await resolve_organization(stores, request.path_params["org"])

    async def application(request: Request) -> Resource | None:
        p = request.path_params
        return await resolve_application(stores, p["org"], p["app"])

    setting_organization = requires("mode.set_organization", organization)
    setting_application = requires("mode.set_application", application)

    @app.put(ORG_PREFIX + "/mode")
    async def set_organization_mode(
        org: str, body: SetModeBody, auth: Authorized = setting_organization
    ) -> JSONResponse:
        assert auth.consumed is not None
        change = await modes.set_organization(
            org, body.mode, actor=auth.actor, marker=auth.consumed
        )
        return JSONResponse(
            {
                "organization_id": org,
                "mode": change.mode,
                "previous_mode": change.previous,
                "in_flight": IN_FLIGHT,
            }
        )

    @app.put(ORG_PREFIX + "/applications/{app}/mode")
    async def set_application_mode(
        org: str,
        body: SetModeBody,
        app_id: str = Path_(alias="app"),
        auth: Authorized = setting_application,
    ) -> JSONResponse:
        assert auth.consumed is not None
        change = await modes.set_application(
            org, app_id, body.mode, actor=auth.actor, marker=auth.consumed
        )
        return JSONResponse(
            {
                "application_id": app_id,
                "mode": change.mode,
                "previous_mode": change.previous,
                # The stricter of the two, which is what its requests are served under.
                "effective_mode": effective_mode(
                    await modes.store.organization_mode(org),
                    await modes.store.application_mode(app_id),
                ),
                "in_flight": IN_FLIGHT,
            }
        )


class SettingsChangeBody(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # The version the change was made from; 0 for settings never set.
    expected_version: int = Field(ge=0, le=999_999_999)
    # Checked by the service against the catalogue, whose refusal names the setting.
    values: dict[str, Any] = Field(default_factory=dict, alias="set", max_length=32)
    unset: list[str] = Field(default_factory=list, max_length=32)


class SettingsRevertBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=0, le=999_999_999)


def _json_value(value: object) -> object:
    """A setting's value as JSON: tuples and read-only maps as lists and objects."""
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, Mapping):
        return dict(value)
    return value


def _effective_view(
    platform: PlatformSettings,
    application: Application,
    organization: Mapping[str, object],
    document: Mapping[str, object],
    effective: EffectiveSettings,
) -> dict[str, object]:
    """Each setting: what is in force, where it came from, what capped it, what the application
    stored, and every cap above it (docs/40)."""
    caps = caps_of(platform, application, organization)
    view: dict[str, object] = {}
    for name, found in effective.explain.items():
        view[name] = {
            "value": _json_value(found.value),
            "source": found.source.value,
            "capped_by": found.capped_by.value if found.capped_by is not None else None,
            "stored": _json_value(document[name]) if name in document else None,
            "caps": {level: _json_value(v) for level, v in caps[name].items()},
        }
    return view


def _version_view(found: SettingsVersion) -> dict[str, object]:
    return {
        "version": found.version,
        "changed_fields": list(found.changed_fields),
        "changed_by_user_id": found.changed_by_user_id,
        "via": found.via,
        "changed_at_ms": found.changed_at_ms,
    }


class RuleChangeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1, le=999_999_999)
    # Checked field by field by gateway.alertrules.validate_rule, whose refusal names it.
    rule: dict[str, Any] = Field(max_length=16)


class RuleVersionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1, le=999_999_999)


def _rule_view(rule: AlertRule) -> dict[str, object]:
    default = DEFAULT_RULES.get(rule.default_key) if rule.default_key is not None else None
    return {
        "id": rule.id,
        "version": rule.version,
        "default_key": rule.default_key,
        # A default rule someone changed: shown so that a weakened default is visible.
        "changed_from_default": default is not None and default != rule.spec,
        **spec_body(rule.spec),
        "created_at_ms": rule.created_at_ms,
        "updated_at_ms": rule.updated_at_ms,
    }


def _incident_view(incident: Incident) -> dict[str, object]:
    return {
        "id": incident.id,
        # Null for the built-in channel_delivery_failing's, which no application owns (docs/46).
        "application_id": incident.application_id or None,
        "rule_id": incident.rule_id,
        "rule_version": incident.rule_version,
        # A provider id or a key id where the signal has series.
        "series": incident.series_key or None,
        "state": incident.state.value,
        "severity": incident.severity,
        "value": incident.value,
        "opened_at_ms": incident.opened_at_ms,
        "pending_since_ms": incident.pending_since_ms,
        "fired_at_ms": incident.fired_at_ms,
        "acknowledged_at_ms": incident.acknowledged_at_ms,
        "acknowledged_by": incident.acknowledged_by,
        "resolved_at_ms": incident.resolved_at_ms,
        "resolve_reason": incident.resolve_reason.value
        if incident.resolve_reason is not None
        else None,
        "no_data_since_ms": incident.no_data_since_ms,
        "reopened": incident.reopened,
    }


def _incident_state(state: str | None) -> IncidentState | None:
    if state is None:
        return None
    try:
        return IncidentState(state)
    except ValueError:
        raise RequestValidationError(
            [{"loc": ("query", "state"), "msg": "not an incident state", "type": "value_error"}]
        ) from None


def _incident_routes(
    app: FastAPI,
    accounts: AccountAccess,
    incidents: IncidentStore,
    epoch: EpochSource,
    outbox: OutboxStore | None = None,
) -> None:
    """The incident endpoints (docs/45), each through ``requires``, with resolvers that answer
    an application outside the path's organization, or an incident outside the path's
    application (or an id not in an incident id's form), as missing."""
    requires = app.state.requires
    service = accounts.service
    stores = Stores(service.organizations, service.store)

    async def organization(request: Request) -> Resource | None:
        return await resolve_organization(stores, request.path_params["org"])

    async def application(request: Request) -> Resource | None:
        p = request.path_params
        return await resolve_application(stores, p["org"], p["app"])

    async def incident(request: Request) -> Resource | None:
        p = request.path_params
        resource = await resolve_application(stores, p["org"], p["app"])
        if resource is None or not INCIDENT_ID_PATTERN.fullmatch(p["incident"]):
            return None
        found = await incidents.get_incident(p["app"], p["incident"])
        return resource if found is not None else None

    reading_organization = requires("incidents.read_organization", organization)
    reading = requires("incidents.read", application)
    reading_incident = requires("incidents.read", incident)
    acknowledging = requires("incidents.acknowledge", incident)
    prefix = ORG_PREFIX + "/applications/{app}/incidents"

    def page(found: list[Incident], limit: int) -> dict[str, object]:
        return {
            "incidents": [_incident_view(i) for i in found],
            "next": found[-1].id if len(found) == limit else None,
        }

    @app.get(ORG_PREFIX + "/incidents")
    async def list_organization_incidents(
        org: str,
        state: str | None = Query(default=None, max_length=20),
        application_id: str | None = Query(default=None, alias="application", max_length=64),
        limit: int = Query(default=50, ge=1, le=100),
        after: str | None = Query(default=None, max_length=20),
        auth: Authorized = reading_organization,
    ) -> JSONResponse:
        found = await incidents.list_incidents(
            org, application_id=application_id, state=_incident_state(state), limit=limit,
            after=after,
        )  # fmt: skip
        return JSONResponse(page(found, limit))

    @app.get(ORG_PREFIX + "/incidents/counts")
    async def count_open_incidents(
        org: str, auth: Authorized = reading_organization
    ) -> JSONResponse:
        """The organization's open incidents by application and state, for the Applications
        list (docs/51): ids and counts only, never a rule, a value or a text."""
        counts = await incidents.open_counts(org)
        return JSONResponse(
            {
                "applications": [
                    {
                        "application_id": application_id,
                        **{
                            state.value: counts[application_id].get(state.value, 0)
                            for state in OPEN_STATES
                        },
                    }
                    for application_id in sorted(counts)
                ]
            }
        )

    @app.get(prefix)
    async def list_application_incidents(
        org: str,
        app_id: str = Path_(alias="app"),
        state: str | None = Query(default=None, max_length=20),
        limit: int = Query(default=50, ge=1, le=100),
        after: str | None = Query(default=None, max_length=20),
        auth: Authorized = reading,
    ) -> JSONResponse:
        found = await incidents.list_incidents(
            org, application_id=app_id, state=_incident_state(state), limit=limit, after=after
        )
        return JSONResponse(page(found, limit))

    @app.get(prefix + "/{incident}")
    async def get_application_incident(
        org: str,
        incident: str,
        app_id: str = Path_(alias="app"),
        auth: Authorized = reading_incident,
    ) -> JSONResponse:
        found = await incidents.get_incident(app_id, incident)
        assert found is not None  # the resolver found it
        notifications = await incidents.notifications(incident)
        deliveries: dict[str, list[dict[str, object]]] = {}
        if outbox is not None:
            for d in await outbox.notification_deliveries([n.id for n in notifications]):
                deliveries.setdefault(d.notification_id, []).append(
                    {"channel_id": d.channel_id, "state": d.state.value, "attempts": d.attempts,
                     "last_code": d.last_code}
                )  # fmt: skip
        return JSONResponse(
            {
                **_incident_view(found),
                # Each notification's deliveries to its channels (docs/46): their state only.
                "notifications": [
                    {"kind": n.kind.value, "state": n.state.value, "at_ms": n.at_ms,
                     "reason": n.reason, "deliveries": deliveries.get(n.id, [])}
                    for n in notifications
                ],
            }
        )  # fmt: skip

    @app.post(prefix + "/{incident}/acknowledge")
    async def acknowledge_incident(
        org: str,
        incident: str,
        app_id: str = Path_(alias="app"),
        auth: Authorized = acknowledging,
    ) -> JSONResponse:
        done = await incidents.acknowledge(app_id, incident, auth.actor.user_id, epoch.epoch_ms())
        if done is None:
            raise ManagementRefusal(
                Reason.INCIDENT_NOT_OPEN, "only a firing incident can be acknowledged"
            )
        await service.audit.write(
            AuditEvent(
                AuditAction.INCIDENT_ACKNOWLEDGED, Outcome.SUCCESS, TargetType.INCIDENT,
                incident, auth.actor.user_id, {"application_id": app_id},
            ),
            epoch.epoch_ms(),
        )  # fmt: skip
        return JSONResponse(_incident_view(done))


def _alert_rule_routes(app: FastAPI, accounts: AccountAccess, rules: RuleService) -> None:
    """The alert rule endpoints under /v1/admin/orgs/{org}/applications/{app} (docs/44), each
    through ``requires`` with a resolver that answers an application outside the path's
    organization, or a rule outside the path's application, as missing. No step-up: a rule
    change is audited (the plan's section 14)."""
    requires = app.state.requires
    stores = Stores(accounts.service.organizations, accounts.service.store)
    store = rules.store

    async def application(request: Request) -> Resource | None:
        p = request.path_params
        return await resolve_application(stores, p["org"], p["app"])

    async def rule(request: Request) -> Resource | None:
        p = request.path_params
        return await resolve_rule(stores, store, p["org"], p["app"], p["rule"])

    reading = requires("alert_rules.read", application)
    writing = requires("alert_rules.write", application)
    reading_rule = requires("alert_rules.read", rule)
    writing_rule = requires("alert_rules.write", rule)
    prefix = ORG_PREFIX + "/applications/{app}/alert-rules"

    @app.exception_handler(RuleInvalidError)
    async def rule_invalid(_: Request, exc: RuleInvalidError) -> JSONResponse:
        return _error(
            422,
            "invalid_request",
            "The request is not valid.",
            details=[{"field": exc.field, "message": str(exc)}],
            headers=_NO_STORE,
        )

    @app.get(prefix)
    async def list_alert_rules(
        org: str,
        app_id: str = Path_(alias="app"),
        limit: int = Query(default=50, ge=1, le=100),
        after: str | None = Query(default=None, max_length=20),
        auth: Authorized = reading,
    ) -> JSONResponse:
        page = await store.list_rules(app_id, limit, after)
        return JSONResponse(
            {
                "rules": [_rule_view(r) for r in page],
                "next": page[-1].id if len(page) == limit else None,
            }
        )

    @app.post(prefix, status_code=201)
    async def create_alert_rule(
        org: str,
        body: dict[str, Any],
        app_id: str = Path_(alias="app"),
        auth: Authorized = writing,
    ) -> JSONResponse:
        created = await rules.create(app_id, body, auth.actor.user_id)
        return JSONResponse(_rule_view(created), status_code=201)

    @app.post(prefix + "/validate")
    async def validate_alert_rule(
        org: str,
        body: dict[str, Any],
        app_id: str = Path_(alias="app"),
        auth: Authorized = reading,
    ) -> JSONResponse:
        """A rule body checked as a write would check it, and what it would have been over
        the last hour from the rollups. Writes nothing."""
        try:
            spec = validate_rule(body)
            # Channel ids checked as a write checks them (docs/46): the same answer for one of
            # another organization as for one that does not exist.
            await rules.check_channels(app_id, spec)
        except RuleInvalidError as exc:
            return JSONResponse(
                {"valid": False, "invalid_field": exc.field, "message": str(exc), "rule": None,
                 "preview": None}
            )  # fmt: skip
        minutes = await rules.preview(app_id, spec)
        preview = (
            None
            if minutes is None
            else {
                "minutes": [
                    {"minute": m.minute, "value": m.value, "why_not": m.why_not, "met": m.met}
                    for m in minutes
                ],
                "met": sum(1 for m in minutes if m.met),
            }
        )
        return JSONResponse(
            {"valid": True, "invalid_field": None, "message": None, "rule": spec_body(spec),
             "preview": preview}
        )  # fmt: skip

    @app.post(prefix + "/restore-defaults")
    async def restore_default_alert_rules(
        org: str, app_id: str = Path_(alias="app"), auth: Authorized = writing
    ) -> JSONResponse:
        return JSONResponse({"written": await rules.restore_defaults(app_id, auth.actor.user_id)})

    @app.get(prefix + "/{rule}")
    async def get_alert_rule(
        org: str, rule: str, app_id: str = Path_(alias="app"), auth: Authorized = reading_rule
    ) -> JSONResponse:
        found = await store.get_rule(app_id, rule)
        assert found is not None  # the resolver found it
        return JSONResponse(_rule_view(found))

    @app.put(prefix + "/{rule}")
    async def update_alert_rule(
        org: str,
        rule: str,
        body: RuleChangeBody,
        app_id: str = Path_(alias="app"),
        auth: Authorized = writing_rule,
    ) -> JSONResponse:
        changed = await rules.update(
            app_id, rule, body.expected_version, body.rule, auth.actor.user_id
        )
        return JSONResponse(_rule_view(changed))

    @app.post(prefix + "/{rule}/enable")
    async def enable_alert_rule(
        org: str,
        rule: str,
        body: RuleVersionBody,
        app_id: str = Path_(alias="app"),
        auth: Authorized = writing_rule,
    ) -> JSONResponse:
        changed = await rules.set_enabled(
            app_id, rule, body.expected_version, True, auth.actor.user_id
        )
        return JSONResponse(_rule_view(changed))

    @app.post(prefix + "/{rule}/disable")
    async def disable_alert_rule(
        org: str,
        rule: str,
        body: RuleVersionBody,
        app_id: str = Path_(alias="app"),
        auth: Authorized = writing_rule,
    ) -> JSONResponse:
        changed = await rules.set_enabled(
            app_id, rule, body.expected_version, False, auth.actor.user_id
        )
        return JSONResponse(_rule_view(changed))

    @app.delete(prefix + "/{rule}")
    async def delete_alert_rule(
        org: str,
        rule: str,
        body: RuleVersionBody,
        app_id: str = Path_(alias="app"),
        auth: Authorized = writing_rule,
    ) -> JSONResponse:
        await rules.delete(app_id, rule, body.expected_version, auth.actor.user_id)
        return JSONResponse({"deleted": rule})


class CreateChannelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Each checked by the channel service, whose refusal names the field and never repeats a
    # URL (the URL is a secret: docs/46).
    type: str = Field(max_length=20)
    name: str = Field(max_length=200)
    # A webhook's URL; an email channel has none, and has 1 to 10 recipients instead (docs/48).
    url: str | None = Field(default=None, max_length=4096)
    recipients: list[str] | None = Field(default=None, max_length=50)
    default: bool = False


class AddRecipientBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Checked by the recipient service, whose refusal never repeats the address.
    address: str = Field(max_length=1024)


class ConfirmRecipientBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Checked only by comparison, after the same steps for every value (docs/48).
    code: str = Field(max_length=64)


class UpdateChannelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1, le=999_999_999)
    name: str | None = Field(default=None, max_length=200)
    url: str | None = Field(default=None, max_length=4096)
    default: bool | None = None
    # Only true: re-enabling needs step-up (this route's action); disabling has its own route.
    enabled: bool | None = None


def _recipient_view(recipient: Recipient) -> dict[str, object]:
    """For the organization's admin, who entered it: the address and its state; never the code
    or its digest."""
    return {
        "id": recipient.id,
        "address": recipient.address,
        "state": recipient.state,
        "added_at_ms": recipient.added_at_ms,
        "verified_at_ms": recipient.verified_at_ms,
        "confirmation_expires_at_ms": recipient.code_expires_at_ms,
    }


def _channel_view(
    channel: Channel, recipients: Sequence[Recipient] | None = None
) -> dict[str, object]:
    """For the organization's admin: only the URL's origin, never the URL or a key; an email
    channel's recipients (docs/48)."""
    view: dict[str, object] = {
        "id": channel.id,
        "name": channel.name,
        "type": channel.type,
        "enabled": channel.enabled,
        "default": channel.is_default,
        "url_origin": channel.url_origin,
        "version": channel.version,
        "created_at_ms": channel.created_at_ms,
        "updated_at_ms": channel.updated_at_ms,
        "health": {
            "last_delivered_at_ms": channel.last_delivered_at_ms,
            "last_failed_at_ms": channel.last_failed_at_ms,
            "consecutive_failures": channel.consecutive_failures,
        },
    }
    if recipients is not None:
        view["recipients"] = [_recipient_view(r) for r in recipients]
    return view


def _channel_application_view(channel: Channel) -> dict[str, object]:
    """For an application's readers: what a rule may name, never a target or its origin."""
    return {
        "id": channel.id,
        "name": channel.name,
        "type": channel.type,
        "default": channel.is_default,
        "enabled": channel.enabled,
    }


def _delivery_view(delivery: Delivery) -> dict[str, object]:
    """A delivery's state, never its payload or anything a receiver answered."""
    return {
        "id": delivery.id,
        "notification_id": delivery.notification_id,
        "channel_id": delivery.channel_id,
        "state": delivery.state.value,
        "attempts": delivery.attempts,
        "next_attempt_at_ms": delivery.next_attempt_at_ms,
        "last_code": delivery.last_code,
        "created_at_ms": delivery.created_at_ms,
        "updated_at_ms": delivery.updated_at_ms,
    }


def _channel_routes(
    app: FastAPI,
    accounts: AccountAccess,
    channels: ChannelService,
    test_sends: TestSends | None = None,
) -> None:
    """The alert channel endpoints under /v1/admin/orgs/{org} (docs/46), each through
    ``requires`` with a resolver that answers a channel outside the path's organization (or an
    id not in a channel id's form) as missing. Creating, changing, re-enabling and rotating are
    ``alert_channels.write``, consume-on-use step-up; disabling and deleting need none; a test
    send is ``alert_channels.test`` (without ``test_sends``, nothing is delivered: 503)."""
    requires = app.state.requires
    stores = Stores(accounts.service.organizations, accounts.service.store)
    store = channels.store
    notices = channels.notices
    assert notices is not None
    recipients = channels.recipients

    async def organization(request: Request) -> Resource | None:
        return await resolve_organization(stores, request.path_params["org"])

    async def recipient(request: Request) -> Resource | None:
        # The channel in the path's organization, and the recipient in that channel; anything
        # else (an id not in a recipient id's form included) is missing, answered as forbidden.
        p = request.path_params
        resource = await resolve_channel(stores, store, p["org"], p["channel"])
        if resource is None or recipients is None:
            return None
        if not await resolve_recipient_in(recipients.store, p["channel"], p["recipient"]):
            return None
        return resource

    async def viewed(channel: Channel) -> dict[str, object]:
        if channel.type == "email" and recipients is not None:
            return _channel_view(channel, await recipients.store.channel_recipients(channel.id))
        return _channel_view(channel)

    async def application(request: Request) -> Resource | None:
        p = request.path_params
        return await resolve_application(stores, p["org"], p["app"])

    async def channel(request: Request) -> Resource | None:
        p = request.path_params
        return await resolve_channel(stores, store, p["org"], p["channel"])

    reading_application = requires("alert_rules.read", application)
    reading = requires("alert_channels.read", organization)
    creating = requires("alert_channels.write", organization)
    reading_channel = requires("alert_channels.read", channel)
    writing_channel = requires("alert_channels.write", channel)
    disabling_channel = requires("alert_channels.disable", channel)
    testing_channel = requires("alert_channels.test", channel)
    confirming_in_channel = requires("alert_channels.confirm_recipient", channel)
    confirming_recipient = requires("alert_channels.confirm_recipient", recipient)
    removing_recipient = requires("alert_channels.disable", recipient)
    prefix = ORG_PREFIX + "/alert-channels"

    @app.exception_handler(EmailUnavailableError)
    async def email_unavailable(_: Request, exc: EmailUnavailableError) -> JSONResponse:
        # Raised, so a claimed step-up window is given back (docs/32).
        return _invalid("type", "email is not set up on this server")

    @app.exception_handler(RecipientGoneError)
    async def recipient_gone(_: Request, exc: RecipientGoneError) -> JSONResponse:
        # Resolved a moment ago, removed since: missing, answered as forbidden.
        return _error(403, "forbidden", "Not permitted, or not found.", headers=_NO_STORE)

    @app.exception_handler(ChannelFieldError)
    async def channel_invalid(_: Request, exc: ChannelFieldError) -> JSONResponse:
        # Raised, not answered, so a claimed step-up window is given back (docs/32).
        return _invalid(exc.field, str(exc))

    @app.exception_handler(NothingChangedError)
    async def nothing_changed(_: Request, exc: NothingChangedError) -> JSONResponse:
        # Raised, so the step-up window this change claimed is given back.
        return JSONResponse({**_channel_view(exc.channel), "changed": False})

    @app.exception_handler(ChannelGoneError)
    async def channel_gone(_: Request, exc: ChannelGoneError) -> JSONResponse:
        # Resolved a moment ago, deleted since: missing, answered as forbidden.
        return _error(403, "forbidden", "Not permitted, or not found.", headers=_NO_STORE)

    @app.get(ORG_PREFIX + "/applications/{app}/alert-channels")
    async def list_application_alert_channels(
        org: str, app_id: str = Path_(alias="app"), auth: Authorized = reading_application
    ) -> JSONResponse:
        found = await store.organization_channels(org)
        return JSONResponse({"channels": [_channel_application_view(c) for c in found]})

    @app.get(prefix)
    async def list_alert_channels(
        org: str,
        limit: int = Query(default=50, ge=1, le=100),
        after: str | None = Query(default=None, max_length=20),
        auth: Authorized = reading,
    ) -> JSONResponse:
        page = await store.list_channels(org, limit, after)
        return JSONResponse(
            {
                "channels": [await viewed(c) for c in page],
                "next": page[-1].id if len(page) == limit else None,
            }
        )

    @app.post(prefix, status_code=201)
    async def create_alert_channel(
        org: str, body: CreateChannelBody, auth: Authorized = creating
    ) -> JSONResponse:
        assert auth.consumed is not None
        made = await channels.create(
            org, name=body.name, url=body.url, is_default=body.default, channel_type=body.type,
            actor=auth.actor, marker=auth.consumed, recipients=body.recipients,
        )  # fmt: skip
        if made.signing_key is None:
            # An email channel: its recipients, each unverified, and each confirmation's outcome.
            return JSONResponse(
                {
                    **_channel_view(made.channel, [r for r, _ in made.recipients]),
                    "confirmations": {r.id: outcome for r, outcome in made.recipients},
                },
                status_code=201,
            )
        # The signing key, this once (no-store, as every admin answer).
        return JSONResponse(
            {**_channel_view(made.channel), "signing_key": made.signing_key}, status_code=201
        )

    @app.get(prefix + "/{channel}")
    async def get_alert_channel(
        org: str, channel: str, auth: Authorized = reading_channel
    ) -> JSONResponse:
        found = await store.get_channel(channel)
        if found is None:
            raise ForbiddenError()
        return JSONResponse(await viewed(found))

    @app.patch(prefix + "/{channel}")
    async def update_alert_channel(
        org: str, channel: str, body: UpdateChannelBody, auth: Authorized = writing_channel
    ) -> JSONResponse:
        assert auth.consumed is not None
        changes = body.model_dump(exclude_unset=True)
        expected = changes.pop("expected_version")
        changed = await channels.update(
            channel, expected, changes, actor=auth.actor, marker=auth.consumed
        )
        return JSONResponse({**_channel_view(changed), "changed": True})

    @app.post(prefix + "/{channel}/disable")
    async def disable_alert_channel(
        org: str, channel: str, auth: Authorized = disabling_channel
    ) -> JSONResponse:
        return JSONResponse(_channel_view(await channels.disable(channel, actor=auth.actor)))

    @app.delete(prefix + "/{channel}")
    async def delete_alert_channel(
        org: str, channel: str, auth: Authorized = disabling_channel
    ) -> JSONResponse:
        await channels.delete(channel, actor=auth.actor)
        return JSONResponse({"deleted": channel})

    @app.post(prefix + "/{channel}/rotate-key")
    async def rotate_alert_channel_key(
        org: str, channel: str, auth: Authorized = writing_channel
    ) -> JSONResponse:
        assert auth.consumed is not None
        key = await channels.rotate_key(channel, actor=auth.actor, marker=auth.consumed)
        return JSONResponse({"id": channel, "signing_key": key})

    @app.post(prefix + "/{channel}/test")
    async def test_alert_channel(
        org: str, channel: str, auth: Authorized = testing_channel
    ) -> JSONResponse:
        if test_sends is None:
            raise StateUnavailableError("delivery")
        # The code alone: never anything the receiver answered.
        code = await test_sends.send(org, channel, auth.actor)
        return JSONResponse({"id": channel, "outcome": code, "delivered": code == DELIVERED})

    @app.post(prefix + "/{channel}/recipients", status_code=201)
    async def add_alert_recipient(
        org: str, channel: str, body: AddRecipientBody, auth: Authorized = writing_channel
    ) -> JSONResponse:
        assert auth.consumed is not None
        found = await store.get_channel(channel)
        if found is None:
            raise ForbiddenError()
        if found.type != "email":
            raise ManagementRefusal(
                Reason.CHANNEL_NOT_EMAIL, "only an email channel has recipients"
            )
        if recipients is None:
            raise EmailUnavailableError()
        made, outcome = await recipients.add(
            org, channel, body.address, actor=auth.actor, marker=auth.consumed
        )
        return JSONResponse({**_recipient_view(made), "confirmation": outcome}, status_code=201)

    @app.post(prefix + "/{channel}/recipients/{recipient}/resend")
    async def resend_alert_recipient(
        org: str, channel: str, recipient: str, auth: Authorized = confirming_recipient
    ) -> JSONResponse:
        assert recipients is not None  # the resolver found the recipient
        made, outcome = await recipients.resend(org, channel, recipient, actor=auth.actor)
        return JSONResponse({**_recipient_view(made), "confirmation": outcome})

    @app.post(prefix + "/{channel}/recipients/{recipient}/confirm")
    async def confirm_alert_recipient(
        org: str,
        channel: str,
        recipient: str,
        body: ConfirmRecipientBody,
        auth: Authorized = confirming_in_channel,
    ) -> JSONResponse:
        # The channel is resolved; the recipient is not: an unknown one is refused exactly as a
        # wrong code is, after the same steps (docs/48).
        if recipients is None:
            raise EmailUnavailableError()
        made = await recipients.confirm(org, channel, recipient, body.code, actor=auth.actor)
        return JSONResponse(_recipient_view(made))

    @app.delete(prefix + "/{channel}/recipients/{recipient}")
    async def remove_alert_recipient(
        org: str, channel: str, recipient: str, auth: Authorized = removing_recipient
    ) -> JSONResponse:
        assert recipients is not None  # the resolver found the recipient
        await recipients.remove(channel, recipient, actor=auth.actor)
        return JSONResponse({"deleted": recipient})

    @app.get(prefix + "/{channel}/deliveries")
    async def list_alert_channel_deliveries(
        org: str,
        channel: str,
        limit: int = Query(default=50, ge=1, le=100),
        after: str | None = Query(default=None, max_length=20),
        auth: Authorized = reading_channel,
    ) -> JSONResponse:
        page = await notices.outbox.channel_deliveries(channel, limit, after)
        return JSONResponse(
            {
                "deliveries": [_delivery_view(d) for d in page],
                "next": page[-1].id if len(page) == limit else None,
            }
        )


def _settings_routes(
    app: FastAPI,
    accounts: AccountAccess,
    settings: SettingsService,
    store: SettingsStore,
    modes: ModeStore,
) -> None:
    """The settings endpoints under /v1/admin/orgs/{org} (docs/40): an organization's caps and
    an application's settings, their versions, revert and the dry run, each through
    ``requires`` with a resolver that answers anything outside the path's organization, or a
    version that does not exist, as missing. A change that loosens S13 to S16 for any
    application it affects (a preset, a revert, an organization cap included) needs step-up,
    consume-on-use, through the ``*.loosen_egress`` action; tightening needs none (docs/42)."""
    requires = app.state.requires
    escalate = vars(requires)["escalate"]
    stores = Stores(accounts.service.organizations, accounts.service.store)
    platform = settings.platform

    async def organization(request: Request) -> Resource | None:
        return await resolve_organization(stores, request.path_params["org"])

    async def application(request: Request) -> Resource | None:
        p = request.path_params
        return await resolve_application(stores, p["org"], p["app"])

    async def organization_version(request: Request) -> Resource | None:
        p = request.path_params
        return await resolve_organization_settings_version(stores, store, p["org"], p["n"])

    async def application_version(request: Request) -> Resource | None:
        p = request.path_params
        return await resolve_application_settings_version(stores, store, p["org"], p["app"], p["n"])

    reading_organization = requires("org_settings.read", organization)
    writing_organization = requires("org_settings.write", organization)
    reading_organization_version = requires("org_settings.read", organization_version)
    reading_application = requires("settings.read", application)
    writing_application = requires("settings.write", application)
    reading_application_version = requires("settings.read", application_version)

    async def application_preset(request: Request) -> Resource | None:
        p = request.path_params
        if p["name"] not in SETTINGS_PRESETS:
            return None
        return await resolve_application(stores, p["org"], p["app"])

    applying_preset = requires("settings.write", application_preset)

    async def written(
        auth: Authorized,
        action: str,
        loosens: Sequence[str],
        write: Callable[[bool], Awaitable[SettingsChange]],
    ) -> SettingsChange:
        """Write, with step-up when the dry run found the change loosens egress; the write
        judges it again inside its transaction and refuses a mismatch (a conflict)."""
        if not loosens:
            return await write(False)
        # Nothing can fail after the write returns, so the window is never given back for a
        # change that was stored: ``escalate`` gives it back only when an exception leaves it.
        async with escalate(auth, action):
            return await write(True)

    @app.exception_handler(SettingsInvalidError)
    async def settings_invalid(_: Request, exc: SettingsInvalidError) -> JSONResponse:
        return _error(
            422,
            "invalid_request",
            "The request is not valid.",
            details=[{"field": exc.setting, "message": str(exc)}],
            headers=_NO_STORE,
        )

    async def organization_body(org: str) -> dict[str, object]:
        view = await settings.view(Scope.ORGANIZATION, org)
        caps = caps_of(platform, None, view.document)
        return {
            "organization_id": org,
            # S12: shown from its own table, set by its own endpoint (docs/33).
            "mode": (await modes.organization_mode(org)).value,
            "version": view.row.version if view.row is not None else 0,
            "settings": {k: _json_value(v) for k, v in view.document.items()},
            "platform_caps": {
                name: {level: _json_value(v) for level, v in levels.items()}
                for name, levels in caps.items()
                if Scope.ORGANIZATION in CATALOGUE[name].levels
            },
        }

    async def application_body(org: str, app_id: str) -> dict[str, object]:
        view = await settings.view(Scope.APPLICATION, app_id)
        found = await store.current(Scope.ORGANIZATION, org)
        organization_document = parse_row(found, Scope.ORGANIZATION) if found is not None else {}
        stored = await accounts.service.organizations.get_application(app_id)
        assert stored is not None
        assert view.effective is not None
        organization_mode = await modes.organization_mode(org)
        application_mode = await modes.application_mode(app_id)
        return {
            "application_id": app_id,
            "organization_id": org,
            # S12: shown from its own table, set by its own endpoint (docs/33).
            "mode": {
                "organization": organization_mode.value,
                "application": application_mode.value,
                "effective": effective_mode(organization_mode, application_mode).value,
            },
            "version": view.row.version if view.row is not None else 0,
            "settings": {k: _json_value(v) for k, v in view.document.items()},
            "effective": _effective_view(
                platform, stored, organization_document, view.document, view.effective
            ),
            "ignored_aliases": list(view.effective.ignored_aliases),
        }

    def change_body(change: SettingsChange) -> dict[str, object]:
        return {
            "version": change.version,
            "changed": list(change.changed),
            "settings": {k: _json_value(v) for k, v in change.document.items()},
        }

    def validation_body(result: SettingsValidation) -> dict[str, object]:
        body: dict[str, object] = {
            "valid": result.valid,
            "code": result.code,
            "message": result.message,
            "invalid_setting": result.invalid_setting,
            "refusals": [{"setting": r.setting, "level": r.level.value} for r in result.refusals],
            "changed": list(result.changed),
            "settings": {k: _json_value(v) for k, v in result.document.items()},
            # A change that loosens S13 to S16 asks for a fresh password (docs/42).
            "loosens": list(result.loosens),
            "step_up_required": bool(result.loosens),
        }
        if result.effective is not None:
            body["effective"] = {
                name: _json_value(found.value) for name, found in result.effective.explain.items()
            }
        return body

    async def versions_body(scope: Scope, scope_id: str, limit: int, after: int | None) -> (
        dict[str, object]
    ):  # fmt: skip
        page = await settings.versions(scope, scope_id, limit=limit, before=after)
        return {
            "versions": [_version_view(v) for v in page],
            # The next page starts after the last version shown, while one was full.
            "next_after": page[-1].version if len(page) == limit else None,
        }

    async def version_body(scope: Scope, scope_id: str, n: str) -> dict[str, object]:
        found = await store.version(scope, scope_id, int(n))
        assert found is not None  # the resolver found it
        return {
            **_version_view(found),
            "settings": json.loads(found.document),
            "previous_settings": json.loads(found.previous_document)
            if found.previous_document is not None
            else None,
        }

    # --- the organization's -------------------------------------------------------------------

    @app.get(ORG_PREFIX + "/settings")
    async def get_organization_settings(
        org: str, auth: Authorized = reading_organization
    ) -> JSONResponse:
        return JSONResponse(await organization_body(org))

    async def dry(scope: Scope, scope_id: str, body: SettingsChangeBody) -> tuple[str, ...]:
        return (
            await settings.validate(
                scope,
                scope_id,
                expected_version=body.expected_version,
                values=body.values,
                unset=body.unset,
            )
        ).loosens

    @app.put(ORG_PREFIX + "/settings")
    @escalates("org_settings.loosen_egress")
    async def put_organization_settings(
        org: str, body: SettingsChangeBody, auth: Authorized = writing_organization
    ) -> JSONResponse:
        async def write(step_up: bool) -> SettingsChange:
            return await settings.set(
                Scope.ORGANIZATION,
                org,
                expected_version=body.expected_version,
                values=body.values,
                unset=body.unset,
                actor_user_id=auth.actor.user_id,
                via="http",
                step_up=step_up,
            )

        loosens = await dry(Scope.ORGANIZATION, org, body)
        change = await written(auth, "org_settings.loosen_egress", loosens, write)
        return JSONResponse(change_body(change))

    @app.post(ORG_PREFIX + "/settings/validate")
    async def validate_organization_settings(
        org: str, body: SettingsChangeBody, auth: Authorized = writing_organization
    ) -> JSONResponse:
        result = await settings.validate(
            Scope.ORGANIZATION,
            org,
            expected_version=body.expected_version,
            values=body.values,
            unset=body.unset,
        )
        return JSONResponse(validation_body(result))

    @app.post(ORG_PREFIX + "/settings/revert")
    @escalates("org_settings.loosen_egress")
    async def revert_organization_settings(
        org: str, body: SettingsRevertBody, auth: Authorized = writing_organization
    ) -> JSONResponse:
        async def write(step_up: bool) -> SettingsChange:
            return await settings.revert(
                Scope.ORGANIZATION,
                org,
                expected_version=body.expected_version,
                actor_user_id=auth.actor.user_id,
                step_up=step_up,
            )

        loosens = await settings.revert_loosens(
            Scope.ORGANIZATION, org, expected_version=body.expected_version
        )
        change = await written(auth, "org_settings.loosen_egress", loosens, write)
        return JSONResponse(change_body(change))

    @app.get(ORG_PREFIX + "/settings/presets")
    async def list_settings_presets(
        org: str, auth: Authorized = reading_organization
    ) -> JSONResponse:
        # Their exact values (the plan's section 4); applied to an application by name.
        return JSONResponse(
            {
                "presets": {
                    name: {k: _json_value(v) for k, v in values.items()}
                    for name, values in SETTINGS_PRESETS.items()
                }
            }
        )

    @app.get(ORG_PREFIX + "/settings/versions")
    async def list_organization_settings_versions(
        org: str,
        limit: int = Query(default=50, ge=1, le=100),
        after: int | None = Query(default=None, ge=1),
        auth: Authorized = reading_organization,
    ) -> JSONResponse:
        return JSONResponse(await versions_body(Scope.ORGANIZATION, org, limit, after))

    @app.get(ORG_PREFIX + "/settings/versions/{n}")
    async def get_organization_settings_version(
        org: str, n: str, auth: Authorized = reading_organization_version
    ) -> JSONResponse:
        return JSONResponse(await version_body(Scope.ORGANIZATION, org, n))

    # --- an application's ---------------------------------------------------------------------

    @app.get(ORG_PREFIX + "/applications/{app}/settings")
    async def get_application_settings(
        org: str, app_id: str = Path_(alias="app"), auth: Authorized = reading_application
    ) -> JSONResponse:
        return JSONResponse(await application_body(org, app_id))

    @app.put(ORG_PREFIX + "/applications/{app}/settings")
    @escalates("settings.loosen_egress")
    async def put_application_settings(
        org: str,
        body: SettingsChangeBody,
        app_id: str = Path_(alias="app"),
        auth: Authorized = writing_application,
    ) -> JSONResponse:
        async def write(step_up: bool) -> SettingsChange:
            return await settings.set(
                Scope.APPLICATION,
                app_id,
                expected_version=body.expected_version,
                values=body.values,
                unset=body.unset,
                actor_user_id=auth.actor.user_id,
                via="http",
                step_up=step_up,
            )

        loosens = await dry(Scope.APPLICATION, app_id, body)
        change = await written(auth, "settings.loosen_egress", loosens, write)
        return JSONResponse(change_body(change))

    @app.post(ORG_PREFIX + "/applications/{app}/settings/validate")
    async def validate_application_settings(
        org: str,
        body: SettingsChangeBody,
        app_id: str = Path_(alias="app"),
        auth: Authorized = writing_application,
    ) -> JSONResponse:
        result = await settings.validate(
            Scope.APPLICATION,
            app_id,
            expected_version=body.expected_version,
            values=body.values,
            unset=body.unset,
        )
        return JSONResponse(validation_body(result))

    @app.post(ORG_PREFIX + "/applications/{app}/settings/revert")
    @escalates("settings.loosen_egress")
    async def revert_application_settings(
        org: str,
        body: SettingsRevertBody,
        app_id: str = Path_(alias="app"),
        auth: Authorized = writing_application,
    ) -> JSONResponse:
        async def write(step_up: bool) -> SettingsChange:
            return await settings.revert(
                Scope.APPLICATION,
                app_id,
                expected_version=body.expected_version,
                actor_user_id=auth.actor.user_id,
                step_up=step_up,
            )

        loosens = await settings.revert_loosens(
            Scope.APPLICATION, app_id, expected_version=body.expected_version
        )
        change = await written(auth, "settings.loosen_egress", loosens, write)
        return JSONResponse(change_body(change))

    @app.post(ORG_PREFIX + "/applications/{app}/settings/presets/{name}")
    @escalates("settings.loosen_egress")
    async def apply_settings_preset(
        org: str,
        name: str,
        body: SettingsRevertBody,
        app_id: str = Path_(alias="app"),
        auth: Authorized = applying_preset,
    ) -> JSONResponse:
        async def write(step_up: bool) -> SettingsChange:
            return await settings.apply_preset(
                app_id,
                name,
                expected_version=body.expected_version,
                actor_user_id=auth.actor.user_id,
                step_up=step_up,
            )

        # Step-up decided from the change the preset makes here, never from its name.
        loosens = (
            await settings.validate(
                Scope.APPLICATION,
                app_id,
                expected_version=body.expected_version,
                values=SETTINGS_PRESETS[name],
            )
        ).loosens
        change = await written(auth, "settings.loosen_egress", loosens, write)
        return JSONResponse(change_body(change))

    @app.get(ORG_PREFIX + "/applications/{app}/settings/versions")
    async def list_application_settings_versions(
        org: str,
        app_id: str = Path_(alias="app"),
        limit: int = Query(default=50, ge=1, le=100),
        after: int | None = Query(default=None, ge=1),
        auth: Authorized = reading_application,
    ) -> JSONResponse:
        return JSONResponse(await versions_body(Scope.APPLICATION, app_id, limit, after))

    @app.get(ORG_PREFIX + "/applications/{app}/settings/versions/{n}")
    async def get_application_settings_version(
        org: str,
        n: str,
        app_id: str = Path_(alias="app"),
        auth: Authorized = reading_application_version,
    ) -> JSONResponse:
        return JSONResponse(await version_body(Scope.APPLICATION, app_id, n))


def _provider_routes(
    app: FastAPI,
    accounts: AccountAccess,
    providers: ProviderService,
    breakers: BreakerStore,
    warnings: SlowWarnings,
) -> None:
    """The provider endpoints under /v1/admin/orgs/{org}/providers (docs/32), each through
    ``requires``; key changes are consume-on-use."""
    service = accounts.service
    requires = app.state.requires
    stores = Stores(service.organizations, service.store)
    store = providers.store

    async def provider(request: Request) -> Resource | None:
        p = request.path_params
        return await resolve_provider(stores, store, p["org"], p["provider_id"])

    async def organization(request: Request) -> Resource | None:
        return await resolve_organization(stores, request.path_params["org"])

    listing = requires("providers.list", organization)
    statuses = requires("providers.status", organization)
    creating = requires("providers.create", organization)
    updating = requires("providers.update", provider)
    disabling = requires("providers.disable", provider)
    enabling = requires("providers.enable", provider)
    deleting = requires("providers.delete", provider)
    testing = requires("providers.test", provider)
    setting_keys = requires("provider_keys.set", provider)
    deleting_keys = requires("provider_keys.delete", provider)

    async def found(provider_id: str) -> ProviderConfig:
        config = await store.get(provider_id)
        if config is None:  # resolved a moment ago: gone since
            raise ForbiddenError()
        return config

    async def view(config: ProviderConfig) -> dict[str, object]:
        """For the organization's admin. Never the key: whether there is one, whether it can be
        read, its last four characters (keys of 20 or more) and when it was set."""
        key = await providers.key_state(config)
        return {
            "provider_id": config.id,
            "type": config.type,
            "base_url": config.base_url,
            "model": config.model,
            "serves_tier": config.serves_tier,
            "role": config.role,
            "enabled": config.is_enabled,
            "organization_switch": config.enabled,
            "platform_switch": config.platform_enabled,
            "price_input_per_mtok": config.price_input_per_mtok,
            "price_output_per_mtok": config.price_output_per_mtok,
            "first_token_budget_ms": config.first_token_budget_ms,
            "capabilities": dict(config.capabilities),
            "key": {
                "present": key.present,
                "key_unreadable": key.present and not key.readable,
                "last4": key.last4,
                "updated_at_ms": key.updated_at_ms,
            },
            "created_at_ms": config.created_at_ms,
            "circuit": await breakers.state(config.organization_id, config.id),
            **slow(config),
        }

    def slow(config: ProviderConfig) -> dict[str, object]:
        """The slow-provider warning (docs/33): set after consecutive missed first-token
        budgets, cleared by an answer within budget."""
        warning = warnings.slow(config.organization_id, config.id)
        return {
            "slow_since_ms": warning.slow_since_ms,
            "consecutive_misses": warning.consecutive_misses,
        }

    def invalid(exc: ValueError, field: str) -> JSONResponse:
        return _invalid(field, str(exc))

    @app.get(ORG_PREFIX + "/providers")
    async def list_providers(org: str, auth: Authorized = listing) -> JSONResponse:
        return JSONResponse({"providers": [await view(c) for c in await store.list_for(org)]})

    @app.get(ORG_PREFIX + "/providers/status")
    async def provider_status(org: str, auth: Authorized = statuses) -> JSONResponse:
        # That each provider exists, whether it is enabled (docs/32), and whether it is slow
        # (docs/33).
        return JSONResponse(
            {
                "providers": [
                    {"provider_id": c.id, "enabled": c.is_enabled, **slow(c)}
                    for c in await store.list_for(org)
                ]
            }
        )

    @app.post(ORG_PREFIX + "/providers")
    async def create_provider(
        org: str, body: CreateProviderBody, auth: Authorized = creating
    ) -> JSONResponse:
        try:
            made = await providers.create(
                org,
                type=body.type,
                base_url=body.base_url,
                model=body.model,
                serves_tier=body.serves_tier,
                role=body.role,
                price_input_per_mtok=body.price_input_per_mtok,
                price_output_per_mtok=body.price_output_per_mtok,
                first_token_budget_ms=body.first_token_budget_ms,
                actor=auth.actor,
            )
        except ManagementRefusal:
            raise
        except ValueError as exc:
            return invalid(exc, "type" if "type" in str(exc) else "base_url")
        return JSONResponse(await view(made), status_code=201)

    @app.patch(ORG_PREFIX + "/providers/{provider_id}")
    async def update_provider(
        provider_id: str, body: UpdateProviderBody, auth: Authorized = updating
    ) -> JSONResponse:
        changes = body.model_dump(exclude_unset=True)
        try:
            updated, key_deleted = await providers.update(
                await found(provider_id), changes, actor=auth.actor
            )
        except ProviderGoneError:
            raise ForbiddenError() from None
        except ManagementRefusal:
            raise
        except ValueError as exc:
            return invalid(exc, "type" if "type" in str(exc) else "base_url")
        return JSONResponse({**await view(updated), "key_deleted": key_deleted})

    async def switch(provider_id: str, auth: Authorized, on: bool) -> JSONResponse:
        # Whose switch: the platform's for platform_admin, the organization's for its
        # org_admin, as authorize allowed it.
        platform = auth.via.startswith("platform_admin")
        try:
            updated = await providers.set_switch(
                await found(provider_id), on, platform=platform, actor=auth.actor
            )
        except ProviderGoneError:
            raise ForbiddenError() from None
        if platform:  # platform_admin sees no more than the status
            return JSONResponse({"provider_id": updated.id, "enabled": updated.is_enabled})
        return JSONResponse(await view(updated))

    @app.post(ORG_PREFIX + "/providers/{provider_id}/disable")
    async def disable_provider(provider_id: str, auth: Authorized = disabling) -> JSONResponse:
        return await switch(provider_id, auth, False)

    @app.post(ORG_PREFIX + "/providers/{provider_id}/enable")
    async def enable_provider(provider_id: str, auth: Authorized = enabling) -> JSONResponse:
        return await switch(provider_id, auth, True)

    @app.delete(ORG_PREFIX + "/providers/{provider_id}")
    async def delete_provider(provider_id: str, auth: Authorized = deleting) -> JSONResponse:
        assert auth.consumed is not None
        await providers.delete(await found(provider_id), actor=auth.actor, marker=auth.consumed)
        return JSONResponse({"provider_id": provider_id, "deleted": True})

    @app.put(ORG_PREFIX + "/providers/{provider_id}/key")
    async def set_provider_key(
        provider_id: str, body: SetProviderKeyBody, auth: Authorized = setting_keys
    ) -> JSONResponse:
        assert auth.consumed is not None
        with providers.redactor.watching(body.key):
            try:
                state = await providers.set_key(
                    await found(provider_id), body.key, actor=auth.actor, marker=auth.consumed
                )
            except ValueError as exc:
                if isinstance(exc, ManagementRefusal):
                    raise
                # Raised, not answered, so the step-up window is given back (docs/32).
                raise ProviderKeyRejectedError(str(exc)) from None
        # Never the key: its last four characters (for keys of 20 or more) and when.
        return JSONResponse({"last4": state.last4, "updated_at_ms": state.updated_at_ms})

    @app.delete(ORG_PREFIX + "/providers/{provider_id}/key")
    async def delete_provider_key(
        provider_id: str, auth: Authorized = deleting_keys
    ) -> JSONResponse:
        assert auth.consumed is not None
        await providers.delete_key(await found(provider_id), actor=auth.actor, marker=auth.consumed)
        return JSONResponse({"provider_id": provider_id, "key_deleted": True})

    @app.post(ORG_PREFIX + "/providers/{provider_id}/test")
    async def test_provider(provider_id: str, auth: Authorized = testing) -> JSONResponse:
        """Spends tokens on the organization's account with this provider (docs/32)."""
        try:
            capabilities = await providers.test(await found(provider_id), actor=auth.actor)
        except ProviderUnreachedError:
            # One answer for a refused address, no such name, no connection and no answer in
            # time: not a way to map a network (docs/32). The audit entry says which.
            return _error(502, "provider_unreachable", "The provider could not be reached.")
        except ProviderError as exc:
            # The host answered: the code and its fixed message, nothing derived from its body.
            return _error(exc.status if exc.status != 422 else 502, exc.code.value, exc.message)
        return JSONResponse({"ok": True, "capabilities": capabilities})

    @app.exception_handler(ProviderKeyRejectedError)
    async def key_rejected(_: Request, exc: ProviderKeyRejectedError) -> JSONResponse:
        return _invalid("key", str(exc))


class ProviderKeyRejectedError(ValueError):
    """A provider key the service refused (its length or characters). The message never
    contains the key."""


def _invalid(field: str, message: str) -> JSONResponse:
    return _error(
        422,
        "invalid_request",
        "The request is not valid.",
        details=[{"field": field, "message": message}],
    )


def hosted_limits(mode: Mode, settings: EffectiveSettings) -> HostedLimits:
    """A request's hosted limits: its mode and settings S13 to S16 (docs/42)."""
    return HostedLimits(
        mode,
        settings.allowed_providers,
        settings.max_hosted_attempts,
        settings.retry_invalid_tool_json,
        settings.hosted_first_token_ms,
    )


# The max_tokens a request was served with (docs/39): its own, or the application's
# default_max_tokens; "none" when no limit applies at all.
MAX_TOKENS_HEADER = "X-Max-Tokens-Applied"


def applied_header(max_tokens: int | None) -> dict[str, str]:
    return {MAX_TOKENS_HEADER: str(max_tokens) if max_tokens is not None else "none"}


def platform_settings(
    registry: ModelRegistry,
    generate_timeout_ms: float | None,
    chat: ChatService | None = None,
    max_hosted_attempts: int = DEFAULT_MAX_HOSTED_ATTEMPTS,
) -> PlatformSettings:
    """This instance's values at the top of the settings' levels (docs/39)."""
    first = chat.first_token_timeout_ms if chat is not None else DEFAULT_FIRST_TOKEN_TIMEOUT_MS
    total = chat.total_timeout_ms if chat is not None else DEFAULT_TOTAL_TIMEOUT_MS
    return PlatformSettings(
        tiers=registry.names,
        reserved_model_names=reserved_names(registry.names, registry.valid_names),
        generate_timeout_ms=generate_timeout_ms,
        chat_first_token_ms=first,
        chat_total_timeout_ms=total,
        max_hosted_attempts=max_hosted_attempts,
    )


def default_platform_settings(
    max_hosted_attempts: int = DEFAULT_MAX_HOSTED_ATTEMPTS,
) -> PlatformSettings:
    """The platform values the server factories run with (``DEFAULT_OVERLOAD``'s timeout, the
    chat service's defaults, the profiles' tiers and aliases), for the operator's CLI, which
    has no registry. A test keeps it equal to the default server's."""
    tiers = tuple(tier.value for tier in Tier)
    return PlatformSettings(
        tiers=tiers,
        reserved_model_names=reserved_names(tiers, (PROFILES[tier].name for tier in Tier)),
        generate_timeout_ms=DEFAULT_OVERLOAD.request_timeout_ms,
        chat_first_token_ms=DEFAULT_FIRST_TOKEN_TIMEOUT_MS,
        chat_total_timeout_ms=DEFAULT_TOTAL_TIMEOUT_MS,
        max_hosted_attempts=max_hosted_attempts,
    )


def create_app(
    registry: ModelRegistry,
    clock: Clock,
    *,
    applications: ApplicationAccess | None = None,
    overload: OverloadPolicy | None = None,
    metrics: MetricsStore | None = None,
    series: TimeSeriesStore | None = None,
    events: EventBus | None = None,
    dashboard_dir: Path | None = None,
    load_control: bool = False,
    seed: int = 0,
    autostart_load: str | None = None,
    usage_store: UsageStore | None = None,
    epoch: EpochSource | None = None,
    usage_retention_ms: float | None = None,
    playground_keys: bool = False,
    local_only: LocalOnlyPolicy | None = None,
    accounts: AccountAccess | None = None,
    chat_adapters: Mapping[str, Backend] | None = None,
    providers: ProviderService | None = None,
    modes: ModeStore | None = None,
    max_hosted_attempts: int = DEFAULT_MAX_HOSTED_ATTEMPTS,
    settings: SettingsStore | None = None,
    rules: RuleStore | None = None,
    incidents: IncidentStore | None = None,
    local_destinations: LocalDestinations | None = None,
    counters: SharedCounters | None = None,
    startup_applications: Sequence[Application] | None = None,
    readiness: Readiness | None = None,
    budgets: SharedBudgets | None = None,
    reservation_bounds: Mapping[str, TierBudget] | None = None,
    durable: Durable | None = None,
    evaluator_context: Callable[[], AbstractAsyncContextManager[None]] | None = None,
    on_start: Sequence[Callable[[], Awaitable[None]]] = (),
    channels: ChannelService | None = None,
) -> FastAPI:
    """Build the API around an already wired registry.

    Tests pass a ``SimClock`` and a registry built on it; production uses ``WallClock``
    (see ``create_default_app``). Each model's scheduler loop runs as a background asyncio
    task on the server's event loop: the worker starts it on the first request and it
    parks when the model is idle. Shutdown stops them through ``registry.aclose()``.

    With ``applications``, every generate request must carry an API key
    (``Authorization: Bearer <key>``); it is served for the key's application, within that
    application's plan, limits and budget, and charged to it. Without ``applications``
    there are no keys and no applications: any model may be used and nothing is charged,
    and the request path has no code for any of it.

    ``local_only`` keeps monitoring, metrics, the overload view, load control, the
    Playground and the dashboard to this machine (``gateway.localonly``, docs/25). Both server
    factories always set it; tests that build an app directly set it when they test it.

    ``playground_keys`` (simulated live mode only; needs an in-memory application store)
    issues one throwaway key per application at startup, kept in memory, and serves
    ``POST /v1/playground/generate`` with them: the dashboard names an application and the
    request goes through the same key check as any other.

    With ``overload``, requests are cancelled after its timeout, and a request that sets
    ``allow_downgrade`` is moved to a cheaper model while its own is missing its SLO.
    Queue limits are a property of the workers in ``registry``.

    ``usage_store`` is where the ledger stores a usage event for every charged request
    (default: in memory), and ``epoch`` dates those events and decides the budget periods,
    calendar months in UTC (default: the clock read as epoch time). With
    ``usage_retention_ms`` older events are pruned. See docs/19-usage-store.md and
    docs/21-persistence.md.

    ``metrics`` replaces the default ``MetricsStore``, for a different window or ring size.
    ``series`` and ``events`` do the same for the monitoring time series and event log.
    All three must be built on the same ``clock``.

    ``dashboard_dir`` is a built copy of the monitoring dashboard (``frontend/``), served
    at ``/dashboard``. Without it, or if the folder has no ``index.html``, ``/dashboard``
    answers 404 and says how to build it.

    ``load_control`` lets clients start and stop the seeded load generator inside this
    process, through ``/v1/control/load``. It is off unless asked for: on a gateway
    anyone can reach it is a switch for overloading it. ``seed`` is the seed a run uses
    when the client names none. ``autostart_load`` names a preset to start when the
    server starts.

    On a ``PacedSimClock`` the gateway is in simulated live mode: ``/v1/control`` says so
    and ``/v1/control/speed`` changes how fast simulated time is let through.

    ``modes`` keeps organizations' and applications' fallback modes (default: in memory, all
    ``local_only``), and ``local_destinations`` are the operator's local destinations (default:
    the loopback destinations of ``chat_adapters``, as the server takes them from its backends
    file); with ``providers``, chat requests are routed among the tier and the organization's
    providers under the effective mode (docs/33).

    ``startup_applications`` are the stored applications, read by the caller, for a store whose
    calls wait on a network (Postgres, docs/36): they are checked against the registry as the
    store's would be. Without it they are read here, from a store that never suspends.

    ``/readyz`` answers ready while the shared counters are healthy and ``readiness`` (the
    durable store's own check, if any) says yes; not ready otherwise, nothing more.

    ``budgets`` are budgets shared between instances (``PostgresBudgets``, docs/37); without
    them the ledger is this process's own (``CostLedger``), as before. With them, each admitted
    request reserves its bound, from ``reservation_bounds`` (each model's settings) and its
    prompt, for the request timeout of ``overload`` plus ``RESERVATION_MARGIN_MS``; every
    instance releases expired reservations every 30 s.

    ``durable`` is a store that must be opened on the server's event loop (Postgres): the
    lifespan opens it first, then checks the stored applications' plans and runs ``on_start``
    (the bootstrap admin), before anything else; it is closed last.

    ``evaluator_context`` is entered around the alert evaluator's task: on Postgres,
    ``PostgresDatabase.evaluation``, so the evaluator never takes a request-pool connection
    (the plan's section 11.7).

    ``channels`` are the organizations' alert channels (docs/46), with the notices their
    changes and rule changes queue; without them a rule may name no channel id.
    """
    if metrics is None:
        metrics = MetricsStore(clock)
    request_numbers = itertools.count(1)
    started_ms = clock.now_ms()
    router = Router(applications.templates, registry) if applications is not None else None
    known_applications: list[Application] = []
    if applications is not None:
        # Before any event loop runs the app: a store call that cannot suspend (docs/34), or
        # what the caller read from one that can.
        if startup_applications is not None:
            known_applications = list(startup_applications)
        elif durable is None:
            known_applications = run_now(applications.store.list_applications())
        # With a durable store to open, the lifespan reads and checks them.
        for application in known_applications:
            Router.check_plan(application.plan, registry)
    rate_limiter: RateLimiter | None = None
    if applications is not None:
        rate_limiter = (
            counters.rate_limiter() if counters is not None else RequestRateLimiter(clock)
        )
    ledger = CostLedger(clock, store=usage_store, epoch=epoch, retention_ms=usage_retention_ms)
    timeout_ms = overload.request_timeout_ms if overload is not None else None
    # What the request path reserves against: the shared budgets, or this process's ledger.
    book: Budgets = budgets if budgets is not None else ledger
    if budgets is not None:
        if timeout_ms is None:
            raise ValueError(
                "shared budgets need a request timeout: a reservation expires that long after "
                "it is made, plus a margin (docs/37)"
            )
        missing = [n for n in registry.names if n not in (reservation_bounds or {})]
        if missing:
            raise ValueError(
                "shared budgets need context_tokens and reserve_completion_tokens for every "
                f"tier (budgets in the backends file, docs/37); missing: {', '.join(missing)}"
            )
    hold_ms = (timeout_ms or 0.0) + RESERVATION_MARGIN_MS

    def reservation_tokens(model: str, prompt: str, max_tokens: int | None) -> int:
        """A request's bound (Q2, B4); nothing is held without shared budgets."""
        if budgets is None or reservation_bounds is None:
            return 0
        return reservation_bounds[model].bound(len(prompt.encode("utf-8")), max_tokens)

    slo_policy = overload.slo if overload is not None else None
    bus = events if events is not None else EventBus(clock)
    # Monitoring only watches: it is told what happened and decides nothing.
    monitoring = Monitor(
        clock,
        registry,
        metrics,
        series=series if series is not None else TimeSeriesStore(clock),
        events=bus,
        ledger=book if applications is not None else None,
        applications=applications.store if applications is not None else None,
        slo_models=tuple(slo_policy.p99_ms) if slo_policy is not None else (),
    )
    monitor = (
        SloMonitor(slo_policy, clock, on_transition=monitoring.slo_transition)
        if slo_policy is not None
        else None
    )

    # This instance's values at the top of the settings' levels (docs/39): the chat service is
    # built on the same defaults.
    settings_platform = platform_settings(
        registry, timeout_ms, max_hosted_attempts=max_hosted_attempts
    )

    # Where load control reads an application's settings (docs/39): the given store, or the
    # in-memory application store's own. The request path reads them with the key instead.
    settings_store = settings
    if settings_store is None and applications is not None:
        if isinstance(applications.store, InMemoryApplicationStore):
            settings_store = applications.store.settings
        elif load_control or autostart_load is not None:
            raise ValueError("load control with a database needs its settings store (settings=)")

    # Fallback modes and the providers' circuit breakers (docs/33). The router owns the
    # breakers and publishes their transitions; nothing reads them back from the bus.
    mode_store = modes if modes is not None else InMemoryModeStore()
    # In memory the modes and the settings are two stores: the hosted policy reads both (docs/42).
    if (
        isinstance(mode_store, InMemoryModeStore)
        and mode_store.settings is None
        and isinstance(settings_store, InMemorySettingsStore)
    ):
        mode_store.settings = settings_store

    def breaker_moved(moved: Transition) -> None:
        bus.publish(
            "circuit_breaker",
            provider=moved.provider_id,
            state=moved.state.value,
            open_ms=moved.open_ms,
        )

    # Shared: every instance's breaker state is one; the slow-provider warning stays this
    # instance's own (docs/35).
    breakers: BreakerStore
    if counters is not None:
        breakers, warnings = counters.breakers(breaker_moved), SlowWarnings(clock)
    else:
        own = Breakers(clock, breaker_moved)
        breakers, warnings = own, own.warnings
    # The reports, off the request path but a half-open probe's (docs/41); flushed at shutdown.
    reports = BreakerReports(breakers, clock, on_drop=monitoring.breaker_report_dropped)

    # The alert recorder (docs/43): told how each keyed request ended. Its rollups are this
    # process's own, written at once, or with shared counters every instance's in Valkey,
    # flushed every 10 s off the request path. Nothing on the request path reads them.
    alert_note: ContextVar[AlertNote | None] = ContextVar("alert_note", default=None)
    alert_recorder: AlertRecorder | None = None
    alert_rollups: AlertRollups | None = None
    shared_rollups: SharedAlertRollups | None = None
    flusher: RollupFlusher | None = None
    # When this process's own rollups began: a window reaching before it says so (docs/51).
    rollups_started_ms: float | None = None
    if applications is not None:
        alert_epoch = epoch if epoch is not None else ClockEpoch(clock)
        if counters is not None:
            shared_rollups = counters.alert_rollups()
            alert_rollups = shared_rollups
            alert_recorder = AlertRecorder(alert_epoch)

            def rollups_dropped(entries: int) -> None:
                logging.getLogger("gateway.alerts").warning(
                    "alert rollups dropped: %d application minutes not sent", entries
                )

            flusher = RollupFlusher(
                alert_recorder,
                shared_rollups,
                clock,
                secrets.token_hex(8),
                on_dropped=rollups_dropped,
            )
        else:
            local_rollups = InMemoryAlertRollups()
            alert_rollups = local_rollups
            rollups_started_ms = alert_epoch.epoch_ms()
            alert_recorder = AlertRecorder(alert_epoch, sink=local_rollups)

    def noted() -> AlertNote | None:
        return alert_note.get()

    # Filled in further down, once the generate handler it sends through exists.
    load: LoadController | None = None

    # The alert rules, and with an incident store the evaluator (docs/45): one at a time across
    # instances by the lease; without one the heartbeat is "disabled" (simulated live mode).
    rule_store: RuleStore = rules if rules is not None else InMemoryRuleStore()
    evaluator: AlertEvaluator | None = None
    # Delivery (docs/46): with channels that deliver and an evaluator, every instance runs a
    # worker, and test sends go through the same attempt.
    delivery_worker: DeliveryWorker | None = None
    test_sends: TestSends | None = None
    ceilings: CeilingsWatch | None = None
    instance_id = secrets.token_hex(8)
    setup = channels.delivery if channels is not None else None
    if (
        setup is not None
        and incidents is not None
        and channels is not None
        and channels.notices is not None
    ):
        alert_log = logging.getLogger("gateway.alerts")

        def attempted(report: AttemptReport) -> None:
            # A count, and for a failure an event and a log line: the channel id and the code.
            monitoring.alert_delivery(report.channel_type, report.code)
            if report.code == RESOLVER_BUSY:
                # Every lookup thread held by lookups past their budget (docs/48).
                bus.publish("alert_smtp_resolver_busy", channel_id=report.channel_id)
            if report.code != DELIVERED:
                bus.publish("alert_delivery_failed", channel_id=report.channel_id,
                            code=report.code)  # fmt: skip
                alert_log.warning(
                    "alert delivery failed: channel %s, %s", report.channel_id, report.code
                )

        def caps_differ(names: list[str]) -> None:
            bus.publish("alert_caps_differ", caps=names)

        delivery_worker = DeliveryWorker(
            channels.notices.outbox,
            channels.store,
            channels.vault,
            setup.redactor,
            store_clock=channels.store_clock,
            clock=clock,
            epoch=epoch if epoch is not None else ClockEpoch(clock),
            client=setup.client,
            rng=random.Random(secrets.randbits(64)),
            random_bytes=secrets.token_bytes,
            on_attempt=attempted,
            email=setup.email,
        )
        if setup.email is not None:
            resolver = setup.email.sender.resolver
            monitoring.watch_smtp_lookups(lambda: resolver.abandoned)
        test_sends = TestSends(
            delivery_worker,
            counters.channel_tests(
                setup.caps.test_sends_per_channel, setup.caps.test_sends_per_organization
            )
            if counters is not None
            else InMemoryConnectionTestCounter(
                clock,
                setup.caps.test_sends_per_channel,
                setup.caps.test_sends_per_organization,
                reason=Reason.TEST_SEND_LIMITED,
                what=("a channel", "an organization's channels"),
            ),
            channels.audit,
            epoch if epoch is not None else ClockEpoch(clock),
            secrets.token_bytes,
        )
        ceilings = CeilingsWatch(setup.ceilings, setup.caps, instance_id, on_differ=caps_differ)
    if incidents is not None and applications is not None:
        app_store = applications.store
        settings_of, settings_many = alert_settings(settings_store, settings_platform)

        usage_source: Budgets = budgets if budgets is not None else ledger

        def heartbeat_moved(was: HeartbeatState, now: HeartbeatState) -> None:
            # An event and a log line at each change, for an operator without Prometheus.
            if now in (HeartbeatState.STALE, HeartbeatState.UNKNOWN):
                bus.publish("alert_evaluator_stale", state=now.value)
                logging.getLogger("gateway.alerts").warning(
                    "alert evaluator heartbeat: %s (was %s)", now.value, was.value
                )
            elif was in (HeartbeatState.STALE, HeartbeatState.UNKNOWN):
                bus.publish("alert_evaluator_recovered", state=now.value)
                logging.getLogger("gateway.alerts").warning(
                    "alert evaluator heartbeat: %s (was %s)", now.value, was.value
                )

        evaluator = AlertEvaluator(
            incidents=incidents,
            rules=rule_store,
            rollups=alert_rollups,
            signals=_DurableSignals(
                app_store,
                usage_source.usage,
                settings_of,
                providers,
                breakers,
                epoch if epoch is not None else ClockEpoch(clock),
                usage_many=(
                    usage_source.usage_many if isinstance(usage_source, BulkUsage) else None
                ),
                settings_many=settings_many,
            ),
            clock=clock,
            holder=instance_id,
            random_bytes=secrets.token_bytes,
            on_heartbeat_change=heartbeat_moved,
            # Queued for delivery only where there is a worker to send them.
            notices=channels.notices if delivery_worker is not None and channels else None,
            ceilings=ceilings,
        )

    async def evaluate() -> None:
        """The evaluator's task, in its own context (its own pool on Postgres)."""
        assert evaluator is not None
        if evaluator_context is None:
            await evaluator.run()
            return
        async with evaluator_context():
            await evaluator.run()

    async def deliver() -> None:
        """The delivery worker's task, on the evaluator's pool on Postgres (the A4 plan's
        section 5): no connection held during a network call."""
        assert delivery_worker is not None
        if evaluator_context is None:
            await delivery_worker.run()
            return
        async with evaluator_context():
            await delivery_worker.run()

    def alert_status() -> AlertEvaluatorGroup:
        """The heartbeat from the evaluator's memory: no store call (docs/45)."""
        if evaluator is None:
            return DISABLED_EVALUATOR
        status = evaluator.status()
        return AlertEvaluatorGroup(
            available=status.state is not HeartbeatState.UNKNOWN,
            state=status.state.value,
            last_cycle_age_ms=status.last_cycle_age_ms,
            holder=status.holder,
            last_evaluation_ms=status.last_evaluation_ms,
            evaluation_failures=status.failures,
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Shared counters are checked before anything is served: a store that could lose a
        # count, or a clock too far from it, refuses the start (docs/35).
        watching = None
        if durable is not None:
            # First: every other start-up step reads it.
            await durable.open()
            if applications is not None:
                for application in await applications.store.list_applications():
                    Router.check_plan(application.plan, registry)
            for start in on_start:
                await start()
        if counters is not None:

            async def restarted(_: str) -> None:
                monitoring.state_store_restarted("valkey")
                if shared_rollups is not None:
                    # Every minute up to now lost its counts (docs/43).
                    minute = int((epoch if epoch is not None else ClockEpoch(clock)).epoch_ms()
                                 // MINUTE_MS)  # fmt: skip
                    try:
                        await shared_rollups.note_restart(minute)
                    except StateUnavailableError:
                        logging.getLogger("gateway.alerts").warning(
                            "alert rollups: the restart could not be marked"
                        )

            await counters.watch.start(restarted)
            watching = asyncio.create_task(counters.watch.run(restarted))
        if load is not None and autostart_load is not None:
            await load.start(autostart_load, seed)
        flushing = asyncio.create_task(flusher.run()) if flusher is not None else None
        evaluating = asyncio.create_task(evaluate()) if evaluator is not None else None
        delivering = (
            asyncio.create_task(deliver())
            if delivery_worker is not None and evaluator is not None
            else None
        )
        # Retention runs beside the server, never on a request (docs/21).
        retainer = budgets if budgets is not None else ledger
        retention = (
            asyncio.create_task(run_retention(retainer, clock))
            if retainer.retention_ms is not None
            else None
        )

        def expired(each: Expired) -> None:
            monitoring.reservation_expired(each.application_id, each.tokens)

        # Reservations whose instance died are released by every instance's sweep (docs/37).
        sweeping = (
            asyncio.create_task(run_sweep(budgets, clock, expired)) if budgets is not None else None
        )
        account_retention = (
            asyncio.create_task(run_account_retention(accounts.service, clock))
            if accounts is not None
            else None
        )
        # Provider keys out of every log record while the server runs (docs/32), and a
        # warning for each key no loaded master key can read.
        remove_redaction = install_redaction(providers.redactor) if providers is not None else None
        # Leased channel secrets too, when their redactor is not the providers' (docs/46).
        remove_channel_redaction = (
            install_redaction(setup.redactor)
            if setup is not None and (providers is None or setup.redactor is not providers.redactor)
            else None
        )
        if providers is not None:
            await providers.warn_unreadable()
        yield
        if delivering is not None:
            delivering.cancel()
        if remove_channel_redaction is not None:
            remove_channel_redaction()
        if remove_redaction is not None:
            remove_redaction()
        if retention is not None:
            retention.cancel()
        if sweeping is not None:
            sweeping.cancel()
        if account_retention is not None:
            account_retention.cancel()
        if load is not None:
            await load.stop()
        await registry.aclose()
        if applications is not None:
            await applications.keys.flush_last_used()
        # Before the counters' client closes: what is queued is sent within the limit, the
        # rest dropped and counted (docs/41).
        await reports.flush()
        if evaluating is not None:
            evaluating.cancel()
        if flushing is not None:
            flushing.cancel()
        if flusher is not None:
            await flusher.flush()  # what this instance recorded since the last flush
        if watching is not None:
            watching.cancel()
        if counters is not None:
            await counters.aclose()
        if budgets is not None:
            await budgets.aclose()
        if durable is not None:
            await durable.aclose()

    app = FastAPI(title="Cost-aware multi-model inference gateway", lifespan=lifespan)
    app.state.settings_platform = settings_platform
    app.state.alert_recorder = alert_recorder
    app.state.alert_rollups = alert_rollups
    app.state.alert_flusher = flusher
    app.state.alert_evaluator = evaluator
    app.state.alert_delivery = delivery_worker
    app.state.alert_test_sends = test_sends
    if alert_recorder is not None:
        app.add_middleware(AlertOutcomeMiddleware, recorder=alert_recorder, note=alert_note)
    if local_only is not None:
        app.add_middleware(LocalOnlyMiddleware, policy=local_only)
    if accounts is not None:
        app.state.accounts = accounts
        _auth_routes(app, accounts)
        # An application's alert rules (docs/44): in the durable store given, or in memory.
        rule_service = RuleService(
            rule_store,
            accounts.service.audit,
            epoch if epoch is not None else ClockEpoch(clock),
            alert_rollups,
            secrets.token_bytes,
            notices=channels.notices if channels is not None else None,
        )
        # Applications and keys over HTTP need both: a session to act, applications to act on.
        management = (
            ManagementService(
                applications.keys,
                applications.templates,
                accounts.service.audit,
                epoch if epoch is not None else ClockEpoch(clock),
                limits=KeyLimits(),
                # Each new application with its default alert rules, in its transaction.
                on_created=rule_service.create_defaults,
            )
            if applications is not None
            else None
        )
        _admin_routes(app, accounts, management, mode_store)
        if management is not None and alert_rollups is not None:
            _usage_routes(
                app,
                accounts,
                management,
                alert_rollups,
                epoch if epoch is not None else ClockEpoch(clock),
                rollups_started_ms,
                clock,
            )
        if providers is not None:
            app.state.providers = providers
            _provider_routes(app, accounts, providers, breakers, warnings)
            if management is not None:
                _mode_routes(
                    app,
                    accounts,
                    ModeService(
                        mode_store,
                        accounts.service.audit,
                        epoch if epoch is not None else ClockEpoch(clock),
                    ),
                )

        async def organization_providers(organization_id: str) -> frozenset[str]:
            """The provider ids an application's ``allowed_providers`` may name (S13)."""
            if providers is None:
                return frozenset()
            return frozenset(c.id for c in await providers.store.list_for(organization_id))

        if management is not None and applications is not None:
            _alert_rule_routes(app, accounts, rule_service)
            if channels is not None:
                _channel_routes(app, accounts, channels, test_sends)
            _incident_routes(
                app,
                accounts,
                incidents
                if incidents is not None
                else InMemoryIncidentStore(epoch if epoch is not None else ClockEpoch(clock)),
                epoch if epoch is not None else ClockEpoch(clock),
                channels.notices.outbox
                if channels is not None and channels.notices is not None
                else None,
            )
        if management is not None and applications is not None and settings_store is not None:
            _settings_routes(
                app,
                accounts,
                SettingsService(
                    settings_store,
                    applications.store,
                    accounts.service.audit,
                    epoch if epoch is not None else ClockEpoch(clock),
                    settings_platform,
                    providers=organization_providers,
                ),
                settings_store,
                mode_store,
            )
        # Outermost, so even the local-only guard's refusals of /v1/admin are not cached; and
        # every answer of the capabilities endpoint, refusals included (docs/50).
        app.add_middleware(NoStoreMiddleware, prefix=CAPABILITIES_PATH)
        app.add_middleware(NoStoreMiddleware)
    app.state.slo_monitor = monitor
    app.state.monitoring = monitoring
    app.state.clock = clock

    @app.exception_handler(UnknownModelError)
    async def unknown_model(_: Request, exc: UnknownModelError) -> JSONResponse:
        return _error(
            404,
            "unknown_model",
            f"Unknown model {exc.model!r}.",
            valid_models=list(exc.valid_names),
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {"field": ".".join(str(part) for part in e["loc"][1:]), "message": e["msg"]}
            for e in exc.errors()
        ]
        for detail in details:
            if detail["field"] == "tenant_id":
                detail["message"] = (
                    "tenant_id is no longer accepted: the API key says which application "
                    "a request is for ('Authorization: Bearer <key>')."
                )
        return _error(
            422,
            "invalid_request",
            "The request is not valid.",
            details=details,
            valid_models=list(registry.valid_names),
        )

    @app.exception_handler(RequestTooLargeError)
    async def too_large(_: Request, exc: RequestTooLargeError) -> JSONResponse:
        return _error(413, "request_too_large", str(exc))

    @app.exception_handler(BackendError)
    async def backend_failed(_: Request, exc: BackendError) -> JSONResponse:
        return JSONResponse({"error": _backend_failure(exc)}, status_code=502)

    @app.exception_handler(ApiKeyRequiredError)
    async def api_key_required(_: Request, exc: ApiKeyRequiredError) -> JSONResponse:
        return _error(401, "api_key_required", str(exc), headers={"WWW-Authenticate": "Bearer"})

    @app.exception_handler(InvalidApiKeyError)
    async def invalid_api_key(_: Request, exc: InvalidApiKeyError) -> JSONResponse:
        # One message for every bad key, so a key cannot be probed.
        return _error(
            401,
            "invalid_api_key",
            INVALID_KEY_MESSAGE,
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        )

    @app.exception_handler(StateUnavailableError)
    async def state_unavailable(_: Request, exc: StateUnavailableError) -> JSONResponse:
        # A shared counter could not answer: what it would have decided is refused (docs/35).
        # Never which store or why: that is in the log.
        return _error(
            503,
            "state_unavailable",
            "The gateway cannot check this request's limits right now. Try again later.",
            headers=_NO_STORE,
        )

    @app.exception_handler(SettingsUnreadableError)
    async def settings_unreadable(request: Request, exc: SettingsUnreadableError) -> JSONResponse:
        # A row that cannot be judged: refused as a store that cannot answer is, never served
        # with a looser value. The log names the scope, its version and the check, never the
        # document (docs/39).
        logging.getLogger("gateway.settings").warning(
            "settings unreadable: %s %s version %d, check %s",
            exc.scope.value,
            exc.scope_id,
            exc.version,
            exc.check,
        )
        return await state_unavailable(request, StateUnavailableError("settings"))

    @app.exception_handler(RateLimitedError)
    async def rate_limited(_: Request, exc: RateLimitedError) -> JSONResponse:
        retry_after_s = max(1, math.ceil(exc.retry_after_ms / 1000))
        return _error(
            429,
            "rate_limited",
            f"{exc} Retry in {retry_after_s} seconds.",
            headers={"Retry-After": str(retry_after_s)},
            limit_per_minute=exc.limit,
            retry_after_s=retry_after_s,
        )

    @app.exception_handler(ModelNotAllowedError)
    async def model_not_allowed(_: Request, exc: ModelNotAllowedError) -> JSONResponse:
        return _error(
            403,
            "model_not_in_plan",
            str(exc),
            plan=exc.plan.name,
            requested_model=exc.model,
            allowed_models=list(exc.plan.models),
            plans_with_model=list(exc.plans_with_model),
        )

    @app.exception_handler(BudgetExceededError)
    async def budget_exhausted(_: Request, exc: BudgetExceededError) -> JSONResponse:
        retry_after_s = max(1, math.ceil(exc.retry_after_ms / 1000))
        return _error(
            429,
            "budget_exhausted",
            f"{exc} {exc.when(retry_after_s)}",
            headers={"Retry-After": str(retry_after_s)},
            used_tokens=exc.used_tokens,
            budget_tokens=exc.budget_tokens,
            retry_after_s=retry_after_s,
        )

    def failure(
        exc: QueueFullError | RequestTimeoutError, model: str, application_id: str | None
    ) -> tuple[int, dict[str, object], dict[str, str] | None]:
        """Status, error body and headers for a request the model could not serve."""
        if isinstance(exc, QueueFullError):
            monitoring.queue_full(model, application_id, exc)
            retry_after_s = max(1, math.ceil(exc.retry_after_ms / 1000))
            return (
                429,
                {
                    "code": "queue_full",
                    "message": f"Model {model!r} is overloaded: {exc.waiting} requests are "
                    f"already queued or in progress (limit {exc.limit}). "
                    f"Retry in {retry_after_s} seconds.",
                    "model": model,
                    "waiting": exc.waiting,
                    "limit": exc.limit,
                    "retry_after_s": retry_after_s,
                },
                {"Retry-After": str(retry_after_s)},
            )
        if monitor is not None:
            # A request that timed out took at least this long; without this the monitor
            # would only ever see the requests that made it.
            monitor.record(model, exc.timeout_ms)
        monitoring.timed_out(model, application_id, exc.timeout_ms)
        return (
            504,
            {
                "code": "request_timeout",
                "message": f"Model {model!r} did not finish the request within "
                f"{exc.timeout_ms:g} ms. It was cancelled and has not been charged.",
                "model": model,
                "timeout_ms": exc.timeout_ms,
            },
            None,
        )

    async def settle(
        result: RequestResult,
        *,
        request_id: str,
        model: str,
        application_id: str | None,
        price_multiplier: float,
        live_tokens: int,
        hold: _Hold | None,
    ) -> GenerateResponse:
        """Record a finished request everywhere it counts and build its response body."""
        metrics.record(model, result)
        if monitor is not None:
            monitor.record(model, result.latency_ms)
        cost_usd = result.cost_usd
        if hold is not None:
            cost_usd = await hold.settle(result, model=model, price_multiplier=price_multiplier)
        monitoring.completed(
            model, application_id, result, billed_cost_usd=cost_usd, live_tokens=live_tokens
        )
        if (note := noted()) is not None:
            note.ttft_ms, note.total_ms = result.time_to_first_token_ms, result.latency_ms
        return GenerateResponse(
            id=request_id,
            # A simulated run generates no text, so say so in its place.
            text=result.text or f"[simulated output, {result.tokens_out} tokens]",
            model=model,
            usage=Usage(
                prompt_tokens=result.tokens_in,
                cached_tokens=result.cached_tokens,
                completion_tokens=result.tokens_out,
                total_tokens=result.tokens_in + result.tokens_out,
            ),
            timing=Timing(
                queue_ms=result.queue_wait_ms,
                ttft_ms=result.time_to_first_token_ms,
                total_ms=result.latency_ms,
            ),
            cost=Cost(usd=cost_usd),
        )

    def choice_error(message: str, field: str) -> JSONResponse:
        return _error(
            422,
            "invalid_request",
            "The request is not valid.",
            details=[{"field": field, "message": message}],
            valid_models=list(registry.valid_names),
        )

    async def serve(
        body: GenerateBody,
        response: Response,
        caller: Application | None,
        settings: EffectiveSettings | None = None,
    ) -> GenerateResponse | JSONResponse | StreamingResponse:
        """Serve one generate request; ``caller`` is None exactly when there are no
        applications, and ``settings`` are then None too. However it ends, a reservation it
        made and did not settle is released here, or by its stream once one has taken it over
        (docs/37)."""
        held: list[_Hold] = []
        try:
            return await serve_held(body, response, caller, settings, held)
        finally:
            if held and not held[0].streaming:
                await held[0].release()

    async def serve_held(
        body: GenerateBody,
        response: Response,
        caller: Application | None,
        settings: EffectiveSettings | None,
        held: list[_Hold],
    ) -> GenerateResponse | JSONResponse | StreamingResponse:
        hold: _Hold | None = None
        quality = body.quality
        if body.model is None and quality is None and settings is not None:
            quality = settings.default_quality
        if body.model is None and quality is None:
            return choice_error("Give either a model or a quality.", "model")
        if body.model is not None and quality is not None:
            return choice_error("Give a model or a quality, not both.", "quality")

        price_multiplier = 1.0
        allowed_models: tuple[str, ...] = registry.names
        max_tokens = body.max_tokens
        allow_downgrade = body.allow_downgrade
        request_timeout_ms = timeout_ms
        if caller is None:
            if body.model is not None:
                model = registry.resolve(body.model)
            else:
                assert quality is not None
                model = pick_by_quality(registry.names, quality)
        else:
            assert router is not None
            assert rate_limiter is not None
            assert settings is not None
            if (
                caller.max_tokens is not None
                and max_tokens is not None
                and max_tokens > caller.max_tokens
            ):
                return choice_error(
                    f"At most {caller.max_tokens} on this application's plan.", "max_tokens"
                )
            if max_tokens is None:
                # Applied and enforced as the request's own (docs/39, Q4).
                max_tokens = settings.default_max_tokens
            if "allow_downgrade" not in body.model_fields_set:
                allow_downgrade = settings.allow_downgrade_default
            request_timeout_ms = settings.generate_timeout_ms
            try:
                await rate_limiter.admit(caller.id, caller.requests_per_minute)
            except RateLimitedError:
                monitoring.rate_limited(caller.id)
                raise
            route = router.route(
                caller, model=body.model, quality=quality, aliases=settings.model_aliases
            )
            try:
                reservation = await book.reserve(
                    caller.id,
                    route.plan.monthly_token_budget,
                    reservation_tokens(route.model, body.prompt, max_tokens),
                    hold_ms=hold_ms,
                )
            except BudgetExceededError as exc:
                monitoring.budget_exhausted(route.model, exc)
                raise
            hold = _Hold(book, reservation)
            held.append(hold)
            model = route.model
            price_multiplier = route.plan.price_multiplier
            allowed_models = route.plan.models

        downgraded_from: str | None = None
        if monitor is not None and allow_downgrade:
            # Only ever to a cheaper model the caller is already entitled to.
            healthier = pick_downgrade(allowed_models, model, monitor.is_degraded)
            if healthier != model:
                downgraded_from, model = model, healthier

        request_id = f"req-{next(request_numbers)}"
        application_id = caller.id if caller is not None else None
        headers = {"X-Model-Used": model, **applied_header(max_tokens)}
        if downgraded_from is not None:
            headers["X-Downgraded-From"] = downgraded_from
            monitoring.downgraded(application_id, downgraded_from, model)
            if (note := noted()) is not None:
                note.downgraded = True

        # Token events for a streaming response; None marks the end of the request.
        tokens: asyncio.Queue[tuple[int, float, str] | None] = asyncio.Queue()
        arrived_ms = clock.now_ms()
        # Output tokens reported to monitoring as they were produced.
        live_tokens = 0

        def on_token(index: int, at_ms: float, text: str) -> None:
            nonlocal live_tokens
            live_tokens += 1
            monitoring.token(model)
            if body.stream:
                tokens.put_nowait((index, at_ms - arrived_ms, text))

        request = GenerateRequest(request_id, model, body.prompt, max_tokens)

        async def tracked() -> RequestResult:
            monitoring.request_started(model)
            try:
                return await registry.submit(request, on_token=on_token)
            finally:
                monitoring.request_ended(model)

        work = tracked()
        guarded = (
            run_with_timeout(clock, work, request_timeout_ms)
            if request_timeout_ms is not None
            else work
        )

        if not body.stream:
            try:
                result = await guarded
            except (QueueFullError, RequestTimeoutError) as exc:
                status, error, error_headers = failure(exc, model, application_id)
                return JSONResponse(
                    {"error": error},
                    status_code=status,
                    headers={**(error_headers or {}), **applied_header(max_tokens)},
                )
            payload = await settle(
                result,
                request_id=request_id,
                model=model,
                application_id=application_id,
                price_multiplier=price_multiplier,
                live_tokens=live_tokens,
                hold=hold,
            )
            response.headers.update(headers)
            response.headers["X-Cost-USD"] = f"{payload.cost.usd:.10f}"
            return payload

        task = asyncio.ensure_future(guarded)
        task.add_done_callback(lambda _: tokens.put_nowait(None))
        try:
            first = await tokens.get()
        except asyncio.CancelledError:
            task.cancel()
            raise
        if first is None:
            # It ended before producing a token, so it failed. Nothing has been sent yet:
            # answer exactly as a non-streaming request would.
            failed = task.exception()
            if isinstance(failed, QueueFullError | RequestTimeoutError):
                status, error, error_headers = failure(failed, model, application_id)
                return JSONResponse(
                    {"error": error},
                    status_code=status,
                    headers={**(error_headers or {}), **applied_header(max_tokens)},
                )
            task.result()
            raise AssertionError("a request finished without producing a token")

        async def events(item: tuple[int, float, str] | None) -> AsyncIterator[str]:
            try:
                while item is not None:
                    index, t_ms, text = item
                    yield _sse("token", {"index": index, "token": text, "t_ms": t_ms})
                    item = await tokens.get()
                if task.cancelled():
                    yield _sse(
                        "error", {"code": "cancelled", "message": "The request was cancelled."}
                    )
                elif (failed := task.exception()) is None:
                    payload = await settle(
                        task.result(),
                        request_id=request_id,
                        model=model,
                        application_id=application_id,
                        price_multiplier=price_multiplier,
                        live_tokens=live_tokens,
                        hold=hold,
                    )
                    yield _sse("done", payload.model_dump())
                elif isinstance(failed, RequestTimeoutError):
                    yield _sse("error", failure(failed, model, application_id)[1])
                elif isinstance(failed, BackendError):
                    yield _sse("error", _backend_failure(failed))
                else:
                    yield _sse(
                        "error", {"code": "internal_error", "message": "The request failed."}
                    )
            finally:
                # The client went away, or the stream ended early: stop the work so it
                # gives its batch slot and KV tokens back, and its reservation.
                if not task.done():
                    task.cancel()
                if hold is not None:
                    await hold.release()

        if hold is not None:
            hold.streaming = True
        return StreamingResponse(
            events(first),
            media_type="text/event-stream",
            headers={**headers, "Cache-Control": "no-cache"},
        )

    async def usage_body(caller: Application) -> ApplicationUsageResponse:
        plan = caller.plan
        usage = await book.usage(caller.id)
        return ApplicationUsageResponse(
            application_id=caller.id,
            name=caller.name,
            plan=plan.name,
            allowed_models=list(plan.models),
            price_multiplier=plan.price_multiplier,
            max_tokens=caller.max_tokens,
            requests_per_minute=caller.requests_per_minute,
            period=Period(start_ms=usage.period_start_ms, end_ms=usage.period_end_ms),
            budget_tokens=plan.monthly_token_budget,
            used_tokens=usage.total.billed_tokens,
            remaining_tokens=max(0, plan.monthly_token_budget - usage.total.billed_tokens),
            total=_totals(usage.total),
            by_model={name: _totals(u) for name, u in usage.by_model.items()},
        )

    if applications is None:
        # No applications: the request path has no key check at all.
        @app.post("/v1/generate", response_model=GenerateResponse)
        @public_route("an application API key, checked by the route")
        async def generate(
            body: GenerateBody, response: Response
        ) -> GenerateResponse | JSONResponse | StreamingResponse:
            return await serve(body, response, None)

        @app.get("/v1/applications/me/usage")
        @public_route("an application API key, checked by the route")
        async def usage_disabled() -> JSONResponse:
            return _error(
                404, "applications_disabled", "This gateway has no applications configured."
            )

    else:
        keys = applications.keys

        chat = ChatService(registry, clock, chat_adapters)
        platform = settings_platform
        assert platform == platform_settings(
            registry, timeout_ms, chat, max_hosted_attempts=max_hosted_attempts
        )
        documents = DocumentCache()

        def keyed(application: Application) -> Keyed:
            """A request's settings, decided once from the rows its key lookup read (docs/39):
            a missing row is the defaults; a row that cannot be judged raises
            ``SettingsUnreadableError`` (503). The application as the request sees it."""
            effective = resolve(platform, application, application.stored_settings, documents)
            return Keyed(effective_application(application, effective), effective, application)

        # async, so it runs on the event loop: a dependency run on a thread would let a
        # SimClock's time jump, and the SQLite connection belongs to the loop's thread.
        async def authenticate(authorization: str | None = Header(default=None)) -> Keyed:
            scheme, _, credentials = (authorization or "").strip().partition(" ")
            if scheme.lower() != "bearer" or not credentials.strip():
                raise ApiKeyRequiredError()
            found = keyed(await keys.authenticate(credentials.strip()))
            if (note := noted()) is not None:
                note.application_id = found.application.id
            return found

        @app.post("/v1/generate", response_model=GenerateResponse)
        @public_route("an application API key, checked by the route")
        async def generate_for_application(
            body: GenerateBody,
            response: Response,
            caller: Keyed = Depends(authenticate),
        ) -> GenerateResponse | JSONResponse | StreamingResponse:
            return await serve(body, response, caller.application, caller.settings)

        @app.get("/v1/applications/me/usage", response_model=ApplicationUsageResponse)
        @public_route("an application API key, checked by the route")
        async def application_usage(
            caller: Keyed = Depends(authenticate),
        ) -> ApplicationUsageResponse:
            return await usage_body(caller.application)

        if local_destinations is None:
            local_destinations = LocalDestinations.loopback_of(
                a.base_url for a in (chat_adapters or {}).values() if isinstance(a, HttpAdapter)
            )
        chat_router = ChatRouter(
            chat,
            mode_store,
            breakers,
            local_destinations,
            providers,
            providers.client_factory if providers is not None else None,
            warnings=warnings,
            reports=reports,
            max_hosted_attempts=max_hosted_attempts,
        )
        app.state.chat_router = chat_router
        chat_epoch = epoch if epoch is not None else ClockEpoch(clock)

        async def account_chat(
            outcome: ChatOutcome,
            hold: _Hold,
            caller: Application,
            multiplier: float,
            model: str,
            source: ChatSource,
            applied: int | None = None,
        ) -> None:
            """What ``settle`` records for a generate request, for a chat one: metrics, the
            SLO monitor, the ledger and monitoring. No cost is computed for chat: only prices
            an organization enters will be (docs/31), so the ledger records 0."""
            produced = outcome.result.usage.output_tokens
            if applied is not None and produced > applied:
                # Sent as the provider's limit, but its tokens cannot be counted as they stream:
                # what it reports is settled, and said here (docs/39). Ids and counts only.
                logging.getLogger("gateway.settings").warning(
                    "provider %s answered %d completion tokens over the applied max_tokens %d",
                    outcome.provider,
                    produced - applied,
                    applied,
                )
            # Keyed by the routed tier, as for /v1/generate (a worker's result names its
            # profile instead).
            timing = outcome.timing
            metrics.record(model, timing)
            if monitor is not None:
                monitor.record(model, timing.latency_ms)
            billed = await hold.settle(
                timing, model=model, price_multiplier=multiplier, source=source
            )
            monitoring.completed(
                model,
                caller.id,
                timing,
                billed_cost_usd=billed,
                live_tokens=timing.tokens_out,
            )
            if (note := noted()) is not None:
                note.ttft_ms, note.total_ms = timing.time_to_first_token_ms, timing.latency_ms

        def source_headers(trace: RouteTrace | ChatSource) -> dict[str, str]:
            """Where the answer came from: a provider id or ``gateway`` (the gateway's own
            tier), and its class; never a URL. The fallback, when there was one (docs/33)."""
            headers = {
                "X-Provider": trace.provider or GATEWAY_PROVIDER,
                "X-Provider-Class": trace.provider_class or "local",
            }
            if trace.fallback_from is not None:
                headers["X-Fallback-From"] = trace.fallback_from
                headers["X-Fallback-Reason"] = trace.fallback_reason or "unknown"
            return headers

        def chat_headers(routed: RoutedOutcome, model: str) -> dict[str, str]:
            outcome = routed.outcome
            headers = {"X-Model-Used": model, **source_headers(routed.source)}
            if outcome.result.usage.estimated:
                headers["X-Usage-Estimated"] = "true"
            if outcome.result.thinking_blocks_omitted:
                headers["X-Thinking-Blocks-Omitted"] = str(outcome.result.thinking_blocks_omitted)
            return headers

        def usage_view(outcome: ChatOutcome) -> dict[str, object]:
            usage = outcome.result.usage
            view: dict[str, object] = {
                "prompt_tokens": usage.input_tokens,
                "completion_tokens": usage.output_tokens,
                "total_tokens": usage.input_tokens + usage.output_tokens,
            }
            if usage.estimated:
                view["usage_estimated"] = True
            if outcome.result.thinking_blocks_omitted:
                view["thinking_blocks_omitted"] = outcome.result.thinking_blocks_omitted
            return view

        def tool_call_view(call: ToolCall, index: int | None = None) -> dict[str, object]:
            view: dict[str, object] = {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            return {"index": index, **view} if index is not None else view

        async def answer_chat(
            request: Request, authorization: str | None, held: list[_Hold]
        ) -> Response:
            scheme, _, credentials = (authorization or "").strip().partition(" ")
            if scheme.lower() != "bearer" or not credentials.strip():
                return _openai_error(
                    401,
                    "api_key_required",
                    str(ApiKeyRequiredError()),
                    kind="authentication_error",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            try:
                found = keyed(await keys.authenticate(credentials.strip()))
                if (note := noted()) is not None:
                    note.application_id = found.application.id
            except InvalidApiKeyError:
                return _openai_error(
                    401,
                    "invalid_api_key",
                    INVALID_KEY_MESSAGE,
                    kind="authentication_error",
                    headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
                )
            try:
                raw = await request.json()
            except ValueError:
                return _openai_error(400, "invalid_json", "The body is not valid JSON.")
            try:
                body = ChatCompletionBody.model_validate(raw)
            except ValidationError as exc:
                problem = exc.errors()[0]
                param = ".".join(str(part) for part in problem["loc"]) or None
                if problem["type"] == "extra_forbidden":
                    return _openai_error(
                        422,
                        "unsupported_field",
                        f"The field {param!r} is not supported by this gateway.",
                        param=param,
                    )
                return _openai_error(422, "invalid_request", problem["msg"], param=param)
            caller, settings = found.application, found.settings
            quality = body.quality
            if body.model is None and quality is None:
                quality = settings.default_quality
            if (body.model is None) == (quality is None):
                return _openai_error(
                    422, "invalid_request", "Give either a model or a quality.", param="model"
                )
            assert router is not None
            assert rate_limiter is not None
            # Applied and enforced as the request's own when it names none (docs/39, Q4).
            max_tokens = body.max_tokens if body.max_tokens is not None else None
            if caller.max_tokens is not None and (body.max_tokens or 0) > caller.max_tokens:
                return _openai_error(
                    422,
                    "invalid_request",
                    f"At most {caller.max_tokens} on this application's plan.",
                    param="max_tokens",
                )
            try:
                await rate_limiter.admit(caller.id, caller.requests_per_minute)
            except RateLimitedError as exc:
                monitoring.rate_limited(caller.id)
                seconds = max(1, math.ceil(exc.retry_after_ms / 1000))
                return _openai_error(
                    429,
                    "rate_limited",
                    f"{exc} Retry in {seconds} seconds.",
                    kind="rate_limit_error",
                    headers={"Retry-After": str(seconds)},
                )
            if max_tokens is None:
                max_tokens = settings.default_max_tokens
            try:
                route = router.route(
                    caller, model=body.model, quality=quality, aliases=settings.model_aliases
                )
            except UnknownModelError as exc:
                return _openai_error(
                    404, "unknown_model", f"Unknown model {exc.model!r}.", param="model"
                )
            except ModelNotAllowedError as exc:
                return _openai_error(
                    403, "model_not_in_plan", str(exc), kind="permission_error", param="model"
                )
            try:
                reservation = await book.reserve(
                    caller.id,
                    route.plan.monthly_token_budget,
                    reservation_tokens(
                        route.model,
                        # Q2, B4: the messages and tools as sent, in bytes.
                        json.dumps([raw.get("messages"), raw.get("tools")]),
                        max_tokens,
                    ),
                    hold_ms=hold_ms,
                )
            except BudgetExceededError as exc:
                monitoring.budget_exhausted(route.model, exc)
                seconds = max(1, math.ceil(exc.retry_after_ms / 1000))
                return _openai_error(
                    429,
                    "budget_exhausted",
                    f"{exc} {exc.when(seconds)}",
                    kind="rate_limit_error",
                    headers={"Retry-After": str(seconds)},
                )
            hold = _Hold(book, reservation)
            held.append(hold)
            request_id = f"req-{next(request_numbers)}"
            try:
                chat_req = chat_request(body, request_id, route.model)
                if chat_req.max_tokens is None and max_tokens is not None:
                    chat_req = dataclasses.replace(chat_req, max_tokens=max_tokens)
            except ChatInputError as exc:
                return _openai_error(422, "invalid_request", str(exc), param=exc.param)
            # The request's mode, read once: the router and the egress guard use this value for
            # the rest of the request; the router reads it again only before a hosted attempt,
            # to refuse one a stricter mode no longer allows (docs/33, docs/38).
            mode = await chat_router.mode(caller.organization_id, caller.id)

            async def recheck() -> HostedLimits:
                # Just before each hosted attempt: both modes and both settings rows in one
                # statement, decided as the key lookup's were (docs/42). A row that cannot be
                # judged raises, and the attempt is not made (closed).
                policy = await mode_store.hosted_policy(caller.organization_id, caller.id)
                now = resolve(platform, found.stored, policy.settings, documents)
                return hosted_limits(
                    effective_mode(policy.organization_mode, policy.application_mode), now
                )

            try:
                # Only to answer at once when no candidate may serve it; the router asks again.
                await chat_router.candidates(
                    caller.organization_id, route.model, mode, wants_tools=bool(chat_req.tools)
                )
            except NoCandidateError as exc:
                if exc.code == "tools_not_supported":
                    return _openai_error(
                        422,
                        "tools_not_supported",
                        f"Model {route.model!r} cannot be used with tools here"
                        + (f" ({exc.reason})." if exc.reason else "."),
                        param="tools",
                    )
                return _openai_error(
                    503,
                    "no_local_provider",
                    f"No local provider serves {route.model!r}, and this application's mode "
                    "allows only local ones.",
                    kind="api_error",
                )
            trace = RouteTrace()
            if (note := noted()) is not None:
                note.trace = trace
            completion_id = "chatcmpl-" + request_id.removeprefix("req-")
            created = int(chat_epoch.epoch_ms() // 1000)
            multiplier = route.plan.price_multiplier

            def chunk(choice: dict[str, object], **extra: object) -> str:
                body_ = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": route.model,
                    "choices": [choice] if choice else [],
                    **extra,
                }
                return f"data: {json.dumps(body_)}\n\n"

            def failure(exc: Exception) -> JSONResponse:
                answer = refusal(exc)
                answer.headers.update(applied_header(max_tokens))
                return answer

            def refusal(exc: Exception) -> JSONResponse:
                if isinstance(exc, ProviderError):
                    return _provider_error(exc)
                if isinstance(exc, ChatInputError):
                    return _openai_error(422, "invalid_request", str(exc), param=exc.param)
                if isinstance(exc, QueueFullError):
                    seconds = max(1, math.ceil(exc.retry_after_ms / 1000))
                    return _openai_error(
                        429,
                        "queue_full",
                        f"Model {route.model!r} is overloaded. Retry in {seconds} seconds.",
                        kind="rate_limit_error",
                        headers={"Retry-After": str(seconds)},
                    )
                if isinstance(exc, RequestTimeoutError):
                    return _openai_error(
                        504, "request_timeout", "The request timed out.", kind="api_error"
                    )
                raise exc

            async def tracked(on_delta: Callable[[ChatDelta], None] | None) -> RoutedOutcome:
                monitoring.request_started(route.model)
                try:
                    work = chat_router.run(
                        chat_req,
                        caller.organization_id,
                        caller.id,
                        mode,
                        on_delta,
                        trace,
                        timeouts=ChatTimeouts(
                            settings.chat_first_token_ms, settings.chat_total_timeout_ms
                        ),
                        limits=hosted_limits(mode, settings),
                        recheck=recheck,
                    )
                    if timeout_ms is not None:
                        return await run_with_timeout(clock, work, timeout_ms)
                    return await work
                finally:
                    monitoring.request_ended(route.model)

            if not body.stream:
                try:
                    routed = await tracked(None)
                except CHAT_FAILURES as exc:
                    return failure(exc)
                outcome = routed.outcome
                await account_chat(
                    outcome, hold, caller, multiplier, route.model, routed.source, max_tokens
                )
                result = outcome.result
                message: dict[str, object] = {
                    "role": "assistant",
                    "content": result.content or (None if result.tool_calls else ""),
                    "refusal": None,
                }
                if result.tool_calls:
                    message["tool_calls"] = [tool_call_view(c) for c in result.tool_calls]
                return JSONResponse(
                    {
                        "id": completion_id,
                        "object": "chat.completion",
                        "created": created,
                        "model": route.model,
                        "choices": [
                            {
                                "index": 0,
                                "message": message,
                                "finish_reason": result.finish_reason,
                                "logprobs": None,
                            }
                        ],
                        "usage": usage_view(outcome),
                    },
                    headers={**chat_headers(routed, route.model), **applied_header(max_tokens)},
                )

            # Streaming. Deltas go to a queue; None marks the end of the work.
            deltas: asyncio.Queue[ChatDelta | None] = asyncio.Queue()
            task = asyncio.ensure_future(tracked(deltas.put_nowait))
            task.add_done_callback(lambda _: deltas.put_nowait(None))
            try:
                first = await deltas.get()
            except asyncio.CancelledError:
                task.cancel()
                raise
            if first is None:
                # It ended before any output, so nothing has been sent: answer as JSON.
                failed = task.exception()
                if isinstance(failed, CHAT_FAILURES):
                    return failure(failed)
                routed = task.result()  # an answer with no delta at all
                await account_chat(
                    routed.outcome,
                    hold,
                    caller,
                    multiplier,
                    route.model,
                    routed.source,
                    max_tokens,
                )
            include_usage = body.stream_options is not None and body.stream_options.include_usage

            async def events(item: ChatDelta | None) -> AsyncIterator[str]:
                try:
                    yield chunk(
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": ""},
                            "finish_reason": None,
                        }
                    )
                    tool_index = 0
                    while item is not None:
                        if item.tool_call is not None:
                            delta: dict[str, object] = {
                                "tool_calls": [tool_call_view(item.tool_call, tool_index)]
                            }
                            tool_index += 1
                        else:
                            delta = {"content": item.text}
                        yield chunk({"index": 0, "delta": delta, "finish_reason": None})
                        item = await deltas.get()
                    failed = task.exception() if not task.cancelled() else None
                    if task.cancelled() or failed is not None:
                        error = (
                            failure(failed)
                            if isinstance(failed, CHAT_FAILURES)
                            else _openai_error(
                                500, "internal_error", "The request failed.", kind="api_error"
                            )
                        )
                        # After output has started the status cannot change: the error goes
                        # in the stream, and it ends without [DONE].
                        yield f"data: {bytes(error.body).decode()}\n\n"
                        return
                    routed = task.result()
                    outcome = routed.outcome
                    if first is not None:
                        await account_chat(
                            outcome,
                            hold,
                            caller,
                            multiplier,
                            route.model,
                            routed.source,
                            max_tokens,
                        )
                    yield chunk(
                        {"index": 0, "delta": {}, "finish_reason": outcome.result.finish_reason}
                    )
                    if include_usage:
                        yield chunk({}, usage=usage_view(outcome))
                    yield "data: [DONE]\n\n"
                finally:
                    # The client went away or the stream ended early: stop the upstream work,
                    # and give back what it held if it did not bill.
                    if not task.done():
                        task.cancel()
                    await hold.release()

            headers = {"Cache-Control": "no-cache", **applied_header(max_tokens)}
            if task.done() and not task.cancelled() and task.exception() is None:
                headers.update(chat_headers(task.result(), route.model))
            else:
                # Output has started, so the router knows who is answering (docs/33).
                headers.update({"X-Model-Used": route.model, **source_headers(trace)})
            hold.streaming = True
            return StreamingResponse(events(first), media_type="text/event-stream", headers=headers)

        @app.post("/v1/chat/completions", response_model=None)
        @public_route("an application API key, checked by the route")
        async def chat_completions(
            request: Request, authorization: str | None = Header(default=None)
        ) -> Response:
            """The OpenAI-compatible chat endpoint (docs/31). Every refusal is in the OpenAI
            error format. A reservation it made and did not settle is released however it
            ends, here or by its stream (docs/37)."""
            held: list[_Hold] = []
            try:
                return await answer_chat(request, authorization, held)
            finally:
                if held and not held[0].streaming:
                    await held[0].release()

    # Throwaway keys for the Playground: in memory, never printed, logged or returned.
    playground: dict[str, str] = {}
    if playground_keys:
        if applications is None or not isinstance(applications.store, InMemoryApplicationStore):
            raise ValueError("playground_keys needs applications in an in-memory store")
        for application in run_now(applications.store.list_applications()):
            issued = run_now(applications.keys.issue(application.id, "playground (in memory)"))
            playground[application.id] = issued.key

    @app.post("/v1/playground/generate", response_model=GenerateResponse)
    @local_only_route("playground.use")
    async def playground_generate(
        body: PlaygroundBody, response: Response
    ) -> GenerateResponse | JSONResponse | StreamingResponse:
        if applications is None or not playground:
            return _error(
                404,
                "playground_unavailable",
                "The Playground sends requests only in simulated live mode. Elsewhere, send "
                "them to POST /v1/generate with an API key. See docs/20-applications-api-keys.md.",
            )
        key = playground.get(body.application_id)
        if key is None:
            raise InvalidApiKeyError()
        found = keyed(await applications.keys.authenticate(key))
        request = GenerateBody.model_validate(
            body.model_dump(exclude={"application_id"}, exclude_unset=True)
        )
        return await serve(request, response, found.application, found.settings)

    @app.get("/")
    @public_route("an index of the API")
    async def index() -> dict[str, object]:
        """A signpost for someone who opens the server's address in a browser."""
        return {
            "service": "Cost-aware multi-model inference gateway",
            "docs": "/docs",
            "endpoints": [
                "POST /v1/generate",
                "GET /v1/models",
                "GET /v1/applications/me/usage",
                "GET /v1/overload",
                "GET /v1/control",
                "GET /v1/monitor/snapshot",
                "GET /v1/monitor/stream",
                "GET /metrics",
                "GET /metrics/summary",
                "GET /metrics/detail",
                "GET /healthz",
                "GET /readyz",
            ],
        }

    @app.get("/v1/overload")
    @local_only_route("monitoring.read")
    async def overload_status() -> OverloadStatus:
        status = {}
        for name in registry.names:
            waiting = getattr(registry.get(name), "waiting", None)
            if monitor is None:
                status[name] = ModelOverload(
                    degraded=False,
                    slo_p99_ms=None,
                    recent_p99_ms=None,
                    samples=0,
                    waiting=waiting,
                )
                continue
            health = monitor.health(name)
            status[name] = ModelOverload(
                degraded=health.degraded,
                slo_p99_ms=health.slo_p99_ms,
                recent_p99_ms=health.recent_p99_ms,
                samples=health.samples,
                waiting=waiting,
            )
        return OverloadStatus(request_timeout_ms=timeout_ms, models=status)

    @app.get("/v1/models")
    @public_route("the models served")
    async def models() -> ModelList:
        return ModelList(
            models=[
                ModelInfo(id=name, aliases=list(registry.aliases_of(name)))
                for name in registry.names
            ]
        )

    @app.get("/healthz")
    @public_route("health")
    async def healthz() -> Health:
        return Health(
            status="ok",
            models=len(registry.names),
            uptime_ms=clock.now_ms() - started_ms,
            alert_evaluator=alert_status().state,
        )

    @app.get("/readyz")
    @public_route("readiness: ready or not, nothing more (docs/36)")
    async def readyz() -> JSONResponse:
        # A budget write kept for later (the database did not take it) admits nothing new.
        ready = (counters is None or counters.healthy) and (budgets is None or budgets.healthy)
        if ready and readiness is not None:
            try:
                ready = await readiness()
            except StateUnavailableError:
                ready = False
        return JSONResponse(
            {"status": "ready" if ready else "not_ready"},
            status_code=200 if ready else 503,
            headers=_NO_STORE,
        )

    @app.get("/metrics/summary")
    @local_only_route("monitoring.read")
    async def metrics_summary() -> MetricsSummary:
        summaries = {name: metrics.summary(name) for name in registry.names}
        return MetricsSummary(
            models={
                name: ModelMetrics(
                    requests=s.requests,
                    queue_ms=_pair(s.queue_ms),
                    ttft_ms=_pair(s.ttft_ms),
                    total_ms=_pair(s.total_ms),
                    itl_ms=_pair(s.itl_ms),
                    prompt_tokens=s.tokens_in,
                    cached_tokens=s.cached_tokens,
                    completion_tokens=s.tokens_out,
                    cost_usd=s.cost_usd,
                )
                for name, s in summaries.items()
            }
        )

    @app.get("/metrics/detail")
    @local_only_route("monitoring.read")
    async def metrics_detail() -> MetricsDetail:
        summaries = {name: metrics.summary(name) for name in registry.names}
        return MetricsDetail(
            percentile_method="nearest-rank",
            models={
                name: ModelMetricsDetail(
                    lifetime=LifetimeStats(
                        requests=s.requests,
                        prompt_tokens=s.tokens_in,
                        cached_tokens=s.cached_tokens,
                        completion_tokens=s.tokens_out,
                        cost_usd=s.cost_usd,
                    ),
                    window=WindowStats(
                        window_ms=s.window.window_ms,
                        span_ms=s.window.span_ms,
                        requests=s.window.requests,
                        prompt_tokens=s.window.tokens_in,
                        cached_tokens=s.window.cached_tokens,
                        completion_tokens=s.window.tokens_out,
                        cost_usd=s.window.cost_usd,
                        truncated=s.window.truncated,
                        itl_truncated=s.window.itl_truncated,
                    ),
                    queue_ms=_stats(s.queue_ms),
                    ttft_ms=_stats(s.ttft_ms),
                    total_ms=_stats(s.total_ms),
                    itl_ms=_stats(s.itl_ms),
                )
                for name, s in summaries.items()
            },
        )

    # --- load control and simulated live mode ---------------------------------------------

    # Which applications may use each model, in the order they were created.
    applications_for: dict[str, list[Application]] = {}
    if applications is not None:
        for application in known_applications:
            for allowed in application.plan.models:
                applications_for.setdefault(allowed, []).append(application)

    async def send_planned(planned: PlannedRequest, allow_downgrade: bool) -> bool:
        """Put one generated request through the same handler an HTTP request reaches,
        for an application that is already resolved (load control holds no keys)."""
        caller = None
        if applications is not None:
            candidates = applications_for.get(registry.resolve(planned.model), [])
            if not candidates:
                return False
            # Spread over the applications that may use the model, with no randomness.
            caller = candidates[planned.index % len(candidates)]
        body = GenerateBody(
            model=planned.model,
            prompt=planned.prompt,
            max_tokens=planned.max_tokens,
            allow_downgrade=allow_downgrade,
        )
        effective: EffectiveSettings | None = None
        if caller is not None:
            # No key, so no key lookup: the two rows are read here, and decided on as a keyed
            # request's are (docs/39). Load control is not the request path.
            assert settings_store is not None
            found = keyed(
                dataclasses.replace(
                    caller,
                    stored_settings=StoredSettings(
                        await settings_store.current(Scope.ORGANIZATION, caller.organization_id),
                        await settings_store.current(Scope.APPLICATION, caller.id),
                    ),
                )
            )
            caller, effective = found.application, found.settings
        try:
            answer = await serve(body, Response(), caller, effective)
        except (
            BackendError,
            BudgetExceededError,
            ModelNotAllowedError,
            RateLimitedError,
            RequestTooLargeError,
            UnknownModelError,
        ):
            return False
        return isinstance(answer, GenerateResponse)

    if load_control:
        load = LoadController(clock, send_planned)
    app.state.load = load

    def control_status() -> dict[str, object]:
        paced = clock if isinstance(clock, PacedSimClock) else None
        return {
            "seed": seed,
            "load": {
                "available": load is not None,
                "presets": [
                    {"name": preset.name, "description": preset.description}
                    for preset in PRESETS.values()
                ]
                if load is not None
                else None,
                "status": dataclasses.asdict(load.status()) if load is not None else None,
            },
            "playground": {"available": bool(playground)},
            "simulation": {
                "active": paced is not None,
                "speed": paced.speed if paced is not None else None,
                "speeds": list(SIMULATION_SPEEDS) if paced is not None else None,
            },
        }

    def load_disabled() -> JSONResponse:
        return _error(
            403,
            "load_control_disabled",
            "Load control is not enabled on this gateway. Start it with "
            "GATEWAY_LOAD_CONTROL=1, or in simulated live mode. See "
            "docs/17-playground-load-replay.md.",
        )

    @app.get("/v1/control")
    @local_only_route("monitoring.read")
    async def control() -> dict[str, object]:
        return control_status()

    @app.post("/v1/control/load/start")
    @local_only_route("control.write")
    async def load_start(body: LoadStartBody) -> JSONResponse:
        if load is None:
            return load_disabled()
        try:
            await load.start(body.preset, seed if body.seed is None else body.seed)
        except UnknownPresetError as exc:
            return _error(404, "unknown_preset", str(exc), valid_presets=list(exc.valid))
        return JSONResponse(control_status())

    @app.post("/v1/control/load/stop")
    @local_only_route("control.write")
    async def load_stop() -> JSONResponse:
        if load is None:
            return load_disabled()
        await load.stop()
        return JSONResponse(control_status())

    @app.post("/v1/control/speed")
    @local_only_route("control.write")
    async def set_speed(body: SpeedBody) -> JSONResponse:
        if not isinstance(clock, PacedSimClock):
            return _error(
                409,
                "not_simulated",
                "This gateway is not in simulated live mode, so its time has no speed to set.",
            )
        if body.speed not in SIMULATION_SPEEDS:
            return _error(
                422,
                "invalid_speed",
                f"Speed must be one of {', '.join(f'{v:g}' for v in SIMULATION_SPEEDS)}.",
                valid_speeds=list(SIMULATION_SPEEDS),
            )
        clock.speed = body.speed
        return JSONResponse(control_status())

    async def take_snapshot(*, history_ms: float = 0.0, recent_events: int = 0) -> Snapshot:
        # Asking the SLO monitor for a model's health makes it re-evaluate, exactly as
        # GET /v1/overload does.
        health = (
            {name: monitor.health(name) for name in registry.names} if monitor is not None else None
        )
        return await monitoring.snapshot(
            history_ms=history_ms,
            recent_events=recent_events,
            health=health,
            alert_evaluator=alert_status(),
        )

    @app.get("/v1/monitor/snapshot")
    @local_only_route("monitoring.read")
    async def monitor_snapshot(
        history_s: float = Query(default=0.0, ge=0.0),
        events: int = Query(default=50, ge=0),
    ) -> JSONResponse:
        snapshot = await take_snapshot(history_ms=history_s * 1000, recent_events=events)
        return JSONResponse(dataclasses.asdict(snapshot))

    @app.get("/v1/monitor/stream")
    @local_only_route("monitoring.read")
    async def monitor_stream(
        request: Request,
        history_s: float = Query(default=0.0, ge=0.0),
        count: int | None = Query(default=None, ge=1),
        last_event_id: int | None = Query(default=None, ge=0),
    ) -> StreamingResponse:
        bus = monitoring.events
        # A reconnecting client says which event it saw last and gets the ones after it:
        # in the header a browser sends when it retries by itself, or in the query, for a
        # client that opens a new connection.
        header = request.headers.get("last-event-id", "")
        if last_event_id is not None:
            cursor = last_event_id
        else:
            cursor = int(header) if header.isdecimal() else bus.last_seq

        async def pushes() -> AsyncIterator[str]:
            nonlocal cursor
            # The first snapshot carries the history asked for; later ones only what one
            # interval can have changed.
            history_ms = history_s * 1000
            sent = 0
            while True:
                # Taken first: building it can itself change a model's state, and that
                # event must reach the client before the snapshot showing it.
                snapshot = await take_snapshot(history_ms=history_ms)
                page = bus.since(cursor)
                cursor = bus.last_seq
                if page.missed:
                    yield _sse("events_missed", {"missed": page.missed})
                for event in page.events:
                    yield _sse("event", dataclasses.asdict(event), event_id=event.seq)
                yield _sse("snapshot", dataclasses.asdict(snapshot))
                sent += 1
                if count is not None and sent >= count:
                    return
                history_ms = MONITOR_STREAM_INTERVAL_MS
                await clock.sleep_ms(MONITOR_STREAM_INTERVAL_MS)

        return StreamingResponse(
            pushes(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )

    @app.get("/metrics")
    @local_only_route("monitoring.read")
    async def prometheus_metrics() -> Response:
        return Response(
            render_prometheus(await take_snapshot()), media_type=PROMETHEUS_CONTENT_TYPE
        )

    if dashboard_dir is not None and (dashboard_dir / "index.html").is_file():
        app.mount("/dashboard", StaticFiles(directory=dashboard_dir, html=True), name="dashboard")
    else:

        @app.get("/dashboard")
        @app.get("/dashboard/{path:path}")
        @local_only_route("monitoring.read")
        async def dashboard_not_built(path: str = "") -> JSONResponse:
            return _error(
                404,
                "dashboard_not_built",
                "The dashboard has not been built. Run `npm install` and `npm run build` in "
                "frontend/, then restart the gateway. See docs/16-dashboard.md.",
            )

    return app


# How long the default server keeps usage events, unless GATEWAY_USAGE_RETENTION_DAYS says
# otherwise: the current month and a little over a year before it, for invoices.
DEFAULT_USAGE_RETENTION_DAYS = 400.0
DAY_MS = 86_400_000.0


def usage_retention_ms(environ: Mapping[str, str]) -> float:
    raw = environ.get("GATEWAY_USAGE_RETENTION_DAYS")
    days = DEFAULT_USAGE_RETENTION_DAYS if raw is None else float(raw)
    if not days > 0:
        raise ValueError("GATEWAY_USAGE_RETENTION_DAYS must be a positive number of days")
    return days * DAY_MS


# Operator settings for the default server, not measurements: queue limits, chunk size and
# SLOs were chosen, not measured.
DEFAULT_MAX_QUEUE_LEN = {Tier.ECONOMY: 256, Tier.STANDARD: 128, Tier.PREMIUM: 64}
DEFAULT_PREFILL_CHUNK_TOKENS = 256
DEFAULT_OVERLOAD = OverloadPolicy(
    request_timeout_ms=120_000.0,
    slo=SloPolicy(p99_ms={"economy": 5_000.0, "standard": 10_000.0, "premium": 30_000.0}),
)


def create_simulated_live_app(
    *,
    seed: int = 0,
    speed: float = 1.0,
    preset: str = "steady",
    wait: Callable[[float], Awaitable[None]] | None = None,
    dashboard_dir: Path | None = DASHBOARD_DIR,
    local_only: LocalOnlyPolicy | None = None,
) -> FastAPI:
    """Simulated live mode: the default simulated wiring on a paced simulation clock.

    Simulated time is let through at ``speed`` times real time, and the load preset is
    started with ``seed`` as the server starts. Every latency, event and timestamp is then
    simulated time: the same seed gives the same sequence on any machine, at any speed.
    Nothing here measures a real model. ``wait`` replaces the real waiting, for tests.
    ``local_only`` defaults to the plain loopback policy (``gateway.localonly``).
    """
    if preset not in PRESETS:
        raise UnknownPresetError(preset, tuple(PRESETS))
    clock = PacedSimClock(speed, wait=wait)
    registry = build_continuous_registry(
        clock,
        seed=seed,
        max_queue_len=DEFAULT_MAX_QUEUE_LEN,
        prefill_chunk_tokens=DEFAULT_PREFILL_CHUNK_TOKENS,
    )
    # Applications and keys in memory only, for the life of the process: the pepper is a
    # throwaway too, and no key is ever printed or written anywhere.
    templates = load_plans()
    epoch = ClockEpoch(clock)
    store = InMemoryApplicationStore(created_at_ms=epoch.epoch_ms())
    run_now(seed_demo_applications(store, templates, created_at_ms=epoch.epoch_ms()))
    keys = ApiKeyService(store, secrets.token_bytes(32), epoch)
    return create_app(
        registry,
        clock,
        applications=ApplicationAccess(keys, templates),
        playground_keys=True,
        local_only=local_only if local_only is not None else LocalOnlyPolicy(),
        overload=DEFAULT_OVERLOAD,
        dashboard_dir=dashboard_dir,
        load_control=True,
        seed=seed,
        autostart_load=preset,
    )


def build_channels(
    state: StateBackend,
    vault: Vault,
    audit: AuditLog,
    epoch: EpochSource,
    environ: Mapping[str, str],
    *,
    pepper: bytes,
    redactor: Redactor,
    clock: Clock,
    counters: SharedCounters | None = None,
    mailer: Mailer | None = None,
) -> ChannelService:
    """The organizations' alert channels for a server (docs/46): sealed under the same vault as
    provider keys, in their own scope; the webhook ports from ``GATEWAY_ALERT_WEBHOOK_PORTS``
    and the caps from their four variables (a value out of range refuses the start); their
    notices queued in the outbox on the state's clock; the built-in's rule ids under their own
    key derived from the pepper; each attempt through a new ``webhook_client`` on the system
    resolver, with one pinned TLS context, its leased secrets scrubbed by ``redactor``.

    Email channels (docs/48): the operator's SMTP settings from the environment (a value that is
    not valid refuses the start); their recipients' codes and the per-address cap under keys
    derived from the pepper; the hourly confirmation windows in ``counters`` when shared, else in
    memory; codes and ids from the operating system's generator. Without ``mailer`` (group 1)
    no email channel can be made.

    With the SMTP settings (group 2), the SMTP sender (the pinned TLS context, its name lookups
    on their own bounded pool, never while a store transaction is open), the recipients' mailer
    over it, and email deliveries through the worker; ``mailer`` replaces the SMTP mailer (tests).
    """
    caps = caps_from(environ)
    email = email_settings_from(environ)
    ports = parse_webhook_ports(environ.get(WEBHOOK_PORTS_ENV))
    notices = Notices(
        state.channels,
        state.outbox,
        state.applications,
        epoch,
        secrets.token_bytes,
        caps=caps,
        store_clock=state.clock,
        builtin_rule_id=builtin_rule_ids(derive_key(pepper, KeyPurpose.BUILTIN_RULE_ID)),
        recipients=state.recipients,
    )
    tls = provider_tls_context()
    sender = (
        SmtpSender(
            email, state.platform_secrets, vault, redactor, clock, context=tls, held=state.outbox
        )
        if email is not None
        else None
    )
    if mailer is None and sender is not None:
        mailer = SmtpMailer(sender, epoch)

    async def organization_name(organization_id: str) -> str | None:
        found = await state.applications.get_organization(organization_id)
        return found.name if found is not None else None

    recipients = RecipientService(
        state.recipients,
        state.confirmations,
        counters.confirmations(caps.confirmations_per_channel, caps.confirmations_per_organization)
        if counters is not None
        else InMemoryConnectionTestCounter(
            clock,
            caps.confirmations_per_channel,
            caps.confirmations_per_organization,
            reason=Reason.CONFIRMATION_LIMITED,
            what=("one channel's recipients", "an organization's recipients"),
            noun="confirmations to",
        ),
        InstanceConfirmations(clock, caps.confirmations_per_instance),
        audit,
        epoch,
        state.clock,
        random.SystemRandom(),
        code_key=derive_key(pepper, KeyPurpose.EMAIL_CONFIRMATION),
        address_key=derive_key(pepper, KeyPurpose.EMAIL_ADDRESS),
        caps=ConfirmationCaps(
            caps.confirmations_per_address,
            caps.confirmations_per_channel,
            caps.confirmations_per_organization,
            caps.confirmations_per_instance,
        ),
        settings=email,
        mailer=mailer,
        organization_name=organization_name,
        held=state.outbox,
    )
    return ChannelService(
        state.channels,
        vault,
        audit,
        epoch,
        secrets.token_bytes,
        notices=notices,
        ports=ports,
        store_clock=state.clock,
        recipients=recipients,
        delivery=DeliverySetup(
            lambda: webhook_client(system_resolver, clock, ports, ssl_context=tls),
            redactor,
            caps,
            state.ceilings,
            EmailSetup(sender, state.recipients) if sender is not None else None,
        ),
    )


def build_accounts(
    state: StateBackend,
    clock: Clock,
    epoch: EpochSource,
    pepper: bytes,
    environ: Mapping[str, str],
    *,
    shared: ValkeyCounters | None = None,
    bootstrap_now: bool = True,
) -> AccountAccess:
    """Console accounts for the default server (docs/27): argon2id at the configured
    parameters on a 2-thread runner, the audit log and its retention, the bootstrap admin
    from the environment (or ``BootstrapError`` if there is no admin and no way to make one),
    and the cookie's ``Secure`` flag from ``GATEWAY_COOKIE_SECURE``. With ``shared`` counters
    the login limiter, the step-up and user-creation caps and the audit cap are shared, and
    the watch writes its restart row to this audit log (docs/35). Windows are decided on the
    state's clock (Postgres's in shared mode, section 3.8). Without ``bootstrap_now`` the
    caller runs ``bootstrap_admin`` once the store is open (shared mode, in the lifespan)."""
    passwords = Passwords(
        PasswordHasher(parameters_from(environ, PRODUCTION)), ThreadHashRunner(max_workers=2)
    )
    step_up_minutes, step_up_session_failures = step_up_settings(environ)
    creation_attempts = user_create_attempts(environ)
    audit = AuditLog(state.audit)
    windows = None
    if shared is not None:
        audit = AuditLog(state.audit, cap=shared.audit_cap(DEFAULT_UNKNOWN_USER_ROWS_PER_MINUTE))
        shared.watch.audit = audit
        windows = AccountWindows(
            *account_windows(
                shared,
                step_up_session_failures=step_up_session_failures,
                step_up_window_ms=STEP_UP_SESSION_WINDOW_MS,
                user_create_attempts=creation_attempts,
                user_create_window_ms=USER_CREATE_WINDOW_MS,
            )
        )
    service = AccountService(
        state.accounts,
        audit,
        passwords,
        roles=state.roles,
        organizations=state.applications,
        clock=clock,
        epoch=epoch,
        secret=pepper,
        audit_retention_ms=audit_retention_ms(environ),
        step_up_minutes=step_up_minutes,
        step_up_session_failures=step_up_session_failures,
        user_create_attempts=creation_attempts,
        windows=windows,
        store_clock=state.clock,
    )
    if bootstrap_now:
        # Before any event loop runs the app: store calls that cannot suspend (docs/34).
        warn_about_bootstrap(run_now(service.bootstrap_admin(environ)), environ)
    return AccountAccess(service, cookie_secure=environ.get(COOKIE_SECURE_ENV) == "1")


def warn_about_bootstrap(outcome: BootstrapOutcome, environ: Mapping[str, str]) -> None:
    if outcome is BootstrapOutcome.EXISTS_WITH_VARIABLES:
        still_set = [name for name in (ADMIN_USERNAME_ENV, ADMIN_PASSWORD_ENV) if name in environ]
        logging.getLogger("gateway").warning(
            "A platform_admin exists, so %s %s ignored; remove %s from the environment.",
            " and ".join(still_set),
            "is" if len(still_set) == 1 else "are",
            "it" if len(still_set) == 1 else "them",
        )


def create_default_app() -> FastAPI:
    """The production wiring: real time, continuous batching, prefix caches, simulated
    models, chunked prefill, application plans, and overload protection.

    ``GATEWAY_BACKEND=llamacpp`` (or a backends file named by ``GATEWAY_BACKENDS``) swaps
    the simulated models for real ones behind llama.cpp servers. The model server then
    does the batching, and the gateway's own scheduler is not in the path.

    ``GATEWAY_PROFILES`` (or ``simulated.profiles`` in the backends file) names a
    calibration file; the simulator then uses timing parameters fitted to real models.

        uvicorn gateway.app:create_default_app --factory

    ``GATEWAY_SEED`` (default 0) seeds the simulated output lengths. ``GATEWAY_PLANS`` is
    the path of a plans file; without it the plans packaged with the gateway are used.

    Applications and API keys are read from the SQLite database ``GATEWAY_DATABASE``
    (default: the user's local data folder, outside the working tree; see
    ``gateway.database``), created owner-only and migrated if needed; a migration that
    rebuilds a table on a database with data is refused here, and run with
    ``scripts/manage.py migrate``, which backs the database up first. Usage events are
    stored there too, kept for ``GATEWAY_USAGE_RETENTION_DAYS`` (default 400), and budget
    periods are calendar months in UTC; ``scripts/manage.py`` creates
    applications and issues keys. ``GATEWAY_KEY_PEPPER`` is required: without it the server
    refuses to start (docs/20-applications-api-keys.md).

    ``GATEWAY_LOAD_CONTROL=1`` lets the dashboard start and stop load against this server.

    ``GATEWAY_SIMULATED_LIVE=1`` gives simulated live mode instead of all the above (see
    ``create_simulated_live_app``): ``GATEWAY_SIM_SPEED`` (1, 5 or 20; default 1) and
    ``GATEWAY_SIM_PRESET`` (default ``steady``) set its speed and load, ``GATEWAY_SEED``
    its seed. It cannot be combined with real models.

    ``GATEWAY_STATE`` is ``sqlite`` (the default, all of the above), or ``shared``: several
    instances on one Postgres and one Valkey (``create_shared_app``, docs/37).
    """
    # Before anything else, simulated live mode included.
    check_state_setting(os.environ)
    if os.environ.get(STATE_ENV) == "shared":
        return create_shared_app(os.environ)
    seed = int(os.environ.get("GATEWAY_SEED", "0"))
    if os.environ.get("GATEWAY_SIMULATED_LIVE") == "1":
        if os.environ.get("GATEWAY_BACKEND", "simulated") != "simulated":
            raise ValueError(
                "GATEWAY_SIMULATED_LIVE=1 replays a simulated workload and cannot be "
                "combined with GATEWAY_BACKEND=" + os.environ["GATEWAY_BACKEND"]
            )
        speed = float(os.environ.get("GATEWAY_SIM_SPEED", "1"))
        if speed not in SIMULATION_SPEEDS:
            raise ValueError("GATEWAY_SIM_SPEED must be 1, 5 or 20")
        return create_simulated_live_app(
            seed=seed,
            speed=speed,
            preset=os.environ.get("GATEWAY_SIM_PRESET", "steady"),
            local_only=policy_from(os.environ),
        )
    clock = WallClock()
    # Before anything else: without the pepper no key can be checked, and without the vault's
    # master key no provider key can be sealed or opened (docs/32).
    pepper = load_pepper(os.environ)
    master, previous_master = load_master_keys(os.environ)
    plans_path = os.environ.get("GATEWAY_PLANS")
    templates = load_plans(Path(plans_path) if plans_path else None)
    epoch = SystemEpoch()
    path = database_path(os.environ)
    # One gateway process per database: a second one refuses to start (docs/21).
    lock = ProcessLock(path)
    database = open_database(path)
    runner = MigrationRunner(database, MIGRATIONS, epoch)
    rebuilds = [m.version for m in runner.pending() if m.rebuilds_tables]
    if rebuilds and runner.current_version() > 0:
        # A migration that rebuilds a table runs only from the CLI, which backs the database
        # up first (docs/26-organizations.md); a new, empty database needs no backup.
        database.close()
        lock.release()
        raise MigrationError(
            f"{path} needs migration {rebuilds[0]}, which rebuilds a table. Stop here and run "
            "`uv run python scripts/manage.py migrate` (it backs the database up first; "
            "`--dry-run` shows what it would do), then start the gateway again."
        )
    runner.migrate()
    state = sqlite_state(database, epoch)
    access = ApplicationAccess(
        ApiKeyService(state.applications, pepper, epoch, store_clock=state.clock), templates
    )
    try:
        # Counters shared with other instances, or this process's own (docs/35).
        shared = shared_counters_from(os.environ, clock, epoch, pepper)
        accounts = build_accounts(state, clock, epoch, pepper, os.environ, shared=shared)
    except BaseException:
        database.close()
        lock.release()
        raise
    retention_ms = usage_retention_ms(os.environ)
    backends = _backends(os.environ)
    registry, chat_adapters, local = _models(backends, clock, seed, os.environ)
    # One TLS context for every organization client (one is made per call, docs/33).
    tls = provider_tls_context()
    # One vault for provider keys and alert channels' secrets, each in its own scope (docs/46),
    # and one redactor for what either leases.
    vault = Vault(master, previous_master)
    redactor = Redactor()
    app = create_app(
        registry,
        clock,
        applications=access,
        accounts=accounts,
        usage_store=state.usage,
        epoch=epoch,
        usage_retention_ms=retention_ms,
        overload=DEFAULT_OVERLOAD,
        dashboard_dir=DASHBOARD_DIR,
        load_control=os.environ.get("GATEWAY_LOAD_CONTROL") == "1",
        # The operator's cap on one request's hosted attempts; out of range refuses the start.
        max_hosted_attempts=max_hosted_attempts_from(os.environ),
        seed=seed,
        local_only=policy_from(os.environ),
        chat_adapters=chat_adapters,
        providers=ProviderService(
            state.providers,
            vault,
            accounts.service.audit,
            clock,
            epoch,
            redactor,
            lambda: organization_client(system_resolver, ssl_context=tls),
            test_counter=shared.connection_tests() if shared is not None else None,
        ),
        channels=build_channels(
            state,
            vault,
            accounts.service.audit,
            epoch,
            os.environ,
            pepper=pepper,
            redactor=redactor,
            clock=clock,
        ),
        modes=state.modes,
        settings=state.settings,
        rules=state.rules,
        incidents=state.incidents,
        local_destinations=local,
        counters=shared,
    )
    # Held for the life of the process; released when it exits, however it exits.
    app.state.database_lock = lock
    return app


def _backends(environ: Mapping[str, str]) -> BackendConfig:
    backends_path = environ.get("GATEWAY_BACKENDS")
    return load_backend_config(
        Path(backends_path) if backends_path else None,
        kind_override=environ.get("GATEWAY_BACKEND"),
    )


def _models(
    backends: BackendConfig, clock: Clock, seed: int, environ: Mapping[str, str]
) -> tuple[ModelRegistry, dict[str, Backend] | None, LocalDestinations]:
    """The registry the backends file asks for, the operator's chat adapters, and the
    operator's local destinations: its list, and the loopback servers in its backends file
    (docs/33), never consulted for an organization's provider."""
    local = LocalDestinations.parse(environ.get(LOCAL_ALLOW_ENV, "")) | (
        LocalDestinations.loopback_of(s.url for s in backends.servers.values())
    )
    if backends.kind == "llamacpp":
        registry = build_llamacpp_registry(backends, clock)
        return (
            registry,
            build_chat_adapters(backends, client=operator_client(system_resolver, local)),
            local,
        )
    profiles_path = environ.get("GATEWAY_PROFILES") or backends.profiles_path
    registry = build_continuous_registry(
        clock,
        seed=seed,
        max_queue_len=DEFAULT_MAX_QUEUE_LEN,
        prefill_chunk_tokens=DEFAULT_PREFILL_CHUNK_TOKENS,
        profiles=load_calibrated_profiles(Path(profiles_path)) if profiles_path else PROFILES,
    )
    return registry, None, local


class _PostgresLifecycle:
    """``Durable`` for Postgres: the pools and the schema at start, the pools at stop."""

    def __init__(self, db: PostgresDatabase) -> None:
        self._db = db

    async def open(self) -> None:
        await self._db.open()
        await apply_schema(self._db)

    async def aclose(self) -> None:
        await self._db.aclose()


def create_shared_app(environ: Mapping[str, str]) -> FastAPI:
    """``GATEWAY_STATE=shared`` (docs/37): one of several instances behind a load balancer,
    every durable store and the budgets on the Postgres of ``GATEWAY_POSTGRES_URL``, every
    counter in the Valkey of ``GATEWAY_VALKEY_URL`` (``GATEWAY_COUNTERS=valkey``), Valkey's skew
    measured against Postgres's ``gateway_now_ms()``. ``check_state_setting`` has made sure both
    are named. A store off this machine needs a verified TLS address (``check_store_address``);
    each tier needs its ``budgets`` settings in the backends file.

    Nothing connects here: the lifespan opens Postgres, applies the schema, checks the stored
    applications and creates the bootstrap admin, then checks Valkey, before serving. No
    ``ProcessLock``: several processes on one database are the point. Real time, the simulated
    or real models of the backends file, and overload protection, as ``create_default_app``.
    """
    if environ.get("GATEWAY_SIMULATED_LIVE") == "1":
        raise SharedStateRefusedError(
            f"{STATE_ENV}=shared cannot be combined with GATEWAY_SIMULATED_LIVE=1: simulated "
            "live mode keeps everything in memory"
        )
    clock = WallClock()
    epoch = SystemEpoch()
    seed = int(environ.get("GATEWAY_SEED", "0"))
    pepper = load_pepper(environ)
    master, previous_master = load_master_keys(environ)
    plans_path = environ.get("GATEWAY_PLANS")
    templates = load_plans(Path(plans_path) if plans_path else None)
    backends = _backends(environ)
    missing = [t.value for t in Tier if t not in backends.budgets]
    if missing:
        raise SharedStateRefusedError(
            f"{STATE_ENV}=shared needs budgets.<tier>.context_tokens and "
            f"reserve_completion_tokens in the backends file for every tier (docs/37); "
            f"missing: {', '.join(missing)}"
        )
    url = environ[POSTGRES_URL_ENV]
    check_store_address("postgres", url, POSTGRES_URL_ENV, environ)
    db = PostgresDatabase.unopened(
        url, clock, env=environ, lock_timeout_ms=lock_timeout_from(environ)
    )
    state = postgres_state(db)
    access = ApplicationAccess(
        ApiKeyService(state.applications, pepper, epoch, store_clock=state.clock), templates
    )
    shared = shared_counters_from(environ, clock, epoch, pepper, reference=db.now_ms)
    assert shared is not None  # check_state_setting asked for GATEWAY_COUNTERS=valkey
    accounts = build_accounts(
        state, clock, epoch, pepper, environ, shared=shared, bootstrap_now=False
    )

    async def bootstrap() -> None:
        warn_about_bootstrap(await accounts.service.bootstrap_admin(environ), environ)

    registry, chat_adapters, local = _models(backends, clock, seed, environ)
    tls = provider_tls_context()
    vault = Vault(master, previous_master)
    redactor = Redactor()
    return create_app(
        registry,
        clock,
        applications=access,
        accounts=accounts,
        usage_store=state.usage,
        epoch=epoch,
        overload=DEFAULT_OVERLOAD,
        dashboard_dir=DASHBOARD_DIR,
        load_control=environ.get("GATEWAY_LOAD_CONTROL") == "1",
        max_hosted_attempts=max_hosted_attempts_from(environ),
        seed=seed,
        local_only=policy_from(environ),
        chat_adapters=chat_adapters,
        providers=ProviderService(
            state.providers,
            vault,
            accounts.service.audit,
            clock,
            epoch,
            redactor,
            lambda: organization_client(system_resolver, ssl_context=tls),
            test_counter=shared.connection_tests(),
        ),
        channels=build_channels(
            state,
            vault,
            accounts.service.audit,
            epoch,
            environ,
            pepper=pepper,
            redactor=redactor,
            clock=clock,
            counters=shared,
        ),
        modes=state.modes,
        settings=state.settings,
        rules=state.rules,
        incidents=state.incidents,
        local_destinations=local,
        counters=shared,
        readiness=db.ready,
        budgets=PostgresBudgets(db, clock, retention_ms=usage_retention_ms(environ)),
        reservation_bounds={tier.value: b for tier, b in backends.budgets.items()},
        durable=_PostgresLifecycle(db),
        evaluator_context=db.evaluation,
        on_start=[bootstrap],
    )
