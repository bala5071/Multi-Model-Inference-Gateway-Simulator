"""The HTTP API. This is the only module that imports FastAPI and the only wiring point.

See docs/06-api.md, docs/09-routing.md, docs/10-overload.md,
docs/11-streaming-chunked-prefill.md and docs/12-real-models.md.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import math
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from gateway.backend_config import build_llamacpp_registry, load_backend_config
from gateway.backends import BackendError
from gateway.clock import Clock, WallClock
from gateway.continuous import RequestTooLargeError, build_continuous_registry
from gateway.ledger import BudgetExceededError, CostLedger, ModelUsage
from gateway.metrics import MetricsRecorder, Percentiles
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
from gateway.registry import ModelRegistry, UnknownModelError
from gateway.routing import (
    ModelNotAllowedError,
    PlansConfig,
    Quality,
    Router,
    UnknownTenantError,
    load_plans,
    pick_by_quality,
)
from gateway.schema import GenerateRequest, RequestResult


class GenerateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Exactly one of model and quality must be given; the handler checks that.
    model: str | None = Field(default=None, min_length=1)
    quality: Quality | None = None
    prompt: str = Field(min_length=1)
    max_tokens: int | None = Field(default=None, ge=1)
    tenant_id: str | None = Field(default=None, min_length=1)
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


class TenantUsageResponse(BaseModel):
    tenant_id: str
    plan: str
    allowed_models: list[str]
    price_multiplier: float
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


def _pair(percentiles: Percentiles | None) -> PercentilePair | None:
    if percentiles is None:
        return None
    return PercentilePair(p50=percentiles.p50, p99=percentiles.p99)


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


def _sse(event: str, data: object) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


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
    plans: PlansConfig | None = None,
    overload: OverloadPolicy | None = None,
) -> FastAPI:
    """Build the API around an already wired registry.

    Tests pass a ``SimClock`` and a registry built on it; production uses ``WallClock``
    (see ``create_default_app``). Each model's scheduler loop runs as a background asyncio
    task on the server's event loop: the worker starts it on the first request and it
    parks when the model is idle. Shutdown stops them through ``registry.aclose()``.

    With ``plans``, every generate request must name a known tenant, is routed within
    that tenant's plan and is charged to the tenant's ledger. Without ``plans`` there are
    no tenants: any model may be used and nothing is charged.

    With ``overload``, requests are cancelled after its timeout, and a request that sets
    ``allow_downgrade`` is moved to a cheaper model while its own is missing its SLO.
    Queue limits are a property of the workers in ``registry``.
    """
    metrics = MetricsRecorder()
    request_numbers = itertools.count(1)
    started_ms = clock.now_ms()
    router = Router(plans, registry) if plans is not None else None
    ledger = CostLedger(clock)
    timeout_ms = overload.request_timeout_ms if overload is not None else None
    monitor = (
        SloMonitor(overload.slo, clock)
        if overload is not None and overload.slo is not None
        else None
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await registry.aclose()

    app = FastAPI(title="Cost-aware multi-model inference gateway", lifespan=lifespan)
    app.state.slo_monitor = monitor

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

    @app.exception_handler(UnknownTenantError)
    async def unknown_tenant(_: Request, exc: UnknownTenantError) -> JSONResponse:
        return _error(404, "unknown_tenant", str(exc))

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
        exc: QueueFullError | RequestTimeoutError, model: str
    ) -> tuple[int, dict[str, object], dict[str, str] | None]:
        """Status, error body and headers for a request the model could not serve."""
        if isinstance(exc, QueueFullError):
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
        tenant_id: str | None,
        price_multiplier: float,
    ) -> GenerateResponse:
        """Record a finished request everywhere it counts and build its response body."""
        metrics.record(model, result)
        if monitor is not None:
            monitor.record(model, result.latency_ms)
        cost_usd = result.cost_usd
        if tenant_id is not None:
            cost_usd = ledger.record(
                tenant_id, result, model=model, price_multiplier=price_multiplier
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

    @app.post("/v1/generate", response_model=GenerateResponse)
    async def generate(
        body: GenerateBody, response: Response
    ) -> GenerateResponse | JSONResponse | StreamingResponse:
        if body.model is None and body.quality is None:
            return choice_error("Give either a model or a quality.", "model")
        if body.model is not None and body.quality is not None:
            return choice_error("Give a model or a quality, not both.", "quality")

        price_multiplier = 1.0
        allowed_models: tuple[str, ...] = registry.names
        if router is None:
            if body.model is not None:
                model = registry.resolve(body.model)
            else:
                assert body.quality is not None
                model = pick_by_quality(registry.names, body.quality)
        else:
            if body.tenant_id is None:
                return _error(
                    401, "tenant_required", "This gateway needs a tenant_id on every request."
                )
            route = router.route(body.tenant_id, model=body.model, quality=body.quality)
            ledger.check_budget(body.tenant_id, route.plan.monthly_token_budget)
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
        tenant_id = body.tenant_id if router is not None else None
        headers = {"X-Model-Used": model}
        if downgraded_from is not None:
            headers["X-Downgraded-From"] = downgraded_from

        # Token events for a streaming response; None marks the end of the request.
        tokens: asyncio.Queue[tuple[int, float, str] | None] = asyncio.Queue()
        arrived_ms = clock.now_ms()

        def on_token(index: int, at_ms: float, text: str) -> None:
            tokens.put_nowait((index, at_ms - arrived_ms, text))

        request = GenerateRequest(request_id, model, body.prompt, body.max_tokens)
        work = registry.submit(request, on_token=on_token if body.stream else None)
        guarded = run_with_timeout(clock, work, timeout_ms) if timeout_ms is not None else work

        if not body.stream:
            try:
                result = await guarded
            except (QueueFullError, RequestTimeoutError) as exc:
                status, error, error_headers = failure(exc, model)
                return JSONResponse({"error": error}, status_code=status, headers=error_headers)
            payload = settle(
                result,
                request_id=request_id,
                model=model,
                tenant_id=tenant_id,
                price_multiplier=price_multiplier,
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
                status, error, error_headers = failure(failed, model)
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
                        tenant_id=tenant_id,
                        price_multiplier=price_multiplier,
                    )
                    yield _sse("done", payload.model_dump())
                elif isinstance(failed, RequestTimeoutError):
                    yield _sse("error", failure(failed, model)[1])
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

    @app.get("/v1/tenants/{tenant_id}/usage", response_model=TenantUsageResponse)
    async def tenant_usage(tenant_id: str) -> TenantUsageResponse | JSONResponse:
        if router is None:
            return _error(404, "tenancy_disabled", "This gateway has no plans configured.")
        plan = router.plan_for(tenant_id)
        usage = ledger.usage(tenant_id)
        return TenantUsageResponse(
            tenant_id=tenant_id,
            plan=plan.name,
            allowed_models=list(plan.models),
            price_multiplier=plan.price_multiplier,
            period=Period(start_ms=usage.period_start_ms, end_ms=usage.period_end_ms),
            budget_tokens=plan.monthly_token_budget,
            used_tokens=usage.total.billed_tokens,
            remaining_tokens=max(0, plan.monthly_token_budget - usage.total.billed_tokens),
            total=_totals(usage.total),
            by_model={name: _totals(u) for name, u in usage.by_model.items()},
        )

    @app.get("/")
    async def index() -> dict[str, object]:
        """A signpost for someone who opens the server's address in a browser."""
        return {
            "service": "Cost-aware multi-model inference gateway",
            "docs": "/docs",
            "endpoints": [
                "POST /v1/generate",
                "GET /v1/models",
                "GET /v1/tenants/{tenant_id}/usage",
                "GET /v1/overload",
                "GET /metrics/summary",
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

    return app


# Illustrative limits for the default server, like the model profiles they sit beside.
DEFAULT_MAX_QUEUE_LEN = {Tier.ECONOMY: 256, Tier.STANDARD: 128, Tier.PREMIUM: 64}
DEFAULT_PREFILL_CHUNK_TOKENS = 256
DEFAULT_OVERLOAD = OverloadPolicy(
    request_timeout_ms=120_000.0,
    slo=SloPolicy(p99_ms={"economy": 5_000.0, "standard": 10_000.0, "premium": 30_000.0}),
)


def create_default_app() -> FastAPI:
    """The production wiring: real time, continuous batching, prefix caches, simulated
    models, chunked prefill, tenant plans, and overload protection.

    ``GATEWAY_BACKEND=llamacpp`` (or a backends file named by ``GATEWAY_BACKENDS``) swaps
    the simulated models for real ones behind llama.cpp servers. The model server then
    does the batching, and the gateway's own scheduler is not in the path.

    ``GATEWAY_PROFILES`` (or ``simulated.profiles`` in the backends file) names a
    calibration file; the simulator then uses timing parameters fitted to real models.

        uvicorn gateway.app:create_default_app --factory

    ``GATEWAY_SEED`` (default 0) seeds the simulated output lengths. ``GATEWAY_PLANS`` is
    the path of a plans file; without it the plans packaged with the gateway are used.
    """
    clock = WallClock()
    seed = int(os.environ.get("GATEWAY_SEED", "0"))
    plans_path = os.environ.get("GATEWAY_PLANS")
    plans = load_plans(Path(plans_path) if plans_path else None)
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
    return create_app(registry, clock, plans=plans, overload=DEFAULT_OVERLOAD)
