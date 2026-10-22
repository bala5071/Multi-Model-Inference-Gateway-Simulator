from __future__ import annotations

import asyncio

import pytest

from gateway.backends import Backend, BackendTokenCallback, Generation, SimulatedBackend
from gateway.baseline import BaselineWorker, build_baseline_registry
from gateway.clock import SimClock
from gateway.cost_model import estimate_cost
from gateway.profiles import PROFILES, ModelProfile, Tier
from gateway.registry import UnknownModelError
from gateway.schema import GenerateRequest, RequestResult
from helpers import (
    EXACT,
    TEST_ECONOMY,
    TEST_PREMIUM,
    TEST_PROFILES,
    TEST_STANDARD,
    unique_tokens,
)

# request_id -> (tokens_in, tokens_out); service time on EXACT is in*0.5 + out*4.
SIZES = {
    "a": (10, 3),  # prefill 5, decode 12, service 17
    "b": (4, 2),  # prefill 2, decode 8, service 10
    "c": (20, 1),  # prefill 10, decode 4, service 14
}


class FixedBackend(Backend):
    """Token counts looked up by request id, so tests control the workload exactly."""

    async def generate(
        self, request: GenerateRequest, *, on_token: BackendTokenCallback | None = None
    ) -> Generation:
        if request.request_id == "bad":
            raise RuntimeError("backend exploded")
        tokens_in, tokens_out = SIZES[request.request_id]
        return Generation(
            prompt_tokens=unique_tokens(request.request_id, tokens_in), tokens_out=tokens_out
        )


def _request(request_id: str, prompt: str = "", model: str = "standard") -> GenerateRequest:
    return GenerateRequest(request_id=request_id, model=model, prompt=prompt)


def _exact_worker(clock: SimClock) -> BaselineWorker:
    return BaselineWorker(EXACT, FixedBackend(), clock)


async def _submit_at(
    clock: SimClock, worker: BaselineWorker, arrival_ms: float, request_id: str
) -> RequestResult:
    await clock.sleep_ms(arrival_ms)
    return await worker.submit(_request(request_id))


# --- timing math ---------------------------------------------------------------------------


async def test_single_request_timing_matches_the_cost_model_exactly() -> None:
    clock = SimClock()
    result = await _exact_worker(clock).submit(_request("a"))

    assert result == RequestResult(
        request_id="a",
        model="exact-test-model",
        enqueue_ms=0.0,
        start_ms=0.0,
        first_token_ms=9.0,  # prefill 5 + one decode step 4
        finish_ms=17.0,  # prefill 5 + three decode steps
        tokens_in=10,
        tokens_out=3,
        cost_usd=0.008,  # (10 * 0.5 + 3 * 1.0) / 1000
        token_times_ms=(9.0, 13.0, 17.0),
    )
    assert clock.now_ms() == 17.0


async def test_timing_is_relative_to_the_clock_not_to_zero() -> None:
    clock = SimClock(start_ms=1000.0)
    result = await _exact_worker(clock).submit(_request("a"))
    assert (result.enqueue_ms, result.start_ms, result.first_token_ms, result.finish_ms) == (
        1000.0,
        1000.0,
        1009.0,
        1017.0,
    )


@pytest.mark.parametrize(
    "profile", [TEST_ECONOMY, TEST_STANDARD, TEST_PREMIUM], ids=lambda p: p.tier.value
)
async def test_real_profile_timing_matches_the_cost_model_exactly(profile: ModelProfile) -> None:
    prompt = "Explain continuous batching to a new engineer in two short paragraphs."
    backend = SimulatedBackend(seed=7)
    generation = await backend.generate(_request("r", prompt))
    estimate = estimate_cost(
        profile,
        new_prefill_tokens=generation.tokens_in,
        output_tokens=generation.tokens_out,
        batch_size=1,
    )
    # Add the durations one at a time, as the clock does, so the comparison is exact.
    expected_first_token_ms = 0.0 + estimate.prefill_ms + estimate.decode_step_ms
    expected_finish_ms = expected_first_token_ms
    for _ in range(generation.tokens_out - 1):
        expected_finish_ms += estimate.decode_step_ms

    clock = SimClock()
    result = await BaselineWorker(profile, backend, clock).submit(_request("r", prompt))

    assert result.model == profile.name
    assert result.tokens_in == generation.tokens_in == 11
    assert result.tokens_out == generation.tokens_out
    assert result.enqueue_ms == 0.0
    assert result.start_ms == 0.0
    assert result.first_token_ms == expected_first_token_ms
    assert result.finish_ms == expected_finish_ms
    assert result.cost_usd == estimate.cost_usd
    # And the closed form agrees to within float rounding.
    assert result.finish_ms == pytest.approx(
        estimate.prefill_ms + generation.tokens_out * estimate.decode_step_ms
    )


async def test_baseline_always_uses_batch_size_one() -> None:
    result = await _exact_worker(SimClock()).submit(_request("c"))
    # One output token: the whole decode is a single step at the base step time.
    assert result.finish_ms - result.start_ms == 20 * 0.5 + EXACT.decode_base_ms


# --- queueing ------------------------------------------------------------------------------


async def test_three_requests_arriving_together_wait_for_those_ahead() -> None:
    clock = SimClock()
    worker = _exact_worker(clock)

    a, b, c = await asyncio.gather(*(worker.submit(_request(i)) for i in ("a", "b", "c")))

    assert (a.enqueue_ms, b.enqueue_ms, c.enqueue_ms) == (0.0, 0.0, 0.0)
    assert (a.start_ms, b.start_ms, c.start_ms) == (0.0, 17.0, 27.0)
    assert (a.first_token_ms, b.first_token_ms, c.first_token_ms) == (9.0, 23.0, 41.0)
    assert (a.finish_ms, b.finish_ms, c.finish_ms) == (17.0, 27.0, 41.0)
    assert (a.queue_wait_ms, b.queue_wait_ms, c.queue_wait_ms) == (0.0, 17.0, 27.0)
    assert (a.latency_ms, b.latency_ms, c.latency_ms) == (17.0, 27.0, 41.0)
    assert (a.time_to_first_token_ms, b.time_to_first_token_ms, c.time_to_first_token_ms) == (
        9.0,
        23.0,
        41.0,
    )
    # Each request starts the instant the one ahead finishes: no gaps, no overlap.
    assert b.start_ms == a.finish_ms
    assert c.start_ms == b.finish_ms
    assert clock.now_ms() == 41.0


async def test_requests_are_served_in_arrival_order_not_shortest_first() -> None:
    clock = SimClock()
    worker = _exact_worker(clock)
    finished: list[str] = []

    async def submit(request_id: str) -> None:
        result = await worker.submit(_request(request_id))
        finished.append(result.request_id)

    # "a" is the longest job and "b" the shortest; arrival order must still win.
    await asyncio.gather(submit("a"), submit("c"), submit("b"))

    assert finished == ["a", "c", "b"]


async def test_staggered_arrivals_keep_fifo_order_and_correct_waits() -> None:
    clock = SimClock()
    worker = _exact_worker(clock)

    a, c, b = await asyncio.gather(
        _submit_at(clock, worker, 0, "a"),
        _submit_at(clock, worker, 6, "c"),
        _submit_at(clock, worker, 5, "b"),
    )

    # b arrived at 5 and c at 6, so b goes before c even though c was listed first.
    assert (b.enqueue_ms, b.start_ms, b.finish_ms) == (5.0, 17.0, 27.0)
    assert (c.enqueue_ms, c.start_ms, c.finish_ms) == (6.0, 27.0, 41.0)
    assert (a.queue_wait_ms, b.queue_wait_ms, c.queue_wait_ms) == (0.0, 12.0, 21.0)


async def test_request_arriving_at_an_idle_worker_starts_immediately() -> None:
    clock = SimClock()
    worker = _exact_worker(clock)

    a, b = await asyncio.gather(
        _submit_at(clock, worker, 0, "a"),  # done at 17
        _submit_at(clock, worker, 100, "b"),  # worker idle since 17
    )

    assert a.finish_ms == 17.0
    assert (b.enqueue_ms, b.start_ms, b.finish_ms) == (100.0, 100.0, 110.0)
    assert b.queue_wait_ms == 0.0


async def test_worker_can_be_reused_after_its_queue_drains() -> None:
    clock = SimClock()
    worker = _exact_worker(clock)
    first = await worker.submit(_request("a"))
    second = await worker.submit(_request("b"))
    assert first.finish_ms == 17.0
    assert (second.enqueue_ms, second.start_ms, second.finish_ms) == (17.0, 17.0, 27.0)


async def test_backend_failure_reaches_the_caller_and_does_not_block_the_queue() -> None:
    clock = SimClock()
    worker = _exact_worker(clock)

    a, bad, b = await asyncio.gather(
        worker.submit(_request("a")),
        worker.submit(_request("bad")),
        worker.submit(_request("b")),
        return_exceptions=True,
    )

    assert isinstance(bad, RuntimeError)
    assert isinstance(a, RequestResult)
    assert isinstance(b, RequestResult)
    assert b.start_ms == a.finish_ms == 17.0


async def test_request_cancelled_while_queued_is_skipped() -> None:
    clock = SimClock()
    worker = _exact_worker(clock)
    a = asyncio.create_task(worker.submit(_request("a")))
    b = asyncio.create_task(worker.submit(_request("b")))
    c = asyncio.create_task(worker.submit(_request("c")))

    await clock.sleep_ms(1)  # a is being served; b and c are queued
    b.cancel()

    a_result, c_result = await asyncio.gather(a, c)
    assert b.cancelled()
    assert c_result.start_ms == a_result.finish_ms == 17.0


# --- registry wiring -----------------------------------------------------------------------


async def test_baseline_registry_has_one_worker_per_tier_with_model_name_aliases() -> None:
    registry = build_baseline_registry(SimClock(), seed=7)

    assert registry.names == ("economy", "standard", "premium")
    for tier in Tier:
        worker = registry.get(tier.value)
        assert isinstance(worker, BaselineWorker)
        assert worker.profile is PROFILES[tier]
        assert registry.get(PROFILES[tier].name) is worker


async def test_baseline_registry_rejects_unknown_models_with_the_valid_names() -> None:
    registry = build_baseline_registry(SimClock(), seed=7)

    with pytest.raises(UnknownModelError) as excinfo:
        await registry.submit(_request("r", "hi", model="llama-3.3-70b"))

    assert excinfo.value.valid_names == (
        "economy",
        "standard",
        "premium",
        "llama-3.2-1b",
        "llama-3.2-3b",
        "llama-3.1-8b",
    )


async def test_models_queue_independently() -> None:
    clock = SimClock()
    registry = build_baseline_registry(clock, seed=7, profiles=TEST_PROFILES)
    prompt = "Explain continuous batching to a new engineer in two short paragraphs."

    results = await asyncio.gather(
        registry.submit(_request("p1", prompt, model="premium")),
        registry.submit(_request("e1", prompt, model="economy")),
        registry.submit(_request("e2", prompt, model="llama-3.2-1b")),
        registry.submit(_request("s1", prompt, model="standard")),
    )
    p1, e1, e2, s1 = results

    # Different models all start at once; the second economy request waits for the first.
    assert (p1.start_ms, e1.start_ms, s1.start_ms) == (0.0, 0.0, 0.0)
    assert e2.start_ms == e1.finish_ms
    assert e1.finish_ms < s1.finish_ms < p1.finish_ms
    assert e1.cost_usd < s1.cost_usd < p1.cost_usd
    assert clock.now_ms() == p1.finish_ms


async def _run_scenario(seed: int) -> list[RequestResult]:
    clock = SimClock()
    registry = build_baseline_registry(clock, seed=seed)

    async def arrive(i: int) -> RequestResult:
        await clock.sleep_ms(i * 40.0)
        model = ("economy", "standard", "premium")[i % 3]
        return await registry.submit(_request(f"r{i}", f"prompt number {i} " * (i + 1), model))

    return list(await asyncio.gather(*(arrive(i) for i in range(12))))


async def test_whole_run_is_reproducible_for_a_seed() -> None:
    first = await _run_scenario(seed=7)
    second = await _run_scenario(seed=7)
    assert first == second
    assert await _run_scenario(seed=8) != first


async def test_aclose_cancels_running_and_queued_requests_and_refuses_new_ones() -> None:
    clock = SimClock()
    worker = _exact_worker(clock)
    a = asyncio.create_task(worker.submit(_request("a")))
    b = asyncio.create_task(worker.submit(_request("b")))
    await clock.sleep_ms(1)  # a is being served, b is queued

    await worker.aclose()
    await asyncio.gather(a, b, return_exceptions=True)

    assert a.cancelled()
    assert b.cancelled()
    with pytest.raises(RuntimeError, match="closed"):
        await worker.submit(_request("c"))
    await worker.aclose()  # closing twice is harmless
