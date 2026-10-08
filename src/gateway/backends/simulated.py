"""A backend that runs no model. Fully deterministic given its seed."""

from __future__ import annotations

from gateway.backends.base import Backend, BackendTokenCallback, Generation
from gateway.cost_model import OutputLengthGenerator
from gateway.schema import GenerateRequest
from gateway.tokenizer import Tokenizer, WordTokenizer


class SimulatedBackend(Backend):
    def __init__(
        self,
        *,
        seed: int,
        tokenizer: Tokenizer | None = None,
        min_output_tokens: int = 16,
        max_output_tokens: int = 256,
    ) -> None:
        self._tokenizer = tokenizer if tokenizer is not None else WordTokenizer()
        self._lengths = OutputLengthGenerator(
            seed=seed, min_tokens=min_output_tokens, max_tokens=max_output_tokens
        )

    async def generate(
        self, request: GenerateRequest, *, on_token: BackendTokenCallback | None = None
    ) -> Generation:
        # No model runs, so there is nothing to report through on_token. The worker
        # in front of this backend decides when the tokens appear.
        tokens_out = self._lengths.length_for(request.prompt)
        if request.max_output_tokens is not None:
            tokens_out = min(tokens_out, request.max_output_tokens)
        return Generation(
            prompt_tokens=self._tokenizer.encode(request.prompt), tokens_out=tokens_out
        )
