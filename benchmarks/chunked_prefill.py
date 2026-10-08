"""Compare inter-token latency and time to first token with chunked prefill off and on.

Simulated, from the illustrative profiles: nothing here is a measurement of a real model.

    uv run python -m benchmarks.chunked_prefill

The workload mixes many short prompts with a few very long ones on one model. With chunked
prefill off, each long prompt is prefilled in one go and every running request stalls for
it. With it on, the prompt is prefilled a chunk at a time between decode steps.
See docs/11-streaming-chunked-prefill.md.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass

from gateway.backends import SimulatedBackend
from gateway.clock import SimClock
from gateway.continuous import ContinuousBatchingWorker
from gateway.metrics import percentile
from gateway.profiles import STANDARD, ModelProfile
from gateway.schema import GenerateRequest, RequestResult

SEED = 7
PROFILE = STANDARD
SHORT_REQUESTS = 400
SHORT_RATE_PER_S = 4.0
SHORT_WORDS = (20, 80)
LONG_REQUESTS = 8
LONG_WORDS = (6000, 10000)
MAX_OUTPUT_TOKENS = 64
CHUNK_SIZES: tuple[int | None, ...] = (None, 4096, 1024, 256, 64, 16)


@dataclass(frozen=True, slots=True)
class Arrival:
    at_ms: float
    request: GenerateRequest
    long: bool


def workload(
    *,
    seed: int = SEED,
    short_requests: int = SHORT_REQUESTS,
    long_requests: int = LONG_REQUESTS,
) -> list[Arrival]:
    """Short prompts arrive as a Poisson stream; long ones are spread evenly through it."""
    rng = random.Random(seed)
    arrivals = []
    at_ms = 0.0
    for i in range(short_requests):
        at_ms += round(rng.expovariate(SHORT_RATE_PER_S / 1000), 6)
        prompt = " ".join(f"s{i}w{j}" for j in range(rng.randint(*SHORT_WORDS)))
        request = GenerateRequest(f"short-{i}", PROFILE.tier.value, prompt, MAX_OUTPUT_TOKENS)
        arrivals.append(Arrival(at_ms, request, long=False))
    span_ms = at_ms
    for i in range(long_requests):
        prompt = " ".join(f"l{i}w{j}" for j in range(rng.randint(*LONG_WORDS)))
        request = GenerateRequest(f"long-{i}", PROFILE.tier.value, prompt, MAX_OUTPUT_TOKENS)
        arrivals.append(Arrival(span_ms * (i + 0.5) / long_requests, request, long=True))
    return sorted(arrivals, key=lambda a: a.at_ms)


async def run(
    arrivals: list[Arrival],
    chunk_tokens: int | None,
    *,
    profile: ModelProfile = PROFILE,
    seed: int = SEED,
) -> dict[str, RequestResult]:
    clock = SimClock()
    worker = ContinuousBatchingWorker(
        profile, SimulatedBackend(seed=seed), clock, prefill_chunk_tokens=chunk_tokens
    )

    async def arrive(arrival: Arrival) -> RequestResult:
        await clock.sleep_ms(arrival.at_ms)
        return await worker.submit(arrival.request)

    results = await asyncio.gather(*(arrive(a) for a in arrivals))
    return {r.request_id: r for r in results}


@dataclass(frozen=True, slots=True)
class Summary:
    itl_p50_ms: float
    itl_p99_ms: float
    itl_p999_ms: float
    itl_max_ms: float
    # p99, over requests, of each request's single longest gap between tokens.
    worst_gap_p99_ms: float
    short_ttft_p50_ms: float
    short_ttft_p99_ms: float
    long_ttft_p50_ms: float
    total_p99_ms: float
    makespan_s: float


def summarise(arrivals: list[Arrival], results: dict[str, RequestResult]) -> Summary:
    long_ids = {a.request.request_id for a in arrivals if a.long}
    gaps = [gap for r in results.values() for gap in r.inter_token_ms]
    short_ttft = [r.time_to_first_token_ms for rid, r in results.items() if rid not in long_ids]
    long_ttft = [r.time_to_first_token_ms for rid, r in results.items() if rid in long_ids]
    return Summary(
        itl_p50_ms=percentile(gaps, 50),
        itl_p99_ms=percentile(gaps, 99),
        itl_p999_ms=percentile(gaps, 99.9),
        itl_max_ms=max(gaps),
        worst_gap_p99_ms=percentile(
            [max(r.inter_token_ms) for r in results.values() if r.inter_token_ms], 99
        ),
        short_ttft_p50_ms=percentile(short_ttft, 50),
        short_ttft_p99_ms=percentile(short_ttft, 99),
        long_ttft_p50_ms=percentile(long_ttft, 50),
        total_p99_ms=percentile([r.latency_ms for r in results.values()], 99),
        makespan_s=max(r.finish_ms for r in results.values()) / 1000,
    )


async def main() -> None:
    arrivals = workload()
    print("SIMULATED with illustrative profiles; not measurements of real models.")
    print(
        f"model: {PROFILE.tier.value} ({PROFILE.name}); {SHORT_REQUESTS} short prompts "
        f"({SHORT_WORDS[0]}-{SHORT_WORDS[1]} words) at {SHORT_RATE_PER_S}/s, "
        f"{LONG_REQUESTS} long prompts ({LONG_WORDS[0]}-{LONG_WORDS[1]} words); "
        f"up to {MAX_OUTPUT_TOKENS} output tokens; seed {SEED}"
    )
    print(
        f"{'chunk tokens':>12} {'ITL p50':>8} {'ITL p99':>8} {'ITL p99.9':>10} {'ITL max':>8} "
        f"{'worst-gap p99':>14} "
        f"{'short TTFT p50':>15} {'short TTFT p99':>15} {'long TTFT p50':>14} "
        f"{'total p99':>10} {'makespan s':>11}"
    )
    for chunk_tokens in CHUNK_SIZES:
        s = summarise(arrivals, await run(arrivals, chunk_tokens))
        label = "off" if chunk_tokens is None else str(chunk_tokens)
        print(
            f"{label:>12} {s.itl_p50_ms:8.1f} {s.itl_p99_ms:8.1f} {s.itl_p999_ms:10.1f} "
            f"{s.itl_max_ms:8.1f} {s.worst_gap_p99_ms:14.1f} "
            f"{s.short_ttft_p50_ms:15.1f} {s.short_ttft_p99_ms:15.1f} {s.long_ttft_p50_ms:14.1f} "
            f"{s.total_p99_ms:10.1f} {s.makespan_s:11.1f}"
        )


if __name__ == "__main__":
    asyncio.run(main())
