from __future__ import annotations

import asyncio
import dataclasses
import itertools
from collections.abc import Callable

import pytest

from gateway.backends import Backend, SimulatedBackend
from gateway.baseline import BaselineWorker, build_baseline_registry
from gateway.clock import SimClock
from gateway.continuous import (
    ContinuousBatchingWorker,
    RequestTooLargeError,
    StepRecord,
    build_continuous_registry,
)
from gateway.cost_model import decode_step_ms
from gateway.overload import QueueFullError
from gateway.profiles import ECONOMY, PREMIUM, PROFILES, STANDARD, ModelProfile, Tier
from gateway.registry import UnknownModelError, Worker
from gateway.schema import GenerateRequest, RequestResult
from helpers import EXACT, SizedBackend

WorkerFactory = Callable[[ModelProfile, Backend, SimClock], Worker]
WORKER_KINDS: tuple[WorkerFactory, ...] = (BaselineWorker, ContinuousBatchingWorker)


def _request(request_id: str, prompt: str = "", model: str = "standard") -> GenerateRequest:
    return GenerateRequest(request_id=request_id, model=model, prompt=prompt)


async def _submit_at(
    clock: SimClock, worker: Worker, arrival_ms: float, request_id: str
) -> RequestResult:
    await clock.sleep_ms(arrival_ms)
    return await worker.submit(_request(request_id))


async def _burst(worker: Worker, request_ids: list[str]) -> list[RequestResult]:
    return list(await asyncio.gather(*(worker.submit(_request(i)) for i in request_ids)))


def _mixed_sizes(count: int) -> dict[str, tuple[int, int]]:
    """A fixed spread of prompt and output lengths, short and long interleaved."""
    return {f"r{i}": (4 + 6 * (i % 5), 1 + (i * 7) % 23) for i in range(count)}


# --- the headline behaviour ----------------------------------------------------------------


async def test_late_short_request_finishes_before_an_earlier_long_one() -> None:
    sizes = {"long": (10, 50), "short": (4, 2)}

    async def run(make_worker: WorkerFactory) -> tuple[RequestResult, RequestResult]:
        clock = SimClock()
        worker = make_worker(EXACT, SizedBackend(sizes), clock)
        long, short = await asyncio.gather(
            _submit_at(clock, worker, 0, "long"),
            _submit_at(clock, worker, 10, "short"),
        )
        return long, short

    long, short = await run(ContinuousBatchingWorker)
    # long: prefill 0..5, alone for two steps (9, 13). short arrived at 10, is admitted at
    # the next iteration boundary (13), prefills 13..15 while long waits, then both decode
    # together at 4.25 ms per step: 19.25, 23.5. long then finishes its other 46 tokens alone.
    assert (long.start_ms, long.first_token_ms, long.finish_ms) == (0.0, 9.0, 207.5)
    assert (short.enqueue_ms, short.start_ms) == (10.0, 13.0)
    assert (short.first_token_ms, short.finish_ms) == (19.25, 23.5)
    assert short.finish_ms < long.finish_ms

    baseline_long, baseline_short = await run(BaselineWorker)
    # The baseline cannot do this: short waits for all of long.
    assert baseline_short.start_ms == baseline_long.finish_ms == 205.0
    assert baseline_short.finish_ms == 215.0


# --- timing --------------------------------------------------------------------------------


async def test_step_time_follows_the_batch_size_and_requests_leave_when_done() -> None:
    clock = SimClock()
    worker = ContinuousBatchingWorker(
        EXACT, SizedBackend({"a": (10, 3), "b": (4, 2), "c": (20, 1)}), clock
    )

    a, b, c = await _burst(worker, ["a", "b", "c"])

    # Prefills run one after another at admission: a 0..5, b 5..7, c 7..17.
    assert (a.start_ms, b.start_ms, c.start_ms) == (0.0, 5.0, 7.0)
    # Step of 3 takes 4.5 (c done), step of 2 takes 4.25 (b done), step of 1 takes 4 (a done).
    assert (a.first_token_ms, b.first_token_ms, c.first_token_ms) == (21.5, 21.5, 21.5)
    assert (c.finish_ms, b.finish_ms, a.finish_ms) == (21.5, 25.75, 29.75)
    assert clock.now_ms() == 29.75  # the baseline needs 41.0 for the same three requests


async def test_single_request_matches_the_baseline_exactly() -> None:
    sizes = {"a": (10, 3)}
    continuous = await ContinuousBatchingWorker(EXACT, SizedBackend(sizes), SimClock()).submit(
        _request("a")
    )
    baseline = await BaselineWorker(EXACT, SizedBackend(sizes), SimClock()).submit(_request("a"))
    assert continuous == baseline


async def test_batch_limit_of_one_behaves_like_the_baseline() -> None:
    profile = dataclasses.replace(EXACT, max_batch_size=1)
    sizes = _mixed_sizes(8)

    async def run(make_worker: WorkerFactory) -> list[RequestResult]:
        clock = SimClock()
        worker = make_worker(profile, SizedBackend(sizes), clock)
        return list(
            await asyncio.gather(
                *(_submit_at(clock, worker, 3.0 * i, name) for i, name in enumerate(sizes))
            )
        )

    assert await run(ContinuousBatchingWorker) == await run(BaselineWorker)


async def test_prefill_of_a_new_request_delays_the_running_batch() -> None:
    sizes = {"first": (0, 4), "heavy": (40, 1)}
    clock = SimClock()
    worker = ContinuousBatchingWorker(EXACT, SizedBackend(sizes), clock)

    first, heavy = await asyncio.gather(
        _submit_at(clock, worker, 0, "first"),
        _submit_at(clock, worker, 1, "heavy"),
    )

    # first: steps end at 4. heavy admitted at 4, prefill 20 ms (4..24) stalls first, then
    # one step of 2 (4.25) to 28.25 finishes heavy; first needs two more steps alone.
    assert (heavy.start_ms, heavy.finish_ms) == (4.0, 28.25)
    assert first.finish_ms == 36.25  # alone it would have finished at 16.0


async def test_request_admitted_mid_flight_uses_the_slot_freed_by_a_finished_one() -> None:
    profile = dataclasses.replace(EXACT, max_batch_size=2)
    sizes = {"a": (0, 1), "b": (0, 3), "c": (0, 2)}
    clock = SimClock()
    worker = ContinuousBatchingWorker(profile, SizedBackend(sizes), clock)

    a, b, c = await _burst(worker, ["a", "b", "c"])

    # a and b run first (step 4.25). a finishes at 4.25 and c takes its slot at once.
    assert a.finish_ms == 4.25
    assert c.start_ms == 4.25
    assert (b.finish_ms, c.finish_ms) == (12.75, 12.75)


# --- limits --------------------------------------------------------------------------------


async def test_batch_never_exceeds_batch_size_or_kv_capacity() -> None:
    sizes = _mixed_sizes(20)
    steps: list[StepRecord] = []
    clock = SimClock()
    worker = ContinuousBatchingWorker(EXACT, SizedBackend(sizes), clock, on_step=steps.append)

    results = await asyncio.gather(
        *(_submit_at(clock, worker, 2.0 * i, name) for i, name in enumerate(sizes))
    )

    assert len(results) == 20
    assert all(1 <= s.batch_size <= EXACT.max_batch_size for s in steps)
    assert all(0 < s.kv_tokens <= EXACT.kv_capacity_tokens for s in steps)
    assert max(s.batch_size for s in steps) == EXACT.max_batch_size  # the limit was reached
    assert all(s.step_ms == decode_step_ms(EXACT, s.batch_size) for s in steps)


async def test_kv_capacity_limits_the_batch_below_max_batch_size() -> None:
    profile = dataclasses.replace(EXACT, kv_capacity_tokens=40)
    sizes = {f"r{i}": (10, 6) for i in range(6)}  # 16 KV tokens each: two fit, three do not
    steps: list[StepRecord] = []
    worker = ContinuousBatchingWorker(
        profile, SizedBackend(sizes), SimClock(), on_step=steps.append
    )

    results = await _burst(worker, list(sizes))

    assert len(results) == 6
    assert {s.batch_size for s in steps} == {2}
    assert {s.kv_tokens for s in steps} == {32}
    # Served in pairs, in arrival order.
    assert [r.start_ms for r in results] == sorted(r.start_ms for r in results)


async def test_request_larger_than_the_kv_cache_is_rejected_without_blocking_others() -> None:
    profile = dataclasses.replace(EXACT, kv_capacity_tokens=40)
    sizes = {"ok1": (10, 6), "huge": (30, 11), "ok2": (10, 6)}
    worker = ContinuousBatchingWorker(profile, SizedBackend(sizes), SimClock())

    ok1, huge, ok2 = await asyncio.gather(
        *(worker.submit(_request(i)) for i in sizes), return_exceptions=True
    )

    assert isinstance(huge, RequestTooLargeError)
    assert (huge.request_id, huge.needed_tokens, huge.capacity_tokens) == ("huge", 41, 40)
    assert isinstance(ok1, RequestResult)
    assert isinstance(ok2, RequestResult)


async def test_admission_is_first_come_first_served_even_if_a_later_request_would_fit() -> None:
    profile = dataclasses.replace(EXACT, kv_capacity_tokens=40)
    sizes = {"a": (10, 10), "big": (20, 10), "small": (2, 2)}  # 20, 30 and 4 KV tokens
    worker = ContinuousBatchingWorker(profile, SizedBackend(sizes), SimClock())

    a, big, small = await _burst(worker, ["a", "big", "small"])

    # small would fit beside a, but big is ahead of it and must not be overtaken.
    assert big.start_ms == a.finish_ms
    assert small.start_ms >= big.start_ms


# --- throughput ----------------------------------------------------------------------------


@pytest.mark.parametrize("profile", [ECONOMY, STANDARD, PREMIUM], ids=lambda p: p.tier.value)
async def test_twenty_concurrent_requests_have_higher_throughput_than_baseline(
    profile: ModelProfile,
) -> None:
    requests = [
        _request(f"r{i}", f"request {i} " + "context " * (10 * (i % 7)), profile.tier.value)
        for i in range(20)
    ]

    async def run(make_worker: WorkerFactory) -> tuple[list[RequestResult], float]:
        clock = SimClock()
        worker = make_worker(profile, SimulatedBackend(seed=11), clock)
        results = list(await asyncio.gather(*(worker.submit(r) for r in requests)))
        return results, clock.now_ms()

    baseline, baseline_makespan_ms = await run(BaselineWorker)
    continuous, continuous_makespan_ms = await run(ContinuousBatchingWorker)

    baseline_throughput = len(requests) / baseline_makespan_ms
    continuous_throughput = len(requests) / continuous_makespan_ms
    assert continuous_throughput > baseline_throughput

    # Same work, same price: batching changes when tokens are produced, not what is produced.
    for b, c in zip(baseline, continuous, strict=True):
        assert (c.request_id, c.tokens_in, c.tokens_out, c.cost_usd) == (
            b.request_id,
            b.tokens_in,
            b.tokens_out,
            b.cost_usd,
        )


# --- invariants ----------------------------------------------------------------------------


async def test_every_request_finishes_exactly_once_with_the_right_token_counts() -> None:
    sizes = _mixed_sizes(40)
    steps: list[StepRecord] = []
    clock = SimClock()
    worker = ContinuousBatchingWorker(EXACT, SizedBackend(sizes), clock, on_step=steps.append)

    results = await asyncio.gather(
        *(_submit_at(clock, worker, 5.0 * (i // 3), name) for i, name in enumerate(sizes))
    )

    assert sorted(r.request_id for r in results) == sorted(sizes)
    for result in results:
        assert (result.tokens_in, result.tokens_out) == sizes[result.request_id]
        assert result.enqueue_ms <= result.start_ms < result.first_token_ms <= result.finish_ms
        assert result.model == EXACT.name
    # Each step yields one token per running request, so the steps account for every token:
    # nothing was decoded twice and nothing was skipped.
    assert sum(s.batch_size for s in steps) == sum(out for _, out in sizes.values())
    # Steps never overlap.
    for earlier, later in itertools.pairwise(steps):
        assert later.start_ms >= earlier.start_ms + earlier.step_ms
    # The worker is idle again: a new request starts immediately.
    idle_since_ms = clock.now_ms()
    again = await worker.submit(_request("r0"))
    assert again.enqueue_ms == again.start_ms == idle_since_ms


async def test_whole_run_is_reproducible() -> None:
    async def run() -> list[RequestResult]:
        clock = SimClock()
        registry = build_continuous_registry(clock, seed=7)

        async def arrive(i: int) -> RequestResult:
            await clock.sleep_ms(i * 15.0)
            model = ("economy", "standard", "premium")[i % 3]
            return await registry.submit(_request(f"r{i}", f"prompt number {i} " * (i + 1), model))

        return list(await asyncio.gather(*(arrive(i) for i in range(30))))

    assert await run() == await run()


# --- failures and cancellation -------------------------------------------------------------


async def test_backend_failure_reaches_the_caller_and_others_still_finish() -> None:
    worker = ContinuousBatchingWorker(EXACT, SizedBackend({"a": (10, 3), "b": (4, 2)}), SimClock())

    a, missing, b = await asyncio.gather(
        worker.submit(_request("a")),
        worker.submit(_request("missing")),
        worker.submit(_request("b")),
        return_exceptions=True,
    )

    assert isinstance(missing, KeyError)
    assert isinstance(a, RequestResult)
    assert isinstance(b, RequestResult)


async def test_request_cancelled_while_queued_is_never_started() -> None:
    profile = dataclasses.replace(EXACT, max_batch_size=1)
    sizes = {"a": (10, 3), "b": (4, 2), "c": (20, 1)}
    steps: list[StepRecord] = []
    clock = SimClock()
    worker = ContinuousBatchingWorker(profile, SizedBackend(sizes), clock, on_step=steps.append)
    a = asyncio.create_task(worker.submit(_request("a")))
    b = asyncio.create_task(worker.submit(_request("b")))
    c = asyncio.create_task(worker.submit(_request("c")))

    await clock.sleep_ms(1)
    b.cancel()

    a_result, c_result = await asyncio.gather(a, c)
    assert b.cancelled()
    assert c_result.start_ms == a_result.finish_ms == 17.0
    assert len(steps) == 3 + 1  # b's two tokens were never decoded


async def test_request_cancelled_while_running_frees_its_slot_at_the_next_iteration() -> None:
    profile = dataclasses.replace(EXACT, max_batch_size=1)
    clock = SimClock()
    worker = ContinuousBatchingWorker(
        profile, SizedBackend({"long": (10, 50), "next": (4, 2)}), clock
    )
    long = asyncio.create_task(worker.submit(_request("long")))
    following = asyncio.create_task(worker.submit(_request("next")))

    await clock.sleep_ms(10)  # long has prefilled (0..5) and is mid-step (9..13)
    long.cancel()

    result = await following
    assert long.cancelled()
    assert result.start_ms == 13.0  # not 205.0, when long would have finished


# --- swappable with the baseline -----------------------------------------------------------


@pytest.mark.parametrize("make_worker", WORKER_KINDS, ids=["baseline", "continuous"])
async def test_both_workers_satisfy_the_same_interface(make_worker: WorkerFactory) -> None:
    worker = make_worker(EXACT, SizedBackend({"a": (10, 3)}), SimClock())
    result = await worker.submit(_request("a"))
    assert (result.tokens_in, result.tokens_out, result.finish_ms) == (10, 3, 17.0)


async def test_continuous_registry_mirrors_the_baseline_registry() -> None:
    continuous = build_continuous_registry(SimClock(), seed=7)
    baseline = build_baseline_registry(SimClock(), seed=7)

    assert continuous.names == baseline.names
    assert continuous.valid_names == baseline.valid_names
    for tier in Tier:
        worker = continuous.get(PROFILES[tier].name)
        assert isinstance(worker, ContinuousBatchingWorker)
        assert worker.profile is PROFILES[tier]
    with pytest.raises(UnknownModelError):
        continuous.get("llama-3.3-70b")


# --- per-request overhead ------------------------------------------------------------------

SLOW_START = dataclasses.replace(EXACT, per_request_overhead_ms=8.0)


@pytest.mark.parametrize("make_worker", WORKER_KINDS)
async def test_per_request_overhead_delays_the_first_token_and_is_queue_wait(
    make_worker: WorkerFactory,
) -> None:
    async def serve(profile: ModelProfile) -> RequestResult:
        return await make_worker(profile, SizedBackend({"a": (10, 3)}), SimClock()).submit(
            _request("a")
        )

    plain = await serve(EXACT)
    delayed = await serve(SLOW_START)

    # 8 ms of overhead, 5 ms of prefill, then three 4 ms steps.
    assert (delayed.enqueue_ms, delayed.start_ms) == (0.0, 8.0)
    assert (delayed.first_token_ms, delayed.finish_ms) == (17.0, 25.0)
    assert delayed.queue_wait_ms == 8.0
    assert delayed.time_to_first_token_ms == plain.time_to_first_token_ms + 8.0
    assert delayed.inter_token_ms == plain.inter_token_ms
    assert delayed.cost_usd == plain.cost_usd


async def test_overheads_of_requests_that_arrive_together_overlap() -> None:
    sizes = dict.fromkeys(("a", "b"), (10, 3))

    clock = SimClock()
    a, b = await _burst(
        ContinuousBatchingWorker(SLOW_START, SizedBackend(sizes), clock), ["a", "b"]
    )
    # Both are ready at 8, not 8 and 16. They are prefilled one after the other (8..13,
    # 13..18) and then decode together at 4.25 ms per step.
    assert (a.start_ms, b.start_ms) == (8.0, 13.0)
    assert (a.first_token_ms, b.first_token_ms) == (22.25, 22.25)

    clock = SimClock()
    a, b = await _burst(BaselineWorker(SLOW_START, SizedBackend(sizes), clock), ["a", "b"])
    # One at a time: a finishes at 25, and b's overhead passed while it waited.
    assert (a.finish_ms, b.start_ms, b.first_token_ms) == (25.0, 25.0, 34.0)


async def test_requests_in_their_overhead_count_against_the_queue_limit() -> None:
    clock = SimClock()
    profile = dataclasses.replace(SLOW_START, max_batch_size=1)
    worker = ContinuousBatchingWorker(
        profile, SizedBackend(dict.fromkeys(("a", "b", "c"), (10, 3))), clock, max_queue_len=1
    )
    first = asyncio.ensure_future(worker.submit(_request("a")))
    second = asyncio.ensure_future(worker.submit(_request("b")))
    await clock.sleep_ms(1.0)

    # Neither has joined the queue yet. One will take the free slot and the other will wait.
    assert worker.waiting == 1
    with pytest.raises(QueueFullError):
        await worker.submit(_request("c"))

    results = await asyncio.gather(first, second)
    assert [r.request_id for r in results] == ["a", "b"]
    assert worker.waiting == 0
