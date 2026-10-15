"""Seeded, weighted, concurrent load generator for the gateway's HTTP API.

In-process on simulated time (deterministic; the default):

    uv run python -m benchmarks.loadgen --seed 7 --requests 300 --rate 8

Against a running server on real time:

    uv run python -m benchmarks.loadgen --url http://127.0.0.1:8000 --tenant acme \
        --requests 50 --rate 5

See docs/07-load.md. In-process results are simulated from illustrative model profiles;
they are not measurements of real models.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass

import httpx

from gateway.app import create_app
from gateway.clock import Clock, SimClock, WallClock
from gateway.continuous import build_continuous_registry
from gateway.metrics import percentile
from gateway.profiles import PROFILES, ModelProfile, Tier
from gateway.registry import ModelRegistry

# The workload itself is planned in the gateway package, so that the server can run the
# same generator (docs/17-playground-load-replay.md). Re-exported here under the old names.
from gateway.workload import (
    DEFAULT_LENGTHS,
    DEFAULT_WEIGHTS,
    LengthBucket,
    LoadConfig,
    PlannedRequest,
    build_workload,
)

__all__ = [
    "DEFAULT_LENGTHS",
    "DEFAULT_WEIGHTS",
    "LengthBucket",
    "LoadConfig",
    "PlannedRequest",
    "build_workload",
]


@dataclass(frozen=True, slots=True)
class RequestOutcome:
    """One request as the client saw it. Timings are the server's own, from its response."""

    index: int
    model: str
    at_ms: float
    done_ms: float
    status: int
    queue_ms: float = 0.0
    ttft_ms: float = 0.0
    total_ms: float = 0.0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    # The model that actually served the request, and the one it was moved away from if
    # the gateway downgraded it. ``model`` above is always the one that was asked for.
    model_used: str = ""
    downgraded_from: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == 200


@dataclass(frozen=True, slots=True)
class ModelReport:
    requests: int
    errors: int
    ttft_p50_ms: float | None
    ttft_p99_ms: float | None
    total_p50_ms: float | None
    total_p99_ms: float | None
    throughput_rps: float | None
    completion_tokens_per_s: float | None
    # Share of successful requests that reused at least one cached token.
    cache_hit_rate: float | None
    # Share of prompt tokens that were served from the cache.
    cache_token_hit_rate: float | None
    cost_usd: float


@dataclass(frozen=True, slots=True)
class LoadReport:
    config: LoadConfig
    models: dict[str, ModelReport]
    overall: ModelReport

    def to_json(self) -> str:
        """Stable text form: the same run always gives the same bytes."""
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"

    def to_table(self) -> str:
        def cell(value: float | None, spec: str) -> str:
            return "-" if value is None else format(value, spec)

        header = (
            f"{'model':10} {'reqs':>5} {'err':>4} {'TTFT p50':>10} {'TTFT p99':>10} "
            f"{'total p50':>10} {'total p99':>10} {'req/s':>7} {'tok/s':>8} "
            f"{'hit':>5} {'tok hit':>7} {'cost USD':>10}"
        )
        lines = [header]
        for name, m in [*self.models.items(), ("ALL", self.overall)]:
            lines.append(
                f"{name:10} {m.requests:5d} {m.errors:4d} "
                f"{cell(m.ttft_p50_ms, '10.1f')} {cell(m.ttft_p99_ms, '10.1f')} "
                f"{cell(m.total_p50_ms, '10.1f')} {cell(m.total_p99_ms, '10.1f')} "
                f"{cell(m.throughput_rps, '7.2f')} {cell(m.completion_tokens_per_s, '8.1f')} "
                f"{cell(m.cache_hit_rate, '5.2f')} {cell(m.cache_token_hit_rate, '7.2f')} "
                f"{m.cost_usd:10.6f}"
            )
        return "\n".join(lines)


async def run_workload(
    planned: Sequence[PlannedRequest],
    client: httpx.AsyncClient,
    clock: Clock,
    *,
    tenant_id: str | None = None,
    allow_downgrade: bool = False,
) -> list[RequestOutcome]:
    """Send every planned request at its arrival time, concurrently, and collect outcomes.

    ``clock`` paces the arrivals. In-process it must be the app's own ``SimClock``.
    """
    started_ms = clock.now_ms()

    async def send(request: PlannedRequest) -> RequestOutcome:
        await clock.sleep_ms(max(0.0, started_ms + request.at_ms - clock.now_ms()))
        body: dict[str, object] = {"model": request.model, "prompt": request.prompt}
        if request.max_tokens is not None:
            body["max_tokens"] = request.max_tokens
        if tenant_id is not None:
            body["tenant_id"] = tenant_id
        if allow_downgrade:
            body["allow_downgrade"] = True
        response = await client.post("/v1/generate", json=body)
        done_ms = clock.now_ms() - started_ms
        if response.status_code != 200:
            return RequestOutcome(
                request.index, request.model, request.at_ms, done_ms, response.status_code
            )
        data = response.json()
        return RequestOutcome(
            index=request.index,
            model=request.model,
            at_ms=request.at_ms,
            done_ms=done_ms,
            status=200,
            queue_ms=data["timing"]["queue_ms"],
            ttft_ms=data["timing"]["ttft_ms"],
            total_ms=data["timing"]["total_ms"],
            prompt_tokens=data["usage"]["prompt_tokens"],
            cached_tokens=data["usage"]["cached_tokens"],
            completion_tokens=data["usage"]["completion_tokens"],
            cost_usd=data["cost"]["usd"],
            model_used=data["model"],
            downgraded_from=response.headers.get("X-Downgraded-From"),
        )

    return list(await asyncio.gather(*(send(r) for r in planned)))


def summarise(outcomes: Sequence[RequestOutcome]) -> ModelReport:
    ok = [o for o in outcomes if o.ok]
    if not ok:
        return ModelReport(
            requests=len(outcomes),
            errors=len(outcomes),
            ttft_p50_ms=None,
            ttft_p99_ms=None,
            total_p50_ms=None,
            total_p99_ms=None,
            throughput_rps=None,
            completion_tokens_per_s=None,
            cache_hit_rate=None,
            cache_token_hit_rate=None,
            cost_usd=0.0,
        )
    # From the first arrival to the last completion among these requests.
    span_s = (max(o.done_ms for o in ok) - min(o.at_ms for o in outcomes)) / 1000
    prompt_tokens = sum(o.prompt_tokens for o in ok)
    return ModelReport(
        requests=len(outcomes),
        errors=len(outcomes) - len(ok),
        ttft_p50_ms=percentile([o.ttft_ms for o in ok], 50),
        ttft_p99_ms=percentile([o.ttft_ms for o in ok], 99),
        total_p50_ms=percentile([o.total_ms for o in ok], 50),
        total_p99_ms=percentile([o.total_ms for o in ok], 99),
        throughput_rps=len(ok) / span_s if span_s > 0 else None,
        completion_tokens_per_s=(
            sum(o.completion_tokens for o in ok) / span_s if span_s > 0 else None
        ),
        cache_hit_rate=sum(1 for o in ok if o.cached_tokens > 0) / len(ok),
        cache_token_hit_rate=(
            sum(o.cached_tokens for o in ok) / prompt_tokens if prompt_tokens else None
        ),
        cost_usd=sum(o.cost_usd for o in ok),
    )


def build_report(config: LoadConfig, outcomes: Sequence[RequestOutcome]) -> LoadReport:
    return LoadReport(
        config=config,
        models={
            model: summarise([o for o in outcomes if o.model == model])
            for model in config.model_weights
        },
        overall=summarise(outcomes),
    )


async def run_load(
    config: LoadConfig,
    client: httpx.AsyncClient,
    clock: Clock,
    *,
    tenant_id: str | None = None,
) -> LoadReport:
    outcomes = await run_workload(build_workload(config), client, clock, tenant_id=tenant_id)
    return build_report(config, outcomes)


@asynccontextmanager
async def in_process_client(
    registry: ModelRegistry, clock: Clock
) -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client wired straight to an app built on ``registry``; no sockets."""
    transport = httpx.ASGITransport(app=create_app(registry, clock))
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
            yield client
    finally:
        await registry.aclose()


async def run_in_process(
    config: LoadConfig, *, profiles: Mapping[Tier, ModelProfile] = PROFILES
) -> LoadReport:
    """Run on simulated time against the default wiring. Deterministic for a config.

    ``profiles`` replaces the standard model profiles, for what-if runs.
    """
    clock = SimClock()
    registry = build_continuous_registry(clock, seed=config.seed, profiles=profiles)
    async with in_process_client(registry, clock) as client:
        return await run_load(config, client, clock)


async def run_live(config: LoadConfig, url: str, *, tenant_id: str | None = None) -> LoadReport:
    """Run on real time against a server that is already running at ``url``.

    A server with plans configured needs a ``tenant_id`` whose plan allows every model in
    the config's weights.
    """
    async with httpx.AsyncClient(base_url=url, timeout=None) as client:
        return await run_load(config, client, WallClock(), tenant_id=tenant_id)


def _parse_weights(text: str) -> dict[str, float]:
    weights = {}
    for part in text.split(","):
        name, _, weight = part.partition("=")
        weights[name.strip()] = float(weight)
    return weights


def _parse_length_range(text: str) -> tuple[LengthBucket, ...]:
    low, _, high = text.partition("-")
    return (LengthBucket(int(low), int(high), 1.0),)


def main() -> None:
    defaults = LoadConfig()
    parser = argparse.ArgumentParser(description="Load generator for the inference gateway.")
    parser.add_argument("--seed", type=int, default=defaults.seed)
    parser.add_argument("--requests", type=int, default=defaults.total_requests)
    parser.add_argument("--rate", type=float, default=defaults.arrival_rate_per_s, help="req/s")
    parser.add_argument(
        "--weights",
        type=_parse_weights,
        default=dict(DEFAULT_WEIGHTS),
        help="e.g. economy=70,standard=20,premium=10",
    )
    parser.add_argument(
        "--shared-prefix-fraction", type=float, default=defaults.shared_prefix_fraction
    )
    parser.add_argument("--shared-prefix-words", type=int, default=defaults.shared_prefix_words)
    parser.add_argument("--max-tokens", type=int, default=None)
    parser.add_argument(
        "--prompt-words",
        type=_parse_length_range,
        default=DEFAULT_LENGTHS,
        help="MIN-MAX words for each prompt's own part, e.g. 20-80 (default: a mix)",
    )
    parser.add_argument("--url", help="run against a live server instead of in-process")
    parser.add_argument("--tenant", help="tenant_id to send with every request (live runs)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args()

    config = LoadConfig(
        seed=args.seed,
        total_requests=args.requests,
        arrival_rate_per_s=args.rate,
        model_weights=args.weights,
        prompt_lengths=args.prompt_words,
        shared_prefix_fraction=args.shared_prefix_fraction,
        shared_prefix_words=args.shared_prefix_words,
        max_tokens=args.max_tokens,
    )
    if args.url:
        report = asyncio.run(run_live(config, args.url, tenant_id=args.tenant))
        mode = f"LIVE against {args.url} on wall-clock time"
    else:
        report = asyncio.run(run_in_process(config))
        mode = "IN-PROCESS on simulated time; illustrative profiles, not real measurements"

    if args.json:
        print(report.to_json(), end="")
    else:
        print(mode)
        print(
            f"seed={config.seed} requests={config.total_requests} "
            f"rate={config.arrival_rate_per_s}/s weights={dict(config.model_weights)} "
            f"shared_prefix={config.shared_prefix_fraction} x {config.shared_prefix_words} words"
        )
        print(report.to_table())


if __name__ == "__main__":
    main()
