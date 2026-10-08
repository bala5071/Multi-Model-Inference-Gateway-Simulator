"""Deterministic time and money cost of serving one request on a model profile.

Everything here is a pure function of its arguments: no clock, no global state, no
randomness at call time. See docs/01-cost-model.md for the assumptions.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from gateway.profiles import ModelProfile


@dataclass(frozen=True, slots=True)
class CostEstimate:
    prefill_ms: float
    decode_step_ms: float
    cost_usd: float
    # The profile's fixed per-request time. It delays this request only: the model is
    # free to serve others meanwhile, and it is not billed.
    overhead_ms: float = 0.0


def decode_step_ms(profile: ModelProfile, batch_size: int) -> float:
    """Wall time of one decode step for a batch of ``batch_size`` sequences."""
    if not 1 <= batch_size <= profile.max_batch_size:
        raise ValueError(
            f"batch_size must be between 1 and {profile.max_batch_size} for {profile.name}"
        )
    return profile.decode_base_ms + profile.decode_ms_per_extra_seq * (batch_size - 1)


def estimate_cost(
    profile: ModelProfile,
    *,
    new_prefill_tokens: int,
    output_tokens: int,
    batch_size: int,
) -> CostEstimate:
    """Cost of one request.

    ``new_prefill_tokens`` counts only prompt tokens that are not already in the KV cache.
    ``batch_size`` is the number of sequences decoding together, including this one.
    ``decode_step_ms`` is the wall time of one decode step for the whole batch; each step
    yields one token per sequence. Served alone, the request's first token comes after
    ``overhead_ms + prefill_ms + decode_step_ms``.
    """
    if new_prefill_tokens < 0:
        raise ValueError("new_prefill_tokens must not be negative")
    if output_tokens < 0:
        raise ValueError("output_tokens must not be negative")
    step_ms = decode_step_ms(profile, batch_size)

    prefill_ms = new_prefill_tokens * profile.prefill_ms_per_token
    cost_usd = (
        new_prefill_tokens * profile.price_per_1k_input_tokens
        + output_tokens * profile.price_per_1k_output_tokens
    ) / 1000
    return CostEstimate(
        prefill_ms=prefill_ms,
        decode_step_ms=step_ms,
        cost_usd=cost_usd,
        overhead_ms=profile.per_request_overhead_ms,
    )


@dataclass(frozen=True, slots=True)
class OutputLengthGenerator:
    """Derives a request's output length from its prompt and a seed.

    The length is a hash of ``(seed, prompt)``, so it does not depend on call order, on how
    many lengths were drawn before, or on the process (``PYTHONHASHSEED`` has no effect).
    """

    seed: int
    min_tokens: int = 16
    max_tokens: int = 256

    def __post_init__(self) -> None:
        if self.min_tokens < 1:
            raise ValueError("min_tokens must be at least 1")
        if self.max_tokens < self.min_tokens:
            raise ValueError("max_tokens must not be less than min_tokens")

    def length_for(self, prompt: str) -> int:
        digest = hashlib.sha256(f"{self.seed}:".encode() + prompt.encode()).digest()
        span = self.max_tokens - self.min_tokens + 1
        return self.min_tokens + int.from_bytes(digest[:8], "big") % span
