from __future__ import annotations

import asyncio
import dataclasses

import pytest
from benchmarks import chunked_prefill as benchmark

from gateway.backends import SimulatedBackend
from gateway.clock import SimClock
from gateway.continuous import ContinuousBatchingWorker, StepRecord, build_continuous_registry
from gateway.cost_model import decode_step_ms
from gateway.prefix_cache import PrefixCache
from gateway.schema import GenerateRequest, RequestResult
from helpers import EXACT, SizedBackend


def _request(request_id: str, prompt: str = "") -> GenerateRequest:
    return GenerateRequest(request_id, "standard", prompt)


async def _run(
    sizes: dict[str, tuple[int, int]],
    arrivals: dict[str, float],
    chunk_tokens: int | None,
    steps: list[StepRecord] | None = None,
) -> dict[str, RequestResult]:
    clock = SimClock()
    worker = ContinuousBatchingWorker(
        EXACT,
        SizedBackend(sizes),
        clock,
        prefill_chunk_tokens=chunk_tokens,
        on_step=steps.append if steps is not None else None,
    )

    async def arrive(request_id: str, at_ms: float) -> RequestResult:
        await clock.sleep_ms(at_ms)
        return await worker.submit(_request(request_id))

    results = await asyncio.gather(*(arrive(name, at) for name, at in arrivals.items()))
    return {r.request_id: r for r in results}


# --- hand-checked timelines ----------------------------------------------------------------
# EXACT profile: 0.5 ms prefill per token; a decode step is 4 ms alone, 4.25 ms for two.


async def test_long_prompt_is_prefilled_in_chunks_between_decode_steps() -> None:
    sizes = {"running": (0, 3), "new": (10, 2)}
    arrivals = {"running": 0.0, "new": 1.0}
    steps: list[StepRecord] = []

    chunked = await _run(sizes, arrivals, 4, steps)
    unchunked = await _run(sizes, arrivals, None)

    # Chunked: new is admitted at t=4 and prefilled 4 + 4 + 2 tokens, one chunk per
    # iteration (2, 2 and 1 ms), while running keeps decoding in between.
    assert chunked["running"].token_times_ms == (4.0, 10.0, 16.0)
    assert chunked["new"].token_times_ms == (21.0, 25.0)
    assert (chunked["new"].start_ms, chunked["new"].first_token_ms) == (4.0, 21.0)
    assert [(s.start_ms, s.batch_size, s.prefill_tokens) for s in steps] == [
        (0.0, 1, 0),
        (6.0, 1, 4),  # running decodes alone; new is still prefilling
        (12.0, 1, 4),
        (17.0, 1, 2),  # new's last chunk, then its first decode step
        (21.0, 1, 0),
    ]

    # Unchunked: the whole 5 ms prefill lands in one gap of the running request.
    assert unchunked["running"].token_times_ms == (4.0, 13.25, 17.5)
    assert unchunked["new"].token_times_ms == (13.25, 17.5)

    # The trade: the running request's worst gap shrinks, the new one's first token is later.
    assert max(chunked["running"].inter_token_ms) == 6.0
    assert max(unchunked["running"].inter_token_ms) == 9.25
    assert chunked["new"].time_to_first_token_ms == 20.0
    assert unchunked["new"].time_to_first_token_ms == 12.25


async def test_prompt_that_fits_in_one_chunk_behaves_as_without_chunking() -> None:
    sizes = {"running": (0, 3), "new": (10, 2)}
    arrivals = {"running": 0.0, "new": 1.0}

    one_chunk = await _run(sizes, arrivals, 10)
    unchunked = await _run(sizes, arrivals, None)

    for name in sizes:
        assert one_chunk[name].token_times_ms == unchunked[name].token_times_ms
    assert one_chunk["new"].token_times_ms == (13.25, 17.5)


async def test_request_alone_takes_the_same_time_however_its_prefill_is_chunked() -> None:
    sizes = {"only": (10, 2)}
    unchunked = await _run(sizes, {"only": 0.0}, None)
    for chunk_tokens in (1, 3, 4, 10, 1000):
        chunked = await _run(sizes, {"only": 0.0}, chunk_tokens)
        # Nothing else is decoding, so the chunks simply follow one another: 5 ms in all.
        assert chunked == unchunked
    assert unchunked["only"].token_times_ms == (9.0, 13.0)


async def test_every_prefilling_request_gets_a_chunk_each_iteration() -> None:
    sizes = {"running": (0, 6), "long": (8, 1), "short": (4, 1)}
    arrivals = {"running": 0.0, "long": 1.0, "short": 1.0}
    steps: list[StepRecord] = []

    results = await _run(sizes, arrivals, 4, steps)

    # At t=4 both are admitted and each gets 4 tokens (4 ms in total). That finishes
    # short's prompt, so it decodes at once and is done at 12.25, before long, which was
    # admitted ahead of it but needs a second chunk.
    assert results["short"].token_times_ms == (12.25,)
    assert results["long"].token_times_ms == (18.5,)
    assert [(s.start_ms, s.batch_size, s.prefill_tokens) for s in steps[:3]] == [
        (0.0, 1, 0),
        (8.0, 2, 8),
        (14.25, 2, 4),
    ]


async def test_cached_prefix_is_not_prefilled_again() -> None:
    clock = SimClock()
    worker = ContinuousBatchingWorker(
        EXACT,
        SimulatedBackend(seed=1, min_output_tokens=2, max_output_tokens=2),
        clock,
        prefix_cache=PrefixCache(EXACT.kv_capacity_tokens),
        prefill_chunk_tokens=4,
    )
    prompt = " ".join(f"w{i}" for i in range(10))

    first = await worker.submit(_request("r1", prompt))
    second = await worker.submit(_request("r2", f"{prompt} extra"))

    # r1: three chunks (2 + 2 + 1 ms), then two decode steps.
    assert (first.cached_tokens, first.token_times_ms) == (0, (9.0, 13.0))
    # r2: ten of its eleven tokens are cached, leaving one token (0.5 ms) to prefill.
    assert second.cached_tokens == 10
    assert second.first_token_ms - second.start_ms == 0.5 + 4.0


async def test_request_cancelled_while_prefilling_frees_its_slot_and_kv_tokens() -> None:
    profile = dataclasses.replace(EXACT, max_batch_size=1, kv_capacity_tokens=40)
    steps: list[StepRecord] = []
    clock = SimClock()
    worker = ContinuousBatchingWorker(
        profile,
        SizedBackend({"long": (30, 5), "next": (2, 1)}),
        clock,
        prefill_chunk_tokens=4,
        on_step=steps.append,
    )
    long = asyncio.create_task(worker.submit(_request("long")))
    await clock.sleep_ms(1.0)
    following = asyncio.create_task(worker.submit(_request("next")))

    await clock.sleep_ms(4.0)  # t=5: long has prefilled two chunks and is on its third
    long.cancel()
    result = await following

    assert long.cancelled()
    # long left at the end of the chunk in progress (t=6), not after its remaining
    # 18 tokens of prefill and five decode steps.
    assert result.start_ms == 6.0
    assert result.token_times_ms == (11.0,)
    assert [(s.batch_size, s.kv_tokens) for s in steps] == [(1, 3)]


# --- invariants ----------------------------------------------------------------------------


@pytest.mark.parametrize("chunk_tokens", [1, 4, 7, 64])
async def test_chunking_changes_timing_but_not_tokens_cost_or_order(chunk_tokens: int) -> None:
    sizes = {f"r{i}": ((i * 13) % 37, 1 + (i * 7) % 11) for i in range(40)}
    arrivals = {name: 3.0 * (i // 2) for i, name in enumerate(sizes)}
    steps: list[StepRecord] = []

    chunked = await _run(sizes, arrivals, chunk_tokens, steps)
    unchunked = await _run(sizes, arrivals, None)

    assert set(chunked) == set(sizes)
    for name, result in chunked.items():
        tokens_in, tokens_out = sizes[name]
        assert (result.tokens_in, result.tokens_out) == (tokens_in, tokens_out)
        assert result.cost_usd == unchunked[name].cost_usd
        # One timestamp per output token, strictly increasing, consistent with the summary.
        assert len(result.token_times_ms) == tokens_out
        assert list(result.token_times_ms) == sorted(set(result.token_times_ms))
        assert result.first_token_ms == result.token_times_ms[0]
        assert result.finish_ms == result.token_times_ms[-1]
        assert result.enqueue_ms <= result.start_ms < result.first_token_ms
        # A request cannot get its first token before all of its prompt has been prefilled.
        assert result.first_token_ms - result.start_ms >= (
            tokens_in * EXACT.prefill_ms_per_token + EXACT.decode_base_ms
        )
    # The steps account for every output token exactly once.
    assert sum(s.batch_size for s in steps) == sum(out for _, out in sizes.values())
    assert all(1 <= s.batch_size <= EXACT.max_batch_size for s in steps)
    assert all(s.step_ms == decode_step_ms(EXACT, s.batch_size) for s in steps)
    # No iteration prefills more than one chunk per running request.
    assert all(s.prefill_tokens <= chunk_tokens * EXACT.max_batch_size for s in steps)


@pytest.mark.parametrize("chunk_tokens", [1, 4, 16])
async def test_no_gap_between_tokens_exceeds_one_iterations_worth_of_chunks(
    chunk_tokens: int,
) -> None:
    # A request with a huge prompt arrives while others are decoding.
    sizes = {**{f"r{i}": (2, 30) for i in range(3)}, "huge": (400, 2)}
    arrivals = {"r0": 0.0, "r1": 0.0, "r2": 0.0, "huge": 20.0}

    chunked = await _run(sizes, arrivals, chunk_tokens)
    unchunked = await _run(sizes, arrivals, None)

    bound_ms = EXACT.max_batch_size * chunk_tokens * EXACT.prefill_ms_per_token + decode_step_ms(
        EXACT, EXACT.max_batch_size
    )
    worst_chunked = max(gap for r in chunked.values() for gap in r.inter_token_ms)
    worst_unchunked = max(gap for r in unchunked.values() for gap in r.inter_token_ms)
    assert worst_chunked <= bound_ms
    assert worst_unchunked > 200.0  # the whole 400-token prefill in one gap
    assert worst_chunked < worst_unchunked


def test_chunk_size_must_be_positive() -> None:
    with pytest.raises(ValueError, match="prefill_chunk_tokens"):
        ContinuousBatchingWorker(EXACT, SizedBackend({}), SimClock(), prefill_chunk_tokens=0)


async def test_registry_builder_turns_chunked_prefill_on_for_every_model() -> None:
    prompt = " ".join(f"w{i}" for i in range(300))

    async def ttft(chunk_tokens: int | None) -> float:
        clock = SimClock()
        registry = build_continuous_registry(
            clock, seed=7, prefix_cache=False, prefill_chunk_tokens=chunk_tokens
        )
        running = asyncio.create_task(
            registry.submit(GenerateRequest("r1", "premium", "short prompt", 20))
        )
        await clock.sleep_ms(1.0)
        late = await registry.submit(GenerateRequest("r2", "premium", prompt, 2))
        await running
        return late.time_to_first_token_ms

    # With chunks of 50, the 300-token prompt needs six iterations, each with a decode
    # step for the request already running, so its first token comes later.
    assert await ttft(50) > await ttft(None)


# --- the expected tail improvement ---------------------------------------------------------


async def test_chunked_prefill_cuts_the_latency_tails_on_a_mixed_workload() -> None:
    arrivals = benchmark.workload()
    off = benchmark.summarise(arrivals, await benchmark.run(arrivals, None))
    mid = benchmark.summarise(arrivals, await benchmark.run(arrivals, 256))
    small = benchmark.summarise(arrivals, await benchmark.run(arrivals, 16))

    # The worst stall: unchunked, one long prompt freezes every stream for over a second.
    assert off.itl_max_ms > 1000.0
    assert mid.itl_max_ms < off.itl_max_ms / 10
    assert small.itl_max_ms < mid.itl_max_ms
    # Seen per request: the p99 of each request's worst gap.
    assert mid.worst_gap_p99_ms < off.worst_gap_p99_ms / 10
    # Short prompts no longer wait behind a long prompt's whole prefill.
    assert mid.short_ttft_p99_ms < off.short_ttft_p99_ms / 5
    # With small enough chunks even the plain ITL p99 improves.
    assert small.itl_p99_ms < off.itl_p99_ms
    # The typical gap barely moves: this is about the tail.
    assert abs(mid.itl_p50_ms - off.itl_p50_ms) < 1.0

    # What it costs: the long prompts themselves start later, more so with smaller chunks.
    assert off.long_ttft_p50_ms < mid.long_ttft_p50_ms < small.long_ttft_p50_ms


async def test_medium_chunks_can_make_the_itl_p99_worse_than_no_chunking() -> None:
    # Long prompts are rare, so unchunked their stalls touch fewer than 1% of all gaps
    # and the ITL p99 does not see them. Chunking turns each rare, huge stall into many
    # moderate ones, which do reach the p99. Pinned so the docs' claim stays true.
    arrivals = benchmark.workload()
    off = benchmark.summarise(arrivals, await benchmark.run(arrivals, None))
    medium = benchmark.summarise(arrivals, await benchmark.run(arrivals, 1024))

    assert medium.itl_p99_ms > off.itl_p99_ms
    assert medium.itl_max_ms < off.itl_max_ms
    assert medium.itl_p999_ms < off.itl_p999_ms
