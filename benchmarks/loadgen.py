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
import random
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field

import httpx

from gateway.app import create_app
from gateway.clock import Clock, SimClock, WallClock
from gateway.continuous import build_continuous_registry
from gateway.metrics import percentile
from gateway.profiles import PROFILES, ModelProfile, Tier
from gateway.registry import ModelRegistry


@dataclass(frozen=True, slots=True)
class LengthBucket:
    """Prompt lengths, in words, drawn uniformly from ``min_words..max_words``."""

    min_words: int
    max_words: int
    weight: float

    def __post_init__(self) -> None:
        if not 1 <= self.min_words <= self.max_words:
            raise ValueError("need 1 <= min_words <= max_words")
        if self.weight <= 0:
            raise ValueError("bucket weight must be positive")


DEFAULT_WEIGHTS: Mapping[str, float] = {"economy": 70, "standard": 20, "premium": 10}
DEFAULT_LENGTHS: tuple[LengthBucket, ...] = (
    LengthBucket(20, 80, 0.6),
    LengthBucket(80, 300, 0.3),
    LengthBucket(300, 800, 0.1),
)


@dataclass(frozen=True, slots=True)
class LoadConfig:
    seed: int = 7
    total_requests: int = 300
    # Mean arrival rate of the Poisson process, over all models together.
    arrival_rate_per_s: float = 8.0
    # Model name -> relative weight. Each request picks its model with these odds.
    model_weights: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    # Length of the part of the prompt that is unique to the request.
    prompt_lengths: tuple[LengthBucket, ...] = DEFAULT_LENGTHS
    # Share of requests that start with the same system prompt, on top of their own part.
    shared_prefix_fraction: float = 0.5
    shared_prefix_words: int = 200
    max_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.total_requests < 1:
            raise ValueError("total_requests must be at least 1")
        if self.arrival_rate_per_s <= 0:
            raise ValueError("arrival_rate_per_s must be positive")
        if not self.model_weights:
            raise ValueError("model_weights must not be empty")
        if any(w < 0 for w in self.model_weights.values()) or not any(
            w > 0 for w in self.model_weights.values()
        ):
            raise ValueError("model_weights must be non-negative with at least one positive")
        if not self.prompt_lengths:
            raise ValueError("prompt_lengths must not be empty")
        if not 0 <= self.shared_prefix_fraction <= 1:
            raise ValueError("shared_prefix_fraction must be between 0 and 1")
        if self.shared_prefix_words < 1:
            raise ValueError("shared_prefix_words must be at least 1")
        if self.max_tokens is not None and self.max_tokens < 1:
            raise ValueError("max_tokens must be at least 1")


@dataclass(frozen=True, slots=True)
class PlannedRequest:
    index: int
    at_ms: float
    model: str
    prompt: str
    max_tokens: int | None
    shares_prefix: bool


def build_workload(config: LoadConfig) -> list[PlannedRequest]:
    """The full request schedule for a config. A pure function of the config."""
    rng = random.Random(config.seed)
    models = list(config.model_weights)
    weights = [config.model_weights[m] for m in models]
    bucket_weights = [b.weight for b in config.prompt_lengths]
    shared_prefix = " ".join(f"sys{j}" for j in range(config.shared_prefix_words))
    mean_gap_ms = 1000 / config.arrival_rate_per_s

    planned = []
    at_ms = 0.0
    for index in range(config.total_requests):
        # The order of these draws is part of the format: changing it changes every workload.
        # Rounded to a nanosecond: math.log may differ in its last bit between platforms,
        # and the schedule has to be the same everywhere.
        at_ms += round(rng.expovariate(1 / mean_gap_ms), 6)
        model = rng.choices(models, weights)[0]
        bucket = rng.choices(config.prompt_lengths, bucket_weights)[0]
        words = rng.randint(bucket.min_words, bucket.max_words)
        shares_prefix = rng.random() < config.shared_prefix_fraction

        # Words carry the seed and the request index, so no two requests share text by
        # accident; only the system prompt is shared.
        own = " ".join(f"s{config.seed}r{index}w{j}" for j in range(words))
        prompt = f"{shared_prefix} {own}" if shares_prefix else own
        planned.append(
            PlannedRequest(index, at_ms, model, prompt, config.max_tokens, shares_prefix)
        )
    return planned


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
