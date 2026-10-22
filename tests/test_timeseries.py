"""The bounded per-step time-series store, on a clock the test moves by hand."""

from __future__ import annotations

from typing import Any

import pytest

from gateway.prefix_cache import CacheStats
from gateway.schema import RequestResult
from gateway.timeseries import (
    ApplicationPoint,
    ApplicationTotals,
    GaugeSample,
    ModelTotals,
    TimeSeriesStore,
)


class ManualClock:
    def __init__(self) -> None:
        self.t_ms = 0.0

    def now_ms(self) -> float:
        return self.t_ms

    async def sleep_ms(self, duration_ms: float) -> None:
        self.t_ms += duration_ms


def _result(
    total_ms: float,
    *,
    ttft_ms: float | None = None,
    token_times_ms: tuple[float, ...] = (),
    tokens_out: int = 2,
) -> RequestResult:
    """A result that was enqueued at 0 and finished ``total_ms`` later."""
    return RequestResult(
        request_id="r",
        model="m",
        enqueue_ms=0.0,
        start_ms=0.0,
        first_token_ms=total_ms if ttft_ms is None else ttft_ms,
        finish_ms=total_ms,
        tokens_in=10,
        tokens_out=tokens_out,
        cost_usd=0.5,
        token_times_ms=token_times_ms,
    )


# --- counts and rates ----------------------------------------------------------------------


def test_a_step_counts_what_happened_in_it_and_turns_counts_into_rates() -> None:
    clock = ManualClock()
    store = TimeSeriesStore(clock, resolution_ms=500.0, window_ms=5000.0)
    clock.t_ms = 1200.0  # inside the step that starts at 1000

    for _ in range(3):
        store.request_started("m")
    store.tokens("m")
    store.tokens("m", 6)
    store.request_completed("m", _result(40.0))
    store.request_ended("m")
    store.queue_full("m")
    store.request_ended("m")
    store.timed_out("m")
    store.downgraded("m")

    (point,) = store.model_series("m")
    assert (point.t_ms, point.final) == (1000.0, False)
    assert (point.arrived, point.completed, point.output_tokens) == (3, 1, 7)
    # Three arrivals and seven tokens in a half-second step.
    assert (point.req_per_s, point.tokens_per_s) == (6.0, 14.0)
    assert (point.queue_full, point.timeouts, point.downgrades) == (1, 1, 1)
    # Three started, two ended: one is still with the worker; three were at the peak.
    assert (point.in_flight, point.in_flight_max) == (1, 3)
    assert store.model_totals("m") == ModelTotals(
        in_flight=1, arrived=3, completed=1, output_tokens=7, queue_full=1, timeouts=1, downgrades=1
    )


def test_a_model_never_recorded_has_no_points_and_zero_totals() -> None:
    store = TimeSeriesStore(ManualClock())

    assert store.model_series("never", 60_000.0) == ()
    assert store.model_totals("never") == ModelTotals(0, 0, 0, 0, 0, 0, 0)
    assert store.application_series("nobody", 60_000.0) == ()
    assert store.application_totals("nobody") == ApplicationTotals(
        0, 0, 0.0, {"budget_exhausted": 0, "queue_full": 0, "rate_limited": 0}, 0, 0
    )
    assert store.observed_ms("never") is None
    assert store.retained_buckets() == 0


def test_requests_in_flight_carry_over_into_later_steps() -> None:
    clock = ManualClock()
    store = TimeSeriesStore(clock)
    store.request_started("m")
    store.request_started("m")
    clock.t_ms = 3500.0
    store.request_ended("m")

    first, later = store.model_series("m", 10_000.0)

    assert (first.t_ms, first.in_flight, first.in_flight_max) == (0.0, 2, 2)
    # The later step opened with two in flight and one of them ended in it.
    assert (later.t_ms, later.in_flight, later.in_flight_max) == (3000.0, 1, 2)
    assert (later.arrived, later.req_per_s) == (0, 0.0)


# --- gauges --------------------------------------------------------------------------------


def test_a_gauge_is_its_last_sample_in_the_step_with_the_largest_beside_it() -> None:
    clock = ManualClock()
    store = TimeSeriesStore(clock)
    store.observe("m", GaugeSample(queue_len=2, batch_size=4, kv_used_tokens=100))
    clock.t_ms = 400.0
    store.observe("m", GaugeSample(queue_len=7, batch_size=3, kv_used_tokens=300))
    clock.t_ms = 900.0
    store.observe("m", GaugeSample(queue_len=1, batch_size=4, kv_used_tokens=200))

    (point,) = store.model_series("m")

    assert (point.queue_len, point.queue_len_max) == (1, 7)
    assert (point.batch_size, point.batch_size_max) == (4, 4)
    assert (point.kv_used_tokens, point.kv_used_tokens_max) == (200, 300)
    assert store.observed_ms("m") == 900.0


def test_a_gauge_nobody_supplied_is_none_not_zero() -> None:
    store = TimeSeriesStore(ManualClock())
    store.request_started("m")
    store.observe("m", GaugeSample())  # a worker with nothing to show

    (point,) = store.model_series("m")

    assert (point.queue_len, point.queue_len_max) == (None, None)
    assert (point.batch_size, point.batch_size_max) == (None, None)
    assert (point.kv_used_tokens, point.kv_used_tokens_max) == (None, None)
    assert (point.cache_lookups, point.cache_hit_rate, point.cache_token_hit_rate) == (
        None,
        None,
        None,
    )
    assert point.degraded is None
    # What the store counts itself is still there.
    assert (point.arrived, point.in_flight) == (1, 1)


def test_gauges_are_not_carried_into_a_step_that_did_not_sample_them() -> None:
    clock = ManualClock()
    store = TimeSeriesStore(clock)
    store.observe("m", GaugeSample(queue_len=5, batch_size=4, kv_used_tokens=9))
    clock.t_ms = 1000.0
    store.tokens("m")

    _, later = store.model_series("m", 5000.0)

    assert (later.queue_len, later.batch_size, later.kv_used_tokens) == (None, None, None)


def test_cache_counters_become_per_step_changes_and_rates() -> None:
    clock = ManualClock()
    store = TimeSeriesStore(clock)
    # The cache had already been used when it was first looked at: that is the baseline.
    store.observe(
        "m", GaugeSample(cache=CacheStats(lookups=10, hits=5, prompt_tokens=100, cached_tokens=50))
    )
    store.observe(
        "m", GaugeSample(cache=CacheStats(lookups=12, hits=6, prompt_tokens=140, cached_tokens=60))
    )
    clock.t_ms = 1000.0
    store.observe(
        "m", GaugeSample(cache=CacheStats(lookups=16, hits=9, prompt_tokens=220, cached_tokens=80))
    )
    clock.t_ms = 2000.0
    store.observe(
        "m", GaugeSample(cache=CacheStats(lookups=16, hits=9, prompt_tokens=220, cached_tokens=80))
    )

    first, second, idle = store.model_series("m", 5000.0)

    # 2 lookups, 1 hit; 40 prompt tokens, 10 of them cached.
    assert (first.cache_lookups, first.cache_hit_rate, first.cache_token_hit_rate) == (2, 0.5, 0.25)
    # 4 lookups, 3 hits; 80 prompt tokens, 20 cached.
    assert (second.cache_lookups, second.cache_hit_rate, second.cache_token_hit_rate) == (
        4,
        0.75,
        0.25,
    )
    # Looked at, but nothing was looked up: zero lookups, and no rate to speak of.
    assert (idle.cache_lookups, idle.cache_hit_rate, idle.cache_token_hit_rate) == (0, None, None)


def test_the_degraded_flag_carries_forward_until_it_changes() -> None:
    clock = ManualClock()
    store = TimeSeriesStore(clock)
    store.set_degraded("m", False)
    clock.t_ms = 1500.0
    store.set_degraded("m", True)
    clock.t_ms = 2500.0
    store.tokens("m")

    assert [p.degraded for p in store.model_series("m", 5000.0)] == [False, True, True]


# --- latency -------------------------------------------------------------------------------


def test_percentiles_cover_the_requests_that_finished_in_the_step() -> None:
    clock = ManualClock()
    store = TimeSeriesStore(clock)
    clock.t_ms = 100.0
    store.request_completed("m", _result(40.0, ttft_ms=10.0, token_times_ms=(10.0, 20.0, 40.0)))
    store.request_completed("m", _result(80.0, ttft_ms=30.0, token_times_ms=(30.0, 80.0)))
    store.request_completed("m", _result(60.0, ttft_ms=20.0, token_times_ms=(60.0,)))
    clock.t_ms = 1100.0
    store.request_completed("m", _result(7.0))

    (open_step,) = store.model_series("m")
    first, second = store.model_series("m", 5000.0)

    # Nearest-rank over three requests: p50 is the second, p99 the largest.
    assert (first.ttft_p50_ms, first.ttft_p99_ms) == (20.0, 30.0)
    assert (first.total_p50_ms, first.total_p99_ms) == (60.0, 80.0)
    # Gaps of 10, 20 and 50 ms, pooled over the requests.
    assert (first.itl_p50_ms, first.itl_p99_ms) == (20.0, 50.0)
    assert (first.final, first.latency_truncated) == (True, False)
    # The next step has only its own request, which had a single token.
    assert second == open_step
    assert (second.ttft_p50_ms, second.total_p99_ms) == (7.0, 7.0)
    assert (second.itl_p50_ms, second.itl_p99_ms) == (None, None)
    assert second.final is False


def test_a_step_with_no_finished_request_has_no_percentiles() -> None:
    store = TimeSeriesStore(ManualClock())
    store.request_started("m")

    (point,) = store.model_series("m")

    assert (point.ttft_p50_ms, point.ttft_p99_ms) == (None, None)
    assert (point.itl_p50_ms, point.itl_p99_ms) == (None, None)
    assert (point.total_p50_ms, point.total_p99_ms) == (None, None)


def test_raw_samples_are_dropped_when_the_step_ends_and_the_percentiles_stay() -> None:
    clock = ManualClock()
    store = TimeSeriesStore(clock)
    for total in (30.0, 10.0, 20.0):
        store.request_completed("m", _result(total, token_times_ms=(1.0, 2.0)))
    assert store.retained_samples() == 9  # three TTFTs, three totals, three gaps
    (while_open,) = store.model_series("m")

    clock.t_ms = 1000.0
    (after_end,) = store.model_series("m", 1000.0)

    assert store.retained_samples() == 0
    assert (after_end.total_p50_ms, after_end.total_p99_ms) == (20.0, 30.0)
    # Ending the step changed nothing but the flag.
    assert (while_open.final, after_end.final) == (False, True)
    assert [getattr(after_end, f) for f in after_end.__slots__ if f != "final"] == [
        getattr(while_open, f) for f in while_open.__slots__ if f != "final"
    ]


def test_a_step_over_its_sample_caps_keeps_the_first_samples_and_says_so() -> None:
    store = TimeSeriesStore(ManualClock(), max_bucket_samples=3, max_bucket_itl_samples=4)
    for total in (1.0, 2.0, 3.0, 400.0, 500.0):
        store.request_completed("m", _result(total, token_times_ms=(0.0, total, 2 * total)))

    (point,) = store.model_series("m")

    assert point.completed == 5
    assert point.latency_truncated is True
    # Only the first three requests and the first four gaps (1, 1, 2, 2) were kept.
    assert (point.total_p50_ms, point.total_p99_ms) == (2.0, 3.0)
    assert (point.itl_p50_ms, point.itl_p99_ms) == (1.0, 2.0)
    assert store.retained_samples() == 3 + 3 + 4


# --- the window ----------------------------------------------------------------------------


def test_history_selects_whole_steps_and_idle_steps_are_gaps() -> None:
    clock = ManualClock()
    store = TimeSeriesStore(clock)
    for t_ms in (100.0, 1900.0, 5000.0, 5999.0, 7300.0):
        clock.t_ms = t_ms
        store.request_started("m")
    clock.t_ms = 7900.0

    def starts(history_ms: float) -> list[float]:
        return [p.t_ms for p in store.model_series("m", history_ms)]

    # Nothing happened in the steps starting at 2000, 3000, 4000 and 6000.
    assert starts(60_000.0) == [0.0, 1000.0, 5000.0, 7000.0]
    assert starts(0.0) == [7000.0]
    # 900 ms back from 7900 is still in the current step; 1900 ms back reaches 6000.
    assert starts(900.0) == [7000.0]
    assert starts(1900.0) == [7000.0]
    assert starts(2900.0) == [5000.0, 7000.0]
    assert starts(-5.0) == [7000.0]
    assert [p.final for p in store.model_series("m", 60_000.0)] == [True, True, True, False]
    assert [p.arrived for p in store.model_series("m", 60_000.0)] == [1, 1, 2, 1]


def test_steps_older_than_the_window_are_dropped() -> None:
    clock = ManualClock()
    store = TimeSeriesStore(clock, window_ms=3000.0)
    assert store.capacity == 3
    for second in range(5):
        clock.t_ms = second * 1000.0
        store.request_started("m")

    assert [p.t_ms for p in store.model_series("m", 60_000.0)] == [2000.0, 3000.0, 4000.0]
    assert store.retained_buckets() == 3

    # Reading is enough to drop them: nothing is written here.
    clock.t_ms = 6999.0
    assert [p.t_ms for p in store.model_series("m", 60_000.0)] == [4000.0]
    clock.t_ms = 7000.0
    assert store.model_series("m", 60_000.0) == ()
    assert store.retained_buckets() == 0
    # The totals are not a window.
    assert store.model_totals("m").arrived == 5


def test_a_window_that_is_not_a_whole_number_of_steps_rounds_up() -> None:
    assert TimeSeriesStore(ManualClock(), window_ms=2500.0, resolution_ms=1000.0).capacity == 3
    assert TimeSeriesStore(ManualClock()).capacity == 900  # 15 minutes at 1 s


# --- applications -------------------------------------------------------------------------------


def test_application_series_and_totals() -> None:
    clock = ManualClock()
    store = TimeSeriesStore(clock)
    store.application_completed("acme", billed_tokens=13, spend_usd=0.25)
    store.application_completed("acme", billed_tokens=7, spend_usd=0.5)
    store.application_rejected("acme", "queue_full")
    clock.t_ms = 2000.0
    store.application_rejected("acme", "budget_exhausted")
    store.application_rejected("acme", "budget_exhausted")
    store.application_timed_out("acme")
    store.application_downgraded("acme")
    store.application_completed("globex", billed_tokens=1, spend_usd=2.0)

    assert store.application_series("acme", 5000.0) == (
        ApplicationPoint(
            t_ms=0.0,
            final=True,
            requests=2,
            billed_tokens=20,
            spend_usd=0.75,
            rejections=1,
            timeouts=0,
            downgrades=0,
        ),
        ApplicationPoint(
            t_ms=2000.0,
            final=False,
            requests=0,
            billed_tokens=0,
            spend_usd=0.0,
            rejections=2,
            timeouts=1,
            downgrades=1,
        ),
    )
    assert store.application_totals("acme") == ApplicationTotals(
        requests=2,
        billed_tokens=20,
        spend_usd=0.75,
        rejections={"budget_exhausted": 2, "queue_full": 1, "rate_limited": 0},
        timeouts=1,
        downgrades=1,
    )
    assert [p.requests for p in store.application_series("globex")] == [1]
    with pytest.raises(ValueError, match="unknown rejection reason"):
        store.application_rejected("acme", "bad_mood")


# --- bounded memory ------------------------------------------------------------------------


def test_memory_stays_bounded_over_many_windows_of_traffic() -> None:
    clock = ManualClock()
    store = TimeSeriesStore(
        clock, window_ms=60_000.0, max_bucket_samples=100, max_bucket_itl_samples=150
    )
    result = _result(30.0, token_times_ms=(10.0, 20.0, 30.0))
    most_buckets = 0
    most_samples = 0

    # One request every millisecond for 300 simulated seconds: five windows.
    for i in range(300_000):
        clock.t_ms += 1.0
        model = "economy" if i % 2 else "premium"
        application = "acme" if i % 3 else "globex"
        store.request_started(model)
        store.tokens(model, 3)
        store.observe(model, GaugeSample(queue_len=i % 7, batch_size=4, kv_used_tokens=i % 900))
        store.request_completed(model, result)
        store.request_ended(model)
        store.application_completed(application, billed_tokens=13, spend_usd=0.001)
        if i % 997 == 0:
            most_buckets = max(most_buckets, store.retained_buckets())
            most_samples = max(most_samples, store.retained_samples())

    # Two models and two applications, 60 one-second buckets each, however long it runs.
    assert most_buckets == 4 * 60
    assert store.retained_buckets() == 4 * 60
    # Raw samples exist only in each model's open bucket: at most 100 TTFTs, 100 totals
    # and 150 gaps. Uncapped, a bucket would see 500 requests and 1000 gaps.
    assert 0 < most_samples <= 2 * (100 + 100 + 150)
    assert len(store.model_series("economy", 10**9)) == 60
    assert store.model_totals("economy").completed == 150_000
    assert store.model_totals("economy").output_tokens == 450_000
    assert store.application_totals("acme").requests == 200_000
    full = store.model_series("economy", 10**9)[-2]
    assert (full.final, full.arrived, full.completed, full.req_per_s) == (True, 500, 500, 500.0)
    assert (full.latency_truncated, full.total_p50_ms, full.itl_p99_ms) == (True, 30.0, 10.0)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"resolution_ms": 0.0},
        {"window_ms": 500.0},
        {"max_bucket_samples": 0},
        {"max_bucket_itl_samples": 0},
    ],
)
def test_store_rejects_limits_that_would_keep_nothing(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="must be"):
        TimeSeriesStore(ManualClock(), **kwargs)
