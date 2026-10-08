"""The prefix cache as used by the continuous batching worker."""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from gateway.backends import SimulatedBackend
from gateway.baseline import BaselineWorker
from gateway.clock import SimClock
from gateway.continuous import ContinuousBatchingWorker, StepRecord, build_continuous_registry
from gateway.prefix_cache import PrefixCache
from gateway.schema import GenerateRequest, RequestResult
from gateway.tokenizer import WordTokenizer
from helpers import EXACT, SizedBackend

TOKENIZER = WordTokenizer()


def _words(stem: str, count: int) -> str:
    return " ".join(f"{stem}{i}" for i in range(count))


SYSTEM = _words("sys", 40)
SYSTEM_TOKENS = TOKENIZER.encode(SYSTEM)


def _request(request_id: str, prompt: str, max_output_tokens: int = 2) -> GenerateRequest:
    return GenerateRequest(request_id, "standard", prompt, max_output_tokens)


def _worker(
    clock: SimClock,
    *,
    kv_capacity_tokens: int = EXACT.kv_capacity_tokens,
    cache: bool = True,
    on_step: list[StepRecord] | None = None,
) -> ContinuousBatchingWorker:
    profile = dataclasses.replace(EXACT, kv_capacity_tokens=kv_capacity_tokens)
    return ContinuousBatchingWorker(
        profile,
        SimulatedBackend(seed=1, min_output_tokens=30, max_output_tokens=30),
        clock,
        prefix_cache=PrefixCache(kv_capacity_tokens) if cache else None,
        on_step=on_step.append if on_step is not None else None,
    )


def _cache(worker: ContinuousBatchingWorker) -> PrefixCache:
    assert worker.prefix_cache is not None
    return worker.prefix_cache


# --- prefill is paid only for uncached tokens ----------------------------------------------


async def test_shared_system_prompt_cuts_prefill_time_and_cost_exactly() -> None:
    clock = SimClock()
    worker = _worker(clock)

    first = await worker.submit(_request("r1", f"{SYSTEM} {_words('a', 10)}"))
    second = await worker.submit(_request("r2", f"{SYSTEM} {_words('b', 6)}"))

    # r1: nothing cached. 50 tokens * 0.5 ms = 25 ms prefill, then two 4 ms steps.
    assert (first.tokens_in, first.cached_tokens) == (50, 0)
    assert (first.start_ms, first.first_token_ms, first.finish_ms) == (0.0, 29.0, 33.0)
    assert first.cost_usd == (50 * 0.5 + 2 * 1.0) / 1000

    # r2: the 40 system tokens are cached. 6 tokens * 0.5 ms = 3 ms prefill.
    assert (second.tokens_in, second.cached_tokens) == (46, 40)
    assert (second.start_ms, second.first_token_ms, second.finish_ms) == (33.0, 40.0, 44.0)
    assert second.cost_usd == (6 * 0.5 + 2 * 1.0) / 1000


async def test_without_a_cache_the_same_request_pays_full_prefill() -> None:
    clock = SimClock()
    worker = _worker(clock, cache=False)

    await worker.submit(_request("r1", f"{SYSTEM} {_words('a', 10)}"))
    second = await worker.submit(_request("r2", f"{SYSTEM} {_words('b', 6)}"))

    assert worker.prefix_cache is None
    assert second.cached_tokens == 0
    # 46 tokens * 0.5 ms = 23 ms prefill: 20 ms more than with the cache.
    assert (second.start_ms, second.first_token_ms, second.finish_ms) == (33.0, 60.0, 64.0)
    assert second.cost_usd == (46 * 0.5 + 2 * 1.0) / 1000


async def test_partial_prefix_matches_are_reused_to_the_token() -> None:
    clock = SimClock()
    worker = _worker(clock)
    await worker.submit(_request("r1", f"{SYSTEM} {_words('a', 10)}"))

    # Shares the system prompt and the first three user words, then diverges.
    deeper = await worker.submit(_request("r2", f"{SYSTEM} a0 a1 a2 different ending"))
    assert (deeper.tokens_in, deeper.cached_tokens) == (45, 43)
    assert deeper.first_token_ms - deeper.start_ms == 2 * 0.5 + 4.0

    # Shares only half of the system prompt.
    half = " ".join(SYSTEM.split()[:20])
    shallower = await worker.submit(_request("r3", f"{half} something else"))
    assert (shallower.tokens_in, shallower.cached_tokens) == (22, 20)

    # Shares nothing.
    unrelated = await worker.submit(_request("r4", _words("x", 8)))
    assert unrelated.cached_tokens == 0


async def test_repeated_prompt_needs_no_prefill_at_all() -> None:
    clock = SimClock()
    worker = _worker(clock)
    prompt = f"{SYSTEM} {_words('a', 10)}"
    await worker.submit(_request("r1", prompt))

    again = await worker.submit(_request("r2", prompt))

    assert (again.tokens_in, again.cached_tokens) == (50, 50)
    assert again.first_token_ms - again.start_ms == 4.0  # one decode step, no prefill
    assert again.cost_usd == (2 * 1.0) / 1000


async def test_prefix_becomes_reusable_only_after_its_request_is_served() -> None:
    clock = SimClock()
    worker = _worker(clock)

    together = await asyncio.gather(
        worker.submit(_request("r1", f"{SYSTEM} {_words('a', 10)}")),
        worker.submit(_request("r2", f"{SYSTEM} {_words('b', 10)}")),
    )
    later = await worker.submit(_request("r3", f"{SYSTEM} {_words('c', 10)}"))

    # r2 was admitted while r1 was still in flight, so r1's prompt was not cached yet.
    assert [r.cached_tokens for r in together] == [0, 0]
    assert later.cached_tokens == 40


async def test_caching_changes_neither_token_counts_nor_the_baseline_comparison() -> None:
    prompts = [f"{SYSTEM} {_words(stem, 5)}" for stem in ("a", "b", "c", "d", "e")]

    async def run(cached: bool) -> list[RequestResult]:
        worker = _worker(SimClock(), cache=cached)
        return [await worker.submit(_request(f"r{i}", p)) for i, p in enumerate(prompts)]

    with_cache = await run(True)
    without_cache = await run(False)

    for cached, plain in zip(with_cache, without_cache, strict=True):
        assert (cached.tokens_in, cached.tokens_out) == (plain.tokens_in, plain.tokens_out)
    assert [r.cached_tokens for r in with_cache] == [0, 40, 40, 40, 40]
    assert with_cache[-1].finish_ms < without_cache[-1].finish_ms
    assert sum(r.cost_usd for r in with_cache) < sum(r.cost_usd for r in without_cache)


async def test_unique_prompts_behave_the_same_with_and_without_a_cache() -> None:
    sizes = {f"r{i}": (4 + 6 * (i % 5), 1 + (i * 7) % 23) for i in range(20)}

    async def run(cache: PrefixCache | None) -> list[RequestResult]:
        clock = SimClock()
        worker = ContinuousBatchingWorker(EXACT, SizedBackend(sizes), clock, prefix_cache=cache)

        async def arrive(i: int, request_id: str) -> RequestResult:
            await clock.sleep_ms(2.0 * i)
            return await worker.submit(GenerateRequest(request_id, "standard", ""))

        return list(await asyncio.gather(*(arrive(i, name) for i, name in enumerate(sizes))))

    assert await run(PrefixCache(EXACT.kv_capacity_tokens)) == await run(None)


# --- capacity is shared between the cache and running requests -----------------------------


async def test_kv_capacity_is_never_exceeded_and_old_prefixes_are_evicted() -> None:
    steps: list[StepRecord] = []
    clock = SimClock()
    worker = _worker(clock, kv_capacity_tokens=120, on_step=steps)
    cache = _cache(worker)

    results = await asyncio.gather(
        *(worker.submit(_request(f"r{i}", f"{SYSTEM} {_words(f'u{i}_', 10)}")) for i in range(12))
    )

    assert len(results) == 12
    assert all(0 < s.kv_tokens <= 120 for s in steps)
    assert cache.size_tokens <= 120
    assert cache.stats.evicted_tokens > 0
    # The system prompt was in use throughout, so it was never the one evicted.
    assert cache.match(SYSTEM_TOKENS) == 40
    # Only two cold requests fit at first (52 tokens each); later ones reuse the prefix.
    assert [r.cached_tokens for r in results] == [0, 0] + [40] * 10
    assert cache.evictable_tokens == cache.size_tokens  # every lease was returned


async def test_prefix_used_by_an_in_flight_request_is_not_evicted() -> None:
    steps: list[StepRecord] = []
    matched_during_steps: list[int] = []
    clock = SimClock()
    profile = dataclasses.replace(EXACT, kv_capacity_tokens=120)
    cache = PrefixCache(120)

    def on_step(step: StepRecord) -> None:
        steps.append(step)
        matched_during_steps.append(cache.match(SYSTEM_TOKENS))

    worker = ContinuousBatchingWorker(
        profile,
        SimulatedBackend(seed=1, min_output_tokens=30, max_output_tokens=30),
        clock,
        prefix_cache=cache,
        on_step=on_step,
    )
    warm_prompt = f"{SYSTEM} {_words('w', 10)}"
    long_prompt = f"{SYSTEM} {_words('l', 10)}"
    await worker.submit(_request("warm", warm_prompt))  # cache: system 40 + w 10
    steps.clear()
    matched_during_steps.clear()

    # long reuses the system prompt and holds 10 + 30 tokens of its own: 90 in use.
    # big needs 62. Only the 10 "w" tokens are evictable; the system prompt is pinned by
    # long. So big cannot make room and has to wait for long to finish.
    long, big = await asyncio.gather(
        worker.submit(_request("long", long_prompt, max_output_tokens=30)),
        worker.submit(_request("big", _words("big", 60))),
    )

    assert long.cached_tokens == 40
    assert big.start_ms == long.finish_ms
    assert matched_during_steps == [40] * len(steps)
    assert all(s.kv_tokens <= 120 for s in steps)
    # To admit big, the least recently used unpinned leaf (the "w" suffix) was evicted.
    assert cache.match(TOKENIZER.encode(warm_prompt)) == 40
    assert cache.match(TOKENIZER.encode(long_prompt)) == 50
    assert cache.match(TOKENIZER.encode(_words("big", 60))) == 60


async def test_cancelled_running_request_returns_its_lease() -> None:
    clock = SimClock()
    worker = _worker(clock)
    cache = _cache(worker)
    await worker.submit(_request("warm", f"{SYSTEM} {_words('w', 10)}"))

    long = asyncio.create_task(
        worker.submit(_request("long", f"{SYSTEM} {_words('l', 10)}", max_output_tokens=30))
    )
    await clock.sleep_ms(20)
    assert cache.evictable_tokens == cache.size_tokens - 40  # system prompt pinned
    long.cancel()
    after = await worker.submit(_request("after", _words("z", 3)))

    assert long.cancelled()
    assert after.cached_tokens == 0
    assert cache.evictable_tokens == cache.size_tokens


def test_cache_capacity_must_match_the_profile() -> None:
    with pytest.raises(ValueError, match="kv_capacity_tokens"):
        ContinuousBatchingWorker(
            EXACT, SimulatedBackend(seed=1), SimClock(), prefix_cache=PrefixCache(10)
        )


# --- per model -----------------------------------------------------------------------------


async def test_caches_are_isolated_per_model_and_report_their_own_hit_rates() -> None:
    clock = SimClock()
    registry = build_continuous_registry(clock, seed=7)
    prompt = f"{SYSTEM} {_words('q', 10)}"

    def request(request_id: str, model: str) -> GenerateRequest:
        return GenerateRequest(request_id, model, prompt, max_output_tokens=2)

    economy_first = await registry.submit(request("e1", "economy"))
    premium_first = await registry.submit(request("p1", "premium"))
    economy_again = await registry.submit(request("e2", "llama-3.2-1b"))
    premium_other = await registry.submit(
        GenerateRequest("p2", "premium", f"{SYSTEM} other question", max_output_tokens=2)
    )

    # The premium model does not benefit from what the economy model has cached.
    assert economy_first.cached_tokens == 0
    assert premium_first.cached_tokens == 0
    assert economy_again.cached_tokens == 50
    assert premium_other.cached_tokens == 40

    caches = {}
    for name in registry.names:
        worker = registry.get(name)
        assert isinstance(worker, ContinuousBatchingWorker)
        caches[name] = _cache(worker)
    assert len({id(c) for c in caches.values()}) == 3

    economy, standard, premium = (caches[n].stats for n in ("economy", "standard", "premium"))
    assert (economy.lookups, economy.hits, economy.prompt_tokens, economy.cached_tokens) == (
        2,
        1,
        100,
        50,
    )
    assert (economy.hit_rate, economy.token_hit_rate) == (0.5, 0.5)
    assert (premium.lookups, premium.hits, premium.prompt_tokens, premium.cached_tokens) == (
        2,
        1,
        92,
        40,
    )
    assert (premium.hit_rate, premium.token_hit_rate) == (0.5, 40 / 92)
    assert (standard.lookups, standard.hit_rate, standard.token_hit_rate) == (0, 0.0, 0.0)
    assert caches["standard"].size_tokens == 0


async def test_registry_can_be_built_without_prefix_caches() -> None:
    registry = build_continuous_registry(SimClock(), seed=7, prefix_cache=False)
    for name in registry.names:
        worker = registry.get(name)
        assert isinstance(worker, ContinuousBatchingWorker)
        assert worker.prefix_cache is None


async def test_request_waiting_for_room_is_counted_once_in_the_stats() -> None:
    clock = SimClock()
    worker = _worker(clock, kv_capacity_tokens=120)
    cache = _cache(worker)

    # 52 KV tokens each and nothing cached: two fit, the third waits several iterations.
    await asyncio.gather(
        *(worker.submit(_request(f"r{i}", f"{SYSTEM} {_words(f'u{i}_', 10)}")) for i in range(3))
    )

    assert cache.stats.lookups == 3
    assert cache.stats.prompt_tokens == 150


async def test_baseline_reports_no_cached_tokens() -> None:
    worker = BaselineWorker(EXACT, SimulatedBackend(seed=1), SimClock())
    result = await worker.submit(_request("r1", SYSTEM))
    assert result.cached_tokens == 0
