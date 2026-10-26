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
import secrets
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeVar

import httpx
from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi import Path as Path_
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from gateway.adapters.base import HttpAdapter
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
    SqliteApplicationStore,
    seed_demo_applications,
)
from gateway.audit import AuditLog, Reason
from gateway.auth import (
    ADMIN_PASSWORD_ENV,
    ADMIN_USERNAME_ENV,
    COOKIE_SECURE_ENV,
    CSRF_HEADER,
    LOGIN_FAILED_MESSAGE,
    SESSION_COOKIE,
    STEP_UP_FAILED_MESSAGE,
    STEP_UP_REQUIRED_MESSAGE,
    AccountAccess,
    AccountService,
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
    build_chat_adapters,
    build_llamacpp_registry,
    load_backend_config,
)
from gateway.backends import Backend, BackendError
from gateway.breaker import Breakers, Transition
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
from gateway.chatrouter import ChatRouter, NoCandidateError, RoutedOutcome, RouteTrace
from gateway.chatservice import ChatOutcome, ChatService
from gateway.clock import Clock, ClockEpoch, EpochSource, PacedSimClock, SystemEpoch, WallClock
from gateway.continuous import RequestTooLargeError, build_continuous_registry
from gateway.database import ProcessLock, database_path, open_database
from gateway.egress import (
    LOCAL_ALLOW_ENV,
    LocalDestinations,
    operator_client,
    organization_client,
    system_resolver,
)
from gateway.events import EventBus
from gateway.ledger import BudgetExceededError, CostLedger, ModelUsage, run_retention
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
    resolve_key,
    resolve_member,
    resolve_organization,
)
from gateway.metrics import Distribution, MetricsStore
from gateway.migrations import MIGRATIONS, MigrationError, MigrationRunner
from gateway.modes import (
    InMemoryModeStore,
    Mode,
    ModeService,
    ModeStore,
    SqliteModeStore,
    effective_mode,
)
from gateway.monitoring import Monitor, Snapshot, render_prometheus
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
from gateway.permissions import PERMISSIONS, Actor, Resource
from gateway.profiles import PROFILES, Tier, load_calibrated_profiles
from gateway.providers import (
    ProviderConfig,
    ProviderService,
    ProviderStore,
    ProviderUnreachedError,
    resolve_provider,
)
from gateway.ratelimit import RateLimitedError, RequestRateLimiter
from gateway.redaction import Redactor
from gateway.redaction import install as install_redaction
from gateway.registry import ModelRegistry, UnknownModelError
from gateway.roles import RoleStore
from gateway.routing import ModelNotAllowedError, Quality, Router, load_plans, pick_by_quality
from gateway.schema import GenerateRequest, RequestResult
from gateway.timeseries import TimeSeriesStore
from gateway.usage import ChatSource, SqliteUsageStore, UsageStore
from gateway.users import AccountStore, User
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
Resolver = Callable[[Request], "Resource | None"]


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
            context = service.authenticate(request.cookies.get(SESSION_COOKIE))
            if request.method.upper() in STATE_CHANGING:
                service.check_csrf(context, csrf)
            if context.user.must_change_password:
                raise PasswordChangeRequiredError()
            actor = service.actor_for(context.user)
            resource = resolver(request)
            decision = authorize(actor, action, resource) if resource is not None else None
            if resource is None or decision is None or not decision.allowed:
                raise ForbiddenError()
            if not consume:
                service.check_step_up(context, action)
                authorized = Authorized(actor, context, action, resource, decision.reason)
                # For the refusal handler, which audits a conflict with who, what and where.
                request.state.authorized = authorized
                yield authorized
                return
            # Consume-on-use: the window is claimed (used up) before the handler, in one
            # transaction, so two requests cannot share it; it is given back only if the
            # handler fails before it has changed anything (docs/32).
            claimed = service.claim_step_up(context, action)
            token = ConsumeToken()
            authorized = Authorized(actor, context, action, resource, decision.reason, token)
            request.state.authorized = authorized
            try:
                yield authorized
            except BaseException:
                if not token.was_changed:
                    service.restore_step_up(context, claimed)
                raise

        setattr(check, DECLARATION, RouteDeclaration("requires", action, "authorize"))
        return Depends(check)

    return requires


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
        return service.authenticate(request.cookies.get(SESSION_COOKIE))

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
            service.record_refusal(
                authorized.actor,
                authorized.action,
                authorized.resource.organization_id,
                exc.reason,
            )
        limited = (Reason.USER_CREATION_LIMITED, Reason.TEST_RATE_LIMITED)
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
        service.logout(context)
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
        live = service.live_sessions(context)
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


def _page_body(name: str, items: list[dict[str, object]], next_cursor: str | None) -> JSONResponse:
    return JSONResponse({name: items, "next": next_cursor})


def _admin_routes(
    app: FastAPI, accounts: AccountAccess, management: ManagementService | None
) -> None:
    """The management endpoints under /v1/admin/orgs/{org} (docs/30): users, and with
    applications, applications and their keys. Every one through ``requires`` with a
    resolver that answers a resource outside the path's organization as missing."""
    service = accounts.service
    requires = app.state.requires
    stores = Stores(service.organizations, service.store)

    def organization(request: Request) -> Resource | None:
        return resolve_organization(stores, request.path_params["org"])

    def member(request: Request) -> Resource | None:
        p = request.path_params
        return resolve_member(stores, p["org"], p["username"])

    # Each route's declaration: its action and the resolver of what it acts on (docs/30).
    creating_users = requires("users.create", organization)
    listing_users = requires("users.list", organization)
    disabling_users = requires("users.disable", member)
    enabling_users = requires("users.enable", member)
    resetting_passwords = requires("users.reset_password", member)
    unlocking_users = requires("users.unlock", member)

    def user_view(user: User) -> dict[str, object]:
        return {
            "user_id": user.id,
            "username": user.username,
            "organization_id": user.organization_id,
            "disabled": user.disabled,
            "must_change_password": user.must_change_password,
            "locked": service.is_locked(user),
            "created_at_ms": user.created_at_ms,
        }

    @app.post(ORG_PREFIX + "/users")
    async def create_user(
        org: str, body: CreateUserBody, auth: Authorized = creating_users
    ) -> JSONResponse:
        service.count_user_creation(auth.context)
        user, temporary = await service.create_user_with_temporary_password(
            body.username, org, actor=auth.actor
        )
        # The one place the temporary password is shown; never logged or stored.
        return JSONResponse(
            {"user": user_view(user), "temporary_password": temporary}, status_code=201
        )

    @app.get(ORG_PREFIX + "/users")
    async def list_users(
        org: str,
        limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
        after: str | None = Query(default=None, max_length=64),
        auth: Authorized = listing_users,
    ) -> JSONResponse:
        page = service.page_users(org, after, limit, actor=auth.actor)
        return _page_body("users", [user_view(u) for u in page.items], page.next)

    @app.post(ORG_PREFIX + "/users/{username}/disable")
    async def disable_user(username: str, auth: Authorized = disabling_users) -> JSONResponse:
        revoked = service.disable_user(username, actor=auth.actor)
        return JSONResponse({"disabled": True, "sessions_revoked": revoked})

    @app.post(ORG_PREFIX + "/users/{username}/enable")
    async def enable_user(username: str, auth: Authorized = enabling_users) -> JSONResponse:
        service.enable_user(username, actor=auth.actor)
        return JSONResponse({"enabled": True})

    @app.post(ORG_PREFIX + "/users/{username}/reset-password")
    async def reset_password(username: str, auth: Authorized = resetting_passwords) -> JSONResponse:
        revoked, temporary = await service.reset_to_temporary_password(username, actor=auth.actor)
        # The one place the temporary password is shown; never logged or stored.
        return JSONResponse({"temporary_password": temporary, "sessions_revoked": revoked})

    @app.post(ORG_PREFIX + "/users/{username}/unlock")
    async def unlock_user(username: str, auth: Authorized = unlocking_users) -> JSONResponse:
        service.unlock_user(username, actor=auth.actor)
        return JSONResponse({"unlocked": True})

    if management is None:
        return
    keys = management

    def application(request: Request) -> Resource | None:
        p = request.path_params
        return resolve_application(stores, p["org"], p["app"])

    def key(request: Request) -> Resource | None:
        p = request.path_params
        return resolve_key(stores, p["org"], p["app"], p["key_id"])

    creating_applications = requires("applications.create", organization)
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

    def key_view(record: ApiKeyRecord) -> dict[str, object]:
        # Never the key, never its HMAC.
        return {
            "key_id": record.key_id,
            "label": record.label,
            "state": keys.key_state(record),
            "created_at_ms": record.created_at_ms,
            "expires_at_ms": record.expires_at_ms,
            "last_used_at_ms": record.last_used_at_ms,
        }

    def issued_view(issued: IssuedKey) -> JSONResponse:
        # The one place a new key is shown; never logged or stored.
        return JSONResponse({"key": issued.key, **key_view(issued.record)}, status_code=201)

    @app.post(ORG_PREFIX + "/applications")
    async def create_application(
        org: str,
        body: CreateApplicationBody,
        auth: Authorized = creating_applications,
    ) -> JSONResponse:
        try:
            made = keys.create_application(org, body.name, body.plan, actor=auth.actor)
        except UnknownPlanError as exc:
            return _invalid("plan", str(exc))
        except ManagementRefusal:
            raise
        except ValueError as exc:  # the name: normalize_name says why, never echoing more
            return _invalid("name", str(exc))
        return JSONResponse(application_view(made), status_code=201)

    @app.get(ORG_PREFIX + "/applications")
    async def list_applications(
        org: str,
        limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
        after: str | None = Query(default=None, max_length=64),
        auth: Authorized = listing_applications,
    ) -> JSONResponse:
        page = keys.page_applications(org, after, limit, actor=auth.actor)
        return _page_body("applications", [application_view(a) for a in page.items], page.next)

    @app.get(ORG_PREFIX + "/applications/{app}")
    async def read_application(
        app_id: str = Path_(alias="app"),
        auth: Authorized = reading_applications,
    ) -> JSONResponse:
        return JSONResponse(application_view(keys.read_application(app_id, actor=auth.actor)))

    @app.post(ORG_PREFIX + "/applications/{app}/keys")
    async def issue_key(
        body: IssueKeyBody,
        app_id: str = Path_(alias="app"),
        auth: Authorized = issuing_keys,
    ) -> JSONResponse:
        issued = keys.issue_key(
            app_id, body.label, actor=auth.actor, expires_in_days=body.expires_in_days
        )
        return issued_view(issued)

    @app.get(ORG_PREFIX + "/applications/{app}/keys")
    async def list_keys(
        app_id: str = Path_(alias="app"),
        limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
        after: str | None = Query(default=None, max_length=64),
        auth: Authorized = listing_keys,
    ) -> JSONResponse:
        page = keys.page_keys(app_id, after, limit, actor=auth.actor)
        return _page_body("keys", [key_view(k) for k in page.items], page.next)

    @app.post(ORG_PREFIX + "/applications/{app}/keys/{key_id}/rotate")
    async def rotate_key(
        key_id: str,
        body: RotateKeyBody,
        auth: Authorized = rotating_keys,
    ) -> JSONResponse:
        issued = keys.rotate_key(
            key_id,
            grace_hours=body.grace_hours,
            actor=auth.actor,
            expires_in_days=body.expires_in_days,
        )
        return issued_view(issued)

    @app.post(ORG_PREFIX + "/applications/{app}/keys/{key_id}/revoke")
    async def revoke_key(key_id: str, auth: Authorized = revoking_keys) -> JSONResponse:
        keys.revoke_key(key_id, actor=auth.actor)
        return JSONResponse({"key_id": key_id, "state": "revoked"})


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

    def organization(request: Request) -> Resource | None:
        return resolve_organization(stores, request.path_params["org"])

    def application(request: Request) -> Resource | None:
        p = request.path_params
        return resolve_application(stores, p["org"], p["app"])

    setting_organization = requires("mode.set_organization", organization)
    setting_application = requires("mode.set_application", application)

    @app.put(ORG_PREFIX + "/mode")
    async def set_organization_mode(
        org: str, body: SetModeBody, auth: Authorized = setting_organization
    ) -> JSONResponse:
        assert auth.consumed is not None
        change = modes.set_organization(org, body.mode, actor=auth.actor, marker=auth.consumed)
        return JSONResponse(
            {"organization_id": org, "mode": change.mode, "previous_mode": change.previous}
        )

    @app.put(ORG_PREFIX + "/applications/{app}/mode")
    async def set_application_mode(
        org: str,
        body: SetModeBody,
        app_id: str = Path_(alias="app"),
        auth: Authorized = setting_application,
    ) -> JSONResponse:
        assert auth.consumed is not None
        change = modes.set_application(
            org, app_id, body.mode, actor=auth.actor, marker=auth.consumed
        )
        return JSONResponse(
            {
                "application_id": app_id,
                "mode": change.mode,
                "previous_mode": change.previous,
                # The stricter of the two, which is what its requests are served under.
                "effective_mode": effective_mode(
                    modes.store.organization_mode(org), modes.store.application_mode(app_id)
                ),
            }
        )


def _provider_routes(
    app: FastAPI, accounts: AccountAccess, providers: ProviderService, breakers: Breakers
) -> None:
    """The provider endpoints under /v1/admin/orgs/{org}/providers (docs/32), each through
    ``requires``; key changes are consume-on-use."""
    service = accounts.service
    requires = app.state.requires
    stores = Stores(service.organizations, service.store)
    store = providers.store

    def provider(request: Request) -> Resource | None:
        p = request.path_params
        return resolve_provider(stores, store, p["org"], p["provider_id"])

    def organization(request: Request) -> Resource | None:
        return resolve_organization(stores, request.path_params["org"])

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

    def found(provider_id: str) -> ProviderConfig:
        config = store.get(provider_id)
        if config is None:  # resolved a moment ago: gone since
            raise ForbiddenError()
        return config

    def view(config: ProviderConfig) -> dict[str, object]:
        """For the organization's admin. Never the key: whether there is one, whether it can be
        read, its last four characters (keys of 20 or more) and when it was set."""
        key = providers.key_state(config)
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
            "circuit": breakers.state(config.organization_id, config.id),
            **slow(config),
        }

    def slow(config: ProviderConfig) -> dict[str, object]:
        """The slow-provider warning (docs/33): set after consecutive missed first-token
        budgets, cleared by an answer within budget."""
        warning = breakers.slow(config.organization_id, config.id)
        return {
            "slow_since_ms": warning.slow_since_ms,
            "consecutive_misses": warning.consecutive_misses,
        }

    def invalid(exc: ValueError, field: str) -> JSONResponse:
        return _invalid(field, str(exc))

    @app.get(ORG_PREFIX + "/providers")
    async def list_providers(org: str, auth: Authorized = listing) -> JSONResponse:
        return JSONResponse({"providers": [view(c) for c in store.list_for(org)]})

    @app.get(ORG_PREFIX + "/providers/status")
    async def provider_status(org: str, auth: Authorized = statuses) -> JSONResponse:
        # That each provider exists, whether it is enabled (docs/32), and whether it is slow
        # (docs/33).
        return JSONResponse(
            {
                "providers": [
                    {"provider_id": c.id, "enabled": c.is_enabled, **slow(c)}
                    for c in store.list_for(org)
                ]
            }
        )

    @app.post(ORG_PREFIX + "/providers")
    async def create_provider(
        org: str, body: CreateProviderBody, auth: Authorized = creating
    ) -> JSONResponse:
        try:
            made = providers.create(
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
        return JSONResponse(view(made), status_code=201)

    @app.patch(ORG_PREFIX + "/providers/{provider_id}")
    async def update_provider(
        provider_id: str, body: UpdateProviderBody, auth: Authorized = updating
    ) -> JSONResponse:
        changes = body.model_dump(exclude_unset=True)
        try:
            updated, key_deleted = providers.update(found(provider_id), changes, actor=auth.actor)
        except ManagementRefusal:
            raise
        except ValueError as exc:
            return invalid(exc, "type" if "type" in str(exc) else "base_url")
        return JSONResponse({**view(updated), "key_deleted": key_deleted})

    def switch(provider_id: str, auth: Authorized, on: bool) -> JSONResponse:
        # Whose switch: the platform's for platform_admin, the organization's for its
        # org_admin, as authorize allowed it.
        platform = auth.via.startswith("platform_admin")
        updated = providers.set_switch(found(provider_id), on, platform=platform, actor=auth.actor)
        if platform:  # platform_admin sees no more than the status
            return JSONResponse({"provider_id": updated.id, "enabled": updated.is_enabled})
        return JSONResponse(view(updated))

    @app.post(ORG_PREFIX + "/providers/{provider_id}/disable")
    async def disable_provider(provider_id: str, auth: Authorized = disabling) -> JSONResponse:
        return switch(provider_id, auth, False)

    @app.post(ORG_PREFIX + "/providers/{provider_id}/enable")
    async def enable_provider(provider_id: str, auth: Authorized = enabling) -> JSONResponse:
        return switch(provider_id, auth, True)

    @app.delete(ORG_PREFIX + "/providers/{provider_id}")
    async def delete_provider(provider_id: str, auth: Authorized = deleting) -> JSONResponse:
        assert auth.consumed is not None
        providers.delete(found(provider_id), actor=auth.actor, marker=auth.consumed)
        return JSONResponse({"provider_id": provider_id, "deleted": True})

    @app.put(ORG_PREFIX + "/providers/{provider_id}/key")
    async def set_provider_key(
        provider_id: str, body: SetProviderKeyBody, auth: Authorized = setting_keys
    ) -> JSONResponse:
        assert auth.consumed is not None
        with providers.redactor.watching(body.key):
            try:
                state = providers.set_key(
                    found(provider_id), body.key, actor=auth.actor, marker=auth.consumed
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
        providers.delete_key(found(provider_id), actor=auth.actor, marker=auth.consumed)
        return JSONResponse({"provider_id": provider_id, "key_deleted": True})

    @app.post(ORG_PREFIX + "/providers/{provider_id}/test")
    async def test_provider(provider_id: str, auth: Authorized = testing) -> JSONResponse:
        """Spends tokens on the organization's account with this provider (docs/32)."""
        try:
            capabilities = await providers.test(found(provider_id), actor=auth.actor)
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
    local_destinations: LocalDestinations | None = None,
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
    """
    if metrics is None:
        metrics = MetricsStore(clock)
    request_numbers = itertools.count(1)
    started_ms = clock.now_ms()
    router = Router(applications.templates, registry) if applications is not None else None
    if applications is not None:
        for application in applications.store.list_applications():
            Router.check_plan(application.plan, registry)
    rate_limiter = RequestRateLimiter(clock) if applications is not None else None
    ledger = CostLedger(clock, store=usage_store, epoch=epoch, retention_ms=usage_retention_ms)
    timeout_ms = overload.request_timeout_ms if overload is not None else None
    slo_policy = overload.slo if overload is not None else None
    bus = events if events is not None else EventBus(clock)
    # Monitoring only watches: it is told what happened and decides nothing.
    monitoring = Monitor(
        clock,
        registry,
        metrics,
        series=series if series is not None else TimeSeriesStore(clock),
        events=bus,
        ledger=ledger if applications is not None else None,
        applications=applications.store if applications is not None else None,
        slo_models=tuple(slo_policy.p99_ms) if slo_policy is not None else (),
    )
    monitor = (
        SloMonitor(slo_policy, clock, on_transition=monitoring.slo_transition)
        if slo_policy is not None
        else None
    )

    # Fallback modes and the providers' circuit breakers (docs/33). The router owns the
    # breakers and publishes their transitions; nothing reads them back from the bus.
    mode_store = modes if modes is not None else InMemoryModeStore()

    def breaker_moved(moved: Transition) -> None:
        bus.publish(
            "circuit_breaker",
            provider=moved.provider_id,
            state=moved.state.value,
            open_ms=moved.open_ms,
        )

    breakers = Breakers(clock, breaker_moved)

    # Filled in further down, once the generate handler it sends through exists.
    load: LoadController | None = None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if load is not None and autostart_load is not None:
            await load.start(autostart_load, seed)
        # Retention runs beside the server, never on a request (docs/21).
        retention = (
            asyncio.create_task(run_retention(ledger, clock))
            if ledger.retention_ms is not None
            else None
        )
        account_retention = (
            asyncio.create_task(run_account_retention(accounts.service, clock))
            if accounts is not None
            else None
        )
        # Provider keys out of every log record while the server runs (docs/32), and a
        # warning for each key no loaded master key can read.
        remove_redaction = install_redaction(providers.redactor) if providers is not None else None
        if providers is not None:
            providers.warn_unreadable()
        yield
        if remove_redaction is not None:
            remove_redaction()
        if retention is not None:
            retention.cancel()
        if account_retention is not None:
            account_retention.cancel()
        if load is not None:
            await load.stop()
        await registry.aclose()
        if applications is not None:
            applications.keys.flush_last_used()

    app = FastAPI(title="Cost-aware multi-model inference gateway", lifespan=lifespan)
    if local_only is not None:
        app.add_middleware(LocalOnlyMiddleware, policy=local_only)
    if accounts is not None:
        app.state.accounts = accounts
        _auth_routes(app, accounts)
        # Applications and keys over HTTP need both: a session to act, applications to act on.
        management = (
            ManagementService(
                applications.keys,
                applications.templates,
                accounts.service.audit,
                epoch if epoch is not None else ClockEpoch(clock),
                limits=KeyLimits(),
            )
            if applications is not None
            else None
        )
        _admin_routes(app, accounts, management)
        if providers is not None:
            app.state.providers = providers
            _provider_routes(app, accounts, providers, breakers)
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
        # Outermost, so even the local-only guard's refusals of /v1/admin are not cached.
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
            f"{exc} The budget resets in {retry_after_s} seconds.",
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

    def settle(
        result: RequestResult,
        *,
        request_id: str,
        model: str,
        application_id: str | None,
        price_multiplier: float,
        live_tokens: int,
    ) -> GenerateResponse:
        """Record a finished request everywhere it counts and build its response body."""
        metrics.record(model, result)
        if monitor is not None:
            monitor.record(model, result.latency_ms)
        cost_usd = result.cost_usd
        if application_id is not None:
            cost_usd = ledger.record(
                application_id, result, model=model, price_multiplier=price_multiplier
            )
        monitoring.completed(
            model, application_id, result, billed_cost_usd=cost_usd, live_tokens=live_tokens
        )
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
        body: GenerateBody, response: Response, caller: Application | None
    ) -> GenerateResponse | JSONResponse | StreamingResponse:
        """Serve one generate request; ``caller`` is None exactly when there are no
        applications."""
        if body.model is None and body.quality is None:
            return choice_error("Give either a model or a quality.", "model")
        if body.model is not None and body.quality is not None:
            return choice_error("Give a model or a quality, not both.", "quality")

        price_multiplier = 1.0
        allowed_models: tuple[str, ...] = registry.names
        max_tokens = body.max_tokens
        if caller is None:
            if body.model is not None:
                model = registry.resolve(body.model)
            else:
                assert body.quality is not None
                model = pick_by_quality(registry.names, body.quality)
        else:
            assert router is not None
            assert rate_limiter is not None
            if caller.max_tokens is not None:
                if max_tokens is not None and max_tokens > caller.max_tokens:
                    return choice_error(
                        f"At most {caller.max_tokens} on this application's plan.", "max_tokens"
                    )
                max_tokens = max_tokens or caller.max_tokens
            try:
                rate_limiter.admit(caller.id, caller.requests_per_minute)
            except RateLimitedError:
                monitoring.rate_limited(caller.id)
                raise
            route = router.route(caller, model=body.model, quality=body.quality)
            try:
                ledger.check_budget(caller.id, route.plan.monthly_token_budget)
            except BudgetExceededError as exc:
                monitoring.budget_exhausted(route.model, exc)
                raise
            model = route.model
            price_multiplier = route.plan.price_multiplier
            allowed_models = route.plan.models

        downgraded_from: str | None = None
        if monitor is not None and body.allow_downgrade:
            # Only ever to a cheaper model the caller is already entitled to.
            healthier = pick_downgrade(allowed_models, model, monitor.is_degraded)
            if healthier != model:
                downgraded_from, model = model, healthier

        request_id = f"req-{next(request_numbers)}"
        application_id = caller.id if caller is not None else None
        headers = {"X-Model-Used": model}
        if downgraded_from is not None:
            headers["X-Downgraded-From"] = downgraded_from
            monitoring.downgraded(application_id, downgraded_from, model)

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
        guarded = run_with_timeout(clock, work, timeout_ms) if timeout_ms is not None else work

        if not body.stream:
            try:
                result = await guarded
            except (QueueFullError, RequestTimeoutError) as exc:
                status, error, error_headers = failure(exc, model, application_id)
                return JSONResponse({"error": error}, status_code=status, headers=error_headers)
            payload = settle(
                result,
                request_id=request_id,
                model=model,
                application_id=application_id,
                price_multiplier=price_multiplier,
                live_tokens=live_tokens,
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
                return JSONResponse({"error": error}, status_code=status, headers=error_headers)
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
                    payload = settle(
                        task.result(),
                        request_id=request_id,
                        model=model,
                        application_id=application_id,
                        price_multiplier=price_multiplier,
                        live_tokens=live_tokens,
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
                # gives its batch slot and KV tokens back.
                if not task.done():
                    task.cancel()

        return StreamingResponse(
            events(first),
            media_type="text/event-stream",
            headers={**headers, "Cache-Control": "no-cache"},
        )

    def usage_body(caller: Application) -> ApplicationUsageResponse:
        plan = caller.plan
        usage = ledger.usage(caller.id)
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

        # async, so it runs on the event loop: a dependency run on a thread would let a
        # SimClock's time jump, and the SQLite connection belongs to the loop's thread.
        async def authenticate(authorization: str | None = Header(default=None)) -> Application:
            scheme, _, credentials = (authorization or "").strip().partition(" ")
            if scheme.lower() != "bearer" or not credentials.strip():
                raise ApiKeyRequiredError()
            return keys.authenticate(credentials.strip())

        @app.post("/v1/generate", response_model=GenerateResponse)
        @public_route("an application API key, checked by the route")
        async def generate_for_application(
            body: GenerateBody,
            response: Response,
            caller: Application = Depends(authenticate),
        ) -> GenerateResponse | JSONResponse | StreamingResponse:
            return await serve(body, response, caller)

        @app.get("/v1/applications/me/usage", response_model=ApplicationUsageResponse)
        @public_route("an application API key, checked by the route")
        async def application_usage(
            caller: Application = Depends(authenticate),
        ) -> ApplicationUsageResponse:
            return usage_body(caller)

        chat = ChatService(registry, clock, chat_adapters)
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
        )
        app.state.chat_router = chat_router
        chat_epoch = epoch if epoch is not None else ClockEpoch(clock)

        def account_chat(
            outcome: ChatOutcome,
            caller: Application,
            multiplier: float,
            model: str,
            source: ChatSource,
        ) -> None:
            """What ``settle`` records for a generate request, for a chat one: metrics, the
            SLO monitor, the ledger and monitoring. No cost is computed for chat: only prices
            an organization enters will be (docs/31), so the ledger records 0."""
            # Keyed by the routed tier, as for /v1/generate (a worker's result names its
            # profile instead).
            timing = outcome.timing
            metrics.record(model, timing)
            if monitor is not None:
                monitor.record(model, timing.latency_ms)
            billed = ledger.record(
                caller.id, timing, model=model, price_multiplier=multiplier, source=source
            )
            monitoring.completed(
                model,
                caller.id,
                timing,
                billed_cost_usd=billed,
                live_tokens=timing.tokens_out,
            )

        def source_headers(trace: RouteTrace | ChatSource) -> dict[str, str]:
            """Where the answer came from: a provider id or ``local``, and its class; never a
            URL. The fallback, when there was one (docs/33)."""
            headers = {
                "X-Provider": trace.provider or "local",
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

        @app.post("/v1/chat/completions", response_model=None)
        @public_route("an application API key, checked by the route")
        async def chat_completions(
            request: Request, authorization: str | None = Header(default=None)
        ) -> Response:
            """The OpenAI-compatible chat endpoint (docs/31). Every refusal is in the OpenAI
            error format."""
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
                caller = keys.authenticate(credentials.strip())
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
            if (body.model is None) == (body.quality is None):
                return _openai_error(
                    422, "invalid_request", "Give either a model or a quality.", param="model"
                )
            assert router is not None
            assert rate_limiter is not None
            if caller.max_tokens is not None and (body.max_tokens or 0) > caller.max_tokens:
                return _openai_error(
                    422,
                    "invalid_request",
                    f"At most {caller.max_tokens} on this application's plan.",
                    param="max_tokens",
                )
            try:
                rate_limiter.admit(caller.id, caller.requests_per_minute)
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
            try:
                route = router.route(caller, model=body.model, quality=body.quality)
            except UnknownModelError as exc:
                return _openai_error(
                    404, "unknown_model", f"Unknown model {exc.model!r}.", param="model"
                )
            except ModelNotAllowedError as exc:
                return _openai_error(
                    403, "model_not_in_plan", str(exc), kind="permission_error", param="model"
                )
            try:
                ledger.check_budget(caller.id, route.plan.monthly_token_budget)
            except BudgetExceededError as exc:
                monitoring.budget_exhausted(route.model, exc)
                seconds = max(1, math.ceil(exc.retry_after_ms / 1000))
                return _openai_error(
                    429,
                    "budget_exhausted",
                    f"{exc} The budget resets in {seconds} seconds.",
                    kind="rate_limit_error",
                    headers={"Retry-After": str(seconds)},
                )
            request_id = f"req-{next(request_numbers)}"
            try:
                chat_req = chat_request(body, request_id, route.model)
                if chat_req.max_tokens is None and caller.max_tokens is not None:
                    chat_req = dataclasses.replace(chat_req, max_tokens=caller.max_tokens)
            except ChatInputError as exc:
                return _openai_error(422, "invalid_request", str(exc), param=exc.param)
            try:
                # Only to answer at once when no candidate may serve it; the router asks again.
                chat_router.candidates(
                    caller.organization_id,
                    caller.id,
                    route.model,
                    wants_tools=bool(chat_req.tools),
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
                        chat_req, caller.organization_id, caller.id, on_delta, trace
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
                account_chat(outcome, caller, multiplier, route.model, routed.source)
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
                    headers=chat_headers(routed, route.model),
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
                account_chat(routed.outcome, caller, multiplier, route.model, routed.source)
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
                        account_chat(outcome, caller, multiplier, route.model, routed.source)
                    yield chunk(
                        {"index": 0, "delta": {}, "finish_reason": outcome.result.finish_reason}
                    )
                    if include_usage:
                        yield chunk({}, usage=usage_view(outcome))
                    yield "data: [DONE]\n\n"
                finally:
                    # The client went away or the stream ended early: stop the upstream work.
                    if not task.done():
                        task.cancel()

            headers = {"Cache-Control": "no-cache"}
            if task.done() and not task.cancelled() and task.exception() is None:
                headers.update(chat_headers(task.result(), route.model))
            else:
                # Output has started, so the router knows who is answering (docs/33).
                headers.update({"X-Model-Used": route.model, **source_headers(trace)})
            return StreamingResponse(events(first), media_type="text/event-stream", headers=headers)

    # Throwaway keys for the Playground: in memory, never printed, logged or returned.
    playground: dict[str, str] = {}
    if playground_keys:
        if applications is None or not isinstance(applications.store, InMemoryApplicationStore):
            raise ValueError("playground_keys needs applications in an in-memory store")
        for application in applications.store.list_applications():
            issued = applications.keys.issue(application.id, "playground (in memory)")
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
        caller = applications.keys.authenticate(key)
        request = GenerateBody.model_validate(body.model_dump(exclude={"application_id"}))
        return await serve(request, response, caller)

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
            status="ok", models=len(registry.names), uptime_ms=clock.now_ms() - started_ms
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
        for application in applications.store.list_applications():
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
        try:
            answer = await serve(body, Response(), caller)
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

    def take_snapshot(*, history_ms: float = 0.0, recent_events: int = 0) -> Snapshot:
        # Asking the SLO monitor for a model's health makes it re-evaluate, exactly as
        # GET /v1/overload does.
        health = (
            {name: monitor.health(name) for name in registry.names} if monitor is not None else None
        )
        return monitoring.snapshot(
            history_ms=history_ms, recent_events=recent_events, health=health
        )

    @app.get("/v1/monitor/snapshot")
    @local_only_route("monitoring.read")
    async def monitor_snapshot(
        history_s: float = Query(default=0.0, ge=0.0),
        events: int = Query(default=50, ge=0),
    ) -> JSONResponse:
        snapshot = take_snapshot(history_ms=history_s * 1000, recent_events=events)
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
                snapshot = take_snapshot(history_ms=history_ms)
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
        return Response(render_prometheus(take_snapshot()), media_type=PROMETHEUS_CONTENT_TYPE)

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
    seed_demo_applications(store, templates, created_at_ms=epoch.epoch_ms())
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


def build_accounts(
    connection: sqlite3.Connection,
    organizations: ApplicationStore,
    clock: Clock,
    epoch: EpochSource,
    pepper: bytes,
    environ: Mapping[str, str],
) -> AccountAccess:
    """Console accounts for the default server (docs/27): argon2id at the configured
    parameters on a 2-thread runner, the audit log and its retention, the bootstrap admin
    from the environment (or ``BootstrapError`` if there is no admin and no way to make one),
    and the cookie's ``Secure`` flag from ``GATEWAY_COOKIE_SECURE``."""
    passwords = Passwords(
        PasswordHasher(parameters_from(environ, PRODUCTION)), ThreadHashRunner(max_workers=2)
    )
    step_up_minutes, step_up_session_failures = step_up_settings(environ)
    service = AccountService(
        AccountStore(connection),
        AuditLog(connection),
        passwords,
        roles=RoleStore(connection),
        organizations=organizations,
        clock=clock,
        epoch=epoch,
        secret=pepper,
        audit_retention_ms=audit_retention_ms(environ),
        step_up_minutes=step_up_minutes,
        step_up_session_failures=step_up_session_failures,
        user_create_attempts=user_create_attempts(environ),
    )
    if service.bootstrap_admin(environ) is BootstrapOutcome.EXISTS_WITH_VARIABLES:
        still_set = [name for name in (ADMIN_USERNAME_ENV, ADMIN_PASSWORD_ENV) if name in environ]
        logging.getLogger("gateway").warning(
            "A platform_admin exists, so %s %s ignored; remove %s from the environment.",
            " and ".join(still_set),
            "is" if len(still_set) == 1 else "are",
            "it" if len(still_set) == 1 else "them",
        )
    return AccountAccess(service, cookie_secure=environ.get(COOKIE_SECURE_ENV) == "1")


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
    """
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
    access = ApplicationAccess(
        ApiKeyService(SqliteApplicationStore(database), pepper, epoch), templates
    )
    try:
        accounts = build_accounts(database, access.store, clock, epoch, pepper, os.environ)
    except BaseException:
        database.close()
        lock.release()
        raise
    retention_ms = usage_retention_ms(os.environ)
    backends_path = os.environ.get("GATEWAY_BACKENDS")
    backends = load_backend_config(
        Path(backends_path) if backends_path else None,
        kind_override=os.environ.get("GATEWAY_BACKEND"),
    )
    chat_adapters = None
    # The operator's local destinations: its list, and the loopback servers in its backends
    # file (docs/33). Never consulted for an organization's provider.
    local = LocalDestinations.parse(os.environ.get(LOCAL_ALLOW_ENV, "")) | (
        LocalDestinations.loopback_of(s.url for s in backends.servers.values())
    )
    # One TLS context for every organization client (one is made per call, docs/33).
    tls = httpx.create_ssl_context()
    if backends.kind == "llamacpp":
        registry = build_llamacpp_registry(backends, clock)
        chat_adapters = build_chat_adapters(
            backends, client=operator_client(system_resolver, local)
        )
    else:
        profiles_path = os.environ.get("GATEWAY_PROFILES") or backends.profiles_path
        registry = build_continuous_registry(
            clock,
            seed=seed,
            max_queue_len=DEFAULT_MAX_QUEUE_LEN,
            prefill_chunk_tokens=DEFAULT_PREFILL_CHUNK_TOKENS,
            profiles=load_calibrated_profiles(Path(profiles_path)) if profiles_path else PROFILES,
        )
    app = create_app(
        registry,
        clock,
        applications=access,
        accounts=accounts,
        usage_store=SqliteUsageStore(database),
        epoch=epoch,
        usage_retention_ms=retention_ms,
        overload=DEFAULT_OVERLOAD,
        dashboard_dir=DASHBOARD_DIR,
        load_control=os.environ.get("GATEWAY_LOAD_CONTROL") == "1",
        seed=seed,
        local_only=policy_from(os.environ),
        chat_adapters=chat_adapters,
        providers=ProviderService(
            ProviderStore(database),
            Vault(master, previous_master),
            accounts.service.audit,
            clock,
            epoch,
            Redactor(),
            lambda: organization_client(system_resolver, ssl_context=tls),
        ),
        modes=SqliteModeStore(database),
        local_destinations=local,
    )
    # Held for the life of the process; released when it exits, however it exits.
    app.state.database_lock = lock
    return app
