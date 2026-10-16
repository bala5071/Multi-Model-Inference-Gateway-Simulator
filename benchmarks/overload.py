"""Compare p99 latency with and without SLO-aware downgrade while one model is overloaded.

A demonstration of the mechanism, not a result: NOT MEASURED ON REAL DATA. On the calibrated
profiles the surge is many times what the premium model can serve, so the run measures
its backlog; and the outcome rests on the cost per extra sequence and the per-request
overhead, which the calibration marks unstable, and on SLO thresholds nobody measured.
Do not quote its numbers (docs/10-overload.md).

    uv run python -m benchmarks.overload

Every request asks for the premium model and opts in to downgrade. The arrival rate is
above what the premium model can serve for the first part of the run and well below it
afterwards. The same workload is run twice: with no overload policy, and with the SLO
policy below. Queue limits and timeouts are off in both, so the only difference is the
downgrade. See docs/10-overload.md.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Mapping

import httpx

from benchmarks.loadgen import (
    LengthBucket,
    LoadConfig,
    PlannedRequest,
    RequestOutcome,
    build_workload,
    run_workload,
)
from gateway.app import create_app
from gateway.clock import SimClock
from gateway.continuous import build_continuous_registry
from gateway.metrics import percentile
from gateway.overload import OverloadPolicy, SloMonitor, SloPolicy, Transition
from gateway.profiles import PROFILES, ModelProfile, Tier

SEED = 7
# 300 requests at 3 req/s, far more than the premium model can serve, then 100 requests at
# 0.25 req/s (about 400 s) so the backlog can clear.
SURGE = LoadConfig(
    seed=SEED,
    total_requests=300,
    arrival_rate_per_s=3.0,
    model_weights={"premium": 1},
    prompt_lengths=(LengthBucket(20, 200, 1.0),),
    shared_prefix_fraction=0.0,
)
CALM = dataclasses.replace(SURGE, seed=SEED + 1, total_requests=100, arrival_rate_per_s=0.25)

SLO = SloPolicy(
    p99_ms={"economy": 5_000.0, "standard": 10_000.0, "premium": 20_000.0},
    window_ms=30_000.0,
    min_samples=10,
    recover_ratio=0.7,
    min_hold_ms=15_000.0,
)


def workload() -> list[PlannedRequest]:
    surge = build_workload(SURGE)
    calm_start_ms = surge[-1].at_ms
    calm = [
        dataclasses.replace(p, index=len(surge) + p.index, at_ms=calm_start_ms + p.at_ms)
        for p in build_workload(CALM)
    ]
    return [*surge, *calm]


async def run(
    policy: OverloadPolicy | None,
    *,
    max_queue_len: Mapping[Tier, int] | None = None,
    profiles: Mapping[Tier, ModelProfile] = PROFILES,
) -> tuple[list[RequestOutcome], tuple[Transition, ...]]:
    clock = SimClock()
    registry = build_continuous_registry(
        clock, seed=SEED, max_queue_len=max_queue_len, profiles=profiles
    )
    app = create_app(registry, clock, overload=policy)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        outcomes = await run_workload(workload(), client, clock, allow_downgrade=True)
    await registry.aclose()
    monitor: SloMonitor | None = app.state.slo_monitor
    return outcomes, monitor.transitions if monitor is not None else ()


def describe(label: str, outcomes: list[RequestOutcome]) -> str:
    latencies = [o.total_ms for o in outcomes]
    served_by = {
        model: sum(1 for o in outcomes if o.model_used == model)
        for model in ("premium", "standard", "economy")
    }
    return (
        f"{label:22} {len(outcomes):5d} {percentile(latencies, 50):11.1f} "
        f"{percentile(latencies, 99):11.1f} {max(latencies):11.1f} "
        f"{served_by['premium']:8d} {served_by['standard']:9d} {served_by['economy']:8d} "
        f"{sum(o.cost_usd for o in outcomes):10.6f}"
    )


async def main() -> None:
    plain, _ = await run(None)
    protected, transitions = await run(OverloadPolicy(slo=SLO))
    assert all(o.ok for o in [*plain, *protected])

    print(
        "MECHANISM DEMO, NOT MEASURED ON REAL DATA: the surge saturates premium on the "
        "calibrated profiles. Do not quote these numbers (docs/10-overload.md)."
    )
    print(
        f"workload: {SURGE.total_requests} premium requests at {SURGE.arrival_rate_per_s}/s, "
        f"then {CALM.total_requests} at {CALM.arrival_rate_per_s}/s; all opt in to downgrade"
    )
    print(
        f"policy: premium p99 SLO {SLO.p99_ms['premium']:.0f} ms, window {SLO.window_ms:.0f} ms, "
        f"recover below {SLO.recover_ratio:.0%} of SLO, hold {SLO.min_hold_ms:.0f} ms"
    )
    print(
        f"{'run':22} {'reqs':>5} {'p50 ms':>11} {'p99 ms':>11} {'max ms':>11} "
        f"{'premium':>8} {'standard':>9} {'economy':>8} {'cost USD':>10}"
    )
    print(describe("no downgrade", plain))
    print(describe("with downgrade", protected))
    print("\nstate changes with downgrade:")
    for t in transitions:
        p99 = "window empty" if t.p99_ms is None else f"window p99 {t.p99_ms:.1f} ms"
        state = "DEGRADED" if t.degraded else "recovered"
        print(f"  t={t.at_ms / 1000:7.1f} s  {t.model:8} {state:9} ({p99})")


if __name__ == "__main__":
    asyncio.run(main())
