"""Generate docs/REPORT.md and its figures.

    uv run python -m benchmarks.report              # everything (same as `make report`)
    uv run python -m benchmarks.report --only batching   # one section, printed

Sections 1 to 5 run the simulator on simulated time, so the same code gives the same
numbers every time. Section 6 summarises the calibration files in benchmarks/calibration/,
which are measurements of real models and are only re-measured by benchmarks/calibrate.py.
Sections 1 and 2 also show a variant on the calibrated profiles: the file named by
GATEWAY_PROFILES, or benchmarks/calibration/calibrated_profiles.json without it.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import random
import subprocess
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from benchmarks import chunked_prefill, overload
from benchmarks.calibrate import spread_across_runs
from benchmarks.loadgen import LengthBucket, LoadConfig, build_workload
from benchmarks.plotstyle import (
    INK,
    MARKERS,
    SERIES,
    SURFACE,
    legend,
    style_axes,
)
from gateway.app import create_app
from gateway.backends import SimulatedBackend
from gateway.baseline import BaselineWorker
from gateway.clock import SimClock
from gateway.continuous import ContinuousBatchingWorker, build_continuous_registry
from gateway.cost_model import OutputLengthGenerator
from gateway.metrics import percentile
from gateway.overload import OverloadPolicy
from gateway.prefix_cache import PrefixCache
from gateway.profiles import PROFILES, ModelProfile, Tier, load_calibrated_profiles
from gateway.registry import Worker
from gateway.routing import load_plans
from gateway.schema import GenerateRequest, RequestResult

SEED = 7
REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"
FIGURES = DOCS / "report"
CALIBRATION = REPO / "benchmarks" / "calibration"


@dataclass(frozen=True, slots=True)
class Section:
    """One part of the report: its heading, its Markdown body and nothing else."""

    title: str
    body: str


def _table(header: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    def cell(value: object) -> str:
        if isinstance(value, float):
            return f"{value:,.1f}" if abs(value) >= 100 else f"{value:,.2f}"
        return str(value)

    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---:" for _ in header) + "|"]
    lines.extend("| " + " | ".join(cell(v) for v in row) + " |" for row in rows)
    return "\n".join(lines)


def _save(fig: Any, name: str) -> str:
    FIGURES.mkdir(parents=True, exist_ok=True)
    fig.savefig(FIGURES / name, dpi=120, facecolor=SURFACE)
    plt.close(fig)
    return f"![{name}](report/{name})"


async def _run_arrivals(
    worker: Worker, clock: SimClock, arrivals: Sequence[tuple[float, GenerateRequest]]
) -> list[RequestResult]:
    async def arrive(at_ms: float, request: GenerateRequest) -> RequestResult:
        await clock.sleep_ms(at_ms)
        return await worker.submit(request)

    return list(await asyncio.gather(*(arrive(at, r) for at, r in arrivals)))


def _throughput(results: Sequence[RequestResult]) -> float:
    span_ms = max(r.finish_ms for r in results) - min(r.enqueue_ms for r in results)
    return 1000 * len(results) / span_ms


@dataclass(frozen=True, slots=True)
class Calibrated:
    """The calibration the calibrated variants of sections 1 and 2 run on."""

    # Where it was read from, as shown in the report.
    source: str
    # Only the tiers the file has; timing from the file, everything else illustrative.
    profiles: dict[Tier, ModelProfile]
    # The same, with batches held to the highest concurrency that was measured.
    capped: dict[Tier, ModelProfile]
    measured_concurrency: dict[Tier, int]
    # Tiers whose fitted cost per extra sequence the calibration marks unstable.
    unstable_batching_cost: tuple[Tier, ...] = ()


def calibrated_profiles() -> Calibrated | None:
    """The calibration file named by ``GATEWAY_PROFILES``, else the one in the repository.

    None when there is no such file. The simulator's own default is unchanged: this is
    only what the report shows beside the illustrative results.
    """
    named = os.environ.get("GATEWAY_PROFILES")
    path = Path(named) if named else CALIBRATION / "calibrated_profiles.json"
    if not path.exists():
        return None
    loaded = load_calibrated_profiles(path)
    entries = json.loads(path.read_text(encoding="utf-8"))["profiles"]
    measured = {
        Tier(name): int(entry.get("max_concurrency_measured", PROFILES[Tier(name)].max_batch_size))
        for name, entry in entries.items()
    }
    profiles = {tier: loaded[tier] for tier in PROFILES if tier in measured}
    capped = {
        tier: dataclasses.replace(
            profile, max_batch_size=min(profile.max_batch_size, measured[tier])
        )
        for tier, profile in profiles.items()
    }
    try:
        source = path.resolve().relative_to(REPO).as_posix()
    except ValueError:
        source = path.name
    unstable = tuple(
        tier
        for tier in profiles
        if "decode_ms_per_extra_seq" in entries[tier.value].get("unstable_parameters", [])
    )
    return Calibrated(source, profiles, capped, measured, unstable)


# --- 1. baseline vs continuous batching ----------------------------------------------------

LOAD_LEVELS = (0.5, 1.0, 2.0, 4.0, 8.0, 16.0)
BATCHING_REQUESTS = 300
Curves = dict[Tier, dict[str, list[tuple[float, float]]]]


def _batching_arrivals(profile: ModelProfile, load: float) -> list[tuple[float, GenerateRequest]]:
    """Poisson arrivals at ``load`` times the rate the baseline can sustain on this model."""
    rng = random.Random(SEED)
    lengths = OutputLengthGenerator(seed=SEED)
    prompts = [f"request {i} " + "word " * rng.randint(18, 398) for i in range(BATCHING_REQUESTS)]
    service_ms = [
        len(p.split()) * profile.prefill_ms_per_token
        + lengths.length_for(p) * profile.decode_base_ms
        for p in prompts
    ]
    mean_gap_ms = sum(service_ms) / len(service_ms) / load
    arrivals = []
    at_ms = 0.0
    for i, prompt in enumerate(prompts):
        arrivals.append((at_ms, GenerateRequest(f"r{i}", profile.tier.value, prompt)))
        at_ms += round(rng.expovariate(1 / mean_gap_ms), 6)
    return arrivals


async def _batching_runs(
    profiles: Mapping[Tier, ModelProfile], loads: Sequence[float] = LOAD_LEVELS
) -> tuple[list[list[object]], Curves]:
    """Both workers on identical arrivals, per tier and load: table rows and the curves."""
    rows = []
    curves: Curves = {}
    for tier, profile in profiles.items():
        curves[tier] = {"baseline": [], "continuous": []}
        for load in loads:
            arrivals = _batching_arrivals(profile, load)
            row: list[object] = [tier.value, f"{load:g}x"]
            for name, make in (
                ("baseline", BaselineWorker),
                ("continuous", ContinuousBatchingWorker),
            ):
                clock = SimClock()
                results = await _run_arrivals(
                    make(profile, SimulatedBackend(seed=SEED), clock), clock, arrivals
                )
                throughput = _throughput(results)
                p99 = percentile([r.latency_ms for r in results], 99)
                curves[tier][name].append((throughput, p99))
                row.extend([throughput, p99])
            rows.append(row)
    return rows, curves


def _batching_figure(curves: Curves, name: str) -> str:
    fig, axes = plt.subplots(2, len(curves), figsize=(13, 7), facecolor=SURFACE, squeeze=False)
    for column, tier in enumerate(curves):
        for index, worker in enumerate(("continuous", "baseline")):
            points = curves[tier][worker]
            for row_index, values in enumerate(([p[0] for p in points], [p[1] for p in points])):
                axes[row_index][column].plot(
                    LOAD_LEVELS,
                    values,
                    color=SERIES[index],
                    marker=MARKERS[index],
                    linewidth=2,
                    markersize=6,
                    label=worker,
                )
        style_axes(
            axes[0][column],
            title=f"{tier.value}: throughput",
            xlabel="Offered load (x baseline capacity)",
            ylabel="Requests per second",
        )
        style_axes(
            axes[1][column],
            title=f"{tier.value}: p99 total latency",
            xlabel="Offered load (x baseline capacity)",
            ylabel="p99 latency (ms, log scale)",
        )
        for row_index in (0, 1):
            axes[row_index][column].set_xscale("log", base=2)
            axes[row_index][column].set_xticks(LOAD_LEVELS, [f"{x:g}" for x in LOAD_LEVELS])
        axes[1][column].set_yscale("log")
    legend(axes[0][0], loc="upper left")
    fig.tight_layout()
    return _save(fig, name)


def _peak_gain(curves: Curves) -> dict[Tier, float]:
    """Continuous over baseline requests per second at the highest load that was run."""
    return {
        tier: by_worker["continuous"][-1][0] / by_worker["baseline"][-1][0]
        for tier, by_worker in curves.items()
    }


@dataclass(frozen=True, slots=True)
class BatchingGains:
    """Throughput gain of continuous batching over the baseline at the top load level."""

    illustrative: dict[Tier, float]
    calibrated: dict[Tier, float]
    capped: dict[Tier, float]
    measured_concurrency: dict[Tier, int]
    unstable_batching_cost: tuple[Tier, ...] = ()


async def batching_gains() -> BatchingGains | None:
    """The gains at the top load level only. None when there is no calibration."""
    found = calibrated_profiles()
    if found is None:
        return None
    top = (LOAD_LEVELS[-1],)
    return BatchingGains(
        illustrative=_peak_gain((await _batching_runs(PROFILES, top))[1]),
        calibrated=_peak_gain((await _batching_runs(found.profiles, top))[1]),
        capped=_peak_gain((await _batching_runs(found.capped, top))[1]),
        measured_concurrency=found.measured_concurrency,
        unstable_batching_cost=found.unstable_batching_cost,
    )


def _unstable_warning(tiers: Sequence[Tier]) -> str:
    """A sentence for results that rest on a cost per extra sequence that did not repeat."""
    if not tiers:
        return ""
    names = ", ".join(tier.value for tier in tiers)
    return (
        f" **The calibration marks the cost per extra sequence unstable on {names}**: it did "
        "not repeat between calibration runs (section 6), so these gains would differ run by "
        "run."
    )


_BATCHING_HEADER = (
    "Tier",
    "Load",
    "Baseline req/s",
    "Baseline p99 ms",
    "Continuous req/s",
    "Continuous p99 ms",
)


async def _calibrated_batching(illustrative: Curves) -> str:
    found = calibrated_profiles()
    if found is None:
        return """### With calibrated profiles

No calibration file was found, so there is no calibrated variant of this section. See
section 6."""
    rows, curves = await _batching_runs(found.profiles)
    figure = _batching_figure(curves, "1-batching-calibrated.png")
    capped = _peak_gain((await _batching_runs(found.capped, (LOAD_LEVELS[-1],)))[1])
    gains = (_peak_gain(illustrative), _peak_gain(curves), capped)
    gain_rows = [
        (
            tier.value,
            PROFILES[tier].max_batch_size,
            f"{gains[0][tier]:.1f}x",
            f"{gains[1][tier]:.1f}x",
            found.measured_concurrency[tier],
            f"{gains[2][tier]:.1f}x",
        )
        for tier in found.profiles
    ]
    top = f"{LOAD_LEVELS[-1]:g}x"
    return f"""### With calibrated profiles

The same runs with the timing parameters fitted to real models (`{found.source}`,
section 6) in place of the illustrative ones. **Still simulated**: this is this repo's scheduler
with measured step costs, not a measurement of a real server's batching. "Load" is relative to the
baseline's capacity on the same profile, so compare the shape and the gain, not the requests
per second, with the table above.

{figure}

{_table(_BATCHING_HEADER, rows)}

Throughput gain of continuous batching over the baseline at {top} load (continuous req/s
divided by baseline req/s):

{
        _table(
            (
                "Tier",
                "Batch limit",
                "Illustrative",
                "Calibrated, same batch limit",
                "Largest batch measured",
                "Calibrated, batches held to that",
            ),
            gain_rows,
        )
    }

The calibrated gain is far smaller because each extra sequence in a batch was measured to cost
several times what the illustrative profiles assume. The middle column lets batches grow to the
illustrative limit, which extrapolates the fitted cost per extra sequence well past the
concurrency it was measured at; the last column stays inside the measured range and is the
safer figure. `per_request_overhead_ms` does not hold up the model, so it moves time to first
token and not these throughputs.{_unstable_warning(found.unstable_batching_cost)}"""


async def batching() -> Section:
    rows, curves = await _batching_runs(PROFILES)
    figure = _batching_figure(curves, "1-batching.png")

    body = f"""{BATCHING_REQUESTS} requests per run with prompts of 20 to 400 words, arriving as a
Poisson stream. "Load" is the arrival rate as a multiple of what the one-at-a-time baseline can
sustain on that model, so 1x is the baseline's limit. Both workers get identical arrivals. No
prefix cache.

{figure}

{_table(_BATCHING_HEADER, rows)}

Below 1x both keep up, so throughput is just the arrival rate and the difference is in latency.
Above 1x the baseline's throughput is pinned at its capacity and its queue grows for the whole
run, so its p99 mostly reflects how long the run was. Continuous batching keeps following the
arrival rate until its own limit, which is lower for the larger models: they have smaller batch
limits and a steeper cost per extra sequence.

{await _calibrated_batching(curves)}"""
    return Section("1. Baseline vs continuous batching", body)


# --- 2. prefix cache -----------------------------------------------------------------------

PREFIX_FRACTIONS = (0.0, 0.25, 0.5, 0.75, 1.0)


async def _prefix_runs(
    profile: ModelProfile,
) -> tuple[list[list[object]], dict[str, dict[str, list[float]]]]:
    """Cache off and on at each sharing level: table rows, and the series for the figure."""
    rows = []
    series: dict[str, dict[str, list[float]]] = {
        "cache on": {"ttft": [], "cost": []},
        "cache off": {"ttft": [], "cost": []},
    }
    for fraction in PREFIX_FRACTIONS:
        config = LoadConfig(
            seed=SEED,
            total_requests=300,
            arrival_rate_per_s=3.0,
            model_weights={"standard": 1},
            prompt_lengths=(LengthBucket(20, 80, 1.0),),
            shared_prefix_fraction=fraction,
            shared_prefix_words=400,
            max_tokens=32,
        )
        arrivals = [
            (p.at_ms, GenerateRequest(f"r{p.index}", p.model, p.prompt, p.max_tokens))
            for p in build_workload(config)
        ]
        row: list[object] = [f"{fraction:.0%}"]
        for name, cached in (("cache off", False), ("cache on", True)):
            clock = SimClock()
            worker = ContinuousBatchingWorker(
                profile,
                SimulatedBackend(seed=SEED),
                clock,
                prefix_cache=PrefixCache(profile.kv_capacity_tokens) if cached else None,
            )
            results = await _run_arrivals(worker, clock, arrivals)
            ttft = [r.time_to_first_token_ms for r in results]
            cost = sum(r.cost_usd for r in results)
            series[name]["ttft"].append(percentile(ttft, 50))
            series[name]["cost"].append(1000 * cost)
            row.extend([percentile(ttft, 50), percentile(ttft, 99), 1000 * cost])
        cached_share = sum(r.cached_tokens for r in results) / sum(r.tokens_in for r in results)
        rows.append([*row, f"{cached_share:.0%}"])
    return rows, series


async def _calibrated_prefix_cache(illustrative: Sequence[Sequence[object]]) -> str:
    found = calibrated_profiles()
    if found is None or Tier.STANDARD not in found.profiles:
        return """### With the calibrated profile

No calibration of the standard tier was found, so there is no calibrated variant of this
section. See section 6."""
    profile = found.profiles[Tier.STANDARD]
    illustrative_prefill = PROFILES[Tier.STANDARD].prefill_ms_per_token
    rows, _ = await _prefix_runs(profile)
    beside = [
        (ours[0], theirs[1], theirs[4], ours[1], ours[4], ours[2], ours[5])
        for theirs, ours in zip(illustrative, rows, strict=True)
    ]
    return f"""### With the calibrated profile

The same runs on the standard tier's calibrated timing (`{found.source}`, section 6).
Prefill is {profile.prefill_ms_per_token:.4f} ms per token against the illustrative {
        illustrative_prefill:g},
and every request has {
        profile.per_request_overhead_ms:.1f} ms of fixed overhead, which the cache does not
remove. **Still simulated.** Cost is not shown again: prices are not calibrated.

{
        _table(
            (
                "Sharing",
                "Illustrative off: TTFT p50",
                "Illustrative on: TTFT p50",
                "Calibrated off: TTFT p50",
                "Calibrated on: TTFT p50",
                "Calibrated off: TTFT p99",
                "Calibrated on: TTFT p99",
            ),
            beside,
        )
    }

The cache still holds time to first token at the no-sharing level, but that level is now mostly
the fixed overhead, so the cache removes a smaller share of the wait than the illustrative
table suggests."""


async def prefix_cache() -> Section:
    profile = PROFILES[Tier.STANDARD]
    rows, series = await _prefix_runs(profile)

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2), facecolor=SURFACE)
    xs = [100 * f for f in PREFIX_FRACTIONS]
    for index, name in enumerate(("cache on", "cache off")):
        for ax, key in zip(axes, ("ttft", "cost"), strict=True):
            ax.plot(
                xs,
                series[name][key],
                color=SERIES[index],
                marker=MARKERS[index],
                linewidth=2,
                markersize=6,
                label=name,
            )
    style_axes(
        axes[0],
        title="Median time to first token",
        xlabel="Requests sharing the system prompt (%)",
        ylabel="TTFT p50 (ms)",
    )
    style_axes(
        axes[1],
        title="Cost of the whole run",
        xlabel="Requests sharing the system prompt (%)",
        ylabel="Cost (thousandths of a USD)",
    )
    for ax in axes:
        ax.set_ylim(bottom=0)
    legend(axes[0], loc="upper left")
    fig.tight_layout()
    figure = _save(fig, "2-prefix-cache.png")

    body = f"""Standard tier, 300 requests at 3 req/s, each with its own 20 to 80 word prompt and 32
output tokens. A share of them also start with the same 400-word system prompt. Cost is in
thousandths of a USD (illustrative prices).

{figure}

{
        _table(
            (
                "Sharing",
                "Off: TTFT p50",
                "Off: TTFT p99",
                "Off: cost",
                "On: TTFT p50",
                "On: TTFT p99",
                "On: cost",
                "Prompt tokens from cache",
            ),
            rows,
        )
    }

With nothing shared the cache changes nothing. As more requests carry the system prompt, the
uncached run gets slower and dearer, because every one of them prefills and pays for those 400
tokens again. With the cache, time to first token and cost stay close to the no-sharing level
however many requests share the prompt. What the cache cannot remove is the cost of the first
sharers: a prefix only becomes reusable once a request carrying it has finished, so requests
that arrive before then pay in full. That is the rise in the cached p99 when everything
shares.

{await _calibrated_prefix_cache(rows)}"""
    return Section("2. Prefix cache on and off", body)


# --- 3. cost against latency, and plans -----------------------------------------------------

PLAN_TENANTS = (("initech", 0.6), ("globex", 0.3), ("acme", 0.1))
PLAN_REQUESTS = 600


async def _plan_scenario(by_plan: bool) -> dict[str, Any]:
    """Every tenant asks for the best model it may have; or, without plans, all get premium."""
    rng = random.Random(SEED)
    clock = SimClock()
    registry = build_continuous_registry(clock, seed=SEED)
    app = create_app(registry, clock, plans=load_plans() if by_plan else None)
    tenants = [name for name, _ in PLAN_TENANTS]
    weights = [w for _, w in PLAN_TENANTS]
    plan = []
    at_ms = 0.0
    for i in range(PLAN_REQUESTS):
        at_ms += round(rng.expovariate(1.0 / 1000), 6)
        tenant = rng.choices(tenants, weights)[0]
        words = rng.randint(20, 200)
        plan.append((at_ms, tenant, " ".join(f"q{i}w{j}" for j in range(words))))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:

        async def send(at: float, tenant: str, prompt: str) -> tuple[str, dict[str, Any]]:
            await clock.sleep_ms(at)
            body: dict[str, Any] = {"prompt": prompt, "max_tokens": 64}
            body.update(
                {"tenant_id": tenant, "quality": "best"} if by_plan else {"model": "premium"}
            )
            response = await client.post("/v1/generate", json=body)
            response.raise_for_status()
            return tenant, response.json()

        answers = await asyncio.gather(*(send(*p) for p in plan))
        usage = {}
        if by_plan:
            for tenant in tenants:
                usage[tenant] = (await client.get(f"/v1/tenants/{tenant}/usage")).json()["total"]
    await registry.aclose()

    per_tenant = {}
    for tenant in tenants:
        mine = [a for t, a in answers if t == tenant]
        per_tenant[tenant] = {
            "requests": len(mine),
            "model": mine[0]["model"],
            # What it costs the provider to serve, at the model's base price.
            "serving_cost": usage[tenant]["base_cost_usd"]
            if by_plan
            else sum(a["cost"]["usd"] for a in mine),
            "p99": percentile([a["timing"]["total_ms"] for a in mine], 99),
        }
    return per_tenant


async def cost_latency() -> Section:
    config = LoadConfig(
        seed=SEED,
        total_requests=200,
        arrival_rate_per_s=0.5,
        prompt_lengths=(LengthBucket(20, 300, 1.0),),
        shared_prefix_fraction=0.5,
        max_tokens=64,
    )
    points = []
    for tier, profile in PROFILES.items():
        arrivals = [
            (p.at_ms, GenerateRequest(f"r{p.index}", tier.value, p.prompt, p.max_tokens))
            for p in build_workload(config)
        ]
        clock = SimClock()
        worker = ContinuousBatchingWorker(
            profile,
            SimulatedBackend(seed=SEED),
            clock,
            prefix_cache=PrefixCache(profile.kv_capacity_tokens),
        )
        results = await _run_arrivals(worker, clock, arrivals)
        points.append(
            (
                tier.value,
                1000 * sum(r.cost_usd for r in results) / len(results),
                percentile([r.latency_ms for r in results], 50),
                percentile([r.latency_ms for r in results], 99),
            )
        )

    routed = await _plan_scenario(by_plan=True)
    all_premium = await _plan_scenario(by_plan=False)
    routed_cost = sum(t["serving_cost"] for t in routed.values())
    premium_cost = sum(t["serving_cost"] for t in all_premium.values())

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.4), facecolor=SURFACE)
    axes[0].scatter(
        [p[3] for p in points],
        [p[1] for p in points],
        s=70,
        color=SERIES[0],
        edgecolors=SURFACE,
        linewidths=1.5,
    )
    for name, cost, _, p99 in points:
        axes[0].annotate(
            name, (p99, cost), xytext=(8, 6), textcoords="offset points", color=INK, fontsize=9
        )
    style_axes(
        axes[0],
        title="Each model on the same workload",
        xlabel="p99 total latency (ms)",
        ylabel="Cost per 1,000 requests (USD)",
    )
    axes[0].set_xlim(left=0)
    axes[0].set_ylim(bottom=0)
    bars = axes[1].bar(
        ["Everything on premium", "Routed by plan"],
        [1000 * premium_cost, 1000 * routed_cost],
        color=SERIES[0],
        width=0.5,
    )
    for bar, value in zip(bars, (premium_cost, routed_cost), strict=True):
        axes[1].annotate(
            f"${value:.4f}",
            (bar.get_x() + bar.get_width() / 2, bar.get_height()),
            xytext=(0, 4),
            textcoords="offset points",
            ha="center",
            color=INK,
            fontsize=9,
        )
    style_axes(
        axes[1],
        title=f"Serving cost of {PLAN_REQUESTS} requests",
        xlabel="",
        ylabel="Cost (thousandths of a USD)",
    )
    axes[1].grid(axis="x", visible=False)
    fig.tight_layout()
    figure = _save(fig, "3-cost-latency.png")

    body = f"""**Illustrative profiles; prices and timings not calibrated.**

**Cost against latency.** The same 200 requests (20 to 300 word prompts, half
sharing a 200-word system prompt, 64 output tokens, 0.5 req/s) served by each model alone, with
continuous batching and the prefix cache.

{figure}

{
        _table(
            ("Model", "Cost per 1,000 requests (USD)", "p50 ms", "p99 ms"),
            [(name, cost, p50, p99) for name, cost, p50, p99 in points],
        )
    }

This is not a trade-off curve. On these two axes the economy model is both cheapest and fastest
and the premium model is both dearest and slowest, so economy dominates. The reason to pay for a
larger model is the quality of its answers, which this simulator does not model at all. The chart
shows what quality costs, not whether it is worth it.

**Routing by plan.** {PLAN_REQUESTS} requests at 1 req/s from three tenants, 60% on the free
plan, 30% on pro and 10% on enterprise. Every request asks for the best model available. With
plans, "best" means the best the tenant's plan includes. Without them, everything goes to the
premium model. Serving cost is at each model's base price, before plan multipliers.

{
        _table(
            (
                "Tenant (plan)",
                "Requests",
                "Routed: model",
                "Routed: serving cost USD",
                "Routed: p99 ms",
                "All premium: serving cost USD",
                "All premium: p99 ms",
            ),
            [
                (
                    f"{tenant} ({plan})",
                    routed[tenant]["requests"],
                    routed[tenant]["model"],
                    f"{routed[tenant]['serving_cost']:.5f}",
                    routed[tenant]["p99"],
                    f"{all_premium[tenant]['serving_cost']:.5f}",
                    all_premium[tenant]["p99"],
                )
                for tenant, plan in (
                    ("initech", "free"),
                    ("globex", "pro"),
                    ("acme", "enterprise"),
                )
            ],
        )
    }

Total serving cost is ${routed_cost:.5f} routed by plan against ${premium_cost:.5f} with
everything on premium: **{1 - routed_cost / premium_cost:.0%} less**. The enterprise tenant also
gets a shorter tail, because the premium model is no longer carrying the other tenants'
traffic. What the free and pro tenants give up is answer quality, which is not measured here."""
    return Section("3. Cost against latency, and routing by plan", body)


# --- 4. overload ---------------------------------------------------------------------------


async def overload_section() -> Section:
    queue_limit = {Tier.PREMIUM: 16}
    slo = OverloadPolicy(slo=overload.SLO)
    configs: tuple[tuple[str, OverloadPolicy | None, dict[Tier, int] | None], ...] = (
        ("No protection", None, None),
        ("Admission control", None, queue_limit),
        ("Downgrade", slo, None),
        ("Both", slo, queue_limit),
    )
    rows = []
    p99s = []
    for name, policy, limit in configs:
        outcomes, transitions = await overload.run(policy, max_queue_len=limit)
        served = [o for o in outcomes if o.ok]
        latencies = [o.total_ms for o in served]
        p99s.append(percentile(latencies, 99))
        rows.append(
            (
                name,
                len(served),
                sum(1 for o in outcomes if o.status == 429),
                percentile(latencies, 50),
                percentile(latencies, 99),
                max(latencies),
                sum(1 for o in served if o.model_used == "premium"),
                sum(1 for o in served if o.model_used == "standard"),
                len(transitions),
            )
        )

    fig, ax = plt.subplots(figsize=(8, 3.6), facecolor=SURFACE)
    bars = ax.barh([c[0] for c in configs][::-1], [p / 1000 for p in p99s][::-1], color=SERIES[0])
    for bar, value in zip(bars, p99s[::-1], strict=True):
        ax.annotate(
            f"{value / 1000:.1f} s",
            (bar.get_width(), bar.get_y() + bar.get_height() / 2),
            xytext=(5, 0),
            textcoords="offset points",
            va="center",
            color=INK,
            fontsize=9,
        )
    style_axes(
        ax, title="p99 total latency of the requests that were served", xlabel="Seconds", ylabel=""
    )
    ax.grid(axis="y", visible=False)
    ax.set_xlim(right=max(p99s) / 1000 * 1.15)
    fig.tight_layout()
    figure = _save(fig, "4-overload.png")

    body = f"""**Illustrative profiles; prices and timings not calibrated.**

Every request asks for the premium model and opts in to downgrade. 300 arrive at
3 req/s, about three times what premium can serve, then 100 at 0.25 req/s. Admission control is a
queue limit of 16 on premium; downgrade uses a 20 s p99 SLO over a 30 s window. Latency is over
the requests that were served; rejected ones got an immediate 429.

{figure}

{
        _table(
            (
                "Protection",
                "Served",
                "Rejected (429)",
                "p50 ms",
                "p99 ms",
                "Max ms",
                "By premium",
                "By standard",
                "State changes",
            ),
            rows,
        )
    }

The two defences fail differently. Admission control bounds latency by refusing work: nothing
it serves waits behind more than a full queue, and everything else is told at once to retry.
Downgrade refuses nothing and serves the overflow on a cheaper model, but it only reacts once
slow requests have finished, so the requests already queued when it engages still wait.
Together, the queue limit caps the damage during that reaction time. "State changes" counts how
often a model switched between healthy and degraded; more than two means it switched back and
forth. Which defence is right depends on whether a refused request or a lower-quality answer is
worse for the client.

{overload_on_calibrated()}"""
    return Section("4. Overload: admission control and downgrade", body)


def _decode_bound_req_per_s(profile: ModelProfile, mean_output_tokens: float) -> float:
    """Most requests per second a profile can decode with every batch full.

    An upper bound on what the scheduler can serve: prefill and overhead are left out.
    """
    step_ms = profile.decode_base_ms + profile.decode_ms_per_extra_seq * (
        profile.max_batch_size - 1
    )
    return profile.max_batch_size * 1000 / step_ms / mean_output_tokens


def overload_on_calibrated() -> str:
    """What the calibration says about this section, without running it on calibrated profiles."""
    sentence = (
        "**How the calibrated batching gain would change this.** With the smaller gain from "
        "batching that sections 1 and 2 show on the calibrated profiles, every model reaches its "
        "limit at a far lower arrival rate than these runs assume, so a downgrade would move "
        "requests onto models with little spare capacity of their own: expect it to relieve less, "
        "and admission control to refuse more, than the table above shows."
    )
    found = calibrated_profiles()
    if found is None or Tier.PREMIUM not in found.profiles:
        return f"{sentence} No calibration of the premium tier was found, so this was not checked."
    lengths = OutputLengthGenerator(seed=overload.SEED)
    surge = build_workload(overload.SURGE)
    mean_tokens = sum(lengths.length_for(p.prompt) for p in surge) / len(surge)
    rate = overload.SURGE.arrival_rate_per_s
    bounds = {
        tier: (
            _decode_bound_req_per_s(PROFILES[tier], mean_tokens),
            _decode_bound_req_per_s(profile, mean_tokens),
        )
        for tier, profile in found.profiles.items()
    }
    rows = [
        (tier.value, illustrative, calibrated, f"{rate / calibrated:.1f}x")
        for tier, (illustrative, calibrated) in bounds.items()
    ]
    table = _table(
        (
            "Tier",
            "Illustrative: most req/s it can decode",
            "Calibrated: most req/s it can decode",
            f"Surge of {rate:g} req/s over the calibrated bound",
        ),
        rows,
    )
    premium = bounds[Tier.PREMIUM][1]
    if premium >= rate:
        verdict = (
            f"By this bound the surge of {rate:g} req/s does not saturate the calibrated premium "
            "profile. Overload numbers on the calibrated profiles are still not given here: this "
            "section has only been run on the illustrative ones."
        )
    else:
        verdict = (
            "**No overload numbers are given for the calibrated profiles, because this workload "
            f"is saturated on them.** The surge sends {rate:g} req/s to premium, "
            f"{rate / premium:.0f} times what the calibrated premium profile can decode even with "
            "every batch full. A run that far over capacity reports the length of its backlog, not "
            "the behaviour of the defences."
        )
    return f"""{sentence}

{verdict} The bound is the decode rate with a full batch at the illustrative batch limit,
ignoring prefill and overhead, for this workload's mean of {mean_tokens:.0f} output tokens. It
is generous to the calibrated profiles: it extends the fitted cost per extra sequence to batches
larger than the 4 requests that were measured together.

{table}"""


# --- 5. chunked prefill --------------------------------------------------------------------


async def chunked() -> Section:
    arrivals = chunked_prefill.workload()
    rows = []
    summaries = []
    labels = []
    for chunk_tokens in chunked_prefill.CHUNK_SIZES:
        summary = chunked_prefill.summarise(
            arrivals, await chunked_prefill.run(arrivals, chunk_tokens)
        )
        label = "off" if chunk_tokens is None else str(chunk_tokens)
        labels.append(label)
        summaries.append(summary)
        rows.append(
            (
                label,
                summary.itl_p50_ms,
                summary.itl_p99_ms,
                summary.itl_p999_ms,
                summary.itl_max_ms,
                summary.short_ttft_p99_ms,
                summary.long_ttft_p50_ms,
            )
        )

    fig, ax = plt.subplots(figsize=(8.5, 4.4), facecolor=SURFACE)
    lines = (
        ("ITL p99", [s.itl_p99_ms for s in summaries]),
        ("Longest gap (ITL max)", [s.itl_max_ms for s in summaries]),
        ("Short prompts: TTFT p99", [s.short_ttft_p99_ms for s in summaries]),
    )
    for index, (name, values) in enumerate(lines):
        ax.plot(
            labels,
            values,
            color=SERIES[index],
            marker=MARKERS[index],
            linewidth=2,
            markersize=6,
            label=name,
        )
    style_axes(
        ax,
        title="Latency tails by prefill chunk size",
        xlabel="Prefill chunk (tokens)",
        ylabel="Milliseconds (log scale)",
    )
    ax.set_yscale("log")
    legend(ax, loc="upper right")
    fig.tight_layout()
    figure = _save(fig, "5-chunked-prefill.png")

    body = f"""**Illustrative profiles; prices and timings not calibrated.**

Standard tier. {chunked_prefill.SHORT_REQUESTS} short prompts of 20 to 80 words at
4 req/s, with {chunked_prefill.LONG_REQUESTS} prompts of 6,000 to 10,000 words spread through
the run. Up to 64 output tokens. ITL is the gap between consecutive tokens of one request.

{figure}

{
        _table(
            (
                "Chunk tokens",
                "ITL p50",
                "ITL p99",
                "ITL p99.9",
                "ITL max",
                "Short TTFT p99",
                "Long TTFT p50",
            ),
            rows,
        )
    }

Chunking removes the long stall: the longest gap falls from over a second to tens of
milliseconds, and short prompts stop waiting behind a long prompt's whole prefill. **ITL p99 is
the wrong place to look for it.** With chunking off, long prompts are rare enough that their
stalls touch fewer than 1% of gaps, so the p99 never sees them; with medium chunks each rare,
huge stall becomes many moderate ones, which do reach the p99 and make it worse. Only very small
chunks improve ITL p99, and they delay the long prompts themselves the most. The gain is in the
far tail: p99.9 and the maximum."""
    return Section("5. Chunked prefill", body)


# --- 6. simulated against real --------------------------------------------------------------

_PARAMETERS = (
    ("prefill_ms_per_token", "prefill ms/token"),
    ("decode_base_ms", "decode base ms"),
    ("decode_ms_per_extra_seq", "ms per extra sequence"),
    ("per_request_overhead_ms", "per-request overhead ms"),
)


def _percent(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}%"


def _spread_rows(spread: Mapping[str, Mapping[str, Any]]) -> list[tuple[object, ...]]:
    return [
        (
            tier,
            label,
            f"{parameters[name]['min']:.4g}",
            f"{parameters[name]['mean']:.4g}",
            f"{parameters[name]['max']:.4g}",
            _percent(parameters[name]["range_percent_of_mean"]),
        )
        for tier, parameters in spread.items()
        for name, label in _PARAMETERS
    ]


_SPREAD_HEADER = ("Tier", "Parameter", "Lowest", "Mean", "Highest", "Range, % of mean")


def _fit_quality(document: dict[str, Any]) -> str:
    """The overhead, how good the fits are, and the spread between repeats.

    Empty for a file written before these were recorded.
    """
    profiles = {
        tier: p
        for tier, p in document["profiles"].items()
        if "per_request_overhead_ms" in p and "mape_percent_by_concurrency" in p
    }
    if not profiles:
        return ""
    rows = []
    for tier, p in profiles.items():
        by_level = p["mape_percent_by_concurrency"]
        alone, most = by_level[min(by_level, key=int)], by_level[max(by_level, key=int)]
        fit = p["fit"]
        r2 = [fit["ttft_vs_prompt_tokens"]["r2"], fit["per_token_vs_extra_sequences"]["r2"]]
        rows.append(
            (
                tier,
                f"{p['per_request_overhead_ms']:.1f}",
                *("-" if value is None else f"{value:.3f}" for value in r2),
                _percent(alone["ttft"]),
                _percent(most["ttft"]),
                _percent(alone["total"]),
                _percent(most["total"]),
            )
        )
    top = max(int(level) for p in profiles.values() for level in p["mape_percent_by_concurrency"])
    text = f"""
The fourth fitted parameter, a fixed overhead per request, with the R² of the two regressions
and the simulator's error over all requests, split by whether a request was sent alone or with
the most others ({top} together). The errors by concurrency are where a poor fit shows:

{
        _table(
            (
                "Tier",
                "Per-request overhead ms",
                "R² TTFT fit",
                "R² decode fit",
                "TTFT error, alone",
                f"TTFT error, {top} together",
                "Total error, alone",
                f"Total error, {top} together",
            ),
            rows,
        )
    }
"""
    spread = {
        tier: p["spread_across_repeats"]
        for tier, p in profiles.items()
        if p.get("spread_across_repeats")
    }
    if spread:
        repeats = document["plan"]["repeats"]
        text += f"""
Spread within this run: each of the {repeats} repeats fitted on its own.

{_table(_SPREAD_HEADER, _spread_rows(spread))}
"""
    return text


def _placement_rows(document: dict[str, Any]) -> list[tuple[object, ...]]:
    rows: list[tuple[object, ...]] = []
    for tier, profile in document["profiles"].items():
        if "hardware_limited" not in profile:
            continue
        by_run = profile.get("placement_by_run")
        placements = (
            list(by_run.values())
            if by_run is not None
            else [document["servers"][tier].get("placement")]
        )
        reported = [p for p in placements if p]
        if not reported:
            rows.append(
                (tier, "not recorded", "not recorded", f"0 of {len(placements)}", "unknown")
            )
            continue
        gpu = sorted(p["gpu_percent"] for p in reported)
        share = f"{gpu[0]:g}%" if gpu[0] == gpu[-1] else f"{gpu[0]:g}% to {gpu[-1]:g}%"
        size = max(p["size_bytes"] for p in reported) / 2**30
        rows.append(
            (
                tier,
                f"{size:.1f} GiB",
                share,
                f"{len(reported)} of {len(placements)}",
                "**hardware-limited**" if profile["hardware_limited"] else "fits on the GPU",
            )
        )
    return rows


def _placement(document: dict[str, Any]) -> str:
    """Where the server had each model loaded when it was measured. Empty if never recorded."""
    rows = _placement_rows(document)
    if not rows:
        return ""
    return f"""
Where Ollama had each model loaded when it was measured, from its process list. A tier marked
**hardware-limited** was partly on the CPU: its timings are of a model that does not fit this
GPU, not of the model on adequate hardware.

{_table(("Tier", "Loaded size", "Share on the GPU", "Runs that recorded it", "Verdict"), rows)}
"""


def _aggregate_quality(document: dict[str, Any]) -> str:
    """For a calibration that is the median of several runs: ranges, flags and errors."""
    runs = document["aggregated_from"]
    limit = document["unstable_above_range_percent"]
    parameter_rows = []
    error_rows = []
    for tier, p in document["profiles"].items():
        for name, label in _PARAMETERS:
            c = p["aggregate"][name]
            parameter_rows.append(
                (
                    tier,
                    label,
                    f"{c['median']:.4g}",
                    f"{c['min']:.4g}",
                    f"{c['max']:.4g}",
                    _percent(c["range_percent_of_median"]),
                    "**unstable**" if c["unstable"] else "yes",
                )
            )
        by_level = p["mape_percent_by_concurrency"]
        alone, most = by_level[min(by_level, key=int)], by_level[max(by_level, key=int)]
        lowest_r2 = [
            min((r2 for r2 in p["fit_r2_by_run"][which] if r2 is not None), default=None)
            for which in ("ttft_vs_prompt_tokens", "per_token_vs_extra_sequences")
        ]
        error_rows.append(
            (
                tier,
                *("-" if r2 is None else f"{r2:.3f}" for r2 in lowest_r2),
                _percent(alone["ttft"]),
                _percent(most["ttft"]),
                _percent(alone["total"]),
                _percent(most["total"]),
            )
        )
    top = max(
        int(level)
        for p in document["profiles"].values()
        for level in p["mape_percent_by_concurrency"]
    )
    return f"""
The parameters above are the **median of {len(runs)} runs** ({", ".join(r["run"] for r in runs)},
in `benchmarks/calibration/`); no run was chosen over another, and the errors are those of the
median profile against every run's requests. A parameter is marked **unstable** when its highest
and lowest fitted values are more than {limit:g}% of the median apart: the median is reported,
but it did not repeat.

{
        _table(
            (
                "Tier",
                "Parameter",
                "Median",
                "Lowest",
                "Highest",
                "Range, % of median",
                "Repeatable?",
            ),
            parameter_rows,
        )
    }

The worst R² of the two regressions over the runs, and the simulator's error over all requests,
split by whether a request was sent alone or with the most others ({top} together). The errors by
concurrency are where a poor fit shows:

{
        _table(
            (
                "Tier",
                "Lowest R² TTFT fit",
                "Lowest R² decode fit",
                "TTFT error, alone",
                f"TTFT error, {top} together",
                "Total error, alone",
                f"Total error, {top} together",
            ),
            error_rows,
        )
    }
"""


def _other_runs(document: dict[str, Any]) -> str:
    """Run-to-run spread, when further runs are kept in ``run*`` folders beside this one."""
    paths = sorted(CALIBRATION.glob("run*/calibrated_profiles.json"))
    if not paths:
        return ""
    documents = [document, *(json.loads(path.read_text(encoding="utf-8")) for path in paths)]
    when = ", ".join(str(d["measured_at"]) for d in documents)
    return f"""
Spread between runs: the whole calibration was run {len(documents)} times (measured at {when}).
The parameters above are the first run's; the others are in `benchmarks/calibration/run*/`.

{_table(_SPREAD_HEADER, _spread_rows(spread_across_runs(documents)))}
"""


async def calibration() -> Section:
    path = CALIBRATION / "calibrated_profiles.json"
    command = "uv run python -m benchmarks.calibrate --backends <file> --out benchmarks/calibration"
    if not path.exists():
        body = f"""**No calibration has been run.** There is no
`benchmarks/calibration/calibrated_profiles.json`, so there is nothing measured to compare the
simulator with, and every number in this report comes from the illustrative model profiles.

To produce it, start the real models (`docs/12-real-models.md`) and run:

```sh
{command}
```

then regenerate this report. See `docs/13-calibration.md`."""
        return Section("6. Simulated against real models", body)

    document = json.loads(path.read_text(encoding="utf-8"))
    machine = document["hardware"]
    rows = []
    figures = []
    notes: list[str] = []
    for tier, profile in document["profiles"].items():
        server = document["servers"][tier]
        illustrative = PROFILES[Tier(tier)]
        error = profile["mape_percent"]
        rows.append(
            (
                tier,
                f"{server['model']} ({server['quantization']})",
                f"{profile['prefill_ms_per_token']:.4f} ({illustrative.prefill_ms_per_token:g})",
                f"{profile['decode_base_ms']:.2f} ({illustrative.decode_base_ms:g})",
                f"{profile['decode_ms_per_extra_seq']:.2f} "
                f"({illustrative.decode_ms_per_extra_seq:g})",
                profile["samples"],
                "-" if error["ttft_holdout"] is None else f"{error['ttft_holdout']:.1f}%",
                "-" if error["total_holdout"] is None else f"{error['total_holdout']:.1f}%",
            )
        )
        if (CALIBRATION / f"{tier}.png").exists():
            figures.append(f"![{tier}](../benchmarks/calibration/{tier}.png)")
        notes.extend(f"- **{tier}:** {note}" for note in profile["notes"])
    servers = sorted({f"{s['kind']} {s['version']}" for s in document["servers"].values()})
    aggregated = "aggregated_from" in document
    quality = _aggregate_quality(document) if aggregated else _fit_quality(document)
    runs = "" if aggregated else _other_runs(document)
    placement = _placement(document)

    body = f"""These are **measurements of real models**, taken on one machine on one day. They are
the only numbers in this report that are not simulated, and they are not reproduced by
`make report`: that would need the same models running on the same hardware. This section reads
the files that `benchmarks/calibrate.py` wrote.

| | |
|---|---|
| Measured at | {document["measured_at"]} |
| CPU | {machine["cpu"]} ({machine["logical_cores"]} logical cores) |
| RAM | {machine["ram_gb"]} GB |
| GPU | {machine["gpu"]} |
| OS | {machine["os"]} |
| Model server | {", ".join(servers)} |
| Notes | {document["notes"] or "none"} |

Fitted timing parameters, with the illustrative value the rest of this report uses in brackets,
and the simulator's error against held-out measurements (MAPE):

{
        _table(
            (
                "Tier",
                "Model",
                "Prefill ms/token",
                "Decode base ms",
                "ms per extra sequence",
                "Requests",
                "TTFT error",
                "Total latency error",
            ),
            rows,
        )
    }

{chr(10).join(notes) if notes else "The tool raised no warnings about these fits."}
{quality}{runs}{placement}
{chr(10).join(chr(10) + figure for figure in figures)}

Sections 1 to 5 use the illustrative profiles, not these fitted ones; sections 1 and 2 show a
calibrated variant beside them. See `docs/13-calibration.md` for how the fit is made and where
the simulator diverges from the real servers. To measure again:

```sh
{command}
```"""
    return Section("6. Simulated against real models", body)


# --- the report ----------------------------------------------------------------------------

SECTIONS: dict[str, Callable[[], Awaitable[Section]]] = {
    "batching": batching,
    "prefix-cache": prefix_cache,
    "cost": cost_latency,
    "overload": overload_section,
    "chunked-prefill": chunked,
    "calibration": calibration,
}

LIMITATIONS = """## Limitations

- **Sections 1 to 5 are simulations.** No model runs. Timings come from a cost model with a
  few parameters per model, and those parameters are illustrative: invented to give the three
  tiers different shapes, not measured. The calibrated variants in sections 1 and 2 swap in
  measured timings and are simulations all the same. Read every figure as "how this scheduler
  behaves under these assumptions", never as the performance of Llama models or of any hardware.
- **Prices are illustrative** and so is every cost. Ratios between tiers follow from the made-up
  prices.
- **Answer quality is not modelled.** Nothing here says what a tenant loses by being served by a
  smaller model, which is the other half of every cost comparison in section 3.
- **The simulator is simplified** in ways that matter: prefill is linear in tokens and blocks the
  batch unless chunked; a decode step is linear in batch size; output length is known when a
  request is admitted; one token is one whitespace-separated word; each model has its hardware
  to itself. See the notes for steps 1, 3, 4 and 11.
- **Each result is one seed and one workload.** They are exactly reproducible, which is not the
  same as representative. Different prompt lengths, arrival patterns or mixes move the numbers.
- **Overloaded runs report the length of the run.** Where a queue never drains (the baseline
  above 1x load, "no protection" in section 4), the p99 grows with the number of requests sent.
  Compare those rows by order of magnitude only.
- **p99 over a few hundred requests is a handful of samples.** It is a nearest-rank percentile
  and can hinge on two or three requests.
- **Sections 3, 4 and 5 were not run on calibrated profiles.** Their prices and timings are
  the illustrative ones throughout. Section 4 says why its workload was not rerun.
- **Section 6 is one machine, on one day.** Real measurements are noisy and specific to that
  hardware, model build and server version. They are not re-measured when this report is
  regenerated.
- **Simulated and real numbers must not be compared directly.** With real models the model
  server does the batching, not this repo's scheduler (`docs/12-real-models.md`).
"""


def _gain_list(gains: Mapping[Tier, float]) -> str:
    return ", ".join(f"{gain:.1f}x on {tier.value}" for tier, gain in gains.items())


def batching_limitation(gains: BatchingGains | None) -> str:
    """The limitation that depends on the calibration: how much the batching gain shrinks."""
    if gains is None:
        return (
            "- **The batching gain has not been checked against a calibration.** No calibration "
            "file was found when this report was generated.\n"
        )
    measured = sorted(set(gains.measured_concurrency.values()))
    batch = " or ".join(str(m) for m in measured)
    top = f"{LOAD_LEVELS[-1]:g}x"
    warning = _unstable_warning(gains.unstable_batching_cost)
    return f"""- **The illustrative profiles overstate what batching buys.** At {top} load in
  section 1, continuous batching serves {_gain_list(gains.illustrative)} of the baseline's requests
  per second. With the calibrated timings and batches held to the {batch} requests that were
  measured together, the same simulation gives {_gain_list(gains.capped)}. Letting batches
  grow to the illustrative limits gives {_gain_list(gains.calibrated)}, which extrapolates the
  fitted cost per extra sequence beyond what was measured. Every one of these is simulated, and
  the calibration behind them is one laptop (section 6).{warning}
"""


def _commit() -> str:
    def git(*args: str) -> str:
        done = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=False)
        return done.stdout.strip()

    head = git("rev-parse", "--short", "HEAD") or "unknown"
    return f"{head} with uncommitted changes" if git("status", "--porcelain") else head


async def build(only: str | None) -> str:
    names = [only] if only else list(SECTIONS)
    # Read before any section runs: the sections write figures, and a new figure would
    # make a clean tree look as if it had uncommitted changes.
    commit = _commit()
    parts = []
    for name in names:
        section = await SECTIONS[name]()
        parts.append(
            f"## {section.title}\n\n{section.body}\n\n"
            f"Reproduce this section: `uv run python -m benchmarks.report --only {name}`\n"
        )
    if only:
        return "\n".join(parts)
    header = f"""# Results report

Generated by `uv run python -m benchmarks.report` (or `make report`) from commit `{commit}`.
Do not edit by hand; change `benchmarks/report.py` and regenerate.

**Sections 1 to 5 are simulated** with seed {SEED} on illustrative model profiles; sections 1
and 2 also show the same simulation on calibrated profiles. They are deterministic: the same
code and calibration file produce the same numbers. **Section 6 is measured** on real models
and is read from files, not re-measured. Nothing here is a benchmark of real hardware except
where section 6 says so.

Times are in milliseconds unless stated. Percentiles are nearest-rank.
"""
    return "\n".join([header, *parts, LIMITATIONS + batching_limitation(await batching_gains())])


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate docs/REPORT.md and its figures.")
    parser.add_argument("--only", choices=list(SECTIONS), help="print one section and stop")
    args = parser.parse_args()
    text = asyncio.run(build(args.only))
    if args.only:
        print(text)
        return
    (DOCS / "REPORT.md").write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {DOCS / 'REPORT.md'} and figures in {FIGURES}")


if __name__ == "__main__":
    main()
