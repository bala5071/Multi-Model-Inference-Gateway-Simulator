"""Request and result value types shared by workers, the registry and the app."""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass

# Called by a worker each time a request gains a token:
# (token index, clock time in ms, the token's text).
TokenCallback = Callable[[int, float, str], None]


@dataclass(frozen=True, slots=True)
class GenerateRequest:
    request_id: str
    model: str
    prompt: str
    max_output_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.max_output_tokens is not None and self.max_output_tokens < 1:
            raise ValueError("max_output_tokens must be at least 1")


@dataclass(frozen=True, slots=True)
class RequestResult:
    """What happened to one request. Times are clock readings in milliseconds."""

    request_id: str
    model: str
    enqueue_ms: float
    start_ms: float
    first_token_ms: float
    finish_ms: float
    tokens_in: int
    tokens_out: int
    cost_usd: float
    # Prompt tokens served from the prefix cache; they are part of tokens_in.
    cached_tokens: int = 0
    # When each output token was produced; one entry per token, in order.
    token_times_ms: tuple[float, ...] = ()
    # The generated text, when the backend produces any. Simulated runs leave it empty.
    text: str = ""

    @property
    def queue_wait_ms(self) -> float:
        return self.start_ms - self.enqueue_ms

    @property
    def time_to_first_token_ms(self) -> float:
        return self.first_token_ms - self.enqueue_ms

    @property
    def latency_ms(self) -> float:
        return self.finish_ms - self.enqueue_ms

    @property
    def inter_token_ms(self) -> tuple[float, ...]:
        """Gaps between consecutive tokens; one fewer than the number of tokens."""
        return tuple(later - earlier for earlier, later in itertools.pairwise(self.token_times_ms))
