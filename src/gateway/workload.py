"""The seeded workload a load run sends: which requests, and when.

Planning is a pure function of a ``LoadConfig``. ``benchmarks/loadgen.py`` runs a plan
over HTTP and reports on it; ``gateway.loadcontrol`` runs one inside the server.
See docs/07-load.md.
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from dataclasses import dataclass, field


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
