"""Latency statistics."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

from gateway.schema import RequestResult


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
class Percentiles:
    p50: float
    p99: float


@dataclass(frozen=True, slots=True)
class ModelSummary:
    """Totals and latency percentiles for one model. Percentiles are None with no requests."""

    requests: int
    queue_ms: Percentiles | None
    ttft_ms: Percentiles | None
    total_ms: Percentiles | None
    # Inter-token latency: gaps between consecutive output tokens, over all requests.
    # None until some request has produced at least two tokens.
    itl_ms: Percentiles | None
    tokens_in: int
    tokens_out: int
    cached_tokens: int
    cost_usd: float


class MetricsRecorder:
    """Keeps every finished request's result, grouped by model.

    Nothing is ever dropped, so memory grows with the number of requests. That is fine for
    simulations and tests; a long-running server would need a bounded window.
    """

    def __init__(self) -> None:
        self._results: dict[str, list[RequestResult]] = {}

    def record(self, model: str, result: RequestResult) -> None:
        self._results.setdefault(model, []).append(result)

    def summary(self, model: str) -> ModelSummary:
        results = self._results.get(model, [])

        def percentiles(values: list[float]) -> Percentiles | None:
            if not values:
                return None
            return Percentiles(p50=percentile(values, 50), p99=percentile(values, 99))

        return ModelSummary(
            requests=len(results),
            queue_ms=percentiles([r.queue_wait_ms for r in results]),
            ttft_ms=percentiles([r.time_to_first_token_ms for r in results]),
            total_ms=percentiles([r.latency_ms for r in results]),
            itl_ms=percentiles([gap for r in results for gap in r.inter_token_ms]),
            tokens_in=sum(r.tokens_in for r in results),
            tokens_out=sum(r.tokens_out for r in results),
            cached_tokens=sum(r.cached_tokens for r in results),
            cost_usd=sum(r.cost_usd for r in results),
        )
