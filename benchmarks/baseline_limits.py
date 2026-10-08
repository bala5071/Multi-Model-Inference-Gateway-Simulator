"""Print the baseline throughput limits quoted in docs/02-baseline.md.

Simulated, from the illustrative profiles: nothing here is a measurement of a real model.

    uv run python -m benchmarks.baseline_limits
"""

from __future__ import annotations

import asyncio

from gateway.backends import Backend, BackendTokenCallback, Generation
from gateway.baseline import BaselineWorker
from gateway.clock import SimClock
from gateway.cost_model import estimate_cost
from gateway.profiles import PROFILES, ModelProfile
from gateway.schema import GenerateRequest, RequestResult

TOKENS_IN = 500
TOKENS_OUT = 136
BURST = 10


class FixedSizeBackend(Backend):
    async def generate(
        self, request: GenerateRequest, *, on_token: BackendTokenCallback | None = None
    ) -> Generation:
        return Generation(prompt_tokens=tuple(range(TOKENS_IN)), tokens_out=TOKENS_OUT)


async def run_burst(profile: ModelProfile) -> list[RequestResult]:
    worker = BaselineWorker(profile, FixedSizeBackend(), SimClock())
    requests = [GenerateRequest(f"r{i}", profile.tier.value, "") for i in range(BURST)]
    return list(await asyncio.gather(*(worker.submit(r) for r in requests)))


def main() -> None:
    print(f"workload: {TOKENS_IN} tokens in, {TOKENS_OUT} tokens out, burst of {BURST}")
    header = (
        f"{'tier':9} {'service ms':>10} {'req/s':>6} {'tok/s':>6} "
        f"{'full-batch tok/s':>16} {'ratio':>6} {'mean wait ms':>12} {'last wait ms':>12}"
    )
    print(header)
    for tier, profile in PROFILES.items():
        results = asyncio.run(run_burst(profile))
        service_ms = results[0].finish_ms - results[0].start_ms
        waits = [r.queue_wait_ms for r in results]
        alone = estimate_cost(
            profile, new_prefill_tokens=TOKENS_IN, output_tokens=TOKENS_OUT, batch_size=1
        )
        full = estimate_cost(
            profile,
            new_prefill_tokens=TOKENS_IN,
            output_tokens=TOKENS_OUT,
            batch_size=profile.max_batch_size,
        )
        decode_tok_s = 1000 / alone.decode_step_ms
        full_batch_tok_s = 1000 * profile.max_batch_size / full.decode_step_ms
        print(
            f"{tier.value:9} {service_ms:10.1f} {1000 / service_ms:6.3f} {decode_tok_s:6.1f} "
            f"{full_batch_tok_s:16.1f} {full_batch_tok_s / decode_tok_s:6.2f} "
            f"{sum(waits) / len(waits):12.1f} {waits[-1]:12.1f}"
        )


if __name__ == "__main__":
    main()
