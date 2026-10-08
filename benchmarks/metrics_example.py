"""Print what the metrics store reports after the default load, as docs/05-metrics.md quotes.

Simulated, from the illustrative profiles: nothing here is a measurement of a real model.

    uv run python -m benchmarks.metrics_example

The default ``LoadConfig`` is run in-process on simulated time, then ``/metrics/detail`` is
read once, as soon as the last request has finished.
"""

from __future__ import annotations

import asyncio
from typing import Any

from benchmarks.loadgen import LoadConfig, build_workload, in_process_client, run_workload
from gateway.clock import SimClock
from gateway.continuous import build_continuous_registry

CONFIG = LoadConfig()
METRICS = ("queue_ms", "ttft_ms", "total_ms", "itl_ms")


async def run(config: LoadConfig = CONFIG) -> tuple[dict[str, Any], float]:
    """The ``/metrics/detail`` body after the load, and the simulated time it was read at."""
    clock = SimClock()
    registry = build_continuous_registry(clock, seed=config.seed)
    async with in_process_client(registry, clock) as client:
        await run_workload(build_workload(config), client, clock)
        detail: dict[str, Any] = (await client.get("/metrics/detail")).json()
        return detail, clock.now_ms()


async def main() -> None:
    detail, read_at_ms = await run()
    print("SIMULATED with illustrative profiles; not measurements of real models.")
    print(
        f"workload: seed={CONFIG.seed} requests={CONFIG.total_requests} "
        f"rate={CONFIG.arrival_rate_per_s}/s; read at t={read_at_ms / 1000:.1f} s; "
        f"percentiles: {detail['percentile_method']}"
    )
    print(
        f"{'model':9} {'metric':9} {'samples':>7} {'mean':>9} {'p50':>9} {'p90':>9} "
        f"{'p99':>9} {'max':>9}"
    )
    for model, m in detail["models"].items():
        for metric in METRICS:
            d = m[metric]
            if d is None:
                print(f"{model:9} {metric:9} {0:7d} {'-':>9} {'-':>9} {'-':>9} {'-':>9} {'-':>9}")
                continue
            print(
                f"{model:9} {metric:9} {d['samples']:7d} {d['mean']:9.1f} {d['p50']:9.1f} "
                f"{d['p90']:9.1f} {d['p99']:9.1f} {d['max']:9.1f}"
            )
    print()
    print(
        f"{'model':9} {'requests':>8} {'in window':>9} {'span s':>7} {'prompt tok':>10} "
        f"{'cached tok':>10} {'output tok':>10} {'cost USD':>10} {'truncated':>9}"
    )
    for model, m in detail["models"].items():
        life, window = m["lifetime"], m["window"]
        print(
            f"{model:9} {life['requests']:8d} {window['requests']:9d} "
            f"{window['span_ms'] / 1000:7.1f} {life['prompt_tokens']:10d} "
            f"{life['cached_tokens']:10d} {life['completion_tokens']:10d} "
            f"{life['cost_usd']:10.6f} {'yes' if window['truncated'] else 'no':>9}"
        )


if __name__ == "__main__":
    asyncio.run(main())
