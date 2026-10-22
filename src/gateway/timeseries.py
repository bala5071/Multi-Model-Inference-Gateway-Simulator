"""Bounded per-second time series for the monitoring API. See docs/15-monitoring-api.md."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import NamedTuple, Protocol, TypeVar

from gateway.clock import Clock
from gateway.metrics import percentile
from gateway.prefix_cache import CacheStats
from gateway.schema import RequestResult

DEFAULT_WINDOW_MS = 900_000.0
DEFAULT_RESOLUTION_MS = 1000.0
DEFAULT_MAX_BUCKET_SAMPLES = 1024
DEFAULT_MAX_BUCKET_ITL_SAMPLES = 8192

REJECTION_REASONS = ("budget_exhausted", "queue_full")


@dataclass(frozen=True, slots=True)
class GaugeSample:
    """What a worker's own scheduler shows at one instant.

    A field is None when the worker has no such thing to show. ``cache`` is the prefix
    cache's counters since it was created; the store turns them into per-bucket changes.
    """

    queue_len: int | None = None
    batch_size: int | None = None
    kv_used_tokens: int | None = None
    cache: CacheStats | None = None


@dataclass(frozen=True, slots=True)
class ModelPoint:
    """One model over one resolution step, starting at ``t_ms``.

    Counts cover the step. A gauge is its last sample in the step and ``*_max`` the largest;
    both are None if the step has no sample of it. Percentiles are nearest-rank over the
    requests that finished in the step and None if none did.
    """

    t_ms: float
    # False while the step is still running: the point can still change.
    final: bool
    arrived: int
    completed: int
    req_per_s: float
    output_tokens: int
    tokens_per_s: float
    queue_full: int
    timeouts: int
    downgrades: int
    in_flight: int
    in_flight_max: int
    queue_len: int | None
    queue_len_max: int | None
    batch_size: int | None
    batch_size_max: int | None
    kv_used_tokens: int | None
    kv_used_tokens_max: int | None
    cache_lookups: int | None
    cache_hit_rate: float | None
    cache_token_hit_rate: float | None
    ttft_p50_ms: float | None
    ttft_p99_ms: float | None
    itl_p50_ms: float | None
    itl_p99_ms: float | None
    total_p50_ms: float | None
    total_p99_ms: float | None
    # True if the step had more finished requests or token gaps than its sample caps, so
    # its percentiles cover only the first ones.
    latency_truncated: bool
    degraded: bool | None


@dataclass(frozen=True, slots=True)
class ModelTotals:
    """One model's counters since the store was created, and its requests in progress."""

    in_flight: int
    arrived: int
    completed: int
    output_tokens: int
    queue_full: int
    timeouts: int
    downgrades: int


@dataclass(frozen=True, slots=True)
class TenantPoint:
    t_ms: float
    final: bool
    requests: int
    billed_tokens: int
    spend_usd: float
    rejections: int
    timeouts: int
    downgrades: int


@dataclass(frozen=True, slots=True)
class TenantTotals:
    requests: int
    billed_tokens: int
    spend_usd: float
    rejections: dict[str, int]
    timeouts: int
    downgrades: int


class _Latency(NamedTuple):
    ttft_p50: float | None
    ttft_p99: float | None
    itl_p50: float | None
    itl_p99: float | None
    total_p50: float | None
    total_p99: float | None


@dataclass(slots=True)
class _ModelBucket:
    index: int
    in_flight: int
    in_flight_max: int
    degraded: bool | None
    arrived: int = 0
    completed: int = 0
    output_tokens: int = 0
    queue_full: int = 0
    timeouts: int = 0
    downgrades: int = 0
    queue_len: int | None = None
    queue_len_max: int | None = None
    batch_size: int | None = None
    batch_size_max: int | None = None
    kv_used_tokens: int | None = None
    kv_used_tokens_max: int | None = None
    # None until the cache has been looked at in this step.
    cache_lookups: int | None = None
    cache_hits: int = 0
    cache_prompt_tokens: int = 0
    cache_cached_tokens: int = 0
    # Raw samples, held only while the step is open.
    ttft: list[float] = field(default_factory=list)
    total: list[float] = field(default_factory=list)
    itl: list[float] = field(default_factory=list)
    truncated: bool = False
    # Set when the step is closed; the raw samples are dropped then.
    latency: _Latency | None = None


@dataclass(slots=True)
class _ModelState:
    buckets: deque[_ModelBucket] = field(default_factory=deque)
    in_flight: int = 0
    degraded: bool | None = None
    arrived: int = 0
    completed: int = 0
    output_tokens: int = 0
    queue_full: int = 0
    timeouts: int = 0
    downgrades: int = 0
    # The cache counters at the last observation; None before the first.
    cache: CacheStats | None = None
    observed_ms: float | None = None


@dataclass(slots=True)
class _TenantBucket:
    index: int
    requests: int = 0
    billed_tokens: int = 0
    spend_usd: float = 0.0
    rejections: int = 0
    timeouts: int = 0
    downgrades: int = 0


@dataclass(slots=True)
class _TenantState:
    buckets: deque[_TenantBucket] = field(default_factory=deque)
    requests: int = 0
    billed_tokens: int = 0
    spend_usd: float = 0.0
    rejections: dict[str, int] = field(default_factory=lambda: dict.fromkeys(REJECTION_REASONS, 0))
    timeouts: int = 0
    downgrades: int = 0


class _Indexed(Protocol):
    @property
    def index(self) -> int: ...


_B = TypeVar("_B", bound=_Indexed)


def _pair(values: list[float]) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    return percentile(values, 50), percentile(values, 99)


def _latency(bucket: _ModelBucket) -> _Latency:
    if bucket.latency is not None:
        return bucket.latency
    return _Latency(*_pair(bucket.ttft), *_pair(bucket.itl), *_pair(bucket.total))


def _close(bucket: _ModelBucket) -> None:
    """Reduce a finished step's raw latency samples to its six percentiles."""
    if bucket.latency is None:
        bucket.latency = _latency(bucket)
        bucket.ttft, bucket.total, bucket.itl = [], [], []


def _larger(current: int | None, value: int) -> int:
    return value if current is None else max(current, value)


class TimeSeriesStore:
    """Rolling per-model and per-tenant series at a fixed resolution, in bounded memory.

    Time on the injected clock is cut into steps of ``resolution_ms``. A step gets a
    bucket the first time something is recorded in it; a step in which nothing happened
    has none, and readers see a gap. A bucket is dropped once it is ``window_ms`` old, so
    each model or tenant holds at most ``ceil(window_ms / resolution_ms)`` of them.

    Only the newest bucket of a model holds raw latency samples, at most
    ``max_bucket_samples`` requests and ``max_bucket_itl_samples`` token gaps. When the
    clock moves into a later step they are reduced to p50 and p99 and dropped.

    The store does not bound the number of models or tenants; callers must only name ones
    that are configured. Nothing here awaits, so each call is atomic on an event loop. Not
    safe to share between threads.
    """

    def __init__(
        self,
        clock: Clock,
        *,
        window_ms: float = DEFAULT_WINDOW_MS,
        resolution_ms: float = DEFAULT_RESOLUTION_MS,
        max_bucket_samples: int = DEFAULT_MAX_BUCKET_SAMPLES,
        max_bucket_itl_samples: int = DEFAULT_MAX_BUCKET_ITL_SAMPLES,
    ) -> None:
        if resolution_ms <= 0:
            raise ValueError("resolution_ms must be positive")
        if window_ms < resolution_ms:
            raise ValueError("window_ms must be at least resolution_ms")
        if max_bucket_samples < 1 or max_bucket_itl_samples < 1:
            raise ValueError("max_bucket_samples and max_bucket_itl_samples must be at least 1")
        self._clock = clock
        self._window_ms = window_ms
        self._resolution_ms = resolution_ms
        self._capacity = math.ceil(window_ms / resolution_ms)
        self._max_samples = max_bucket_samples
        self._max_itl_samples = max_bucket_itl_samples
        self._models: dict[str, _ModelState] = {}
        self._tenants: dict[str, _TenantState] = {}

    @property
    def window_ms(self) -> float:
        return self._window_ms

    @property
    def resolution_ms(self) -> float:
        return self._resolution_ms

    @property
    def capacity(self) -> int:
        """The most buckets one model or tenant can hold."""
        return self._capacity

    # --- models: writes --------------------------------------------------------------------

    def request_started(self, model: str) -> None:
        """A request was handed to the model's worker."""
        state, bucket = self._model(model)
        state.arrived += 1
        bucket.arrived += 1
        self._set_in_flight(state, bucket, state.in_flight + 1)

    def request_ended(self, model: str) -> None:
        """A started request is no longer with the worker, however it ended."""
        state, bucket = self._model(model)
        self._set_in_flight(state, bucket, max(0, state.in_flight - 1))

    def tokens(self, model: str, count: int = 1) -> None:
        """The model produced ``count`` output tokens just now."""
        state, bucket = self._model(model)
        state.output_tokens += count
        bucket.output_tokens += count

    def request_completed(self, model: str, result: RequestResult) -> None:
        """A request finished successfully; its latencies join the current step's."""
        state, bucket = self._model(model)
        state.completed += 1
        bucket.completed += 1
        if len(bucket.total) < self._max_samples:
            bucket.ttft.append(result.time_to_first_token_ms)
            bucket.total.append(result.latency_ms)
        else:
            bucket.truncated = True
        gaps = result.inter_token_ms
        room = self._max_itl_samples - len(bucket.itl)
        if len(gaps) > room:
            bucket.truncated = True
        bucket.itl.extend(gaps[:room])

    def queue_full(self, model: str) -> None:
        state, bucket = self._model(model)
        state.queue_full += 1
        bucket.queue_full += 1

    def timed_out(self, model: str) -> None:
        state, bucket = self._model(model)
        state.timeouts += 1
        bucket.timeouts += 1

    def downgraded(self, model: str) -> None:
        """A request that would have used ``model`` was moved to a cheaper one."""
        state, bucket = self._model(model)
        state.downgrades += 1
        bucket.downgrades += 1

    def set_degraded(self, model: str, degraded: bool) -> None:
        state, bucket = self._model(model)
        state.degraded = degraded
        bucket.degraded = degraded

    def observe(self, model: str, sample: GaugeSample) -> None:
        """Record what the model's scheduler shows now."""
        state, bucket = self._model(model)
        state.observed_ms = self._clock.now_ms()
        if sample.queue_len is not None:
            bucket.queue_len = sample.queue_len
            bucket.queue_len_max = _larger(bucket.queue_len_max, sample.queue_len)
        if sample.batch_size is not None:
            bucket.batch_size = sample.batch_size
            bucket.batch_size_max = _larger(bucket.batch_size_max, sample.batch_size)
        if sample.kv_used_tokens is not None:
            bucket.kv_used_tokens = sample.kv_used_tokens
            bucket.kv_used_tokens_max = _larger(bucket.kv_used_tokens_max, sample.kv_used_tokens)
        if sample.cache is not None:
            # Lookups made before the first observation belong to no step.
            before = state.cache if state.cache is not None else sample.cache
            state.cache = sample.cache
            bucket.cache_lookups = (bucket.cache_lookups or 0) + (
                sample.cache.lookups - before.lookups
            )
            bucket.cache_hits += sample.cache.hits - before.hits
            bucket.cache_prompt_tokens += sample.cache.prompt_tokens - before.prompt_tokens
            bucket.cache_cached_tokens += sample.cache.cached_tokens - before.cached_tokens

    def observed_ms(self, model: str) -> float | None:
        """When ``observe`` was last called for the model; None if never."""
        state = self._models.get(model)
        return state.observed_ms if state is not None else None

    # --- models: reads ---------------------------------------------------------------------

    def model_totals(self, model: str) -> ModelTotals:
        state = self._models.get(model)
        if state is None:
            return ModelTotals(0, 0, 0, 0, 0, 0, 0)
        return ModelTotals(
            in_flight=state.in_flight,
            arrived=state.arrived,
            completed=state.completed,
            output_tokens=state.output_tokens,
            queue_full=state.queue_full,
            timeouts=state.timeouts,
            downgrades=state.downgrades,
        )

    def model_series(self, model: str, history_ms: float = 0.0) -> tuple[ModelPoint, ...]:
        """The model's points, oldest first, from the step ``history_ms`` ago to now.

        With ``history_ms`` 0 that is the current step alone. Steps with no bucket are
        left out. Never more than ``capacity`` points.
        """
        state = self._models.get(model)
        if state is None:
            return ()
        now_index = self._index()
        self._expire(state.buckets, now_index)
        if state.buckets and state.buckets[-1].index < now_index:
            _close(state.buckets[-1])
        return tuple(self._point(b, now_index) for b in self._newest(state.buckets, history_ms))

    def _point(self, b: _ModelBucket, now_index: int) -> ModelPoint:
        seconds = self._resolution_ms / 1000
        latency = _latency(b)
        return ModelPoint(
            t_ms=b.index * self._resolution_ms,
            final=b.index < now_index,
            arrived=b.arrived,
            completed=b.completed,
            req_per_s=b.arrived / seconds,
            output_tokens=b.output_tokens,
            tokens_per_s=b.output_tokens / seconds,
            queue_full=b.queue_full,
            timeouts=b.timeouts,
            downgrades=b.downgrades,
            in_flight=b.in_flight,
            in_flight_max=b.in_flight_max,
            queue_len=b.queue_len,
            queue_len_max=b.queue_len_max,
            batch_size=b.batch_size,
            batch_size_max=b.batch_size_max,
            kv_used_tokens=b.kv_used_tokens,
            kv_used_tokens_max=b.kv_used_tokens_max,
            cache_lookups=b.cache_lookups,
            cache_hit_rate=b.cache_hits / b.cache_lookups if b.cache_lookups else None,
            cache_token_hit_rate=(
                b.cache_cached_tokens / b.cache_prompt_tokens if b.cache_prompt_tokens else None
            ),
            ttft_p50_ms=latency.ttft_p50,
            ttft_p99_ms=latency.ttft_p99,
            itl_p50_ms=latency.itl_p50,
            itl_p99_ms=latency.itl_p99,
            total_p50_ms=latency.total_p50,
            total_p99_ms=latency.total_p99,
            latency_truncated=b.truncated,
            degraded=b.degraded,
        )

    # --- tenants ---------------------------------------------------------------------------

    def tenant_completed(self, tenant_id: str, *, billed_tokens: int, spend_usd: float) -> None:
        state, bucket = self._tenant(tenant_id)
        state.requests += 1
        state.billed_tokens += billed_tokens
        state.spend_usd += spend_usd
        bucket.requests += 1
        bucket.billed_tokens += billed_tokens
        bucket.spend_usd += spend_usd

    def tenant_rejected(self, tenant_id: str, reason: str) -> None:
        state, bucket = self._tenant(tenant_id)
        if reason not in state.rejections:
            raise ValueError(f"unknown rejection reason {reason!r}")
        state.rejections[reason] += 1
        bucket.rejections += 1

    def tenant_timed_out(self, tenant_id: str) -> None:
        state, bucket = self._tenant(tenant_id)
        state.timeouts += 1
        bucket.timeouts += 1

    def tenant_downgraded(self, tenant_id: str) -> None:
        state, bucket = self._tenant(tenant_id)
        state.downgrades += 1
        bucket.downgrades += 1

    def tenant_totals(self, tenant_id: str) -> TenantTotals:
        state = self._tenants.get(tenant_id)
        if state is None:
            return TenantTotals(0, 0, 0.0, dict.fromkeys(REJECTION_REASONS, 0), 0, 0)
        return TenantTotals(
            requests=state.requests,
            billed_tokens=state.billed_tokens,
            spend_usd=state.spend_usd,
            rejections=dict(state.rejections),
            timeouts=state.timeouts,
            downgrades=state.downgrades,
        )

    def tenant_series(self, tenant_id: str, history_ms: float = 0.0) -> tuple[TenantPoint, ...]:
        state = self._tenants.get(tenant_id)
        if state is None:
            return ()
        now_index = self._index()
        self._expire(state.buckets, now_index)
        return tuple(
            TenantPoint(
                t_ms=b.index * self._resolution_ms,
                final=b.index < now_index,
                requests=b.requests,
                billed_tokens=b.billed_tokens,
                spend_usd=b.spend_usd,
                rejections=b.rejections,
                timeouts=b.timeouts,
                downgrades=b.downgrades,
            )
            for b in self._newest(state.buckets, history_ms)
        )

    # --- memory ----------------------------------------------------------------------------

    def retained_buckets(self) -> int:
        """Buckets held right now, over all models and tenants."""
        return sum(len(s.buckets) for s in self._models.values()) + sum(
            len(s.buckets) for s in self._tenants.values()
        )

    def retained_samples(self) -> int:
        """Raw latency samples held right now, over all models."""
        return sum(
            len(b.ttft) + len(b.total) + len(b.itl)
            for state in self._models.values()
            for b in state.buckets
        )

    # --- internals -------------------------------------------------------------------------

    def _index(self) -> int:
        return int(self._clock.now_ms() // self._resolution_ms)

    def _expire(self, buckets: deque[_B], now_index: int) -> None:
        while buckets and buckets[0].index <= now_index - self._capacity:
            buckets.popleft()

    def _newest(self, buckets: deque[_B], history_ms: float) -> list[_B]:
        first = int((self._clock.now_ms() - max(0.0, history_ms)) // self._resolution_ms)
        newest: list[_B] = []
        for bucket in reversed(buckets):
            if bucket.index < first:
                break
            newest.append(bucket)
        newest.reverse()
        return newest

    def _model(self, model: str) -> tuple[_ModelState, _ModelBucket]:
        state = self._models.get(model)
        if state is None:
            state = self._models[model] = _ModelState()
        index = self._index()
        buckets = state.buckets
        if not buckets or buckets[-1].index != index:
            if buckets:
                _close(buckets[-1])
            buckets.append(_ModelBucket(index, state.in_flight, state.in_flight, state.degraded))
            self._expire(buckets, index)
        return state, buckets[-1]

    def _set_in_flight(self, state: _ModelState, bucket: _ModelBucket, value: int) -> None:
        state.in_flight = value
        bucket.in_flight = value
        bucket.in_flight_max = max(bucket.in_flight_max, value)

    def _tenant(self, tenant_id: str) -> tuple[_TenantState, _TenantBucket]:
        state = self._tenants.get(tenant_id)
        if state is None:
            state = self._tenants[tenant_id] = _TenantState()
        index = self._index()
        buckets = state.buckets
        if not buckets or buckets[-1].index != index:
            buckets.append(_TenantBucket(index))
            self._expire(buckets, index)
        return state, buckets[-1]
