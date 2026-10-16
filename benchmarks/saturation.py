"""Is any model saturated by a workload? Check before reporting a latency from it.

Simulated, on the default (calibrated) profiles: nothing here is a measurement of a real model.

    uv run python -m benchmarks.saturation                 # the named workloads below
    uv run python -m benchmarks.saturation --rate 1.0      # the gate's mix at another rate

For each model a workload sends requests to, it reports:

- capacity: requests per second the continuous worker manages when that model's own
  requests from the workload all arrive at once (a burst), with the prefix cache on;
- utilisation: the workload's offered rate for that model divided by that capacity;
- the p99 total latency of the workload, and of the same workload with twice as many
  requests at the same rate. A p99 that grows with the length of the run is a backlog.

A model counts as saturated when utilisation is above ``MAX_UTILISATION`` or the p99 grows
by more than ``MAX_P99_GROWTH`` when the run is doubled. A saturated run's p99 measures how
long the run was, not latency, and must not be reported as a latency result.

Capacity depends on ``decode_ms_per_extra_seq``, which the calibration marks unstable on
every tier (docs/13), so the margin below ``MAX_UTILISATION`` is deliberately wide.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from benchmarks.loadgen import LoadConfig, PlannedRequest, build_workload
from gateway.backends import SimulatedBackend
from gateway.clock import SimClock
from gateway.continuous import ContinuousBatchingWorker
from gateway.metrics import percentile
from gateway.prefix_cache import PrefixCache
from gateway.profiles import PROFILES, ModelProfile, Tier
from gateway.schema import GenerateRequest, RequestResult

MAX_UTILISATION = 0.6
MAX_P99_GROWTH = 0.25


@dataclass(frozen=True, slots=True)
class ModelLoad:
    model: str
    requests: int
    offered_per_s: float
    capacity_per_s: float
    p99_ms: float
    p99_doubled_ms: float

    @property
    def utilisation(self) -> float:
        return self.offered_per_s / self.capacity_per_s

    @property
    def p99_growth(self) -> float:
        return self.p99_doubled_ms / self.p99_ms - 1

    @property
    def saturated(self) -> bool:
        return self.utilisation > MAX_UTILISATION or self.p99_growth > MAX_P99_GROWTH


async def _serve(
    profile: ModelProfile,
    arrivals: Sequence[tuple[float, GenerateRequest]],
    *,
    prefix_cache: bool,
    seed: int,
) -> list[RequestResult]:
    clock = SimClock()
    worker = ContinuousBatchingWorker(
        profile,
        SimulatedBackend(seed=seed),
        clock,
        prefix_cache=PrefixCache(profile.kv_capacity_tokens) if prefix_cache else None,
    )

    async def arrive(at_ms: float, request: GenerateRequest) -> RequestResult:
        await clock.sleep_ms(at_ms)
        return await worker.submit(request)

    return list(await asyncio.gather(*(arrive(at, r) for at, r in arrivals)))


def _requests(planned: Sequence[PlannedRequest]) -> list[tuple[float, GenerateRequest]]:
    return [
        (p.at_ms, GenerateRequest(f"r{p.index}", p.model, p.prompt, p.max_tokens)) for p in planned
    ]


async def check(
    config: LoadConfig,
    *,
    profiles: Mapping[Tier, ModelProfile] = PROFILES,
    prefix_cache: bool = True,
) -> list[ModelLoad]:
    """One ``ModelLoad`` per model the workload uses, in the order of its weights."""
    planned = build_workload(config)
    doubled = build_workload(dataclasses.replace(config, total_requests=2 * config.total_requests))
    span_s = planned[-1].at_ms / 1000
    loads = []
    for model in config.model_weights:
        mine = [p for p in planned if p.model == model]
        if not mine:
            continue
        profile = profiles[Tier(model)]
        arrivals = _requests(mine)
        burst = await _serve(
            profile, [(0.0, r) for _, r in arrivals], prefix_cache=prefix_cache, seed=config.seed
        )
        run = await _serve(profile, arrivals, prefix_cache=prefix_cache, seed=config.seed)
        longer = await _serve(
            profile,
            _requests([p for p in doubled if p.model == model]),
            prefix_cache=prefix_cache,
            seed=config.seed,
        )
        loads.append(
            ModelLoad(
                model=model,
                requests=len(mine),
                offered_per_s=len(mine) / span_s,
                capacity_per_s=len(burst) / (max(r.finish_ms for r in burst) / 1000),
                p99_ms=percentile([r.latency_ms for r in run], 99),
                p99_doubled_ms=percentile([r.latency_ms for r in longer], 99),
            )
        )
    return loads


def table(loads: Sequence[ModelLoad]) -> str:
    lines = [
        f"{'model':9} {'reqs':>5} {'offered/s':>9} {'capacity/s':>10} {'util':>5} "
        f"{'p99 ms':>10} {'p99 ms, 2x run':>14} {'growth':>7}  verdict"
    ]
    lines.extend(
        f"{m.model:9} {m.requests:5d} {m.offered_per_s:9.3f} {m.capacity_per_s:10.3f} "
        f"{m.utilisation:5.0%} {m.p99_ms:10.1f} {m.p99_doubled_ms:14.1f} {m.p99_growth:+7.0%}  "
        f"{'SATURATED' if m.saturated else 'ok'}"
        for m in loads
    )
    return "\n".join(lines)


def workloads() -> dict[str, tuple[LoadConfig, bool]]:
    """Every seeded workload the docs and the report quote, with whether the cache is on."""
    # Imported here, not at the top: benchmarks.report imports this module.
    from benchmarks import fairness, gate, metrics_example, report

    named: dict[str, tuple[LoadConfig, bool]] = {
        "gate": (gate.STANDARD_LOAD, True),
        "load-default": (metrics_example.CONFIG, True),
        "fairness": (fairness.CONFIG, True),
    }
    for name, (config, cached) in report.saturation_workloads().items():
        named[f"report: {name}"] = (config, cached)
    return named


def main(argv: Sequence[str] | None = None) -> int:
    from benchmarks import gate

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--rate", type=float, nargs="+", help="check the gate's mix at these rates instead"
    )
    args = parser.parse_args(argv)
    print("SIMULATED on the calibrated profiles, batches of at most 4; not measurements.")
    print(
        f"saturated = utilisation above {MAX_UTILISATION:.0%} or p99 growing by more than "
        f"{MAX_P99_GROWTH:.0%} when the run is doubled"
    )
    if args.rate:
        named = {
            f"gate mix at {rate:g}/s": (
                dataclasses.replace(gate.STANDARD_LOAD, arrival_rate_per_s=rate),
                True,
            )
            for rate in args.rate
        }
    else:
        named = workloads()
    for name, (config, cached) in named.items():
        print(
            f"\n{name}: seed {config.seed}, {config.total_requests} requests at "
            f"{config.arrival_rate_per_s:g}/s, prefix cache {'on' if cached else 'off'}"
        )
        print(table(asyncio.run(check(config, prefix_cache=cached))))
    return 0


__all__ = ["ModelLoad", "check", "main", "table"]

if __name__ == "__main__":
    raise SystemExit(main())
