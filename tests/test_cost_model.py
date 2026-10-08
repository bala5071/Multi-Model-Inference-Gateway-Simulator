from __future__ import annotations

import dataclasses
import itertools

import pytest

from gateway.cost_model import CostEstimate, OutputLengthGenerator, estimate_cost
from gateway.profiles import ECONOMY, PREMIUM, PROFILES, STANDARD, ModelProfile, Tier

ALL_PROFILES = (ECONOMY, STANDARD, PREMIUM)
PROMPTS = ("", "hello", "Summarise this contract.", "héllo wörld", "a" * 5000)


def _estimate(
    profile: ModelProfile,
    new_prefill_tokens: int = 500,
    output_tokens: int = 100,
    batch_size: int = 1,
) -> CostEstimate:
    return estimate_cost(
        profile,
        new_prefill_tokens=new_prefill_tokens,
        output_tokens=output_tokens,
        batch_size=batch_size,
    )


# --- determinism ---------------------------------------------------------------------------


def test_output_length_same_seed_same_lengths() -> None:
    first = OutputLengthGenerator(seed=7)
    second = OutputLengthGenerator(seed=7)
    assert [first.length_for(p) for p in PROMPTS] == [second.length_for(p) for p in PROMPTS]


def test_output_length_is_independent_of_call_order() -> None:
    generator = OutputLengthGenerator(seed=7)
    forward = {p: generator.length_for(p) for p in PROMPTS}
    backward = {p: generator.length_for(p) for p in reversed(PROMPTS)}
    assert forward == backward


def test_output_length_is_stable_across_processes() -> None:
    # Pinned values: these fail if the derivation starts depending on process state
    # (for example the builtin hash(), which varies with PYTHONHASHSEED).
    generator = OutputLengthGenerator(seed=7)
    assert [generator.length_for(p) for p in ("", "hello", "Summarise this contract.")] == [
        17,
        18,
        207,
    ]


def test_output_length_depends_on_seed_and_prompt() -> None:
    assert OutputLengthGenerator(seed=7).length_for("hello") == 18
    assert OutputLengthGenerator(seed=8).length_for("hello") == 74
    lengths = {OutputLengthGenerator(seed=7).length_for(f"prompt {i}") for i in range(50)}
    assert len(lengths) > 10


def test_output_length_stays_in_bounds() -> None:
    generator = OutputLengthGenerator(seed=3, min_tokens=5, max_tokens=9)
    lengths = {generator.length_for(f"prompt {i}") for i in range(500)}
    assert lengths == {5, 6, 7, 8, 9}


def test_output_length_fixed_when_bounds_are_equal() -> None:
    generator = OutputLengthGenerator(seed=3, min_tokens=42, max_tokens=42)
    assert generator.length_for("anything") == 42


@pytest.mark.parametrize(("min_tokens", "max_tokens"), [(0, 10), (10, 9)])
def test_output_length_rejects_bad_bounds(min_tokens: int, max_tokens: int) -> None:
    with pytest.raises(ValueError, match="tokens"):
        OutputLengthGenerator(seed=1, min_tokens=min_tokens, max_tokens=max_tokens)


@pytest.mark.parametrize("profile", ALL_PROFILES, ids=lambda p: p.tier.value)
def test_estimate_is_deterministic(profile: ModelProfile) -> None:
    assert _estimate(profile, batch_size=4) == _estimate(profile, batch_size=4)


def test_estimate_known_values() -> None:
    estimate = _estimate(STANDARD, new_prefill_tokens=1000, output_tokens=500, batch_size=5)
    assert estimate.prefill_ms == pytest.approx(150.0)
    assert estimate.decode_step_ms == pytest.approx(14.4)
    assert estimate.cost_usd == pytest.approx(0.0006)


# --- monotonicity --------------------------------------------------------------------------


@pytest.mark.parametrize("profile", ALL_PROFILES, ids=lambda p: p.tier.value)
def test_more_prefill_tokens_cost_more_time_and_money(profile: ModelProfile) -> None:
    estimates = [_estimate(profile, new_prefill_tokens=n) for n in (0, 1, 100, 4000)]
    for smaller, larger in itertools.pairwise(estimates):
        assert larger.prefill_ms > smaller.prefill_ms
        assert larger.cost_usd > smaller.cost_usd
        assert larger.decode_step_ms == smaller.decode_step_ms


@pytest.mark.parametrize("profile", ALL_PROFILES, ids=lambda p: p.tier.value)
def test_more_output_tokens_cost_more_time_and_money(profile: ModelProfile) -> None:
    token_counts = (0, 1, 100, 4000)
    estimates = [_estimate(profile, output_tokens=n) for n in token_counts]
    # One decode step per output token, so total decode time is steps * step time.
    decode_totals = [n * e.decode_step_ms for n, e in zip(token_counts, estimates, strict=True)]
    for i in range(len(estimates) - 1):
        assert decode_totals[i + 1] > decode_totals[i]
        assert estimates[i + 1].cost_usd > estimates[i].cost_usd
        assert estimates[i + 1].prefill_ms == estimates[i].prefill_ms


def test_zero_tokens_cost_nothing() -> None:
    estimate = _estimate(PREMIUM, new_prefill_tokens=0, output_tokens=0)
    assert estimate.prefill_ms == 0
    assert estimate.cost_usd == 0


@pytest.mark.parametrize(
    ("new_prefill_tokens", "output_tokens"),
    [(-1, 10), (10, -1)],
)
def test_negative_token_counts_are_rejected(new_prefill_tokens: int, output_tokens: int) -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        _estimate(ECONOMY, new_prefill_tokens=new_prefill_tokens, output_tokens=output_tokens)


# --- batch-size effect ---------------------------------------------------------------------


@pytest.mark.parametrize("profile", ALL_PROFILES, ids=lambda p: p.tier.value)
def test_larger_batch_slows_each_step_but_raises_throughput(profile: ModelProfile) -> None:
    sizes = range(1, profile.max_batch_size + 1)
    step_ms = [_estimate(profile, batch_size=b).decode_step_ms for b in sizes]
    ms_per_token = [step / b for step, b in zip(step_ms, sizes, strict=True)]

    assert step_ms[0] == profile.decode_base_ms
    for i in range(len(step_ms) - 1):
        assert step_ms[i + 1] > step_ms[i]
        assert ms_per_token[i + 1] < ms_per_token[i]


@pytest.mark.parametrize("profile", ALL_PROFILES, ids=lambda p: p.tier.value)
def test_batch_size_does_not_change_prefill_or_price(profile: ModelProfile) -> None:
    alone = _estimate(profile, batch_size=1)
    full = _estimate(profile, batch_size=profile.max_batch_size)
    assert full.prefill_ms == alone.prefill_ms
    assert full.cost_usd == alone.cost_usd


@pytest.mark.parametrize("profile", ALL_PROFILES, ids=lambda p: p.tier.value)
def test_batch_size_outside_profile_limits_is_rejected(profile: ModelProfile) -> None:
    for batch_size in (0, -1, profile.max_batch_size + 1):
        with pytest.raises(ValueError, match="batch_size"):
            _estimate(profile, batch_size=batch_size)


# --- the three profiles --------------------------------------------------------------------


def test_profiles_cover_every_tier_once() -> None:
    assert set(PROFILES) == set(Tier)
    assert [PROFILES[tier] for tier in Tier] == list(ALL_PROFILES)
    assert len({p.name for p in ALL_PROFILES}) == len(ALL_PROFILES)


def test_profiles_are_ordered_on_every_axis() -> None:
    for cheaper, pricier in ((ECONOMY, STANDARD), (STANDARD, PREMIUM)):
        assert pricier.price_per_1k_input_tokens > cheaper.price_per_1k_input_tokens
        assert pricier.price_per_1k_output_tokens > cheaper.price_per_1k_output_tokens
        assert pricier.prefill_ms_per_token > cheaper.prefill_ms_per_token
        assert pricier.decode_base_ms > cheaper.decode_base_ms
        assert pricier.decode_ms_per_extra_seq > cheaper.decode_ms_per_extra_seq
        assert pricier.max_batch_size < cheaper.max_batch_size
        assert pricier.kv_capacity_tokens < cheaper.kv_capacity_tokens


def test_same_workload_gives_distinct_estimates_per_profile() -> None:
    economy, standard, premium = (_estimate(p, batch_size=4) for p in ALL_PROFILES)
    assert economy.prefill_ms < standard.prefill_ms < premium.prefill_ms
    assert economy.decode_step_ms < standard.decode_step_ms < premium.decode_step_ms
    assert economy.cost_usd < standard.cost_usd < premium.cost_usd


def test_profiles_differ_in_shape_not_only_in_scale() -> None:
    # If premium were just "economy times k", the batching penalty relative to the base
    # step would be the same for every tier. It should be steeper for the larger model.
    penalties = [p.decode_ms_per_extra_seq / p.decode_base_ms for p in ALL_PROFILES]
    assert penalties[0] < penalties[1] < penalties[2]

    # Best-case decode throughput (full batch) should also separate the tiers clearly.
    best_ms_per_token = [
        _estimate(p, batch_size=p.max_batch_size).decode_step_ms / p.max_batch_size
        for p in ALL_PROFILES
    ]
    assert best_ms_per_token[1] > 2 * best_ms_per_token[0]
    assert best_ms_per_token[2] > 2 * best_ms_per_token[1]


def test_profiles_are_immutable() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        ECONOMY.max_batch_size = 64  # type: ignore[misc]
    with pytest.raises(TypeError):
        PROFILES[Tier.ECONOMY] = PREMIUM  # type: ignore[index]


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": ""},
        {"price_per_1k_input_tokens": 0.0},
        {"price_per_1k_output_tokens": -1.0},
        {"prefill_ms_per_token": 0.0},
        {"decode_base_ms": 0.0},
        {"decode_ms_per_extra_seq": -0.1},
        {"per_request_overhead_ms": -1.0},
        {"max_batch_size": 0},
        {"kv_capacity_tokens": 0},
    ],
    ids=lambda o: next(iter(o)),
)
def test_invalid_profile_values_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="must"):
        dataclasses.replace(ECONOMY, **overrides)  # type: ignore[arg-type]


def test_illustrative_profiles_have_no_per_request_overhead() -> None:
    assert [p.per_request_overhead_ms for p in PROFILES.values()] == [0.0, 0.0, 0.0]


def test_estimate_carries_the_per_request_overhead_and_does_not_bill_it() -> None:
    slow_start = dataclasses.replace(ECONOMY, per_request_overhead_ms=90.0)
    plain = estimate_cost(ECONOMY, new_prefill_tokens=100, output_tokens=10, batch_size=1)
    delayed = estimate_cost(slow_start, new_prefill_tokens=100, output_tokens=10, batch_size=1)

    assert plain.overhead_ms == 0.0
    assert delayed == dataclasses.replace(plain, overhead_ms=90.0)
