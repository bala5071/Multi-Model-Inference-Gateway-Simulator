"""Show that a saturated model does not slow a lightly loaded one, and what breaks that.

Simulated, on the calibrated profiles: nothing here is a measurement of a real model.

    uv run python -m benchmarks.fairness

The same workload is run three ways: each model on its own worker; the light model's
requests alone; and both models' requests through one shared worker and queue.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence

from benchmarks.loadgen import (
    LengthBucket,
    LoadConfig,
    PlannedRequest,
    RequestOutcome,
    build_workload,
    in_process_client,
    run_workload,
    summarise,
)
from gateway.backends import SimulatedBackend
from gateway.clock import SimClock
from gateway.continuous import ContinuousBatchingWorker, build_continuous_registry
from gateway.prefix_cache import PrefixCache
from gateway.profiles import ECONOMY
from gateway.registry import ModelRegistry

# Economy is saturated on purpose (about 140% of its capacity); premium gets about a third
# of its own. Its 60 or so requests are enough for a p99 that is not just the maximum.
# Checked with ``uv run python -m benchmarks.saturation`` (docs/07-load.md).
CONFIG = LoadConfig(
    seed=7,
    total_requests=2000,
    arrival_rate_per_s=20,
    model_weights={"economy": 97, "premium": 3},
    prompt_lengths=(LengthBucket(20, 80, 1.0),),
    shared_prefix_fraction=0.0,
    max_tokens=16,
)


def separate_workers(clock: SimClock) -> ModelRegistry:
    return build_continuous_registry(clock, seed=CONFIG.seed)


def one_shared_queue(clock: SimClock) -> ModelRegistry:
    worker = ContinuousBatchingWorker(
        ECONOMY,
        SimulatedBackend(seed=CONFIG.seed),
        clock,
        prefix_cache=PrefixCache(ECONOMY.kv_capacity_tokens),
    )
    registry = ModelRegistry()
    registry.register("economy", worker, aliases=["premium"])
    return registry


async def run(
    planned: Sequence[PlannedRequest], make_registry: Callable[[SimClock], ModelRegistry]
) -> list[RequestOutcome]:
    clock = SimClock()
    async with in_process_client(make_registry(clock), clock) as client:
        return await run_workload(planned, client, clock)


async def main() -> None:
    planned = build_workload(CONFIG)
    scenarios = {
        "separate workers": (planned, separate_workers),
        "premium alone": ([p for p in planned if p.model == "premium"], separate_workers),
        "one shared queue": (planned, one_shared_queue),
    }
    print("SIMULATED on the calibrated profiles; not measurements of real models.")
    print("economy is saturated on purpose: its p99 measures its backlog, not latency.")
    print(f"{'scenario':18} {'model':8} {'reqs':>5} {'total p50 ms':>13} {'total p99 ms':>13}")
    for name, (plan, make_registry) in scenarios.items():
        outcomes = await run(plan, make_registry)
        for model in CONFIG.model_weights:
            subset = [o for o in outcomes if o.model == model]
            if not subset:
                continue
            s = summarise(subset)
            print(
                f"{name:18} {model:8} {s.requests:5d} "
                f"{s.total_p50_ms or 0:13.1f} {s.total_p99_ms or 0:13.1f}"
            )


if __name__ == "__main__":
    asyncio.run(main())
