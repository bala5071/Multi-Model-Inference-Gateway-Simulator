"""Generate docs/REPORT.md and its figures.

    uv run python -m benchmarks.report              # everything (same as `make report`)
    uv run python -m benchmarks.report --only batching   # one section, printed

Sections 1 to 3 run the simulator on simulated time on the calibrated model profiles (the
median of the runs in benchmarks/calibration/, batches of at most the concurrency that was
measured), so the same code gives the same numbers every time. ``GATEWAY_PROFILES`` names
another calibration file to run them on instead. Every run is checked for saturation first
(benchmarks/saturation.py); a saturated run's p99 is not reported. Sections 4 and 5 have no
results: they were not measured on real data. Section 6 summarises the calibration files,
which are measurements of real models and are only re-measured by benchmarks/calibrate.py.
Section 7 reads the accuracy measured by benchmarks/quality_eval.py.
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

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from benchmarks import saturation
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
from gateway.backends import SimulatedBackend
from gateway.baseline import BaselineWorker
from gateway.clock import SimClock
from gateway.continuous import ContinuousBatchingWorker
from gateway.cost_model import OutputLengthGenerator
from gateway.metrics import percentile
from gateway.prefix_cache import PrefixCache
from gateway.profiles import (
    HARDWARE_LIMITED,
    PROFILES,
    UNSTABLE_PARAMETERS,
    ModelProfile,
    Tier,
    load_calibrated_profiles,
)
from gateway.registry import Worker
from gateway.routing import load_plans
from gateway.schema import GenerateRequest, RequestResult

SEED = 7
REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"
FIGURES = DOCS / "report"
CALIBRATION = REPO / "benchmarks" / "calibration"
QUALITY_RESULTS = REPO / "benchmarks" / "quality" / "results.json"
SATURATED = "saturated"


@dataclass(frozen=True, slots=True)
class Section:
    """One part of the report: its heading, its Markdown body and nothing else."""

    title: str
    body: str


# "R squared", written as an escape: tracked code is ASCII (scripts/check_characters.py).
R_SQUARED = "R\u00b2"


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


# --- the profiles every simulated section runs on ------------------------------------------


@dataclass(frozen=True, slots=True)
class Calibration:
    """The calibrated profiles the simulated sections run on, and what is known about them."""

    # Where the profiles came from, as shown in the report.
    source: str
    profiles: Mapping[Tier, ModelProfile]
    # Per tier, the parameters that did not repeat between calibration runs.
    unstable: Mapping[Tier, frozenset[str]]
    hardware_limited: frozenset[Tier]
    # Per tier, the lowest and highest cost per extra sequence over the runs, when the file
    # is an aggregate of several runs. Measured values, used to show how much a result
    # depends on that unstable parameter.
    batching_cost_range: Mapping[Tier, tuple[float, float]]


def _entries(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    entries: dict[str, Any] = json.loads(path.read_text(encoding="utf-8")).get("profiles", {})
    return entries


def calibration_in_use() -> Calibration:
    """The default profiles (the tracked calibration), or the file ``GATEWAY_PROFILES`` names."""
    named = os.environ.get("GATEWAY_PROFILES")
    if named:
        path = Path(named)
        profiles = load_calibrated_profiles(path)
        entries = _entries(path)
        unstable = {
            tier: frozenset(entries.get(tier.value, {}).get("unstable_parameters", []))
            for tier in profiles
        }
        limited = frozenset(
            tier for tier in profiles if entries.get(tier.value, {}).get("hardware_limited")
        )
    else:
        path = CALIBRATION / "calibrated_profiles.json"
        profiles, entries = PROFILES, _entries(path)
        unstable, limited = dict(UNSTABLE_PARAMETERS), HARDWARE_LIMITED
    ranges = {}
    for tier in profiles:
        cost = entries.get(tier.value, {}).get("aggregate", {}).get("decode_ms_per_extra_seq")
        if cost is not None:
            ranges[tier] = (float(cost["min"]), float(cost["max"]))
    try:
        source = path.resolve().relative_to(REPO).as_posix()
    except ValueError:
        source = path.name
    return Calibration(source, profiles, unstable, limited, ranges)


def _caveat(found: Calibration) -> str:
    """The sentence every simulated section carries about the profiles it ran on."""
    unstable = "; ".join(
        f"{tier.value}: {', '.join(sorted(found.unstable[tier]))}"
        for tier in found.profiles
        if found.unstable.get(tier)
    )
    limited = ", ".join(t.value for t in found.profiles if t in found.hardware_limited)
    text = (
        f"**Simulated** on the calibrated profiles (`{found.source}`, section 6): the median of "
        "calibration runs on one laptop with Ollama, batches of at most "
        f"{max(p.max_batch_size for p in found.profiles.values())}, the most requests that were "
        "measured together. This is this repo's scheduler with measured step costs, not a "
        "measurement of a real server."
    )
    if unstable:
        text += f" **Unstable parameters** (did not repeat between runs): {unstable}."
    if limited:
        text += f" **Hardware-limited** (model partly on the CPU): {limited}."
    return text


def _verdict_table(rows: Sequence[tuple[str, Sequence[saturation.ModelLoad]]]) -> str:
    """The saturation check behind a section, one line per workload and model."""
    body = [
        (
            name,
            m.model,
            f"{m.offered_per_s:.3f}",
            f"{m.capacity_per_s:.3f}",
            f"{m.utilisation:.0%}",
            f"{m.p99_growth:+.0%}",
            "**saturated**" if m.saturated else "no",
        )
        for name, loads in rows
        for m in loads
    ]
    return _table(
        (
            "Workload",
            "Model",
            "Offered req/s",
            "Capacity req/s",
            "Utilisation",
            "p99 when the run is doubled",
            "Saturated?",
        ),
        body,
    )


SATURATION_RULE = (
    f"A model counts as saturated when it is offered more than "
    f"{saturation.MAX_UTILISATION:.0%} of its capacity (requests per second when the same "
    "requests arrive all at once), or when its p99 grows by more than "
    f"{saturation.MAX_P99_GROWTH:.0%} as the run is doubled. A saturated run's p99 measures how "
    "long the run was, so it is not reported."
)


# --- 1. baseline vs continuous batching ----------------------------------------------------

LOAD_LEVELS = (0.25, 0.5, 1.0, 2.0, 4.0)
BATCHING_REQUESTS = 300
Curves = dict[Tier, dict[str, list[tuple[float, float | None]]]]


def _batching_prompts() -> list[str]:
    rng = random.Random(SEED)
    return [f"request {i} " + "word " * rng.randint(18, 398) for i in range(BATCHING_REQUESTS)]


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


_WORKERS = (("baseline", BaselineWorker), ("continuous", ContinuousBatchingWorker))


async def capacity(profile: ModelProfile, worker: str) -> float:
    """Requests per second one worker manages when the section's requests all arrive at once."""
    make = dict(_WORKERS)[worker]
    clock = SimClock()
    requests = [
        (0.0, GenerateRequest(f"r{i}", profile.tier.value, p))
        for i, p in enumerate(_batching_prompts())
    ]
    results = await _run_arrivals(
        make(profile, SimulatedBackend(seed=SEED), clock), clock, requests
    )
    return 1000 * len(results) / max(r.finish_ms for r in results)


async def _batching_runs(
    profiles: Mapping[Tier, ModelProfile],
) -> tuple[list[list[object]], Curves, dict[Tier, dict[str, float]]]:
    """Both workers on identical arrivals: table rows, curves, and each worker's capacity."""
    rows = []
    curves: Curves = {}
    capacities: dict[Tier, dict[str, float]] = {}
    for tier, profile in profiles.items():
        curves[tier] = {"baseline": [], "continuous": []}
        capacities[tier] = {name: await capacity(profile, name) for name, _ in _WORKERS}
        for load in LOAD_LEVELS:
            arrivals = _batching_arrivals(profile, load)
            offered = 1000 * len(arrivals) / arrivals[-1][0]
            row: list[object] = [tier.value, f"{load:g}x", offered]
            for name, make in _WORKERS:
                clock = SimClock()
                results = await _run_arrivals(
                    make(profile, SimulatedBackend(seed=SEED), clock), clock, arrivals
                )
                throughput = _throughput(results)
                saturated = offered > saturation.MAX_UTILISATION * capacities[tier][name]
                p99 = None if saturated else percentile([r.latency_ms for r in results], 99)
                curves[tier][name].append((throughput, p99))
                row.extend([throughput, SATURATED if p99 is None else p99])
            rows.append(row)
    return rows, curves, capacities


def _batching_figure(curves: Curves, name: str) -> str:
    fig, axes = plt.subplots(2, len(curves), figsize=(13, 7), facecolor=SURFACE, squeeze=False)
    for column, tier in enumerate(curves):
        for index, worker in enumerate(("continuous", "baseline")):
            points = curves[tier][worker]
            axes[0][column].plot(
                LOAD_LEVELS,
                [p[0] for p in points],
                color=SERIES[index],
                marker=MARKERS[index],
                linewidth=2,
                markersize=6,
                label=worker,
            )
            kept = [(load, p[1]) for load, p in zip(LOAD_LEVELS, points, strict=True) if p[1]]
            axes[1][column].plot(
                [k[0] for k in kept],
                [k[1] for k in kept],
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
            title=f"{tier.value}: p99, unsaturated runs only",
            xlabel="Offered load (x baseline capacity)",
            ylabel="p99 latency (ms, log scale)",
        )
        for row_index in (0, 1):
            axes[row_index][column].set_xscale("log", base=2)
            axes[row_index][column].set_xticks(LOAD_LEVELS, [f"{x:g}" for x in LOAD_LEVELS])
            axes[row_index][column].set_xlim(LOAD_LEVELS[0] / 1.3, LOAD_LEVELS[-1] * 1.3)
        axes[1][column].set_yscale("log")
    legend(axes[0][0], loc="upper left")
    fig.suptitle("Simulated on calibrated profiles, batches of at most 4", color=INK)
    fig.tight_layout()
    return _save(fig, name)


@dataclass(frozen=True, slots=True)
class BatchingGains:
    """Continuous batching's capacity over the baseline's, per tier.

    At the median cost per extra sequence, and at the lowest and highest value the
    calibration runs fitted (absent when the calibration file is not an aggregate).
    """

    median: dict[Tier, float]
    lowest_cost: dict[Tier, float]
    highest_cost: dict[Tier, float]
    max_batch: dict[Tier, int]


async def batching_gains(
    found: Calibration, capacities: Mapping[Tier, Mapping[str, float]] | None = None
) -> BatchingGains:
    median: dict[Tier, float] = {}
    lowest: dict[Tier, float] = {}
    highest: dict[Tier, float] = {}
    for tier, profile in found.profiles.items():
        caps = capacities[tier] if capacities is not None else None
        baseline = caps["baseline"] if caps else await capacity(profile, "baseline")
        continuous = caps["continuous"] if caps else await capacity(profile, "continuous")
        median[tier] = continuous / baseline
        if tier in found.batching_cost_range:
            low, high = found.batching_cost_range[tier]
            for into, cost in ((lowest, low), (highest, high)):
                varied = dataclasses.replace(profile, decode_ms_per_extra_seq=cost)
                into[tier] = await capacity(varied, "continuous") / baseline
    return BatchingGains(
        median, lowest, highest, {t: p.max_batch_size for t, p in found.profiles.items()}
    )


_BATCHING_HEADER = (
    "Tier",
    "Load",
    "Offered req/s",
    "Baseline req/s",
    "Baseline p99 ms",
    "Continuous req/s",
    "Continuous p99 ms",
)


def _gain_rows(gains: BatchingGains) -> list[tuple[object, ...]]:
    def fmt(values: Mapping[Tier, float], tier: Tier) -> str:
        return f"{values[tier]:.2f}x" if tier in values else "-"

    return [
        (
            tier.value,
            gains.max_batch[tier],
            fmt(gains.median, tier),
            fmt(gains.lowest_cost, tier),
            fmt(gains.highest_cost, tier),
        )
        for tier in gains.median
    ]


async def batching() -> Section:
    found = calibration_in_use()
    rows, curves, capacities = await _batching_runs(found.profiles)
    figure = _batching_figure(curves, "1-batching.png")
    gains = await batching_gains(found, capacities)
    unstable = [t.value for t in found.profiles if "decode_ms_per_extra_seq" in found.unstable[t]]

    body = f"""{_caveat(found)}

{BATCHING_REQUESTS} requests per run with prompts of 20 to 400 words, arriving as a
Poisson stream. "Load" is the arrival rate as a multiple of what the one-at-a-time baseline can
sustain on that model, so 1x is the baseline's limit. Both workers get identical arrivals. No
prefix cache. {SATURATION_RULE} Here capacity is each worker's own on these {BATCHING_REQUESTS}
requests.

{figure}

{_table(_BATCHING_HEADER, rows)}

Below its capacity a worker keeps up, so its throughput is the arrival rate and the difference
is in latency. Past it, throughput stays at capacity and the queue grows for the whole run.

**Capacity of continuous batching over the baseline**, with batches of at most the
measured number of requests. The cost per extra sequence is the parameter this rests on, so
the gain is also shown with that parameter at the lowest and the highest value the
calibration runs fitted (both measured; every other parameter at its median):

{
        _table(
            (
                "Tier",
                "Largest batch (measured)",
                "Gain, median cost per extra sequence",
                "Gain, lowest fitted cost",
                "Gain, highest fitted cost",
            ),
            _gain_rows(gains),
        )
    }

**The cost per extra sequence is unstable on {", ".join(unstable) or "no tier"}**: it did not
repeat between calibration runs (section 6), and the two right-hand columns show how far the
gain moves across the values it took. Nothing here batches more requests than were measured
together, so nothing is extrapolated past that. `per_request_overhead_ms` does not hold up the
model, so it moves time to first token and not these capacities."""
    return Section("1. Baseline vs continuous batching", body)


# --- 2. prefix cache -----------------------------------------------------------------------

PREFIX_FRACTIONS = (0.0, 0.25, 0.5, 0.75, 1.0)
# 3 req/s saturated the calibrated standard model with the cache off and every request
# sharing the system prompt, and at 1.5 req/s one row's p99 still grew by more than the limit
# when the run was doubled; 0.75 req/s passes in every row (checked below).
PREFIX_RATE_PER_S = 0.75


def _prefix_config(fraction: float) -> LoadConfig:
    return LoadConfig(
        seed=SEED,
        total_requests=300,
        arrival_rate_per_s=PREFIX_RATE_PER_S,
        model_weights={"standard": 1},
        prompt_lengths=(LengthBucket(20, 80, 1.0),),
        shared_prefix_fraction=fraction,
        shared_prefix_words=400,
        max_tokens=32,
    )


async def _prefix_runs(
    profile: ModelProfile,
) -> tuple[list[list[object]], dict[str, list[float]], list[tuple[str, Any]]]:
    """Cache off and on at each sharing level: rows, TTFT p50 series, saturation checks."""
    rows = []
    series: dict[str, list[float]] = {"cache on": [], "cache off": []}
    checks = []
    for fraction in PREFIX_FRACTIONS:
        config = _prefix_config(fraction)
        arrivals = [
            (p.at_ms, GenerateRequest(f"r{p.index}", p.model, p.prompt, p.max_tokens))
            for p in build_workload(config)
        ]
        row: list[object] = [f"{fraction:.0%}"]
        for name, cached in (("cache off", False), ("cache on", True)):
            loads = await saturation.check(
                config, profiles={Tier.STANDARD: profile}, prefix_cache=cached
            )
            checks.append((f"{fraction:.0%} sharing, {name}", loads))
            clock = SimClock()
            worker = ContinuousBatchingWorker(
                profile,
                SimulatedBackend(seed=SEED),
                clock,
                prefix_cache=PrefixCache(profile.kv_capacity_tokens) if cached else None,
            )
            results = await _run_arrivals(worker, clock, arrivals)
            ttft = [r.time_to_first_token_ms for r in results]
            series[name].append(percentile(ttft, 50))
            if any(m.saturated for m in loads):
                row.extend([SATURATED, SATURATED])
            else:
                row.extend([percentile(ttft, 50), percentile(ttft, 99)])
        cached_share = sum(r.cached_tokens for r in results) / sum(r.tokens_in for r in results)
        rows.append([*row, f"{cached_share:.0%}"])
    return rows, series, checks


async def prefix_cache() -> Section:
    found = calibration_in_use()
    if Tier.STANDARD not in found.profiles:
        return Section("2. Prefix cache on and off", "There is no standard tier to run on.")
    profile = found.profiles[Tier.STANDARD]
    rows, series, checks = await _prefix_runs(profile)

    fig, ax = plt.subplots(figsize=(7.5, 4.2), facecolor=SURFACE)
    xs = [100 * f for f in PREFIX_FRACTIONS]
    for index, name in enumerate(("cache on", "cache off")):
        ax.plot(
            xs,
            series[name],
            color=SERIES[index],
            marker=MARKERS[index],
            linewidth=2,
            markersize=6,
            label=name,
        )
    style_axes(
        ax,
        title="Median time to first token (simulated, calibrated standard profile)",
        xlabel="Requests sharing the system prompt (%)",
        ylabel="TTFT p50 (ms)",
    )
    ax.set_ylim(bottom=0)
    legend(ax, loc="upper left")
    fig.tight_layout()
    figure = _save(fig, "2-prefix-cache.png")

    body = f"""{_caveat(found)}

**The prefix cache's behaviour was never measured.** It is this repo's stand-in (a radix
tree over token ids, docs/04), and its capacity is the placeholder `kv_capacity_tokens`
({profile.kv_capacity_tokens:,} tokens), operator-supplied, not data. What is calibrated is
only the cost of the prefill it saves. No cost is shown: prices were never measured.

Standard tier, 300 requests at {PREFIX_RATE_PER_S:g} req/s, each with its own 20 to 80 word
prompt and 32 output tokens. A share of them also start with the same 400-word system prompt.
Prefill is {profile.prefill_ms_per_token:.4f} ms per token and every request has
{profile.per_request_overhead_ms:.1f} ms of fixed overhead (unstable), which the cache does not
remove.

{figure}

{
        _table(
            (
                "Sharing",
                "Off: TTFT p50",
                "Off: TTFT p99",
                "On: TTFT p50",
                "On: TTFT p99",
                "Prompt tokens from cache",
            ),
            rows,
        )
    }

As more requests carry the system prompt, the uncached run gets slower, because every one of
them prefills those 400 tokens again. With the cache, time to first token stays close to the
no-sharing level, which on this profile is mostly the fixed overhead. What the cache cannot
remove is the cost of the first sharers: a prefix only becomes reusable once a request
carrying it has finished, so requests that arrive before then pay in full.

{SATURATION_RULE}

{_verdict_table(checks)}"""
    return Section("2. Prefix cache on and off", body)


# --- 3. each model on the same workload -----------------------------------------------------

# 0.5 req/s offered the calibrated premium model more than its capacity on this workload;
# 0.2 req/s keeps every model under the saturation limit (checked below).
EACH_MODEL_RATE_PER_S = 0.2


def _each_model_config(model: str) -> LoadConfig:
    return LoadConfig(
        seed=SEED,
        total_requests=200,
        arrival_rate_per_s=EACH_MODEL_RATE_PER_S,
        model_weights={model: 1},
        prompt_lengths=(LengthBucket(20, 300, 1.0),),
        shared_prefix_fraction=0.5,
        max_tokens=64,
    )


async def _each_model(
    found: Calibration,
) -> tuple[list[tuple[object, ...]], list[tuple[str, Any]]]:
    rows: list[tuple[object, ...]] = []
    checks = []
    for tier, profile in found.profiles.items():
        config = _each_model_config(tier.value)
        loads = await saturation.check(config, profiles={tier: profile})
        checks.append((f"{tier.value} alone", loads))
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
        latency = [r.latency_ms for r in results]
        ttft = [r.time_to_first_token_ms for r in results]
        if any(m.saturated for m in loads):
            rows.append((tier.value, SATURATED, SATURATED, SATURATED, SATURATED))
        else:
            rows.append(
                (
                    tier.value,
                    percentile(ttft, 50),
                    percentile(ttft, 99),
                    percentile(latency, 50),
                    percentile(latency, 99),
                )
            )
    return rows, checks


async def each_model() -> Section:
    found = calibration_in_use()
    rows, checks = await _each_model(found)
    body = f"""{_caveat(found)}

The same 200 requests (20 to 300 word prompts, half sharing a 200-word system prompt, 64
output tokens, {EACH_MODEL_RATE_PER_S:g} req/s) served by each model alone, with continuous
batching and the prefix cache.

{_table(("Model", "TTFT p50 ms", "TTFT p99 ms", "Total p50 ms", "Total p99 ms"), rows)}

{SATURATION_RULE}

{_verdict_table(checks)}

**Cost and routing by plan are not reported.** The gateway routes each application to the models
its plan includes and meters the tokens each request uses (docs/09); what that costs depends on
prices, which were never measured. The prices in `gateway.profiles` are placeholders,
operator-supplied, not data, so no cost, saving or ratio between plans is given here. The
reason to pay for a larger model is the quality of its answers; section 7 measures that."""
    return Section("3. Each model on the same workload", body)


# --- 4. overload, and 5. chunked prefill: not measured --------------------------------------


async def overload_section() -> Section:
    body = """**Not measured on real data.** No results are given.

The mechanism is described in `docs/10-overload.md`: a queue limit per model (429 when full),
a request timeout (504), and SLO-aware downgrade, which moves requests that opt in from a model
whose recent p99 is over its SLO to a cheaper one in the application's plan. Its tests run on
hand-made test profiles and pin the behaviour, not a performance figure.

Why there are no numbers: the earlier results came from invented model profiles and have been
removed. On the calibrated profiles the old surge workload is many times what the premium
model can serve, so a run measures its backlog; and what the defences achieve rests on each
model's capacity, which depends on the cost per extra sequence and the per-request overhead,
both unstable on every tier, and on SLO thresholds that nobody measured. See Limitations."""
    return Section("4. Overload: admission control and downgrade", body)


async def chunked() -> Section:
    body = """**Not measured on real data.** No results are given.

The mechanism is described in `docs/11-streaming-chunked-prefill.md`: a long prompt is
prefilled a chunk at a time between decode steps, so it no longer stalls every running
request for its whole prefill. Its tests run on hand-made test profiles and pin the behaviour,
not a performance figure.

Why there are no numbers: the earlier results came from invented model profiles and have been
removed. The workload that shows the effect needs prompts of 6,000 to 10,000 words, far
beyond the 1,536 words the calibration measured and the 4,096-token context the models were run
with; and how a real server's prefill behaves when interleaved with decoding was never
measured. See Limitations."""
    return Section("5. Chunked prefill", body)


def saturation_workloads() -> dict[str, tuple[LoadConfig, bool]]:
    """The seeded workloads of sections 2 and 3, for ``benchmarks.saturation``."""
    named = {
        f"2. prefix cache, {f:.0%} sharing, cache {name}": (_prefix_config(f), cached)
        for f in PREFIX_FRACTIONS
        for name, cached in (("off", False), ("on", True))
    }
    named.update(
        {f"3. {tier.value} alone": (_each_model_config(tier.value), True) for tier in Tier}
    )
    return named


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
The fourth fitted parameter, a fixed overhead per request, with the R\u00b2 of the two regressions
and the simulator's error over all requests, split by whether a request was sent alone or with
the most others ({top} together). The errors by concurrency are where a poor fit shows:

{
        _table(
            (
                "Tier",
                "Per-request overhead ms",
                R_SQUARED + " TTFT fit",
                R_SQUARED + " decode fit",
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

The worst R\u00b2 of the two regressions over the runs, and the simulator's error over all requests,
split by whether a request was sent alone or with the most others ({top} together). The errors by
concurrency are where a poor fit shows:

{
        _table(
            (
                "Tier",
                f"Lowest {R_SQUARED} TTFT fit",
                f"Lowest {R_SQUARED} decode fit",
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
simulator with, and nothing in this report has been calibrated.

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
        error = profile["mape_percent"]
        rows.append(
            (
                tier,
                f"{server['model']} ({server['quantization']})",
                f"{profile['prefill_ms_per_token']:.4f}",
                f"{profile['decode_base_ms']:.2f}",
                f"{profile['decode_ms_per_extra_seq']:.2f}",
                f"{profile.get('per_request_overhead_ms', 0.0):.1f}",
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

Fitted timing parameters, which sections 1 to 3 run on, and the simulator's error against
held-out measurements (MAPE):

{
        _table(
            (
                "Tier",
                "Model",
                "Prefill ms/token",
                "Decode base ms",
                "ms per extra sequence",
                "Per-request overhead ms",
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

Sections 1 to 3 run on these fitted profiles, with batches of at most the concurrency that was
measured. See `docs/13-calibration.md` for how the fit is made and where the simulator diverges
from the real servers. To measure again:

```sh
{command}
```"""
    return Section("6. Simulated against real models", body)


# --- 7. answer quality ---------------------------------------------------------------------


def _points(value: float) -> str:
    return f"{100 * value:+.1f}"


def _difference(document: dict[str, Any], better: str, worse: str) -> dict[str, float] | None:
    found = document["differences"].get(f"{better}_minus_{worse}")
    return found if isinstance(found, dict) else None


async def quality() -> Section:
    title = "7. Answer quality"
    command = "uv run python -m benchmarks.quality_eval --backends <file>"
    if not QUALITY_RESULTS.exists():
        body = f"""**No quality evaluation has been run.** There is no
`benchmarks/quality/results.json`, so nothing here says what an application gives up by being
served by a smaller model.

To produce it, start the real models (`docs/12-real-models.md`) and run:

```sh
{command}
```

then regenerate this report. See `docs/14-quality-eval.md`."""
        return Section(title, body)

    document = json.loads(QUALITY_RESULTS.read_text(encoding="utf-8"))
    tiers = document["tiers"]
    method = document["method"]
    asked = document["questions"]
    machine = document["hardware"]
    servers = sorted({f"{s['kind']} {s['version']}" for s in document["servers"].values()})

    rows = []
    notes: list[str] = []
    option_rows = []
    widths = []
    for name, t in tiers.items():
        widths.append(100 * (t["ci95"]["high"] - t["ci95"]["low"]))
        answers = document["answers"][name]
        option_rows.append(
            (
                name,
                *(sum(1 for a in answers if a["chosen"] == letter) for letter in "ABCD"),
                t["unparsed"] + t["failed"],
            )
        )
        if t["unparsed"]:
            parsed = t["questions"] - t["unparsed"] - t["failed"]
            notes.append(
                f"- **{name}:** {t['unparsed']} replies did not open with an option letter and "
                f"count as wrong. Of the {parsed} that did, {t['correct']} were right "
                f"({100 * t['correct'] / parsed:.0f}%)."
            )
        rows.append(
            (
                name,
                document["servers"][name]["model"],
                f"{t['correct']} of {t['questions']}",
                f"{100 * t['accuracy']:.0f}%",
                f"{100 * t['ci95']['low']:.0f}% to {100 * t['ci95']['high']:.0f}%",
                t["unparsed"],
                "-" if t["latency_ms"]["total_p99"] is None else t["latency_ms"]["total_p99"],
            )
        )
        notes.extend(f"- **{name}:** {note}" for note in t["notes"])

    fig, ax = plt.subplots(figsize=(7.5, 4.4), facecolor=SURFACE)
    names = list(tiers)
    for index, name in enumerate(names):
        t = tiers[name]
        accuracy = 100 * t["accuracy"]
        ax.errorbar(
            [index],
            [accuracy],
            yerr=[[accuracy - 100 * t["ci95"]["low"]], [100 * t["ci95"]["high"] - accuracy]],
            fmt="o",
            color=SERIES[0],
            ecolor=SERIES[0],
            markersize=7,
            markeredgecolor=SURFACE,
            capsize=4,
            linewidth=1.5,
        )
        ax.annotate(
            f"{accuracy:.0f}%",
            (index, accuracy),
            xytext=(9, -3),
            textcoords="offset points",
            color=INK,
            fontsize=9,
        )
    ax.axhline(25, color=INK, linewidth=1, linestyle=":")
    ax.annotate(
        "guessing",
        (len(names) - 0.6, 25),
        xytext=(0, 4),
        textcoords="offset points",
        color=INK,
        fontsize=8,
    )
    style_axes(
        ax,
        title=f"Accuracy on {asked['count']} questions, measured once (bars: 95% interval)",
        xlabel="Tier",
        ylabel="Accuracy, %",
    )
    ax.set_xticks(range(len(names)), names)
    ax.set_xlim(-0.5, len(names) - 0.5)
    ax.set_ylim(0, 100)
    fig.tight_layout()
    figure = _save(fig, "7-quality.png")

    difference_lines = []
    for pair, d in document["differences"].items():
        better, worse = pair.split("_minus_")
        apart = d["low"] > 0 or d["high"] < 0
        difference_lines.append(
            f"- **{better} against {worse}:** {_points(d['difference'])} points "
            f"(95% interval {_points(d['low'])} to {_points(d['high'])}). "
            + (
                "The interval excludes zero."
                if apart
                else "The interval includes zero: on this sample the two cannot be told apart."
            )
        )

    first = next(iter(document["answers"].values()))
    option_rows.append(
        ("right answer", *(sum(1 for a in first if a["gold"] == letter) for letter in "ABCD"), 0)
    )

    plans = load_plans()
    best = Tier.PREMIUM.value
    plan_rows = []
    for plan_name, plan in plans.plans.items():
        model = plan.models[-1] if plan.models else "-"
        d = _difference(document, best, model)
        if model == best:
            given_up = "nothing: this plan includes the best model"
        elif d is None or model not in tiers:
            given_up = "not measured"
        else:
            given_up = (
                f"{_points(-d['difference'])} points "
                f"({_points(-d['high'])} to {_points(-d['low'])})"
            )
        plan_rows.append(
            (
                plan_name,
                model,
                f"{100 * tiers[model]['accuracy']:.0f}%" if model in tiers else "-",
                given_up,
            )
        )

    body = f"""**Measured on real models.** No simulated number is in this section.

**This is a small, one-run measurement: {asked["count"]} questions, each asked once.** The 95%
intervals around the accuracies below are {min(widths):.0f} to {max(widths):.0f} points wide, and
nothing here was repeated.

| | |
|---|---|
| Questions | {asked["count"]} multiple-choice questions from {asked["name"]}, license {
        asked["license"]
    }; {asked["selection"]} |
| Method | temperature {method["temperature"]:g}, seed {method["seed"]}, at most {
        method["max_tokens"]
    } output tokens, one fixed prompt template, one request at a time |
| Scoring | {method["scoring"]} |
| Measured at | {document["measured_at"]} |
| Machine | {machine["cpu"]}, {machine["ram_gb"]} GB RAM, {machine["gpu"]} |
| Model server | {", ".join(servers)} |

{figure}

{
        _table(
            (
                "Tier",
                "Model",
                "Correct",
                "Accuracy",
                "95% interval",
                "Replies with no option letter",
                "p99 latency ms of these questions",
            ),
            rows,
        )
    }

A question has four options, so guessing scores about 25%. The latency is of a prompt with a
one-letter answer, one request at a time, through Ollama: it says nothing about this repo's
scheduler.

{chr(10).join(notes) if notes else ""}

Which option each model chose, against how often each is the right answer. A model that
leans towards one letter is answering the format as well as the question:

{_table(("Tier", "Chose A", "Chose B", "Chose C", "Chose D", "No option"), option_rows)}

Differences between tiers on the same questions:

{chr(10).join(difference_lines)}

**What a plan gives up in accuracy.** The best model each plan includes, with the accuracy
measured for it and the difference against {best} on the same questions, with its 95% interval.
No cost is shown: prices were never measured.

{
        _table(
            ("Plan", "Best model included", "Accuracy", f"Accuracy given up against {best}"),
            plan_rows,
        )
    }

The accuracy given up is an estimate from {asked["count"]} questions of one kind.
Multiple-choice recall is not what applications send, and a plan is worth what its model does on
their own requests. See `docs/14-quality-eval.md`. To measure again:

```sh
{command}
```"""
    return Section(title, body)


# --- the report ----------------------------------------------------------------------------

SECTIONS: dict[str, Callable[[], Awaitable[Section]]] = {
    "batching": batching,
    "prefix-cache": prefix_cache,
    "each-model": each_model,
    "overload": overload_section,
    "chunked-prefill": chunked,
    "calibration": calibration,
    "quality": quality,
}

LIMITATIONS = """## Limitations

- **Sections 1 to 3 are simulations.** No model runs. Timings come from a cost model with four
  parameters per model, fitted to real measurements: the median of six calibration runs on one
  laptop, with Ollama, on one day. Read every figure as "how this scheduler behaves with these
  step costs", never as the performance of Llama models or of a model server.
- **Seven of the twelve fitted parameters are unstable**: they did not repeat between
  calibration runs. They include the cost per extra sequence on every tier, which the batching
  results rest on, and the per-request overhead on every tier. Section 1 shows how far the
  batching gain moves across the measured values.
- **The premium tier is hardware-limited**: the 8B model did not fit the 8 GB GPU and ran partly
  on the CPU. Its timings are of that laptop's limits as much as of the model.
- **Batches stop at 4**, the most requests that were measured together. Nothing in this report
  batches more, so nothing extrapolates the fitted cost past that; how the models behave with
  larger batches is not known.
- **Never measured: prices, `kv_capacity_tokens`, and the prefix cache's behaviour.** The
  prices and KV capacities in `gateway.profiles` are placeholders, operator-supplied, not data.
  No cost, saving or ratio between plans is reported. The prefix cache is this repo's
  stand-in, not a measurement of a real server's cache.
- **Overload (section 4): not measured on real data.** No results. The earlier ones came from
  invented profiles; on the calibrated ones the workload is saturated, and the outcome rests on
  unstable parameters and unmeasured SLO thresholds.
- **Chunked prefill (section 5): not measured on real data.** No results. The workload that
  shows it needs prompts far beyond the measured range and the models' configured context.
- **Saturated runs are not reported as latency.** Every simulated workload here was checked
  first (`uv run python -m benchmarks.saturation`); a saturated run's p99 is left out, because
  it measures the length of the run.
- **The simulator is simplified** in ways that matter: prefill is linear in tokens and blocks the
  batch unless chunked; requests admitted together are prefilled one after another; a decode
  step is linear in batch size; output length is known when a request is admitted; one token is
  one whitespace-separated word; each model has its hardware to itself. Against the real models
  its time to first token was 17% to 49% off with four requests together (section 6).
- **Each result is one seed and one workload.** They are exactly reproducible, which is not the
  same as representative. p99 over a few hundred requests is a handful of samples.
- **Answer quality is one small measurement.** Section 7 asks each model a hundred
  multiple-choice questions, once. That is enough to see a large gap between models and not
  enough to size a small one, and it says nothing about other kinds of request.
- **Sections 6 and 7 are one machine.** Real measurements are noisy and specific to that
  hardware, model build and server version. They are not re-measured when this report is
  regenerated.
- **Simulated and real numbers must not be compared directly.** With real models the model
  server does the batching, not this repo's scheduler (`docs/12-real-models.md`).
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

**Sections 1 to 3 are simulated** with seed {SEED} on the calibrated model profiles: the median
of six calibration runs of the real models, on one laptop with Ollama, the 8B model partly on
the CPU, with batches of at most the 4 requests that were measured together. They are
deterministic: the same code and calibration give the same numbers. **Sections 4 and 5 have no
results: not measured on real data.** **Sections 6 and 7 are measured** on real models and are
read from files, not re-measured. Nothing here is a benchmark of real hardware except where
those sections say so.

Times are in milliseconds unless stated. Percentiles are nearest-rank.
"""
    return "\n".join([header, *parts, LIMITATIONS])


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
