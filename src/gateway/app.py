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
import math
import os
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from gateway.apikeys import (
    INVALID_KEY_MESSAGE,
    ApiKeyRequiredError,
    ApiKeyService,
    ApplicationAccess,
    InvalidApiKeyError,
    load_pepper,
)
from gateway.applications import (
    Application,
    InMemoryApplicationStore,
    SqliteApplicationStore,
    seed_demo_applications,
)
from gateway.backend_config import build_llamacpp_registry, load_backend_config
from gateway.backends import BackendError
from gateway.clock import Clock, ClockEpoch, EpochSource, PacedSimClock, SystemEpoch, WallClock
from gateway.continuous import RequestTooLargeError, build_continuous_registry
from gateway.database import ProcessLock, database_path, open_database
from gateway.events import EventBus
from gateway.ledger import BudgetExceededError, CostLedger, ModelUsage, run_retention
from gateway.loadcontrol import PRESETS, LoadController, UnknownPresetError
from gateway.localonly import LocalOnlyMiddleware, LocalOnlyPolicy, policy_from
from gateway.metrics import Distribution, MetricsStore
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.monitoring import Monitor, Snapshot, render_prometheus
from gateway.overload import (
    OverloadPolicy,
    QueueFullError,
    RequestTimeoutError,
    SloMonitor,
    SloPolicy,
    pick_downgrade,
    run_with_timeout,
)
from gateway.profiles import PROFILES, Tier, load_calibrated_profiles
from gateway.ratelimit import RateLimitedError, RequestRateLimiter
from gateway.registry import ModelRegistry, UnknownModelError
from gateway.routing import ModelNotAllowedError, Quality, Router, load_plans, pick_by_quality
from gateway.schema import GenerateRequest, RequestResult
from gateway.timeseries import TimeSeriesStore
from gateway.usage import SqliteUsageStore, UsageStore
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
    # Monitoring only watches: it is told what happened and decides nothing.
    monitoring = Monitor(
        clock,
        registry,
        metrics,
        series=series if series is not None else TimeSeriesStore(clock),
        events=events if events is not None else EventBus(clock),
        ledger=ledger if applications is not None else None,
        applications=applications.store if applications is not None else None,
        slo_models=tuple(slo_policy.p99_ms) if slo_policy is not None else (),
    )
    monitor = (
        SloMonitor(slo_policy, clock, on_transition=monitoring.slo_transition)
        if slo_policy is not None
        else None
    )

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
        yield
        if retention is not None:
            retention.cancel()
        if load is not None:
            await load.stop()
        await registry.aclose()
        if applications is not None:
            applications.keys.flush_last_used()

    app = FastAPI(title="Cost-aware multi-model inference gateway", lifespan=lifespan)
    if local_only is not None:
        app.add_middleware(LocalOnlyMiddleware, policy=local_only)
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
        async def generate(
            body: GenerateBody, response: Response
        ) -> GenerateResponse | JSONResponse | StreamingResponse:
            return await serve(body, response, None)

        @app.get("/v1/applications/me/usage")
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
        async def generate_for_application(
            body: GenerateBody,
            response: Response,
            caller: Application = Depends(authenticate),
        ) -> GenerateResponse | JSONResponse | StreamingResponse:
            return await serve(body, response, caller)

        @app.get("/v1/applications/me/usage", response_model=ApplicationUsageResponse)
        async def application_usage(
            caller: Application = Depends(authenticate),
        ) -> ApplicationUsageResponse:
            return usage_body(caller)

    # Throwaway keys for the Playground: in memory, never printed, logged or returned.
    playground: dict[str, str] = {}
    if playground_keys:
        if applications is None or not isinstance(applications.store, InMemoryApplicationStore):
            raise ValueError("playground_keys needs applications in an in-memory store")
        for application in applications.store.list_applications():
            issued = applications.keys.issue(application.id, "playground (in memory)")
            playground[application.id] = issued.key

    @app.post("/v1/playground/generate", response_model=GenerateResponse)
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
    async def models() -> ModelList:
        return ModelList(
            models=[
                ModelInfo(id=name, aliases=list(registry.aliases_of(name)))
                for name in registry.names
            ]
        )

    @app.get("/healthz")
    async def healthz() -> Health:
        return Health(
            status="ok", models=len(registry.names), uptime_ms=clock.now_ms() - started_ms
        )

    @app.get("/metrics/summary")
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
    async def control() -> dict[str, object]:
        return control_status()

    @app.post("/v1/control/load/start")
    async def load_start(body: LoadStartBody) -> JSONResponse:
        if load is None:
            return load_disabled()
        try:
            await load.start(body.preset, seed if body.seed is None else body.seed)
        except UnknownPresetError as exc:
            return _error(404, "unknown_preset", str(exc), valid_presets=list(exc.valid))
        return JSONResponse(control_status())

    @app.post("/v1/control/load/stop")
    async def load_stop() -> JSONResponse:
        if load is None:
            return load_disabled()
        await load.stop()
        return JSONResponse(control_status())

    @app.post("/v1/control/speed")
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
    async def monitor_snapshot(
        history_s: float = Query(default=0.0, ge=0.0),
        events: int = Query(default=50, ge=0),
    ) -> JSONResponse:
        snapshot = take_snapshot(history_ms=history_s * 1000, recent_events=events)
        return JSONResponse(dataclasses.asdict(snapshot))

    @app.get("/v1/monitor/stream")
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
    async def prometheus_metrics() -> Response:
        return Response(render_prometheus(take_snapshot()), media_type=PROMETHEUS_CONTENT_TYPE)

    if dashboard_dir is not None and (dashboard_dir / "index.html").is_file():
        app.mount("/dashboard", StaticFiles(directory=dashboard_dir, html=True), name="dashboard")
    else:

        @app.get("/dashboard")
        @app.get("/dashboard/{path:path}")
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
    store = InMemoryApplicationStore()
    epoch = ClockEpoch(clock)
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
    ``gateway.database``), created owner-only and migrated if needed. Usage events are
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
    # Before anything else: without the pepper no key can be checked.
    pepper = load_pepper(os.environ)
    plans_path = os.environ.get("GATEWAY_PLANS")
    templates = load_plans(Path(plans_path) if plans_path else None)
    epoch = SystemEpoch()
    path = database_path(os.environ)
    # One gateway process per database: a second one refuses to start (docs/21).
    lock = ProcessLock(path)
    database = open_database(path)
    MigrationRunner(database, MIGRATIONS, epoch).migrate()
    access = ApplicationAccess(
        ApiKeyService(SqliteApplicationStore(database), pepper, epoch), templates
    )
    retention_ms = usage_retention_ms(os.environ)
    backends_path = os.environ.get("GATEWAY_BACKENDS")
    backends = load_backend_config(
        Path(backends_path) if backends_path else None,
        kind_override=os.environ.get("GATEWAY_BACKEND"),
    )
    if backends.kind == "llamacpp":
        registry = build_llamacpp_registry(backends, clock)
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
        usage_store=SqliteUsageStore(database),
        epoch=epoch,
        usage_retention_ms=retention_ms,
        overload=DEFAULT_OVERLOAD,
        dashboard_dir=DASHBOARD_DIR,
        load_control=os.environ.get("GATEWAY_LOAD_CONTROL") == "1",
        seed=seed,
        local_only=policy_from(os.environ),
    )
    # Held for the life of the process; released when it exits, however it exits.
    app.state.database_lock = lock
    return app
