"""Overload protection below the HTTP layer: SLO monitor, timeouts, queue limits."""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from gateway.clock import SimClock
from gateway.continuous import ContinuousBatchingWorker, StepRecord
from gateway.overload import (
    OverloadPolicy,
    QueueFullError,
    RequestTimeoutError,
    SloMonitor,
    SloPolicy,
    Transition,
    pick_downgrade,
    run_with_timeout,
)
from gateway.schema import GenerateRequest
from helpers import EXACT, SizedBackend

# SLO 100 ms, so "recovered" means a p99 under 50 ms.
POLICY = SloPolicy(
    p99_ms={"m": 100.0}, window_ms=1000.0, min_samples=3, recover_ratio=0.5, min_hold_ms=200.0
)


def _request(request_id: str) -> GenerateRequest:
    return GenerateRequest(request_id, "standard", "")


def _record(monitor: SloMonitor, latency_ms: float, times: int = 3) -> None:
    for _ in range(times):
        monitor.record("m", latency_ms)


# --- SLO monitor ---------------------------------------------------------------------------


async def test_model_starts_healthy_and_a_model_without_an_slo_never_degrades() -> None:
    monitor = SloMonitor(POLICY, SimClock())
    assert not monitor.is_degraded("m")
    for _ in range(50):
        monitor.record("unlisted", 1_000_000.0)
    assert not monitor.is_degraded("unlisted")
    assert monitor.transitions == ()


async def test_model_degrades_when_recent_p99_exceeds_the_slo() -> None:
    clock = SimClock(start_ms=10.0)
    monitor = SloMonitor(POLICY, clock)

    _record(monitor, 500.0, times=2)
    assert not monitor.is_degraded("m")  # two samples are not enough to judge
    monitor.record("m", 500.0)

    assert monitor.is_degraded("m")
    assert monitor.transitions == (Transition(10.0, "m", True, 500.0),)
    health = monitor.health("m")
    assert (health.degraded, health.slo_p99_ms, health.recent_p99_ms, health.samples) == (
        True,
        100.0,
        500.0,
        3,
    )


async def test_p99_equal_to_the_slo_is_not_a_breach() -> None:
    monitor = SloMonitor(POLICY, SimClock())
    _record(monitor, 100.0, times=10)
    assert not monitor.is_degraded("m")


async def test_one_slow_request_among_many_fast_ones_moves_the_p99() -> None:
    monitor = SloMonitor(POLICY, SimClock())
    _record(monitor, 10.0, times=99)
    assert not monitor.is_degraded("m")
    monitor.record("m", 500.0)
    monitor.record("m", 500.0)  # 2 of 101: the nearest-rank p99 is now a slow one
    assert monitor.is_degraded("m")


async def test_hysteresis_keeps_the_state_between_the_two_thresholds() -> None:
    clock = SimClock()
    monitor = SloMonitor(POLICY, clock)

    # Healthy, with a p99 in the band (50..100): stays healthy.
    _record(monitor, 80.0)
    assert not monitor.is_degraded("m")

    # Breach, then let the slow samples age out of the window.
    _record(monitor, 500.0)
    assert monitor.is_degraded("m")
    await clock.sleep_ms(1000.0)

    # Degraded, with a p99 back in the band: under the SLO, but not recovered.
    _record(monitor, 80.0)
    assert monitor.health("m").recent_p99_ms == 80.0
    assert monitor.is_degraded("m")

    # Only a p99 below recover_ratio * SLO ends it.
    await clock.sleep_ms(1000.0)
    _record(monitor, 40.0)
    assert not monitor.is_degraded("m")
    assert [(t.degraded, t.p99_ms) for t in monitor.transitions] == [(True, 500.0), (False, 40.0)]


async def test_no_state_change_within_the_hold_time_of_the_previous_one() -> None:
    clock = SimClock()
    policy = dataclasses.replace(POLICY, window_ms=100.0)
    monitor = SloMonitor(policy, clock)
    _record(monitor, 500.0)  # degraded at t=0

    # Clear evidence of recovery at t=50, but the hold lasts until t=200.
    await clock.sleep_ms(50.0)
    _record(monitor, 1.0, times=300)
    assert monitor.health("m").recent_p99_ms == 1.0
    assert monitor.is_degraded("m")

    await clock.sleep_ms(150.0)  # t=200: the hold is over and the window is empty
    assert not monitor.is_degraded("m")

    # And the same in the other direction: a breach at t=210 has to wait until t=400.
    await clock.sleep_ms(10.0)
    _record(monitor, 500.0)
    assert not monitor.is_degraded("m")
    await clock.sleep_ms(190.0)  # t=400
    _record(monitor, 500.0)
    assert monitor.is_degraded("m")
    assert [(t.at_ms, t.degraded) for t in monitor.transitions] == [
        (0.0, True),
        (200.0, False),
        (400.0, True),
    ]


async def test_degraded_model_recovers_when_its_window_is_empty() -> None:
    clock = SimClock()
    monitor = SloMonitor(POLICY, clock)
    _record(monitor, 500.0)

    await clock.sleep_ms(999.0)
    assert monitor.is_degraded("m")  # the samples are still inside the window
    await clock.sleep_ms(1.0)

    assert not monitor.is_degraded("m")
    assert monitor.transitions[-1] == Transition(1000.0, "m", False, None)
    assert monitor.health("m").samples == 0


async def test_too_few_samples_do_not_end_a_degradation() -> None:
    clock = SimClock()
    monitor = SloMonitor(POLICY, clock)
    _record(monitor, 500.0)
    await clock.sleep_ms(1000.0)

    _record(monitor, 1.0, times=2)  # fast, but only two of them

    assert monitor.is_degraded("m")
    monitor.record("m", 1.0)
    assert not monitor.is_degraded("m")


async def test_models_are_judged_independently() -> None:
    policy = dataclasses.replace(POLICY, p99_ms={"a": 100.0, "b": 100.0})
    monitor = SloMonitor(policy, SimClock())
    for _ in range(3):
        monitor.record("a", 500.0)
        monitor.record("b", 10.0)
    assert monitor.is_degraded("a")
    assert not monitor.is_degraded("b")


@pytest.mark.parametrize(
    "overrides",
    [
        {"p99_ms": {"m": 0.0}},
        {"window_ms": 0.0},
        {"min_samples": 0},
        {"recover_ratio": 0.0},
        {"recover_ratio": 1.1},
        {"min_hold_ms": -1.0},
    ],
    ids=lambda o: next(iter(o)),
)
def test_invalid_slo_policy_is_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"must|positive"):
        dataclasses.replace(POLICY, **overrides)  # type: ignore[arg-type]


def test_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError, match="request_timeout_ms"):
        OverloadPolicy(request_timeout_ms=0)


# --- choosing the downgrade ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("chosen", "degraded", "expected"),
    [
        ("premium", set(), "premium"),
        ("premium", {"premium"}, "standard"),
        ("premium", {"premium", "standard"}, "economy"),
        ("premium", {"premium", "standard", "economy"}, "economy"),
        ("premium", {"standard"}, "premium"),  # only the chosen model's health matters
        ("standard", {"standard"}, "economy"),
        ("standard", {"premium"}, "standard"),
        ("economy", {"economy"}, "economy"),  # nothing cheaper to go to
    ],
)
def test_pick_downgrade(chosen: str, degraded: set[str], expected: str) -> None:
    models = ["economy", "standard", "premium"]
    assert pick_downgrade(models, chosen, degraded.__contains__) == expected


def test_pick_downgrade_stays_within_the_given_models() -> None:
    assert pick_downgrade(["standard", "premium"], "premium", lambda _: True) == "standard"
    assert pick_downgrade(["premium"], "premium", lambda _: True) == "premium"


# --- timeouts ------------------------------------------------------------------------------


async def test_work_that_finishes_in_time_returns_its_result() -> None:
    clock = SimClock()

    async def work() -> str:
        await clock.sleep_ms(30.0)
        return "done"

    assert await run_with_timeout(clock, work(), 100.0) == "done"
    assert clock.now_ms() == 30.0
    # The unused timer does not move the clock later on.
    await clock.sleep_ms(1.0)
    assert clock.now_ms() == 31.0


async def test_work_that_overruns_is_cancelled_at_the_timeout() -> None:
    clock = SimClock()
    events: list[str] = []

    async def work() -> str:
        try:
            await clock.sleep_ms(500.0)
        except asyncio.CancelledError:
            events.append(f"cancelled at {clock.now_ms()}")
            raise
        return "done"

    with pytest.raises(RequestTimeoutError) as excinfo:
        await run_with_timeout(clock, work(), 100.0)

    assert excinfo.value.timeout_ms == 100.0
    assert events == ["cancelled at 100.0"]  # and it had unwound before the error was raised
    assert clock.now_ms() == 100.0


async def test_work_finishing_exactly_at_the_timeout_wins() -> None:
    clock = SimClock()

    async def work() -> str:
        await clock.sleep_ms(100.0)
        return "done"

    assert await run_with_timeout(clock, work(), 100.0) == "done"


async def test_cancelling_the_caller_cancels_the_work() -> None:
    clock = SimClock()
    cancelled: list[bool] = []

    async def work() -> None:
        try:
            await clock.sleep_ms(500.0)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    caller = asyncio.create_task(run_with_timeout(clock, work(), 1000.0))
    await clock.sleep_ms(10.0)
    caller.cancel()
    await asyncio.gather(caller, return_exceptions=True)
    await clock.sleep_ms(0.0)

    assert caller.cancelled()
    assert cancelled == [True]


# --- cancelled requests free capacity ------------------------------------------------------


async def test_timed_out_request_leaves_the_batch_and_frees_its_kv_tokens() -> None:
    # KV capacity 40. "long" holds 30 tokens, so "next" (16 tokens) cannot be admitted
    # beside it and would have to wait until long finishes at t=85.
    profile = dataclasses.replace(EXACT, kv_capacity_tokens=40)
    steps: list[StepRecord] = []
    clock = SimClock()
    worker = ContinuousBatchingWorker(
        profile, SizedBackend({"long": (10, 20), "next": (10, 6)}), clock, on_step=steps.append
    )

    async def timed_out() -> None:
        with pytest.raises(RequestTimeoutError):
            await run_with_timeout(clock, worker.submit(_request("long")), 10.0)

    long = asyncio.create_task(timed_out())
    await clock.sleep_ms(1.0)  # long is in the batch before next arrives
    following = await worker.submit(_request("next"))
    await long

    # long prefills 0..5 and decodes steps ending at 9 and 13. It times out at 10, is
    # dropped at the next iteration boundary (13), and next is admitted there and then.
    assert following.enqueue_ms == 1.0
    assert following.start_ms == 13.0
    assert following.finish_ms == 13.0 + 5.0 + 6 * 4.0
    assert [(s.start_ms, s.batch_size, s.kv_tokens) for s in steps[:3]] == [
        (5.0, 1, 30),
        (9.0, 1, 30),
        (18.0, 1, 16),
    ]
    assert worker.waiting == 0


async def test_request_cancelled_during_another_requests_prefill_gets_no_further_step() -> None:
    steps: list[StepRecord] = []
    clock = SimClock()
    worker = ContinuousBatchingWorker(
        EXACT, SizedBackend({"first": (0, 5), "heavy": (40, 2)}), clock, on_step=steps.append
    )
    first = asyncio.create_task(worker.submit(_request("first")))
    await clock.sleep_ms(1.0)
    heavy = asyncio.create_task(worker.submit(_request("heavy")))

    await clock.sleep_ms(9.0)  # t=10: heavy is being prefilled (4..24), first is waiting on it
    first.cancel()
    result = await heavy

    assert first.cancelled()
    # The step after heavy's prefill carries heavy alone: first has already left.
    assert [(s.start_ms, s.batch_size, s.kv_tokens) for s in steps] == [
        (0.0, 1, 5),
        (24.0, 1, 42),
        (28.0, 1, 42),
    ]
    assert result.finish_ms == 32.0


# --- queue limit ---------------------------------------------------------------------------


def _limited_worker(
    clock: SimClock, *, max_batch_size: int, limit: int
) -> ContinuousBatchingWorker:
    return ContinuousBatchingWorker(
        dataclasses.replace(EXACT, max_batch_size=max_batch_size),
        SizedBackend(dict.fromkeys("abcdefgh", (0, 10))),
        clock,
        max_queue_len=limit,
    )


async def test_requests_beyond_the_queue_limit_are_rejected() -> None:
    clock = SimClock()
    worker = _limited_worker(clock, max_batch_size=1, limit=2)
    accepted = [asyncio.create_task(worker.submit(_request(name))) for name in "abc"]
    await clock.sleep_ms(1.0)  # a is running; b and c wait
    assert worker.waiting == 2

    with pytest.raises(QueueFullError) as excinfo:
        await worker.submit(_request("d"))

    error = excinfo.value
    assert (error.model, error.waiting, error.limit, error.retry_after_ms) == (
        "exact-test-model",
        2,
        2,
        1000.0,
    )
    assert "queue for model 'exact-test-model' is full (2 waiting, limit 2)" in str(error)
    assert clock.now_ms() == 1.0  # rejected at once

    # Once a finishes, b takes the slot and there is room for one more.
    await accepted[0]
    assert worker.waiting == 1
    late = asyncio.create_task(worker.submit(_request("d")))
    results = await asyncio.gather(*accepted[1:], late)
    assert [r.request_id for r in results] == ["b", "c", "d"]


async def test_a_burst_fills_free_batch_slots_before_it_counts_as_waiting() -> None:
    clock = SimClock()
    worker = _limited_worker(clock, max_batch_size=4, limit=1)

    results = await asyncio.gather(
        *(worker.submit(_request(name)) for name in "abcdefg"), return_exceptions=True
    )

    # Four seats plus one place in the queue; the sixth and seventh are turned away.
    assert [type(r).__name__ for r in results] == ["RequestResult"] * 5 + ["QueueFullError"] * 2


async def test_limit_of_zero_allows_only_what_can_be_seated_at_once() -> None:
    clock = SimClock()
    worker = _limited_worker(clock, max_batch_size=2, limit=0)
    results = await asyncio.gather(
        *(worker.submit(_request(name)) for name in "abc"), return_exceptions=True
    )
    assert [type(r).__name__ for r in results] == [
        "RequestResult",
        "RequestResult",
        "QueueFullError",
    ]


async def test_cancelled_queued_requests_do_not_count_against_the_limit() -> None:
    clock = SimClock()
    worker = _limited_worker(clock, max_batch_size=1, limit=2)
    a, b, c = (asyncio.create_task(worker.submit(_request(name))) for name in "abc")
    await clock.sleep_ms(1.0)

    b.cancel()
    await asyncio.gather(b, return_exceptions=True)
    assert worker.waiting == 1
    d = asyncio.create_task(worker.submit(_request("d")))  # takes the place b gave up

    results = await asyncio.gather(a, c, d)
    assert [r.request_id for r in results] == ["a", "c", "d"]


async def test_no_limit_by_default() -> None:
    clock = SimClock()
    worker = ContinuousBatchingWorker(
        dataclasses.replace(EXACT, max_batch_size=1),
        SizedBackend({f"r{i}": (0, 1) for i in range(200)}),
        clock,
    )
    results = await asyncio.gather(*(worker.submit(_request(f"r{i}")) for i in range(200)))
    assert len(results) == 200


def test_queue_limit_must_not_be_negative() -> None:
    with pytest.raises(ValueError, match="max_queue_len"):
        _limited_worker(SimClock(), max_batch_size=1, limit=-1)
