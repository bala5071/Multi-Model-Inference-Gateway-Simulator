"""Compare the baseline worker with continuous batching on the same seeded workload.

Simulated, on the calibrated profiles: nothing here is a measurement of a real model.

    uv run python -m benchmarks.compare_batching [--seed 7] [--requests 200] [--load 0.5]

Two arrival patterns are run per tier:

- burst: every request arrives at t=0, which shows the maximum throughput. Its queue is
  the whole run, so no latency is printed for it;
- poisson: random arrivals at ``--load`` times the rate the baseline can sustain. The
  default of 0.5 keeps both workers under 60% of their capacity on the calibrated profiles
  (above that a p99 measures the backlog; see benchmarks/saturation.py).
"""

from __future__ import annotations

import argparse
import asyncio
import random
from collections.abc import Callable
from dataclasses import dataclass

from gateway.backends import Backend, SimulatedBackend
from gateway.baseline import BaselineWorker
from gateway.clock import Clock, SimClock
from gateway.continuous import ContinuousBatchingWorker
from gateway.metrics import percentile
from gateway.profiles import PROFILES, ModelProfile
from gateway.registry import Worker
from gateway.schema import GenerateRequest, RequestResult

WorkerFactory = Callable[[ModelProfile, Backend, Clock], Worker]
WORKERS: dict[str, WorkerFactory] = {
    "baseline": BaselineWorker,
    "continuous": ContinuousBatchingWorker,
}


@dataclass(frozen=True, slots=True)
class Arrival:
    at_ms: float
    request: GenerateRequest


def make_prompts(seed: int, count: int) -> list[str]:
    """Prompts of 20 to 400 words; each is unique so output lengths vary."""
    rng = random.Random(seed)
    return [f"request {i} " + "word " * rng.randint(18, 398) for i in range(count)]


async def baseline_service_ms(profile: ModelProfile, prompts: list[str], seed: int) -> float:
    """Mean time the baseline needs per request when it never has to queue."""
    total_ms = 0.0
    for i, prompt in enumerate(prompts):
        worker = BaselineWorker(profile, SimulatedBackend(seed=seed), SimClock())
        result = await worker.submit(GenerateRequest(f"r{i}", profile.tier.value, prompt))
        total_ms += result.finish_ms
    return total_ms / len(prompts)


def make_arrivals(
    profile: ModelProfile, prompts: list[str], seed: int, mean_gap_ms: float
) -> list[Arrival]:
    rng = random.Random(seed)
    at_ms = 0.0
    arrivals = []
    for i, prompt in enumerate(prompts):
        arrivals.append(Arrival(at_ms, GenerateRequest(f"r{i}", profile.tier.value, prompt)))
        if mean_gap_ms > 0:
            at_ms += rng.expovariate(1 / mean_gap_ms)
    return arrivals


async def run(
    make_worker: WorkerFactory, profile: ModelProfile, arrivals: list[Arrival], seed: int
) -> list[RequestResult]:
    clock = SimClock()
    worker = make_worker(profile, SimulatedBackend(seed=seed), clock)

    async def arrive(arrival: Arrival) -> RequestResult:
        await clock.sleep_ms(arrival.at_ms)
        return await worker.submit(arrival.request)

    return list(await asyncio.gather(*(arrive(a) for a in arrivals)))


def report(label: str, results: list[RequestResult], *, latency: bool = True) -> str:
    span_s = (max(r.finish_ms for r in results) - min(r.enqueue_ms for r in results)) / 1000
    latencies = [r.latency_ms for r in results]
    ttfts = [r.time_to_first_token_ms for r in results]
    tokens_out = sum(r.tokens_out for r in results)
    line = f"  {label:11} {len(results) / span_s:8.2f} {tokens_out / span_s:9.1f}"
    if not latency:
        return line
    return (
        f"{line} {percentile(latencies, 50):12.1f} {percentile(latencies, 99):12.1f} "
        f"{percentile(ttfts, 99):12.1f}"
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--load", type=float, default=0.5)
    args = parser.parse_args()

    prompts = make_prompts(args.seed, args.requests)
    print(f"seed={args.seed} requests={args.requests} load={args.load}")
    print("SIMULATED on the calibrated profiles (batches of at most 4); not measurements.")
    for tier, profile in PROFILES.items():
        service_ms = await baseline_service_ms(profile, prompts, args.seed)
        patterns = {
            "burst": 0.0,
            f"poisson, {args.load}x baseline capacity": service_ms / args.load,
        }
        for pattern, mean_gap_ms in patterns.items():
            arrivals = make_arrivals(profile, prompts, args.seed, mean_gap_ms)
            print(f"\n{tier.value} ({profile.name}), {pattern}")
            burst = mean_gap_ms == 0
            header = f"  {'worker':11} {'req/s':>8} {'tok/s':>9}"
            if not burst:
                header += f" {'p50 ms':>12} {'p99 ms':>12} {'p99 TTFT ms':>12}"
            print(header)
            for label, make_worker in WORKERS.items():
                results = await run(make_worker, profile, arrivals, args.seed)
                print(report(label, results, latency=not burst))


if __name__ == "__main__":
    asyncio.run(main())
