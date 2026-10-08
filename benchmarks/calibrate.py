"""Calibrate the simulator's model profiles against real model servers.

    uv run python -m benchmarks.calibrate --backends examples/backends.ollama.yaml \
        --out benchmarks/calibration

For each tier it sends controlled requests to the real model (see docs/12-real-models.md
for starting one), measures time to first token and per-token latency, fits the three
timing parameters of the simulator's profile by linear regression, then replays the same
requests through the simulator and reports how far it is from what was measured.

Writes ``calibrated_profiles.json``, ``samples.json`` and one plot per tier. Everything in
those files is measured on the machine that ran this, which the JSON records.
See docs/13-calibration.md.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import dataclasses
import datetime
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from gateway.backend_config import (
    BackendConfig,
    LlamaServer,
    build_llamacpp_registry,
    load_backend_config,
)
from gateway.backends import Backend, BackendTokenCallback, Generation
from gateway.clock import Clock, SimClock, WallClock
from gateway.continuous import ContinuousBatchingWorker
from gateway.overload import run_with_timeout
from gateway.profiles import PROFILES, ModelProfile, Tier
from gateway.registry import ModelRegistry
from gateway.schema import GenerateRequest, RequestResult

SCHEMA_VERSION = 1
# A fitted time has to be positive to be a valid profile; this is the floor used when a
# fit comes out at or below zero, which is then flagged in the notes.
MIN_MS = 1e-6

_WORDS = [
    "river",
    "stone",
    "garden",
    "window",
    "paper",
    "silver",
    "morning",
    "quiet",
    "engine",
    "market",
    "forest",
    "letter",
    "bridge",
    "yellow",
    "simple",
    "distant",
    "copper",
    "winter",
    "harbor",
    "signal",
    "number",
    "travel",
    "pocket",
    "candle",
    "meadow",
    "orange",
    "lantern",
    "station",
    "marble",
    "thunder",
    "valley",
    "ribbon",
    "anchor",
    "mirror",
    "saddle",
    "timber",
]
_ASK = "Write a very long and detailed story about this. Do not stop early."


@dataclass(frozen=True, slots=True)
class Scenario:
    sweep: str  # which parameter this scenario varies: prompt, output or concurrency
    prompt_words: int
    max_tokens: int
    concurrency: int


@dataclass(frozen=True, slots=True)
class CalibrationPlan:
    prompt_words: tuple[int, ...] = (32, 128, 512, 1536)
    output_tokens: tuple[int, ...] = (16, 64, 128)
    concurrency: tuple[int, ...] = (1, 2, 4)
    # Each scenario runs this many times. With two or more, the last run is held out: it
    # is not used for fitting, only for checking the fit.
    repeats: int = 3
    base_prompt_words: int = 32
    base_output_tokens: int = 64
    prompt_sweep_output_tokens: int = 8
    request_timeout_ms: float = 300_000.0

    def __post_init__(self) -> None:
        if self.repeats < 1:
            raise ValueError("repeats must be at least 1")
        if not (self.prompt_words and self.output_tokens and self.concurrency):
            raise ValueError("every sweep needs at least one value")
        if min(self.concurrency) < 1 or min(self.prompt_words) < 1 or min(self.output_tokens) < 2:
            raise ValueError("need concurrency >= 1, prompt words >= 1, output tokens >= 2")

    def scenarios(self) -> list[Scenario]:
        found = [
            *(Scenario("prompt", p, self.prompt_sweep_output_tokens, 1) for p in self.prompt_words),
            *(Scenario("output", self.base_prompt_words, o, 1) for o in self.output_tokens),
            *(
                Scenario("concurrency", self.base_prompt_words, self.base_output_tokens, c)
                for c in self.concurrency
            ),
        ]
        return list(dict.fromkeys(found))


@dataclass(frozen=True, slots=True)
class Sample:
    """One real request: what was asked, and what was measured."""

    tier: str
    sweep: str
    prompt_words: int
    max_tokens: int
    concurrency: int
    repeat: int
    holdout: bool
    prompt_tokens: int
    completion_tokens: int
    ttft_ms: float
    total_ms: float
    # Mean gap between streamed pieces; None if fewer than two arrived.
    per_token_ms: float | None


@dataclass(frozen=True, slots=True)
class Fit:
    slope: float
    intercept: float
    # None when every x is the same, so a slope cannot be estimated.
    r2: float | None
    n: int


def linear_fit(xs: Sequence[float], ys: Sequence[float]) -> Fit:
    """Ordinary least squares for y = intercept + slope * x."""
    n = len(xs)
    if n == 0 or n != len(ys):
        raise ValueError("need equally many x and y values, and at least one")
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx == 0:
        return Fit(slope=0.0, intercept=mean_y, r2=None, n=n)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / sxx
    intercept = mean_y - slope * mean_x
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys, strict=True))
    return Fit(slope, intercept, 1 - ss_res / ss_tot if ss_tot else 1.0, n)


def mape(measured: Sequence[float], predicted: Sequence[float]) -> float | None:
    """Mean absolute percentage error of ``predicted`` against ``measured``."""
    pairs = [(m, p) for m, p in zip(measured, predicted, strict=True) if m > 0]
    if not pairs:
        return None
    return 100 * sum(abs(p - m) / m for m, p in pairs) / len(pairs)


def _prompt(run_id: str, tier: str, scenario: Scenario, repeat: int, slot: int) -> str:
    # The prompt starts with something unique to this request, so the model server cannot
    # serve any of it from a cached prefix and every prompt token is really processed.
    tag = (
        f"[{run_id}-{tier}-{scenario.sweep}-{scenario.prompt_words}-{scenario.max_tokens}"
        f"-{scenario.concurrency}-{repeat}-{slot}]"
    )
    words = [_WORDS[(i * 7 + slot + repeat) % len(_WORDS)] for i in range(scenario.prompt_words)]
    return f"{tag} {' '.join(words)}\n\n{_ASK}\n"


def _sample(tier: str, scenario: Scenario, repeat: int, holdout: bool, r: RequestResult) -> Sample:
    times = r.token_times_ms
    return Sample(
        tier=tier,
        sweep=scenario.sweep,
        prompt_words=scenario.prompt_words,
        max_tokens=scenario.max_tokens,
        concurrency=scenario.concurrency,
        repeat=repeat,
        holdout=holdout,
        prompt_tokens=r.tokens_in,
        completion_tokens=r.tokens_out,
        ttft_ms=r.time_to_first_token_ms,
        total_ms=r.latency_ms,
        per_token_ms=(times[-1] - times[0]) / (len(times) - 1) if len(times) >= 2 else None,
    )


async def measure_tier(
    registry: ModelRegistry, clock: Clock, tier: str, plan: CalibrationPlan, run_id: str
) -> tuple[list[Sample], list[str]]:
    """Run the plan against one tier. Returns the samples and a note per failed request."""
    worker = registry.get(tier)
    # One request first, thrown away: it makes the server load the model. If the server
    # is down this is where it shows, as an error rather than as an empty result.
    warm_up = Scenario("warm-up", plan.base_prompt_words, 4, 1)
    await worker.submit(GenerateRequest("warm-up", tier, _prompt(run_id, tier, warm_up, 0, 0), 4))

    samples: list[Sample] = []
    failures: list[str] = []
    for scenario in plan.scenarios():
        for repeat in range(plan.repeats):
            holdout = plan.repeats >= 2 and repeat == plan.repeats - 1
            requests = [
                GenerateRequest(
                    f"{scenario.sweep}-{repeat}-{slot}",
                    tier,
                    _prompt(run_id, tier, scenario, repeat, slot),
                    scenario.max_tokens,
                )
                for slot in range(scenario.concurrency)
            ]
            outcomes = await asyncio.gather(
                *(
                    run_with_timeout(clock, worker.submit(r), plan.request_timeout_ms)
                    for r in requests
                ),
                return_exceptions=True,
            )
            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    failures.append(f"{tier} {scenario}: {type(outcome).__name__}: {outcome}")
                else:
                    samples.append(_sample(tier, scenario, repeat, holdout, outcome))
    return samples, failures


@dataclass(frozen=True, slots=True)
class TierFit:
    profile: ModelProfile
    prefill: Fit  # TTFT against prompt tokens, at concurrency 1
    decode: Fit  # per-token latency against (concurrency - 1)
    max_concurrency_measured: int
    notes: tuple[str, ...]


def fit_profile(samples: Sequence[Sample], base: ModelProfile) -> TierFit:
    """Fit the three timing parameters from the samples that are not held out.

    ``prefill_ms_per_token`` is the slope of TTFT against prompt tokens at concurrency 1.
    ``decode_base_ms`` and ``decode_ms_per_extra_seq`` are the intercept and slope of
    per-token latency against the number of *other* requests decoding at the same time.
    Everything else in the profile is copied from ``base``.
    """
    used = [s for s in samples if not s.holdout]
    alone = [s for s in used if s.concurrency == 1]
    decoding = [s for s in used if s.per_token_ms is not None]
    if not alone or not decoding:
        raise ValueError("not enough successful requests to fit a profile")
    notes: list[str] = []

    prefill = linear_fit([s.prompt_tokens for s in alone], [s.ttft_ms for s in alone])
    if prefill.r2 is None:
        notes.append("all prompts had the same token count; prefill cost could not be fitted")
    prefill_ms = prefill.slope
    if prefill_ms <= 0:
        notes.append(
            f"fitted prefill_ms_per_token was {prefill_ms:.6g}, not positive; "
            f"set to {MIN_MS:g}. Prompt processing is too fast or too noisy to resolve here."
        )
        prefill_ms = MIN_MS

    decode = linear_fit(
        [s.concurrency - 1 for s in decoding],
        [s.per_token_ms for s in decoding if s.per_token_ms is not None],
    )
    if decode.r2 is None:
        notes.append(
            "only one concurrency level was measured; decode_ms_per_extra_seq is set to 0 "
            "and says nothing about batching"
        )
    decode_base_ms = decode.intercept
    extra_ms = decode.slope
    if decode_base_ms <= 0:
        notes.append(f"fitted decode_base_ms was {decode_base_ms:.6g}; set to {MIN_MS:g}")
        decode_base_ms = MIN_MS
    if extra_ms < 0:
        notes.append(
            f"fitted decode_ms_per_extra_seq was {extra_ms:.6g}, negative; set to 0. "
            "Per-token latency did not grow with concurrency in this range."
        )
        extra_ms = 0.0

    queued = _queueing_note(used)
    if queued:
        notes.append(queued)

    profile = dataclasses.replace(
        base,
        prefill_ms_per_token=prefill_ms,
        decode_base_ms=decode_base_ms,
        decode_ms_per_extra_seq=extra_ms,
    )
    return TierFit(profile, prefill, decode, max(s.concurrency for s in used), tuple(notes))


def _queueing_note(samples: Sequence[Sample]) -> str | None:
    """Say so if requests sent together look as if they were served one after another.

    A server with fewer free slots than requests queues the rest. Their first token then
    arrives late while their per-token latency stays the same, and the concurrency sweep
    measures the server's queue, not the cost of batching.
    """
    sweep = [s for s in samples if s.sweep == "concurrency"]
    alone = [s.total_ms for s in sweep if s.concurrency == 1]
    top = max((s.concurrency for s in sweep), default=1)
    together = [s.ttft_ms for s in sweep if s.concurrency == top]
    if top == 1 or not alone:
        return None
    # Batched requests start producing tokens at about the same time. A request whose
    # first token took nearly as long as a whole request does alone was waiting for one.
    whole_request_alone = sum(alone) / len(alone)
    slowest_together = max(together)
    if slowest_together < 0.8 * whole_request_alone:
        return None
    return (
        f"with {top} requests sent together, the slowest waited {slowest_together:.0f} ms for "
        f"its first token, about as long as a whole request takes alone "
        f"({whole_request_alone:.0f} ms). The server appears to have served them one after "
        "another (fewer slots than requests), so decode_ms_per_extra_seq was not really "
        f"measured. Give the server at least {top} parallel slots and run again."
    )


class _ReplayBackend(Backend):
    """Gives the simulator the token counts the real model reported, request by request."""

    def __init__(self, samples: Sequence[Sample]) -> None:
        self._samples = samples

    async def generate(
        self, request: GenerateRequest, *, on_token: BackendTokenCallback | None = None
    ) -> Generation:
        sample = self._samples[int(request.request_id)]
        return Generation(
            prompt_tokens=(),
            tokens_out=max(1, sample.completion_tokens),
            prompt_token_count=sample.prompt_tokens,
        )


async def simulate(samples: Sequence[Sample], profile: ModelProfile) -> list[tuple[float, float]]:
    """Replay the samples through the simulator; returns (TTFT, total) for each, in order.

    Requests that were sent together are submitted together, to a fresh
    ``ContinuousBatchingWorker`` on simulated time, with the token counts the real model
    reported. Limits are lifted so the comparison is about timing only.
    """
    roomy = dataclasses.replace(
        profile,
        max_batch_size=max(profile.max_batch_size, *(s.concurrency for s in samples)),
        kv_capacity_tokens=10**9,
    )
    groups: dict[tuple[str, int, int, int, int], list[int]] = {}
    for i, s in enumerate(samples):
        key = (s.sweep, s.prompt_words, s.max_tokens, s.concurrency, s.repeat)
        groups.setdefault(key, []).append(i)

    simulated: list[tuple[float, float]] = [(0.0, 0.0)] * len(samples)
    for indices in groups.values():
        worker = ContinuousBatchingWorker(roomy, _ReplayBackend(samples), SimClock())
        results = await asyncio.gather(
            *(worker.submit(GenerateRequest(str(i), roomy.tier.value, "")) for i in indices)
        )
        for i, result in zip(indices, results, strict=True):
            simulated[i] = (result.time_to_first_token_ms, result.latency_ms)
    return simulated


@dataclass(frozen=True, slots=True)
class Errors:
    """MAPE of the simulator against the measurements, in percent. None if no samples."""

    ttft_fit: float | None
    total_fit: float | None
    ttft_holdout: float | None
    total_holdout: float | None


def errors(samples: Sequence[Sample], simulated: Sequence[tuple[float, float]]) -> Errors:
    def one(holdout: bool, which: int) -> float | None:
        rows = [(s, sim) for s, sim in zip(samples, simulated, strict=True) if s.holdout == holdout]
        measured = [s.ttft_ms if which == 0 else s.total_ms for s, _ in rows]
        return mape(measured, [sim[which] for _, sim in rows])

    return Errors(one(False, 0), one(False, 1), one(True, 0), one(True, 1))


# --- plots ---------------------------------------------------------------------------------

_SURFACE = "#fcfcfb"
_INK = "#0b0b0b"
_INK_SECONDARY = "#52514e"
_MUTED = "#898781"
_GRID = "#e1e0d9"
_AXIS = "#c3c2b7"
_SERIES = "#2a78d6"
# Concurrency is shown by marker shape, so it does not depend on telling colours apart.
_MARKERS = ("o", "s", "^", "D", "v", "P", "X")


def plot_tier(
    path: Path,
    title: str,
    samples: Sequence[Sample],
    simulated: Sequence[tuple[float, float]],
) -> None:
    """Simulated against measured TTFT and total latency, and the residuals of each."""
    levels = sorted({s.concurrency for s in samples})
    fig, axes = plt.subplots(2, 2, figsize=(11, 8.5), facecolor=_SURFACE)
    panels = (
        (0, "Time to first token", "Prompt tokens"),
        (1, "Total latency", "Output tokens"),
    )
    for column, name, x_label in panels:
        top, bottom = axes[0][column], axes[1][column]
        measured = [s.ttft_ms if column == 0 else s.total_ms for s in samples]
        xs = [s.prompt_tokens if column == 0 else s.completion_tokens for s in samples]
        predicted = [sim[column] for sim in simulated]
        limit = max(*measured, *predicted) * 1.05
        top.plot([0, limit], [0, limit], color=_MUTED, linewidth=1, linestyle=(0, (4, 3)))
        top.annotate(
            "simulated = measured",
            (limit, limit),
            xytext=(-6, -14),
            textcoords="offset points",
            ha="right",
            color=_INK_SECONDARY,
            fontsize=8,
        )
        bottom.axhline(0, color=_MUTED, linewidth=1)
        for level, marker in zip(levels, _MARKERS, strict=False):
            rows = [i for i, s in enumerate(samples) if s.concurrency == level]
            style = {
                "marker": marker,
                "s": 46,
                "color": _SERIES,
                "edgecolors": _SURFACE,
                "linewidths": 1.2,
                "label": f"{level} at once",
            }
            top.scatter([measured[i] for i in rows], [predicted[i] for i in rows], **style)
            bottom.scatter(
                [xs[i] for i in rows],
                [predicted[i] - measured[i] for i in rows],
                **style,
            )
        error = mape(measured, predicted)
        top.set_title(
            f"{name}: simulated vs measured"
            + (f" (MAPE {error:.1f}%)" if error is not None else ""),
            color=_INK,
            fontsize=11,
            loc="left",
        )
        top.set_xlabel("Measured (ms)")
        top.set_ylabel("Simulated (ms)")
        top.set_xlim(0, limit)
        top.set_ylim(0, limit)
        bottom.set_title(
            f"{name}: residual (simulated - measured)", color=_INK, fontsize=11, loc="left"
        )
        bottom.set_xlabel(x_label)
        bottom.set_ylabel("Residual (ms)")
    for ax in axes.flat:
        ax.set_facecolor(_SURFACE)
        ax.grid(True, color=_GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        ax.tick_params(colors=_MUTED, labelsize=8)
        ax.xaxis.label.set_color(_INK_SECONDARY)
        ax.yaxis.label.set_color(_INK_SECONDARY)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(_AXIS)
    # One legend for the whole figure, outside the plots so it never covers a point.
    handles, labels = axes[0][0].get_legend_handles_labels()
    fig.legend(
        handles,
        labels,
        title="Requests sent together",
        frameon=False,
        fontsize=8,
        title_fontsize=8,
        loc="upper right",
        ncol=len(labels),
    )
    fig.suptitle(title, color=_INK, fontsize=13, x=0.01, ha="left")
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(path, dpi=130, facecolor=_SURFACE)
    plt.close(fig)


# --- what this ran on ----------------------------------------------------------------------


def _total_ram_gb() -> float | None:
    # Looked up by name so that this type-checks the same on every platform.
    try:
        if platform.system() == "Windows":

            class MemoryStatus(ctypes.Structure):
                _fields_ = (
                    ("length", ctypes.c_ulong),
                    ("load", ctypes.c_ulong),
                    ("total_phys", ctypes.c_ulonglong),
                    ("rest", ctypes.c_ulonglong * 6),
                )

            status = MemoryStatus()
            status.length = ctypes.sizeof(MemoryStatus)
            kernel32: Any = getattr(ctypes, "windll").kernel32  # noqa: B009
            if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return None
            total_bytes = int(status.total_phys)
        else:
            sysconf: Any = getattr(os, "sysconf")  # noqa: B009
            total_bytes = int(sysconf("SC_PHYS_PAGES")) * int(sysconf("SC_PAGE_SIZE"))
    except (AttributeError, OSError, ValueError):
        return None
    return round(total_bytes / 2**30, 1)


def _run(command: list[str]) -> str | None:
    if shutil.which(command[0]) is None:
        return None
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() or None


def _cpu_name() -> str:
    if sys.platform == "win32":
        name = _run(
            ["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor).Name"]
        )
        if name:
            return name
    if sys.platform.startswith("linux"):
        try:
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
        except OSError:
            pass
    if sys.platform == "darwin":
        name = _run(["sysctl", "-n", "machdep.cpu.brand_string"])
        if name:
            return name
    return platform.processor() or "unknown"


async def describe_server(client: httpx.AsyncClient, server: LlamaServer) -> dict[str, Any]:
    """Ask a model server what it is. Anything it will not say is recorded as unknown."""
    info: dict[str, Any] = {
        "url": server.url,
        "kind": "unknown",
        "version": "unknown",
        "model": server.model or "unknown",
        "quantization": "unknown",
    }
    try:
        version = await client.get(f"{server.url}/api/version", timeout=5)
        if version.status_code == 200 and "version" in version.json():
            info["kind"] = "ollama"
            info["version"] = str(version.json()["version"])
            if server.model:
                shown = await client.post(
                    f"{server.url}/api/show", json={"model": server.model}, timeout=10
                )
                details = shown.json().get("details", {}) if shown.status_code == 200 else {}
                info["quantization"] = str(details.get("quantization_level", "unknown"))
                info["parameter_size"] = str(details.get("parameter_size", "unknown"))
            return info
        props = await client.get(f"{server.url}/props", timeout=5)
        if props.status_code == 200:
            data = props.json()
            info["kind"] = "llama-server"
            info["version"] = str(data.get("build_info", "unknown"))
            model_path = str(data.get("model_path", ""))
            if model_path:
                info["model"] = Path(model_path).name
                found = re.search(r"(Q\d+_[A-Z0-9_]+|Q\d+|BF16|F16|F32)", model_path, re.I)
                if found:
                    info["quantization"] = found.group(1).upper()
    except (httpx.HTTPError, ValueError):
        pass
    return info


def describe_machine() -> dict[str, Any]:
    return {
        "cpu": _cpu_name(),
        "logical_cores": os.cpu_count(),
        "ram_gb": _total_ram_gb(),
        "gpu": _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"])
        or "none detected (nvidia-smi not available)",
        "os": f"{platform.system()} {platform.release()} ({platform.version()})",
        "machine": platform.machine(),
        "python": platform.python_version(),
    }


# --- putting it together -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TierRun:
    title: str
    samples: list[Sample]
    simulated: list[tuple[float, float]]


@dataclass(frozen=True, slots=True)
class CalibrationRun:
    # What goes into calibrated_profiles.json.
    document: dict[str, Any]
    tiers: dict[str, TierRun]


async def calibrate(
    config: BackendConfig,
    plan: CalibrationPlan,
    *,
    tiers: Sequence[Tier],
    clock: Clock,
    client: httpx.AsyncClient,
    run_id: str,
    measured_at: str,
    machine: dict[str, Any],
    notes: str = "",
) -> CalibrationRun:
    """Measure each tier, fit its profile, and replay the requests through the simulator."""
    registry = build_llamacpp_registry(config, clock, client=client)
    document: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "what_this_is": (
            "Simulator timing parameters fitted to real measurements on the machine "
            "described under 'hardware'. They do not transfer to other hardware, models, "
            "quantizations or server versions."
        ),
        "measured_at": measured_at,
        "hardware": machine,
        "endpoint": config.endpoint,
        "plan": dataclasses.asdict(plan),
        "notes": notes,
        "servers": {},
        "profiles": {},
    }
    runs: dict[str, TierRun] = {}
    for tier in tiers:
        name = tier.value
        document["servers"][name] = await describe_server(client, config.servers[tier])
        samples, failures = await measure_tier(registry, clock, name, plan, run_id)
        fitted = fit_profile(samples, PROFILES[tier])
        simulated = await simulate(samples, fitted.profile)
        error = errors(samples, simulated)
        runs[name] = TierRun(
            f"{name} ({document['servers'][name]['model']}): simulator vs real model",
            samples,
            simulated,
        )
        document["profiles"][name] = {
            "prefill_ms_per_token": fitted.profile.prefill_ms_per_token,
            "decode_base_ms": fitted.profile.decode_base_ms,
            "decode_ms_per_extra_seq": fitted.profile.decode_ms_per_extra_seq,
            "max_concurrency_measured": fitted.max_concurrency_measured,
            "samples": len(samples),
            "failed_requests": len(failures),
            "fit": {
                "ttft_vs_prompt_tokens": dataclasses.asdict(fitted.prefill),
                "per_token_vs_extra_sequences": dataclasses.asdict(fitted.decode),
            },
            "mape_percent": dataclasses.asdict(error),
            "notes": [*fitted.notes, *failures],
        }
    return CalibrationRun(document, runs)


def write_outputs(run: CalibrationRun, out_dir: Path) -> None:
    """Write calibrated_profiles.json, samples.json and one plot per tier."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, tier in run.tiers.items():
        plot_tier(out_dir / f"{name}.png", tier.title, tier.samples, tier.simulated)
    (out_dir / "calibrated_profiles.json").write_text(
        json.dumps(run.document, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    samples = [dataclasses.asdict(s) for tier in run.tiers.values() for s in tier.samples]
    (out_dir / "samples.json").write_text(
        json.dumps(samples, indent=1) + "\n", encoding="utf-8", newline="\n"
    )


def summary(document: dict[str, Any]) -> str:
    def number(value: float | None, spec: str = ".1f") -> str:
        return "-" if value is None else format(value, spec)

    lines = [
        f"{'tier':9} {'prefill ms/tok':>14} {'decode base ms':>14} {'ms per extra':>12} "
        f"{'n':>4} {'TTFT MAPE fit':>13} {'held out':>8} {'total MAPE fit':>14} {'held out':>8}"
    ]
    for tier, p in document["profiles"].items():
        e = p["mape_percent"]
        lines.append(
            f"{tier:9} {p['prefill_ms_per_token']:14.4f} {p['decode_base_ms']:14.3f} "
            f"{p['decode_ms_per_extra_seq']:12.3f} {p['samples']:4d} "
            f"{number(e['ttft_fit']):>12}% {number(e['ttft_holdout']):>7}% "
            f"{number(e['total_fit']):>13}% {number(e['total_holdout']):>7}%"
        )
        lines.extend(f"  note: {note}" for note in p["notes"])
    return "\n".join(lines)


def _ints(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split(","))


async def _measure(args: argparse.Namespace) -> CalibrationRun:
    config = load_backend_config(args.backends, kind_override="llamacpp")
    defaults = CalibrationPlan()
    plan = CalibrationPlan(
        prompt_words=args.prompt_words or defaults.prompt_words,
        output_tokens=args.output_tokens or defaults.output_tokens,
        concurrency=args.concurrency or defaults.concurrency,
        repeats=args.repeats,
    )
    now = datetime.datetime.now(datetime.UTC)
    async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5.0)) as client:
        return await calibrate(
            config,
            plan,
            tiers=[Tier(name) for name in args.tiers.split(",")],
            clock=WallClock(),
            client=client,
            run_id=now.strftime("%Y%m%d%H%M%S"),
            measured_at=now.isoformat(timespec="seconds"),
            machine=describe_machine(),
            notes=args.notes,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Calibrate simulator profiles on real models.")
    parser.add_argument("--backends", type=Path, help="backends file naming the model servers")
    parser.add_argument("--out", type=Path, default=Path("benchmarks/calibration"))
    parser.add_argument("--tiers", default="economy,standard,premium")
    parser.add_argument("--prompt-words", type=_ints, help="e.g. 32,128,512,1536")
    parser.add_argument("--output-tokens", type=_ints, help="e.g. 16,64,128")
    parser.add_argument("--concurrency", type=_ints, help="e.g. 1,2,4; at most the server's slots")
    parser.add_argument("--repeats", type=int, default=CalibrationPlan().repeats)
    parser.add_argument("--notes", default="", help="free text stored in the output")
    args = parser.parse_args()
    run = asyncio.run(_measure(args))
    write_outputs(run, args.out)
    print("MEASURED on this machine against real model servers.")
    print(json.dumps(run.document["hardware"], indent=2))
    print(summary(run.document))
    print(f"wrote {args.out / 'calibrated_profiles.json'}, samples.json and one plot per tier")


if __name__ == "__main__":
    main()
