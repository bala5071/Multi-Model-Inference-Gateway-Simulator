"""Shared test fixtures that are plain values, not pytest fixtures."""

from __future__ import annotations

import zlib
from collections.abc import Mapping

from gateway.backends import Backend, BackendTokenCallback, Generation
from gateway.profiles import ModelProfile, Tier
from gateway.schema import GenerateRequest

# Every value is a power of two (or a small multiple), so expected times computed by hand
# are exact in floating point and can be compared with ==.
EXACT = ModelProfile(
    name="exact-test-model",
    tier=Tier.STANDARD,
    price_per_1k_input_tokens=0.5,
    price_per_1k_output_tokens=1.0,
    prefill_ms_per_token=0.5,
    decode_base_ms=4.0,
    decode_ms_per_extra_seq=0.25,
    max_batch_size=4,
    kv_capacity_tokens=1024,
)


class SizedBackend(Backend):
    """Token counts looked up by request id, so tests control the workload exactly.

    An id that is not in ``sizes`` makes ``generate`` raise ``KeyError``.
    """

    def __init__(self, sizes: Mapping[str, tuple[int, int]]) -> None:
        self._sizes = dict(sizes)

    async def generate(
        self, request: GenerateRequest, *, on_token: BackendTokenCallback | None = None
    ) -> Generation:
        tokens_in, tokens_out = self._sizes[request.request_id]
        return Generation(
            prompt_tokens=unique_tokens(request.request_id, tokens_in), tokens_out=tokens_out
        )


def unique_tokens(request_id: str, count: int) -> tuple[int, ...]:
    """``count`` token ids that no other request id shares, so nothing is a cache hit."""
    base = zlib.crc32(request_id.encode()) * 1_000_000
    return tuple(base + i for i in range(count))
