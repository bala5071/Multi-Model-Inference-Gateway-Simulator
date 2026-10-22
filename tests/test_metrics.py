from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from gateway.app import create_app
from gateway.backends import SimulatedBackend
from gateway.baseline import BaselineWorker
from gateway.clock import SimClock
from gateway.metrics import (
    Distribution,
    MetricsStore,
    ModelSummary,
    WindowTotals,
    percentile,
)
from gateway.registry import ModelRegistry
from gateway.schema import GenerateRequest, RequestResult
from helpers import EXACT, SizedBackend


def test_percentile_uses_nearest_rank() -> None:
    values = [float(v) for v in range(1, 101)]
    assert percentile(values, 50) == 50.0
    assert percentile(values, 99) == 99.0
    assert percentile(values, 100) == 100.0
    assert percentile(values, 0.5) == 1.0


def test_percentile_returns_an_input_value_and_ignores_order() -> None:
    assert percentile([30.0, 10.0, 20.0], 50) == 20.0
    assert percentile([30.0, 10.0, 20.0], 99) == 30.0
    assert percentile([10.0, 20.0, 30.0, 40.0], 50) == 20.0
    assert percentile([7.0], 99) == 7.0


def test_percentile_accepts_any_iterable() -> None:
    assert percentile((float(v) for v in (3, 1, 2)), 100) == 3.0


@pytest.mark.parametrize("q", [0, -1, 100.1])
def test_percentile_rejects_q_out_of_range(q: float) -> None:
    with pytest.raises(ValueError, match="q must be"):
        percentile([1.0], q)


def test_percentile_of_nothing_is_an_error() -> None:
    with pytest.raises(ValueError, match="no values"):
        percentile([], 50)


# --- MetricsStore --------------------------------------------------------------------------


class ManualClock:
    """A clock the test moves by hand, so a million records need no event loop."""

    def __init__(self) -> None:
        self.t_ms = 0.0

    def now_ms(self) -> float:
        return self.t_ms

    async def sleep_ms(self, duration_ms: float) -> None:
        self.t_ms += duration_ms


def _result(
    total_ms: float,
    *,
    queue_ms: float = 0.0,
    ttft_ms: float | None = None,
    token_times_ms: tuple[float, ...] = (),
    tokens_in: int = 10,
    tokens_out: int = 2,
    cached_tokens: int = 0,
    cost_usd: float = 0.5,
) -> RequestResult:
    """A result that was enqueued at 0 and finished ``total_ms`` later."""
    return RequestResult(
        request_id="r",
        model="m",
        enqueue_ms=0.0,
        start_ms=queue_ms,
        first_token_ms=total_ms if ttft_ms is None else ttft_ms,
        finish_ms=total_ms,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
        cached_tokens=cached_tokens,
        token_times_ms=token_times_ms,
    )


def test_store_reports_known_percentiles_on_fixed_data() -> None:
    store = MetricsStore(ManualClock())
    # Shuffled on purpose: the order of arrival must not matter.
    for total in sorted(range(1, 101), key=lambda v: (v * 37) % 101):
        store.record("m", _result(float(total), queue_ms=total / 2, ttft_ms=total / 4))

    summary = store.summary("m")

    assert summary.total_ms == Distribution(
        samples=100, mean=50.5, p50=50.0, p90=90.0, p99=99.0, max=100.0
    )
    assert summary.queue_ms == Distribution(
        samples=100, mean=25.25, p50=25.0, p90=45.0, p99=49.5, max=50.0
    )
    assert summary.ttft_ms is not None
    assert (summary.ttft_ms.p50, summary.ttft_ms.p99) == (12.5, 24.75)


def test_store_adds_up_tokens_and_cost_per_model() -> None:
    store = MetricsStore(ManualClock())
    store.record("a", _result(5.0, tokens_in=10, tokens_out=3, cached_tokens=4, cost_usd=0.25))
    store.record("a", _result(5.0, tokens_in=20, tokens_out=5, cached_tokens=0, cost_usd=0.5))
    store.record("b", _result(5.0, tokens_in=1, tokens_out=1, cost_usd=2.0))

    a = store.summary("a")

    assert (a.requests, a.tokens_in, a.tokens_out, a.cached_tokens, a.cost_usd) == (
        2,
        30,
        8,
        4,
        0.75,
    )
    assert (a.window.requests, a.window.tokens_in, a.window.tokens_out) == (2, 30, 8)
    assert (a.window.cached_tokens, a.window.cost_usd) == (4, 0.75)
    assert store.summary("b").cost_usd == 2.0


def test_inter_token_latency_pools_the_gaps_of_all_requests() -> None:
    store = MetricsStore(ManualClock())
    store.record("m", _result(30.0, token_times_ms=(10.0, 14.0, 30.0)))  # gaps 4 and 16
    store.record("m", _result(20.0, token_times_ms=(12.0, 20.0)))  # gap 8
    store.record("m", _result(9.0, token_times_ms=(9.0,)))  # one token: no gap

    assert store.summary("m").itl_ms == Distribution(
        samples=3, mean=28 / 3, p50=8.0, p90=16.0, p99=16.0, max=16.0
    )


def test_an_empty_store_gives_zeros_and_nones_not_errors() -> None:
    summary = MetricsStore(ManualClock(), window_ms=1000.0).summary("never-seen")

    assert summary == ModelSummary(
        requests=0,
        queue_ms=None,
        ttft_ms=None,
        total_ms=None,
        itl_ms=None,
        tokens_in=0,
        tokens_out=0,
        cached_tokens=0,
        cost_usd=0.0,
        window=WindowTotals(
            window_ms=1000.0,
            span_ms=0.0,
            requests=0,
            tokens_in=0,
            tokens_out=0,
            cached_tokens=0,
            cost_usd=0.0,
            truncated=False,
            itl_truncated=False,
        ),
    )


def test_itl_is_none_until_a_request_has_two_tokens() -> None:
    store = MetricsStore(ManualClock())
    store.record("m", _result(9.0, token_times_ms=(9.0,)))

    summary = store.summary("m")

    assert summary.itl_ms is None
    assert summary.total_ms is not None


def test_tiny_samples_report_observed_values_and_say_how_many() -> None:
    store = MetricsStore(ManualClock())
    store.record("m", _result(7.0))
    assert store.summary("m").total_ms == Distribution(
        samples=1, mean=7.0, p50=7.0, p90=7.0, p99=7.0, max=7.0
    )

    store.record("m", _result(3.0))
    # Two samples: the median is the lower one, every higher percentile is the upper one.
    assert store.summary("m").total_ms == Distribution(
        samples=2, mean=5.0, p50=3.0, p90=7.0, p99=7.0, max=7.0
    )


def test_an_outlier_shows_in_p99_but_the_mean_does_not_show_it() -> None:
    store = MetricsStore(ManualClock())
    for _ in range(98):
        store.record("m", _result(10.0))
    for _ in range(2):
        store.record("m", _result(10_000.0))

    total = store.summary("m").total_ms

    assert total is not None
    assert total.p50 == 10.0
    assert total.p99 == 10_000.0
    # The mean moves, but to a latency no request had and far below the outlier.
    assert total.mean == pytest.approx(209.8)


def test_one_outlier_in_a_hundred_is_in_max_but_not_in_nearest_rank_p99() -> None:
    store = MetricsStore(ManualClock())
    for _ in range(99):
        store.record("m", _result(10.0))
    store.record("m", _result(10_000.0))

    total = store.summary("m").total_ms

    assert total is not None
    assert (total.p99, total.max) == (10.0, 10_000.0)


def test_total_latency_includes_queue_wait() -> None:
    store = MetricsStore(ManualClock())
    # Waited 100 ms, then 10 ms to the first token and 40 ms more to the end.
    store.record("m", _result(150.0, queue_ms=100.0, ttft_ms=110.0))

    summary = store.summary("m")

    assert summary.queue_ms is not None
    assert summary.ttft_ms is not None
    assert summary.total_ms is not None
    assert (summary.queue_ms.max, summary.ttft_ms.max, summary.total_ms.max) == (
        100.0,
        110.0,
        150.0,
    )


async def test_total_latency_of_queued_simulator_requests_is_wait_plus_service() -> None:
    clock = SimClock()
    ids = ("a", "b", "c")
    # 10 tokens in, 3 out on EXACT: 5 ms of prefill and three 4 ms steps, 17 ms each.
    worker = BaselineWorker(EXACT, SizedBackend(dict.fromkeys(ids, (10, 3))), clock)
    store = MetricsStore(clock)

    results = await asyncio.gather(
        *(worker.submit(GenerateRequest(request_id=i, model="m", prompt="")) for i in ids)
    )
    for result in results:
        store.record("m", result)
    await worker.aclose()

    summary = store.summary("m")
    assert summary.queue_ms is not None
    assert summary.total_ms is not None
    assert [r.queue_wait_ms for r in results] == [0.0, 17.0, 34.0]
    assert [r.latency_ms for r in results] == [17.0, 34.0, 51.0]
    assert summary.total_ms.max == summary.queue_ms.max + 17.0
    assert summary.total_ms.p50 == summary.queue_ms.p50 + 17.0


def test_samples_leave_when_they_are_older_than_the_window() -> None:
    clock = ManualClock()
    store = MetricsStore(clock, window_ms=1000.0)
    store.record("m", _result(500.0, token_times_ms=(1.0, 2.0)))
    clock.t_ms = 600.0
    store.record("m", _result(20.0))

    clock.t_ms = 999.0
    both = store.summary("m")
    assert both.window.requests == 2
    assert both.window.span_ms == 999.0
    assert both.total_ms is not None
    assert both.total_ms.max == 500.0

    # Exactly one window after it was recorded, the first sample is out.
    clock.t_ms = 1000.0
    later = store.summary("m")
    assert later.window.requests == 1
    assert later.total_ms is not None
    assert later.total_ms.max == 20.0
    assert later.itl_ms is None
    assert later.window.truncated is False

    clock.t_ms = 5000.0
    idle = store.summary("m")
    # Lifetime counters stay; the window is empty again.
    assert (idle.requests, idle.tokens_in, idle.cost_usd) == (2, 20, 1.0)
    assert (idle.queue_ms, idle.ttft_ms, idle.total_ms, idle.itl_ms) == (None, None, None, None)
    assert (idle.window.requests, idle.window.span_ms, idle.window.cost_usd) == (0, 0.0, 0.0)
    assert store.retained_samples() == 0


def test_a_full_ring_keeps_the_newest_samples_and_says_it_truncated() -> None:
    clock = ManualClock()
    store = MetricsStore(clock, window_ms=1000.0, max_samples=3, max_itl_samples=2)
    for total in (1.0, 2.0, 3.0, 4.0, 5.0):
        clock.t_ms += 1.0
        store.record("m", _result(total, token_times_ms=(0.0, total)))

    summary = store.summary("m")

    assert summary.requests == 5
    assert summary.total_ms == Distribution(samples=3, mean=4.0, p50=4.0, p90=5.0, p99=5.0, max=5.0)
    assert summary.itl_ms is not None
    assert (summary.itl_ms.samples, summary.itl_ms.max) == (2, 5.0)
    assert (summary.window.truncated, summary.window.itl_truncated) == (True, True)

    # Once the dropped samples would have aged out anyway, nothing is missing any more.
    clock.t_ms += 998.0
    assert store.summary("m").window.truncated is False


def test_memory_stays_bounded_after_a_million_records() -> None:
    clock = ManualClock()
    store = MetricsStore(clock, window_ms=60_000.0, max_samples=1000, max_itl_samples=4000)
    result = _result(30.0, queue_ms=5.0, token_times_ms=(10.0, 20.0, 30.0), cost_usd=0.001)
    most_retained = 0

    for i in range(1_000_000):
        clock.t_ms += 1.0
        store.record("economy" if i % 2 else "premium", result)
        if i % 50_000 == 0:
            most_retained = max(most_retained, store.retained_samples())
    most_retained = max(most_retained, store.retained_samples())

    # Two models, each with both rings full: 1000 requests and 4000 gaps. The 60 s window
    # alone would hold 30,000 requests and 60,000 gaps per model.
    assert most_retained == 2 * (1000 + 4000)
    summary = store.summary("economy")
    assert summary.requests == 500_000
    assert summary.tokens_out == 1_000_000
    assert summary.cost_usd == pytest.approx(500.0)
    assert summary.window.requests == 1000
    assert summary.window.truncated is True
    assert summary.total_ms is not None
    assert summary.total_ms.samples == 1000


def test_the_time_window_bounds_memory_when_the_ring_does_not() -> None:
    clock = ManualClock()
    store = MetricsStore(clock, window_ms=100.0, max_samples=10_000)
    for _ in range(50_000):
        clock.t_ms += 1.0
        store.record("m", _result(1.0))

    assert store.retained_samples() == 100
    assert store.summary("m").window.truncated is False


async def test_concurrent_tasks_never_lose_or_tear_an_update() -> None:
    clock = SimClock()
    store = MetricsStore(clock)
    seen: list[ModelSummary] = []

    async def writer(n: int) -> None:
        for i in range(50):
            await clock.sleep_ms(1 + (n * 7 + i) % 5)
            store.record("m", _result(float(n), tokens_in=3, tokens_out=2, cost_usd=0.25))

    async def reader() -> None:
        for _ in range(100):
            await clock.sleep_ms(2.5)
            seen.append(store.summary("m"))

    await asyncio.gather(*(writer(n) for n in range(1, 21)), reader())

    final = store.summary("m")
    assert (final.requests, final.tokens_in, final.tokens_out, final.cost_usd) == (
        1000,
        3000,
        2000,
        250.0,
    )
    assert final.total_ms is not None
    assert final.total_ms.samples == 1000
    # Every snapshot a reader took was whole: its counters agree with each other.
    assert all(s.tokens_in == 3 * s.requests and s.cost_usd == 0.25 * s.requests for s in seen)
    assert [s.requests for s in seen] == sorted(s.requests for s in seen)


@pytest.mark.parametrize("kwargs", [{"window_ms": 0.0}, {"max_samples": 0}, {"max_itl_samples": 0}])
def test_store_rejects_limits_that_would_keep_nothing(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="must be"):
        MetricsStore(ManualClock(), **kwargs)


# --- the store behind the API --------------------------------------------------------------


async def test_metrics_detail_reports_the_window_and_summary_keeps_its_shape() -> None:
    clock = SimClock()
    registry = ModelRegistry()
    registry.register(
        "slow", BaselineWorker(EXACT, SimulatedBackend(seed=1, max_output_tokens=30), clock)
    )
    app = create_app(registry, clock, metrics=MetricsStore(clock, window_ms=1000.0))
    transport = httpx.ASGITransport(app=app)
    prompts = [f"{name} two three four five six seven eight nine ten" for name in "abc"]
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        await asyncio.gather(
            *(
                client.post("/v1/generate", json={"model": "slow", "prompt": p, "max_tokens": 3})
                for p in prompts
            )
        )
        detail = (await client.get("/metrics/detail")).json()
        summary = (await client.get("/metrics/summary")).json()["models"]["slow"]
        await clock.sleep_ms(1000.0)
        later = (await client.get("/metrics/summary")).json()["models"]["slow"]
        later_detail = (await client.get("/metrics/detail")).json()["models"]["slow"]
    await registry.aclose()

    cost = 3 * (10 * 0.5 + 3 * 1.0) / 1000

    def stats(samples: int, mean: float, p50: float, top: float) -> dict[str, float]:
        return {"samples": samples, "mean": mean, "p50": p50, "p90": top, "p99": top, "max": top}

    # Three requests arriving together finish at 17, 34 and 51 ms.
    assert detail == {
        "percentile_method": "nearest-rank",
        "models": {
            "slow": {
                "lifetime": {
                    "requests": 3,
                    "prompt_tokens": 30,
                    "cached_tokens": 0,
                    "completion_tokens": 9,
                    "cost_usd": cost,
                },
                "window": {
                    "window_ms": 1000.0,
                    "span_ms": 34.0,
                    "requests": 3,
                    "prompt_tokens": 30,
                    "cached_tokens": 0,
                    "completion_tokens": 9,
                    "cost_usd": cost,
                    "truncated": False,
                    "itl_truncated": False,
                },
                "queue_ms": stats(3, 17.0, 17.0, 34.0),
                "ttft_ms": stats(3, 26.0, 26.0, 43.0),
                "total_ms": stats(3, 34.0, 34.0, 51.0),
                "itl_ms": stats(6, 4.0, 4.0, 4.0),
            }
        },
    }
    assert summary == {
        "requests": 3,
        "queue_ms": {"p50": 17.0, "p99": 34.0},
        "ttft_ms": {"p50": 26.0, "p99": 43.0},
        "total_ms": {"p50": 34.0, "p99": 51.0},
        "itl_ms": {"p50": 4.0, "p99": 4.0},
        "prompt_tokens": 30,
        "cached_tokens": 0,
        "completion_tokens": 9,
        "cost_usd": cost,
    }
    # A window later the totals are still there and the percentiles are null again.
    assert later == {**summary, "queue_ms": None, "ttft_ms": None, "total_ms": None, "itl_ms": None}
    assert later_detail["lifetime"]["requests"] == 3
    assert later_detail["window"]["requests"] == 0
    assert later_detail["total_ms"] is None
