"""Latency statistics: the nearest-rank percentile and the bounded per-model store."""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import NamedTuple

from gateway.clock import Clock
from gateway.schema import RequestResult

DEFAULT_WINDOW_MS = 300_000.0
DEFAULT_MAX_SAMPLES = 10_000
DEFAULT_MAX_ITL_SAMPLES = 50_000


def percentile(values: Iterable[float], q: float) -> float:
    """Nearest-rank percentile: the smallest value with at least ``q`` % of values <= it.

    Always returns one of the input values; it never interpolates.
    """
    if not 0 < q <= 100:
        raise ValueError("q must be in (0, 100]")
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile of no values is undefined")
    return ordered[math.ceil(q / 100 * len(ordered)) - 1]


@dataclass(frozen=True, slots=True)
class Distribution:
    """One metric over the samples currently retained. Percentiles are nearest-rank.

    With fewer than 100 samples p99 is the largest sample, and with one sample every field
    is that sample; ``samples`` is there so a reader can tell.
    """

    samples: int
    mean: float
    p50: float
    p90: float
    p99: float
    max: float


@dataclass(frozen=True, slots=True)
class WindowTotals:
    """What the retained requests add up to, and how much of the window they cover."""

    window_ms: float
    # Age of the oldest retained request; 0 when nothing is retained.
    span_ms: float
    requests: int
    tokens_in: int
    tokens_out: int
    cached_tokens: int
    cost_usd: float
    # True when the ring was full and dropped a sample that is still inside the time
    # window: the distributions then cover less than ``window_ms``.
    truncated: bool
    itl_truncated: bool


@dataclass(frozen=True, slots=True)
class ModelSummary:
    """One model's metrics.

    The counters are totals since the store was created. The distributions and ``window``
    cover only what is retained, and the distributions are None when that is nothing.
    """

    requests: int
    queue_ms: Distribution | None
    ttft_ms: Distribution | None
    total_ms: Distribution | None
    # Inter-token latency: gaps between consecutive output tokens, over all retained
    # requests. None until some retained request has produced at least two tokens.
    itl_ms: Distribution | None
    tokens_in: int
    tokens_out: int
    cached_tokens: int
    cost_usd: float
    window: WindowTotals


class _Sample(NamedTuple):
    at_ms: float
    queue_ms: float
    ttft_ms: float
    total_ms: float
    tokens_in: int
    tokens_out: int
    cached_tokens: int
    cost_usd: float


class _Gap(NamedTuple):
    at_ms: float
    gap_ms: float


@dataclass(slots=True)
class _ModelState:
    samples: deque[_Sample]
    gaps: deque[_Gap]
    requests: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
    # When the newest sample pushed out by a full ring was recorded.
    sample_dropped_at_ms: float | None = None
    gap_dropped_at_ms: float | None = None


def _distribution(values: list[float]) -> Distribution | None:
    if not values:
        return None
    ordered = sorted(values)

    def rank(q: float) -> float:
        return ordered[math.ceil(q / 100 * len(ordered)) - 1]

    return Distribution(
        samples=len(ordered),
        mean=math.fsum(ordered) / len(ordered),
        p50=rank(50),
        p90=rank(90),
        p99=rank(99),
        max=ordered[-1],
    )


class MetricsStore:
    """Per-model request metrics in bounded memory.

    Each model has lifetime counters (requests, tokens, cost) and two rings of recent
    samples: one entry per request, and one per inter-token gap. A sample is kept while it
    is both younger than ``window_ms`` on the clock and among the newest ``max_samples``
    (``max_itl_samples`` for gaps), so memory per model never exceeds those two limits
    however many requests are recorded. Percentiles are exact over what is kept.

    A sample is stamped with the clock when it is recorded, which is when its request
    finished. One recorded exactly ``window_ms`` ago is no longer in the window.

    ``record`` and ``summary`` never await, so on an event loop each runs to completion
    before any other task: concurrent tasks cannot interleave inside an update or see half
    of one. The store is not safe to share between threads.
    """

    def __init__(
        self,
        clock: Clock,
        *,
        window_ms: float = DEFAULT_WINDOW_MS,
        max_samples: int = DEFAULT_MAX_SAMPLES,
        max_itl_samples: int = DEFAULT_MAX_ITL_SAMPLES,
    ) -> None:
        if window_ms <= 0:
            raise ValueError("window_ms must be positive")
        if max_samples < 1 or max_itl_samples < 1:
            raise ValueError("max_samples and max_itl_samples must be at least 1")
        self._clock = clock
        self._window_ms = window_ms
        self._max_samples = max_samples
        self._max_itl_samples = max_itl_samples
        self._models: dict[str, _ModelState] = {}

    def record(self, model: str, result: RequestResult) -> None:
        state = self._models.get(model)
        if state is None:
            state = self._models[model] = _ModelState(
                samples=deque(maxlen=self._max_samples), gaps=deque(maxlen=self._max_itl_samples)
            )
        now_ms = self._clock.now_ms()
        self._expire(state, now_ms)

        state.requests += 1
        state.tokens_in += result.tokens_in
        state.tokens_out += result.tokens_out
        state.cached_tokens += result.cached_tokens
        state.cost_usd += result.cost_usd

        if len(state.samples) == self._max_samples:
            state.sample_dropped_at_ms = state.samples[0].at_ms
        state.samples.append(
            _Sample(
                at_ms=now_ms,
                queue_ms=result.queue_wait_ms,
                ttft_ms=result.time_to_first_token_ms,
                total_ms=result.latency_ms,
                tokens_in=result.tokens_in,
                tokens_out=result.tokens_out,
                cached_tokens=result.cached_tokens,
                cost_usd=result.cost_usd,
            )
        )
        for gap_ms in result.inter_token_ms:
            if len(state.gaps) == self._max_itl_samples:
                state.gap_dropped_at_ms = state.gaps[0].at_ms
            state.gaps.append(_Gap(now_ms, gap_ms))

    def summary(self, model: str) -> ModelSummary:
        """The model's metrics now. A model with nothing recorded gives zeros and Nones."""
        now_ms = self._clock.now_ms()
        state = self._models.get(model)
        if state is None:
            return ModelSummary(
                requests=0,
                queue_ms=None,
                ttft_ms=None,
                total_ms=None,
                itl_ms=None,
                tokens_in=0,
                tokens_out=0,
                cached_tokens=0,
                cost_usd=0.0,
                window=WindowTotals(self._window_ms, 0.0, 0, 0, 0, 0, 0.0, False, False),
            )
        self._expire(state, now_ms)
        samples = state.samples
        cutoff_ms = now_ms - self._window_ms

        def in_window(dropped_at_ms: float | None) -> bool:
            return dropped_at_ms is not None and dropped_at_ms > cutoff_ms

        return ModelSummary(
            requests=state.requests,
            queue_ms=_distribution([s.queue_ms for s in samples]),
            ttft_ms=_distribution([s.ttft_ms for s in samples]),
            total_ms=_distribution([s.total_ms for s in samples]),
            itl_ms=_distribution([g.gap_ms for g in state.gaps]),
            tokens_in=state.tokens_in,
            tokens_out=state.tokens_out,
            cached_tokens=state.cached_tokens,
            cost_usd=state.cost_usd,
            window=WindowTotals(
                window_ms=self._window_ms,
                span_ms=now_ms - samples[0].at_ms if samples else 0.0,
                requests=len(samples),
                tokens_in=sum(s.tokens_in for s in samples),
                tokens_out=sum(s.tokens_out for s in samples),
                cached_tokens=sum(s.cached_tokens for s in samples),
                cost_usd=math.fsum(s.cost_usd for s in samples),
                truncated=in_window(state.sample_dropped_at_ms),
                itl_truncated=in_window(state.gap_dropped_at_ms),
            ),
        )

    def retained_samples(self) -> int:
        """Samples held right now, over all models and both rings."""
        return sum(len(state.samples) + len(state.gaps) for state in self._models.values())

    def _expire(self, state: _ModelState, now_ms: float) -> None:
        cutoff_ms = now_ms - self._window_ms
        while state.samples and state.samples[0].at_ms <= cutoff_ms:
            state.samples.popleft()
        while state.gaps and state.gaps[0].at_ms <= cutoff_ms:
            state.gaps.popleft()
