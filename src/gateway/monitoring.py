"""The data layer of the monitoring dashboard: what to record, and the snapshot read back.

``Monitor`` is told what happens to each request by ``app.py`` and reads the rest from the
components it is given. It never changes what they do. See docs/15-monitoring-api.md.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from gateway.applications import ApplicationStore
from gateway.clock import Clock
from gateway.events import Event, EventBus
from gateway.ledger import BudgetExceededError, CostLedger
from gateway.metrics import Distribution, MetricsStore
from gateway.overload import ModelHealth, QueueFullError, Transition
from gateway.prefix_cache import PrefixCache
from gateway.profiles import ModelProfile
from gateway.registry import ModelRegistry
from gateway.schema import RequestResult
from gateway.timeseries import GaugeSample, ModelPoint, TenantPoint, TimeSeriesStore


@runtime_checkable
class SchedulerView(Protocol):
    """What a worker that schedules requests itself can show about its queue and batch.

    ``ContinuousBatchingWorker`` has all of these. ``PassthroughWorker`` does not: the
    model server behind it queues and batches out of the gateway's sight. A worker that
    does not match is reported with those groups unavailable, never as zeros.
    """

    @property
    def profile(self) -> ModelProfile: ...

    @property
    def waiting(self) -> int: ...

    @property
    def batch_size(self) -> int: ...

    @property
    def kv_used_tokens(self) -> int: ...

    @property
    def prefix_cache(self) -> PrefixCache | None: ...


# Every group below has ``available``. When it is False the gateway cannot observe the
# group for this model (or at all) and every other field of the group is None.


@dataclass(frozen=True, slots=True)
class TrafficGroup:
    available: bool
    in_flight: int
    arrived_total: int
    completed_total: int
    output_tokens_total: int
    queue_full_total: int
    timeout_total: int
    downgrade_total: int


@dataclass(frozen=True, slots=True)
class QueueGroup:
    available: bool
    length: int | None


@dataclass(frozen=True, slots=True)
class BatchGroup:
    available: bool
    size: int | None
    max_size: int | None


@dataclass(frozen=True, slots=True)
class KvGroup:
    available: bool
    used_tokens: int | None
    capacity_tokens: int | None


@dataclass(frozen=True, slots=True)
class CacheGroup:
    available: bool
    size_tokens: int | None
    lookups_total: int | None
    hits_total: int | None
    prompt_tokens_total: int | None
    cached_tokens_total: int | None
    # Since the cache was created; None before the first lookup.
    hit_rate: float | None
    token_hit_rate: float | None


@dataclass(frozen=True, slots=True)
class Quantiles:
    p50: float
    p99: float
    samples: int


@dataclass(frozen=True, slots=True)
class LatencyGroup:
    """Percentiles over the metrics store's sliding window, as in ``/metrics/detail``."""

    available: bool
    window_ms: float
    ttft_ms: Quantiles | None
    itl_ms: Quantiles | None
    total_ms: Quantiles | None


@dataclass(frozen=True, slots=True)
class SloGroup:
    available: bool
    degraded: bool | None
    slo_p99_ms: float | None
    # Over the SLO monitor's own window, ``window_ms``, which is not the latency group's.
    recent_p99_ms: float | None
    samples: int | None
    window_ms: float | None
    min_samples: int | None


@dataclass(frozen=True, slots=True)
class ModelSnapshot:
    traffic: TrafficGroup
    queue: QueueGroup
    batch: BatchGroup
    kv: KvGroup
    cache: CacheGroup
    latency: LatencyGroup
    slo: SloGroup
    series: tuple[ModelPoint, ...]


@dataclass(frozen=True, slots=True)
class TenantSnapshot:
    plan: str
    # The budget period these four belong to; they start again from zero in the next one.
    period_start_ms: float
    period_end_ms: float
    budget_tokens: int
    used_tokens: int
    remaining_tokens: int
    spend_usd: float
    requests: int
    # Since the gateway started, not per period.
    rejections_total: dict[str, int]
    timeout_total: int
    downgrade_total: int
    series: tuple[TenantPoint, ...]


@dataclass(frozen=True, slots=True)
class TenantsGroup:
    available: bool
    tenants: dict[str, TenantSnapshot] | None


@dataclass(frozen=True, slots=True)
class EventsGroup:
    available: bool
    capacity: int
    retained: int
    last_seq: int
    counts: dict[str, int]
    recent: tuple[Event, ...]


@dataclass(frozen=True, slots=True)
class Snapshot:
    at_ms: float
    resolution_ms: float
    window_ms: float
    models: dict[str, ModelSnapshot]
    tenants: TenantsGroup
    events: EventsGroup


def _quantiles(distribution: Distribution | None) -> Quantiles | None:
    if distribution is None:
        return None
    return Quantiles(distribution.p50, distribution.p99, distribution.samples)


class Monitor:
    """Feeds the time-series store and the event bus, and builds snapshots from them.

    ``slo_models`` are the models that have an SLO; only they have a degraded flag.
    ``ledger`` and ``applications`` go together: without them there are no tenants to
    report. Each application is reported as a tenant (the snapshot's names change in step 4
    of docs/platform-plan.md). Only registered models and authenticated applications are
    ever recorded, which is what bounds the number of series.
    """

    def __init__(
        self,
        clock: Clock,
        registry: ModelRegistry,
        metrics: MetricsStore,
        *,
        series: TimeSeriesStore,
        events: EventBus,
        ledger: CostLedger | None = None,
        applications: ApplicationStore | None = None,
        slo_models: Collection[str] = (),
    ) -> None:
        self._clock = clock
        self._metrics = metrics
        self._series = series
        self._events = events
        self._ledger = ledger
        self._applications = applications
        # Looked up once: checking a worker against the protocol reads its properties.
        self._views: dict[str, SchedulerView | None] = {}
        for name in registry.names:
            worker = registry.get(name)
            self._views[name] = worker if isinstance(worker, SchedulerView) else None
        for model in slo_models:
            if model in self._views:
                series.set_degraded(model, False)

    @property
    def events(self) -> EventBus:
        return self._events

    @property
    def series(self) -> TimeSeriesStore:
        return self._series

    # --- what app.py reports ---------------------------------------------------------------

    def request_started(self, model: str) -> None:
        self._series.request_started(model)
        self._observe(model)

    def request_ended(self, model: str) -> None:
        self._series.request_ended(model)
        self._observe(model)

    def token(self, model: str) -> None:
        """The model produced one output token for some request."""
        self._series.tokens(model)
        # Every request in a batch gets its token at the same instant: look once.
        if self._series.observed_ms(model) != self._clock.now_ms():
            self._observe(model)

    def completed(
        self,
        model: str,
        tenant_id: str | None,
        result: RequestResult,
        *,
        billed_cost_usd: float,
        live_tokens: int,
    ) -> None:
        """A request finished. ``live_tokens`` of its tokens were already reported one by one."""
        if result.tokens_out > live_tokens:
            self._series.tokens(model, result.tokens_out - live_tokens)
        self._series.request_completed(model, result)
        if tenant_id is not None:
            self._series.tenant_completed(
                tenant_id,
                billed_tokens=result.tokens_in - result.cached_tokens + result.tokens_out,
                spend_usd=billed_cost_usd,
            )

    def queue_full(self, model: str, tenant_id: str | None, exc: QueueFullError) -> None:
        self._series.queue_full(model)
        if tenant_id is not None:
            self._series.tenant_rejected(tenant_id, "queue_full")
        self._events.publish(
            "queue_full", model=model, tenant_id=tenant_id, waiting=exc.waiting, limit=exc.limit
        )

    def timed_out(self, model: str, tenant_id: str | None, timeout_ms: float) -> None:
        self._series.timed_out(model)
        if tenant_id is not None:
            self._series.tenant_timed_out(tenant_id)
        self._events.publish("timeout", model=model, tenant_id=tenant_id, timeout_ms=timeout_ms)

    def downgraded(self, tenant_id: str | None, from_model: str, to_model: str) -> None:
        self._series.downgraded(from_model)
        if tenant_id is not None:
            self._series.tenant_downgraded(tenant_id)
        self._events.publish("downgrade", model=from_model, tenant_id=tenant_id, to_model=to_model)

    def budget_exhausted(self, model: str, exc: BudgetExceededError) -> None:
        self._series.tenant_rejected(exc.tenant_id, "budget_exhausted")
        self._events.publish(
            "budget_exhausted",
            model=model,
            tenant_id=exc.tenant_id,
            used_tokens=exc.used_tokens,
            budget_tokens=exc.budget_tokens,
        )

    def slo_transition(self, transition: Transition) -> None:
        """For ``SloMonitor(on_transition=...)``."""
        self._series.set_degraded(transition.model, transition.degraded)
        self._events.publish(
            "state_change",
            model=transition.model,
            degraded=transition.degraded,
            p99_ms=transition.p99_ms,
        )

    # --- reading back ----------------------------------------------------------------------

    def snapshot(
        self,
        *,
        history_ms: float = 0.0,
        recent_events: int = 0,
        health: Mapping[str, ModelHealth] | None = None,
    ) -> Snapshot:
        """Everything now, with each series going back ``history_ms``.

        ``health`` is the SLO monitor's view of each model, or None if there is none.
        """
        models = {name: self._model(name, history_ms, health) for name in self._views}
        return Snapshot(
            at_ms=self._clock.now_ms(),
            resolution_ms=self._series.resolution_ms,
            window_ms=self._series.window_ms,
            models=models,
            tenants=self._tenants(history_ms),
            events=EventsGroup(
                available=True,
                capacity=self._events.capacity,
                retained=self._events.retained,
                last_seq=self._events.last_seq,
                counts=self._events.counts,
                recent=self._events.recent(recent_events),
            ),
        )

    def _observe(self, model: str) -> None:
        view = self._views.get(model)
        if view is None:
            # Nothing to read, but the step still gets its bucket, so that a model
            # behind a pass-through worker has a point for every step that was looked at.
            self._series.observe(model, GaugeSample())
            return
        cache = view.prefix_cache
        self._series.observe(
            model,
            GaugeSample(
                queue_len=view.waiting,
                batch_size=view.batch_size,
                kv_used_tokens=view.kv_used_tokens,
                cache=cache.stats if cache is not None else None,
            ),
        )

    def _model(
        self, name: str, history_ms: float, health: Mapping[str, ModelHealth] | None
    ) -> ModelSnapshot:
        self._observe(name)
        view = self._views[name]
        if view is None:
            queue = QueueGroup(False, None)
            batch = BatchGroup(False, None, None)
            kv = KvGroup(False, None, None)
        else:
            queue = QueueGroup(True, view.waiting)
            batch = BatchGroup(True, view.batch_size, view.profile.max_batch_size)
            kv = KvGroup(True, view.kv_used_tokens, view.profile.kv_capacity_tokens)
        prefix_cache = view.prefix_cache if view is not None else None
        if prefix_cache is None:
            cache = CacheGroup(False, None, None, None, None, None, None, None)
        else:
            stats = prefix_cache.stats
            cache = CacheGroup(
                available=True,
                size_tokens=prefix_cache.size_tokens,
                lookups_total=stats.lookups,
                hits_total=stats.hits,
                prompt_tokens_total=stats.prompt_tokens,
                cached_tokens_total=stats.cached_tokens,
                hit_rate=stats.hit_rate if stats.lookups else None,
                token_hit_rate=stats.token_hit_rate if stats.prompt_tokens else None,
            )

        model_health = health.get(name) if health is not None else None
        if model_health is None or model_health.slo_p99_ms is None:
            slo = SloGroup(False, None, None, None, None, None, None)
        else:
            slo = SloGroup(
                available=True,
                degraded=model_health.degraded,
                slo_p99_ms=model_health.slo_p99_ms,
                recent_p99_ms=model_health.recent_p99_ms,
                samples=model_health.samples,
                window_ms=model_health.window_ms,
                min_samples=model_health.min_samples,
            )

        summary = self._metrics.summary(name)
        totals = self._series.model_totals(name)
        return ModelSnapshot(
            traffic=TrafficGroup(
                available=True,
                in_flight=totals.in_flight,
                arrived_total=totals.arrived,
                completed_total=totals.completed,
                output_tokens_total=totals.output_tokens,
                queue_full_total=totals.queue_full,
                timeout_total=totals.timeouts,
                downgrade_total=totals.downgrades,
            ),
            queue=queue,
            batch=batch,
            kv=kv,
            cache=cache,
            latency=LatencyGroup(
                available=True,
                window_ms=summary.window.window_ms,
                ttft_ms=_quantiles(summary.ttft_ms),
                itl_ms=_quantiles(summary.itl_ms),
                total_ms=_quantiles(summary.total_ms),
            ),
            slo=slo,
            series=self._series.model_series(name, history_ms),
        )

    def _tenants(self, history_ms: float) -> TenantsGroup:
        if self._applications is None or self._ledger is None:
            return TenantsGroup(False, None)
        tenants: dict[str, TenantSnapshot] = {}
        for application in self._applications.list_applications():
            tenant_id, plan = application.id, application.plan
            usage = self._ledger.usage(tenant_id)
            totals = self._series.tenant_totals(tenant_id)
            tenants[tenant_id] = TenantSnapshot(
                plan=plan.name,
                period_start_ms=usage.period_start_ms,
                period_end_ms=usage.period_end_ms,
                budget_tokens=plan.monthly_token_budget,
                used_tokens=usage.total.billed_tokens,
                remaining_tokens=max(0, plan.monthly_token_budget - usage.total.billed_tokens),
                spend_usd=usage.total.billed_cost_usd,
                requests=usage.total.requests,
                rejections_total=totals.rejections,
                timeout_total=totals.timeouts,
                downgrade_total=totals.downgrades,
                series=self._series.tenant_series(tenant_id, history_ms),
            )
        return TenantsGroup(True, tenants)


# --- Prometheus text format ----------------------------------------------------------------

_Sample = tuple[Mapping[str, str], float | None]


def _label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _number(value: float) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value) if isinstance(value, int) else repr(float(value))


def _family(name: str, kind: str, text: str, samples: Iterable[_Sample]) -> list[str]:
    """One metric family. Samples whose value is None are left out, and so is a family
    with none left: an absent series means unavailable, which a 0 would not."""
    lines = []
    for labels, value in samples:
        if value is None:
            continue
        rendered = ",".join(f'{k}="{_label_value(v)}"' for k, v in labels.items())
        series = f"{name}{{{rendered}}}" if rendered else name
        lines.append(f"{series} {_number(value)}")
    if not lines:
        return []
    return [f"# HELP {name} {text}", f"# TYPE {name} {kind}", *lines]


def render_prometheus(snapshot: Snapshot) -> str:
    """The snapshot's current values in the Prometheus text exposition format (0.0.4).

    Times are in milliseconds, as everywhere in the gateway, and the metric names say so.
    """
    models = snapshot.models

    def per_model(value_of: Mapping[str, float | None]) -> list[_Sample]:
        return [({"model": name}, value) for name, value in value_of.items()]

    def quantiles(pick: str) -> list[_Sample]:
        samples: list[_Sample] = []
        for name, model in models.items():
            found: Quantiles | None = getattr(model.latency, pick)
            if found is not None:
                samples.append(({"model": name, "quantile": "0.5"}, found.p50))
                samples.append(({"model": name, "quantile": "0.99"}, found.p99))
        return samples

    groups = ("traffic", "queue", "batch", "kv", "cache", "latency", "slo")
    families: list[tuple[str, str, str, list[_Sample]]] = [
        (
            "gateway_metric_group_available",
            "gauge",
            "1 if the gateway can observe this group of metrics for the model, else 0.",
            [
                ({"model": name, "group": group}, getattr(model, group).available)
                for name, model in models.items()
                for group in groups
            ],
        ),
        (
            "gateway_requests_arrived_total",
            "counter",
            "Requests handed to the model's worker, including ones it refused.",
            per_model({n: m.traffic.arrived_total for n, m in models.items()}),
        ),
        (
            "gateway_requests_completed_total",
            "counter",
            "Requests that finished successfully.",
            per_model({n: m.traffic.completed_total for n, m in models.items()}),
        ),
        (
            "gateway_output_tokens_total",
            "counter",
            "Output tokens produced, including those of requests that did not finish.",
            per_model({n: m.traffic.output_tokens_total for n, m in models.items()}),
        ),
        (
            "gateway_queue_full_total",
            "counter",
            "Requests refused with 429 because the model's queue was full.",
            per_model({n: m.traffic.queue_full_total for n, m in models.items()}),
        ),
        (
            "gateway_timeouts_total",
            "counter",
            "Requests cancelled with 504 at the request timeout.",
            per_model({n: m.traffic.timeout_total for n, m in models.items()}),
        ),
        (
            "gateway_downgrades_total",
            "counter",
            "Requests moved from this model to a cheaper one.",
            per_model({n: m.traffic.downgrade_total for n, m in models.items()}),
        ),
        (
            "gateway_in_flight_requests",
            "gauge",
            "Requests currently with the model's worker.",
            per_model({n: m.traffic.in_flight for n, m in models.items()}),
        ),
        (
            "gateway_queue_length",
            "gauge",
            "Requests waiting for a batch slot.",
            per_model({n: m.queue.length for n, m in models.items()}),
        ),
        (
            "gateway_batch_size",
            "gauge",
            "Requests seated in the running batch.",
            per_model({n: m.batch.size for n, m in models.items()}),
        ),
        (
            "gateway_batch_max_size",
            "gauge",
            "Most requests the batch can seat.",
            per_model({n: m.batch.max_size for n, m in models.items()}),
        ),
        (
            "gateway_kv_used_tokens",
            "gauge",
            "KV tokens held by running requests and the prefix cache.",
            per_model({n: m.kv.used_tokens for n, m in models.items()}),
        ),
        (
            "gateway_kv_capacity_tokens",
            "gauge",
            "KV tokens the model can hold.",
            per_model({n: m.kv.capacity_tokens for n, m in models.items()}),
        ),
        (
            "gateway_cache_lookups_total",
            "counter",
            "Prefix cache lookups.",
            per_model({n: m.cache.lookups_total for n, m in models.items()}),
        ),
        (
            "gateway_cache_hits_total",
            "counter",
            "Prefix cache lookups that matched at least one token.",
            per_model({n: m.cache.hits_total for n, m in models.items()}),
        ),
        (
            "gateway_cache_prompt_tokens_total",
            "counter",
            "Prompt tokens looked up in the prefix cache.",
            per_model({n: m.cache.prompt_tokens_total for n, m in models.items()}),
        ),
        (
            "gateway_cache_cached_tokens_total",
            "counter",
            "Looked-up prompt tokens that were already cached.",
            per_model({n: m.cache.cached_tokens_total for n, m in models.items()}),
        ),
        (
            "gateway_ttft_ms",
            "gauge",
            "Time to first token over the metrics window, nearest-rank, in ms.",
            quantiles("ttft_ms"),
        ),
        (
            "gateway_itl_ms",
            "gauge",
            "Inter-token latency over the metrics window, nearest-rank, in ms.",
            quantiles("itl_ms"),
        ),
        (
            "gateway_total_latency_ms",
            "gauge",
            "Total latency over the metrics window, nearest-rank, in ms.",
            quantiles("total_ms"),
        ),
        (
            "gateway_degraded",
            "gauge",
            "1 while the model is missing its p99 SLO, else 0.",
            per_model({n: m.slo.degraded for n, m in models.items()}),
        ),
    ]

    tenants = snapshot.tenants.tenants or {}

    def per_tenant(value_of: Mapping[str, float | None]) -> list[_Sample]:
        return [({"tenant": tenant_id}, value) for tenant_id, value in value_of.items()]

    families += [
        (
            "gateway_tenant_spend_usd",
            "gauge",
            "Billed to the tenant in its current budget period.",
            per_tenant({t: s.spend_usd for t, s in tenants.items()}),
        ),
        (
            "gateway_tenant_used_tokens",
            "gauge",
            "Billed tokens the tenant has used in its current budget period.",
            per_tenant({t: s.used_tokens for t, s in tenants.items()}),
        ),
        (
            "gateway_tenant_budget_tokens",
            "gauge",
            "Billed tokens the tenant's plan allows per budget period.",
            per_tenant({t: s.budget_tokens for t, s in tenants.items()}),
        ),
        (
            "gateway_tenant_requests",
            "gauge",
            "Requests the tenant completed in its current budget period.",
            per_tenant({t: s.requests for t, s in tenants.items()}),
        ),
        (
            "gateway_tenant_rejections_total",
            "counter",
            "Requests of the tenant refused with 429, by reason.",
            [
                ({"tenant": tenant_id, "reason": reason}, count)
                for tenant_id, tenant in tenants.items()
                for reason, count in tenant.rejections_total.items()
            ],
        ),
        (
            "gateway_tenant_timeouts_total",
            "counter",
            "Requests of the tenant cancelled at the request timeout.",
            per_tenant({t: s.timeout_total for t, s in tenants.items()}),
        ),
        (
            "gateway_tenant_downgrades_total",
            "counter",
            "Requests of the tenant moved to a cheaper model.",
            per_tenant({t: s.downgrade_total for t, s in tenants.items()}),
        ),
        (
            "gateway_events_total",
            "counter",
            "Events published on the gateway's event bus, by type.",
            [({"type": kind}, count) for kind, count in snapshot.events.counts.items()],
        ),
    ]

    lines: list[str] = []
    for name, kind, text, samples in families:
        lines += _family(name, kind, text, samples)
    return "\n".join(lines) + "\n"
