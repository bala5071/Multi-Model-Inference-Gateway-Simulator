"""Model profiles for the three plan tiers.

All numbers here are illustrative, not measured. See docs/01-cost-model.md.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final


class Tier(StrEnum):
    ECONOMY = "economy"
    STANDARD = "standard"
    PREMIUM = "premium"


@dataclass(frozen=True, slots=True)
class ModelProfile:
    name: str
    tier: Tier
    price_per_1k_input_tokens: float
    price_per_1k_output_tokens: float
    prefill_ms_per_token: float
    decode_base_ms: float
    decode_ms_per_extra_seq: float
    max_batch_size: int
    kv_capacity_tokens: int
    # Fixed time every request spends before the model starts on it, whatever its
    # prompt length. 0 in the illustrative profiles; calibration fits it
    # (docs/13-calibration.md).
    per_request_overhead_ms: float = 0.0

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("name must not be empty")
        for field_name in (
            "price_per_1k_input_tokens",
            "price_per_1k_output_tokens",
            "prefill_ms_per_token",
            "decode_base_ms",
        ):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} must be positive")
        if self.decode_ms_per_extra_seq < 0:
            raise ValueError("decode_ms_per_extra_seq must not be negative")
        if self.per_request_overhead_ms < 0:
            raise ValueError("per_request_overhead_ms must not be negative")
        if self.max_batch_size < 1:
            raise ValueError("max_batch_size must be at least 1")
        if self.kv_capacity_tokens < 1:
            raise ValueError("kv_capacity_tokens must be at least 1")


ECONOMY: Final = ModelProfile(
    name="llama-3.2-1b",
    tier=Tier.ECONOMY,
    price_per_1k_input_tokens=0.0001,
    price_per_1k_output_tokens=0.0002,
    prefill_ms_per_token=0.05,
    decode_base_ms=6.0,
    decode_ms_per_extra_seq=0.2,
    max_batch_size=32,
    kv_capacity_tokens=65_536,
)

STANDARD: Final = ModelProfile(
    name="llama-3.2-3b",
    tier=Tier.STANDARD,
    price_per_1k_input_tokens=0.0003,
    price_per_1k_output_tokens=0.0006,
    prefill_ms_per_token=0.15,
    decode_base_ms=12.0,
    decode_ms_per_extra_seq=0.6,
    max_batch_size=16,
    kv_capacity_tokens=32_768,
)

PREMIUM: Final = ModelProfile(
    name="llama-3.1-8b",
    tier=Tier.PREMIUM,
    price_per_1k_input_tokens=0.0010,
    price_per_1k_output_tokens=0.0020,
    prefill_ms_per_token=0.60,
    decode_base_ms=30.0,
    decode_ms_per_extra_seq=2.5,
    max_batch_size=8,
    kv_capacity_tokens=16_384,
)

PROFILES: Final[Mapping[Tier, ModelProfile]] = MappingProxyType(
    {profile.tier: profile for profile in (ECONOMY, STANDARD, PREMIUM)}
)


_CALIBRATED_FIELDS = ("prefill_ms_per_token", "decode_base_ms", "decode_ms_per_extra_seq")
_OVERHEAD_FIELD = "per_request_overhead_ms"


def load_calibrated_profiles(path: Path) -> Mapping[Tier, ModelProfile]:
    """The standard profiles with the timing parameters from a calibration file.

    The file is what ``benchmarks/calibrate.py`` writes. Only the timing parameters are
    taken from it; prices, batch limits and KV capacities stay as they are, because
    calibration does not measure them. ``per_request_overhead_ms`` is optional, since
    files written before it was fitted do not have it, and is then 0. A tier missing
    from the file keeps its illustrative profile. Raises ``ValueError`` if the file
    does not have the expected shape.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    entries = data.get("profiles") if isinstance(data, dict) else None
    if not isinstance(entries, dict) or not entries:
        raise ValueError(f"{path} has no 'profiles' section")

    profiles = dict(PROFILES)
    for name, entry in entries.items():
        try:
            tier = Tier(name)
        except ValueError:
            raise ValueError(f"{path}: {name!r} is not a tier") from None
        values: dict[str, float] = {}
        for field_name in _CALIBRATED_FIELDS:
            value = entry.get(field_name) if isinstance(entry, dict) else None
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ValueError(f"{path}: profiles.{name}.{field_name} must be a number")
            values[field_name] = float(value)
        overhead = entry.get(_OVERHEAD_FIELD, 0.0)
        if isinstance(overhead, bool) or not isinstance(overhead, int | float):
            raise ValueError(f"{path}: profiles.{name}.{_OVERHEAD_FIELD} must be a number")
        profiles[tier] = dataclasses.replace(
            PROFILES[tier],
            prefill_ms_per_token=values["prefill_ms_per_token"],
            decode_base_ms=values["decode_base_ms"],
            decode_ms_per_extra_seq=values["decode_ms_per_extra_seq"],
            per_request_overhead_ms=float(overhead),
        )
    return MappingProxyType(profiles)
