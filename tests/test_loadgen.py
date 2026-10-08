from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable, Sequence

import pytest
from benchmarks.loadgen import (
    LengthBucket,
    LoadConfig,
    PlannedRequest,
    RequestOutcome,
    build_report,
    build_workload,
    in_process_client,
    run_in_process,
    run_workload,
    summarise,
)

from gateway.backends import SimulatedBackend
from gateway.clock import SimClock, WallClock
from gateway.continuous import ContinuousBatchingWorker, build_continuous_registry
from gateway.prefix_cache import PrefixCache
from gateway.profiles import ECONOMY, PREMIUM
from gateway.registry import ModelRegistry

SMALL = LoadConfig(seed=7, total_requests=60, arrival_rate_per_s=20, max_tokens=8)


# --- workload ------------------------------------------------------------------------------


def test_workload_is_a_pure_function_of_the_config() -> None:
    assert build_workload(SMALL) == build_workload(SMALL)
    assert build_workload(SMALL) != build_workload(dataclasses.replace(SMALL, seed=8))


def test_workload_has_the_requested_size_and_increasing_arrivals() -> None:
    planned = build_workload(SMALL)
    assert [p.index for p in planned] == list(range(60))
    assert all(p.at_ms > 0 for p in planned)
    assert [p.at_ms for p in planned] == sorted(p.at_ms for p in planned)
    assert all(p.max_tokens == 8 for p in planned)


def test_arrivals_follow_the_configured_mean_rate() -> None:
    config = LoadConfig(seed=3, total_requests=5000, arrival_rate_per_s=50)
    planned = build_workload(config)
    mean_gap_ms = planned[-1].at_ms / len(planned)
    assert mean_gap_ms == pytest.approx(20.0, rel=0.05)  # 1000 ms / 50 per second


def test_models_are_chosen_in_proportion_to_their_weights() -> None:
    config = LoadConfig(seed=3, total_requests=5000, model_weights={"a": 70, "b": 20, "c": 10})
    planned = build_workload(config)
    share = {m: sum(1 for p in planned if p.model == m) / len(planned) for m in "abc"}
    assert share["a"] == pytest.approx(0.70, abs=0.02)
    assert share["b"] == pytest.approx(0.20, abs=0.02)
    assert share["c"] == pytest.approx(0.10, abs=0.02)


def test_model_with_zero_weight_gets_no_requests() -> None:
    config = LoadConfig(total_requests=200, model_weights={"a": 1, "b": 0})
    assert {p.model for p in build_workload(config)} == {"a"}


def test_prompt_lengths_come_from_the_configured_buckets() -> None:
    config = LoadConfig(
        seed=3,
        total_requests=2000,
        prompt_lengths=(LengthBucket(5, 10, 3), LengthBucket(100, 120, 1)),
        shared_prefix_fraction=0.0,
    )
    lengths = [len(p.prompt.split()) for p in build_workload(config)]
    short = [n for n in lengths if n <= 10]
    long = [n for n in lengths if n >= 100]

    assert len(short) + len(long) == len(lengths)
    assert min(short) == 5
    assert max(short) == 10
    assert min(long) == 100
    assert max(long) == 120
    assert len(short) / len(lengths) == pytest.approx(0.75, abs=0.03)


@pytest.mark.parametrize(("fraction", "expected"), [(0.0, 0.0), (1.0, 1.0), (0.3, 0.3)])
def test_shared_prefix_fraction_controls_how_many_prompts_share_the_system_prompt(
    fraction: float, expected: float
) -> None:
    config = LoadConfig(
        seed=3,
        total_requests=2000,
        prompt_lengths=(LengthBucket(5, 10, 1),),
        shared_prefix_fraction=fraction,
        shared_prefix_words=40,
    )
    planned = build_workload(config)
    prefix = " ".join(f"sys{j}" for j in range(40)) + " "
    sharing = [p for p in planned if p.shares_prefix]

    assert len(sharing) / len(planned) == pytest.approx(expected, abs=0.03)
    assert all(p.prompt.startswith(prefix) for p in sharing)
    assert all(45 <= len(p.prompt.split()) <= 50 for p in sharing)
    assert not any(p.prompt.startswith("sys0 ") for p in planned if not p.shares_prefix)


def test_requests_share_no_text_except_the_system_prompt() -> None:
    planned = build_workload(dataclasses.replace(SMALL, shared_prefix_fraction=0.0))
    first_words = [p.prompt.split()[0] for p in planned]
    assert len(set(first_words)) == len(planned)


@pytest.mark.parametrize(
    "overrides",
    [
        {"total_requests": 0},
        {"arrival_rate_per_s": 0.0},
        {"model_weights": {}},
        {"model_weights": {"a": 0}},
        {"model_weights": {"a": 1, "b": -1}},
        {"prompt_lengths": ()},
        {"shared_prefix_fraction": 1.5},
        {"shared_prefix_fraction": -0.1},
        {"shared_prefix_words": 0},
        {"max_tokens": 0},
    ],
    ids=lambda o: f"{next(iter(o))}={next(iter(o.values()))}",
)
def test_invalid_config_is_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"must|need"):
        dataclasses.replace(SMALL, **overrides)  # type: ignore[arg-type]


@pytest.mark.parametrize(("lo", "hi", "weight"), [(0, 5, 1.0), (6, 5, 1.0), (1, 5, 0.0)])
def test_invalid_length_bucket_is_rejected(lo: int, hi: int, weight: float) -> None:
    with pytest.raises(ValueError, match=r"must|need"):
        LengthBucket(lo, hi, weight)


# --- running -------------------------------------------------------------------------------


async def test_in_process_run_reports_every_model_and_every_request() -> None:
    report = await run_in_process(SMALL)

    assert list(report.models) == ["economy", "standard", "premium"]
    assert sum(m.requests for m in report.models.values()) == 60
    assert report.overall.requests == 60
    assert report.overall.errors == 0
    assert report.overall.cost_usd == pytest.approx(sum(m.cost_usd for m in report.models.values()))
    for model in report.models.values():
        assert model.requests > 0
        assert model.ttft_p50_ms is not None
        assert model.ttft_p99_ms is not None
        assert model.total_p50_ms is not None
        assert model.total_p99_ms is not None
        assert 0 < model.ttft_p50_ms <= model.ttft_p99_ms <= model.total_p99_ms
        assert model.total_p50_ms <= model.total_p99_ms
        assert model.throughput_rps is not None
        assert model.throughput_rps > 0
        assert model.cost_usd > 0


async def test_same_seed_gives_byte_identical_results() -> None:
    first = (await run_in_process(SMALL)).to_json().encode()
    second = (await run_in_process(SMALL)).to_json().encode()
    other_seed = (await run_in_process(dataclasses.replace(SMALL, seed=8))).to_json().encode()

    assert first == second
    assert first != other_seed
    # And it really is the full report, not an empty shell.
    decoded = json.loads(first)
    assert decoded["config"]["seed"] == 7
    assert set(decoded["models"]) == {"economy", "standard", "premium"}
    assert decoded["overall"]["requests"] == 60


async def test_same_seed_gives_identical_per_request_outcomes() -> None:
    async def run() -> list[RequestOutcome]:
        clock = SimClock()
        registry = build_continuous_registry(clock, seed=SMALL.seed)
        async with in_process_client(registry, clock) as client:
            return await run_workload(build_workload(SMALL), client, clock)

    assert await run() == await run()


async def test_cache_hit_rate_follows_the_shared_prefix_fraction() -> None:
    base = dataclasses.replace(
        SMALL,
        model_weights={"economy": 1},
        prompt_lengths=(LengthBucket(10, 20, 1),),
        shared_prefix_words=100,
    )

    none_shared = await run_in_process(dataclasses.replace(base, shared_prefix_fraction=0.0))
    all_shared = await run_in_process(dataclasses.replace(base, shared_prefix_fraction=1.0))

    cold = none_shared.models["economy"]
    warm = all_shared.models["economy"]
    assert (cold.cache_hit_rate, cold.cache_token_hit_rate) == (0.0, 0.0)
    assert warm.cache_hit_rate is not None
    assert warm.cache_token_hit_rate is not None
    # Everything after the first requests to finish reuses the 100-word system prompt,
    # which is most of each 110-120 word prompt.
    assert warm.cache_hit_rate > 0.8
    assert 0.5 < warm.cache_token_hit_rate < 1.0


async def test_arrivals_are_sent_at_their_planned_times() -> None:
    clock = SimClock()
    registry = build_continuous_registry(clock, seed=7)
    planned = build_workload(dataclasses.replace(SMALL, total_requests=10))
    async with in_process_client(registry, clock) as client:
        outcomes = await run_workload(planned, client, clock)

    for plan, outcome in zip(planned, outcomes, strict=True):
        assert outcome.index == plan.index
        assert outcome.at_ms == plan.at_ms
        # The server saw the request at its arrival time: done = arrival + server latency.
        assert outcome.done_ms == pytest.approx(plan.at_ms + outcome.total_ms)


async def test_failed_requests_are_counted_as_errors_not_latencies() -> None:
    clock = SimClock()
    registry = build_continuous_registry(clock, seed=7)
    planned = [
        PlannedRequest(0, 1.0, "economy", "hello there", 4, False),
        PlannedRequest(1, 2.0, "no-such-model", "hello there", 4, False),
    ]
    async with in_process_client(registry, clock) as client:
        outcomes = await run_workload(planned, client, clock)

    assert [o.status for o in outcomes] == [200, 404]
    report = build_report(
        LoadConfig(total_requests=2, model_weights={"economy": 1, "no-such-model": 1}), outcomes
    )
    assert (report.models["economy"].requests, report.models["economy"].errors) == (1, 0)
    failed = report.models["no-such-model"]
    assert (failed.requests, failed.errors) == (1, 1)
    assert failed.total_p99_ms is None
    assert failed.throughput_rps is None
    assert (report.overall.requests, report.overall.errors) == (2, 1)


async def test_runs_on_wall_clock_too() -> None:
    clock = WallClock()
    registry = build_continuous_registry(clock, seed=7)
    config = LoadConfig(
        total_requests=4,
        arrival_rate_per_s=500,
        model_weights={"economy": 1},
        prompt_lengths=(LengthBucket(3, 5, 1),),
        shared_prefix_fraction=0.0,
        max_tokens=1,
    )
    async with in_process_client(registry, clock) as client:
        outcomes = await run_workload(build_workload(config), client, clock)

    assert [o.status for o in outcomes] == [200] * 4
    assert all(o.total_ms > 0 for o in outcomes)
    assert all(o.done_ms >= o.at_ms for o in outcomes)


def test_report_table_has_a_row_per_model_and_a_total() -> None:
    outcomes = [
        RequestOutcome(0, "economy", 0.0, 100.0, 200, 0.0, 10.0, 100.0, 50, 25, 8, 0.001),
        RequestOutcome(1, "premium", 5.0, 5.0, 404),
    ]
    config = LoadConfig(total_requests=2, model_weights={"economy": 1, "premium": 1})
    table = build_report(config, outcomes).to_table().splitlines()

    assert [line.split()[0] for line in table] == ["model", "economy", "premium", "ALL"]
    assert " - " in table[2]  # no latencies for a model whose only request failed


def test_summary_math() -> None:
    outcomes = [
        RequestOutcome(0, "m", 0.0, 1000.0, 200, 0.0, 10.0, 1000.0, 100, 0, 20, 0.001),
        RequestOutcome(1, "m", 500.0, 2000.0, 200, 0.0, 30.0, 1500.0, 100, 60, 30, 0.002),
        RequestOutcome(2, "m", 600.0, 600.0, 413),
    ]
    summary = summarise(outcomes)

    assert (summary.requests, summary.errors) == (3, 1)
    assert (summary.ttft_p50_ms, summary.ttft_p99_ms) == (10.0, 30.0)
    assert (summary.total_p50_ms, summary.total_p99_ms) == (1000.0, 1500.0)
    assert summary.throughput_rps == 2 / 2.0  # 2 ok requests over 0..2000 ms
    assert summary.completion_tokens_per_s == 50 / 2.0
    assert summary.cache_hit_rate == 0.5
    assert summary.cache_token_hit_rate == 60 / 200
    assert summary.cost_usd == 0.003


# --- fairness ------------------------------------------------------------------------------

# 98% of a 200 req/s stream goes to the economy model, far more than it can serve, while the
# premium model gets about 4 req/s. Prompts are 20-80 words and outputs 16 tokens.
FAIRNESS = LoadConfig(
    seed=7,
    total_requests=1000,
    arrival_rate_per_s=200,
    model_weights={"economy": 98, "premium": 2},
    prompt_lengths=(LengthBucket(20, 80, 1.0),),
    shared_prefix_fraction=0.0,
    max_tokens=16,
)
# The slowest a single request can be on an idle model: longest prompt, then 16 decode steps.
ECONOMY_WORST_ALONE_MS = 80 * ECONOMY.prefill_ms_per_token + 16 * ECONOMY.decode_base_ms
PREMIUM_WORST_ALONE_MS = 80 * PREMIUM.prefill_ms_per_token + 16 * PREMIUM.decode_base_ms
# The bound on the light model's p99: twice its slowest unloaded request.
PREMIUM_P99_BOUND_MS = 2 * PREMIUM_WORST_ALONE_MS


def _separate_workers(clock: SimClock) -> ModelRegistry:
    return build_continuous_registry(clock, seed=FAIRNESS.seed)


def _one_shared_queue(clock: SimClock) -> ModelRegistry:
    """Both model names feed a single worker, so all requests wait in one queue."""
    worker = ContinuousBatchingWorker(
        ECONOMY,
        SimulatedBackend(seed=FAIRNESS.seed),
        clock,
        prefix_cache=PrefixCache(ECONOMY.kv_capacity_tokens),
    )
    registry = ModelRegistry()
    registry.register("economy", worker, aliases=["premium"])
    return registry


async def _run(
    planned: Sequence[PlannedRequest], make_registry: Callable[[SimClock], ModelRegistry]
) -> list[RequestOutcome]:
    clock = SimClock()
    async with in_process_client(make_registry(clock), clock) as client:
        return await run_workload(planned, client, clock)


def _timings(outcomes: Sequence[RequestOutcome], model: str) -> list[tuple[int, float, float]]:
    return [(o.index, o.ttft_ms, o.total_ms) for o in outcomes if o.model == model]


async def test_saturated_model_does_not_starve_a_lightly_loaded_one() -> None:
    planned = build_workload(FAIRNESS)
    outcomes = await _run(planned, _separate_workers)
    economy = summarise([o for o in outcomes if o.model == "economy"])
    premium = summarise([o for o in outcomes if o.model == "premium"])

    assert economy.errors == premium.errors == 0
    assert premium.requests >= 20
    # The heavy model is saturated: its p99 is over 30 times its slowest unloaded request.
    assert economy.total_p99_ms is not None
    assert economy.total_p99_ms > 30 * ECONOMY_WORST_ALONE_MS
    # The light model is not affected: its p99 stays within the bound.
    assert premium.total_p99_ms is not None
    assert premium.total_p99_ms <= PREMIUM_P99_BOUND_MS

    # Stronger: the light model's requests are served exactly as if the heavy load did not
    # exist. Remove every economy request and nothing about the premium ones changes.
    premium_only = await _run([p for p in planned if p.model == "premium"], _separate_workers)
    assert _timings(outcomes, "premium") == _timings(premium_only, "premium")


async def test_a_single_shared_queue_breaks_that_isolation() -> None:
    planned = build_workload(FAIRNESS)
    outcomes = await _run(planned, _one_shared_queue)
    premium = summarise([o for o in outcomes if o.model == "premium"])

    # Same workload, but the light model's requests now wait behind the heavy model's
    # backlog. The shared worker even runs on the faster economy profile, and the light
    # model's p99 still breaks the bound it met with its own queue.
    assert premium.errors == 0
    assert premium.total_p99_ms is not None
    assert premium.total_p99_ms > PREMIUM_P99_BOUND_MS
