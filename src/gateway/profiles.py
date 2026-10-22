"""Model profiles for the three plan tiers.

The timing parameters are the median of six calibration runs of the real models on one
laptop with Ollama (``benchmarks/calibration/calibrated_profiles.json``, docs/13). Seven of
the twelve did not repeat between runs and are listed in ``UNSTABLE_PARAMETERS``; the
premium tier ran partly on the CPU (``HARDWARE_LIMITED``). ``tests/test_profiles.py`` fails
if the values here and the calibration file ever differ.

The batch limit is 4, the most requests that were measured together. Prices and KV
capacities were never measured: they are PLACEHOLDER values, operator-supplied, not data.
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
    # prompt length. Fitted by calibration (docs/13-calibration.md).
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


# The most requests the calibration sent together. The simulator batches no further, so
# nothing it reports extrapolates the fitted cost per extra sequence past what was measured.
MEASURED_MAX_BATCH_SIZE: Final = 4

# PLACEHOLDER values: operator-supplied, not data. Nothing measured them. They exist because
# the ledger and the KV accounting need a number; no result may be read from them.
PLACEHOLDER_PRICE_PER_1K_INPUT_TOKENS: Final[Mapping[Tier, float]] = MappingProxyType(
    {Tier.ECONOMY: 0.0001, Tier.STANDARD: 0.0003, Tier.PREMIUM: 0.0010}
)
PLACEHOLDER_PRICE_PER_1K_OUTPUT_TOKENS: Final[Mapping[Tier, float]] = MappingProxyType(
    {Tier.ECONOMY: 0.0002, Tier.STANDARD: 0.0006, Tier.PREMIUM: 0.0020}
)
PLACEHOLDER_KV_CAPACITY_TOKENS: Final[Mapping[Tier, int]] = MappingProxyType(
    {Tier.ECONOMY: 65_536, Tier.STANDARD: 32_768, Tier.PREMIUM: 16_384}
)

_NAMES: Final[Mapping[Tier, str]] = MappingProxyType(
    {Tier.ECONOMY: "llama-3.2-1b", Tier.STANDARD: "llama-3.2-3b", Tier.PREMIUM: "llama-3.1-8b"}
)

# Median of six runs, 2026-10-08, benchmarks/calibration/calibrated_profiles.json.
_CALIBRATED_TIMING: Final[Mapping[Tier, Mapping[str, float]]] = MappingProxyType(
    {
        Tier.ECONOMY: {
            "prefill_ms_per_token": 0.19604634758337078,
            "decode_base_ms": 6.7105306756000225,
            "decode_ms_per_extra_seq": 2.6755785484127514,  # unstable
            "per_request_overhead_ms": 90.07723028380775,  # unstable
        },
        Tier.STANDARD: {
            "prefill_ms_per_token": 0.37539946480443515,
            "decode_base_ms": 15.297783920187612,  # unstable
            "decode_ms_per_extra_seq": 4.1332772531601165,  # unstable
            "per_request_overhead_ms": 88.43684341604634,  # unstable
        },
        Tier.PREMIUM: {
            "prefill_ms_per_token": 0.86225807289451,
            "decode_base_ms": 45.13840485128059,
            "decode_ms_per_extra_seq": 24.759616220374745,  # unstable
            "per_request_overhead_ms": 161.93209566970938,  # unstable
        },
    }
)

# Parameters whose highest and lowest value over the six runs are more than 25% of the median
# apart. Every result that rests on one of them must say so.
UNSTABLE_PARAMETERS: Final[Mapping[Tier, frozenset[str]]] = MappingProxyType(
    {
        Tier.ECONOMY: frozenset({"decode_ms_per_extra_seq", "per_request_overhead_ms"}),
        Tier.STANDARD: frozenset(
            {"decode_base_ms", "decode_ms_per_extra_seq", "per_request_overhead_ms"}
        ),
        Tier.PREMIUM: frozenset({"decode_ms_per_extra_seq", "per_request_overhead_ms"}),
    }
)

# Tiers whose model did not fit on the GPU when it was measured.
HARDWARE_LIMITED: Final = frozenset({Tier.PREMIUM})


def _profile(tier: Tier) -> ModelProfile:
    timing = _CALIBRATED_TIMING[tier]
    return ModelProfile(
        name=_NAMES[tier],
        tier=tier,
        price_per_1k_input_tokens=PLACEHOLDER_PRICE_PER_1K_INPUT_TOKENS[tier],
        price_per_1k_output_tokens=PLACEHOLDER_PRICE_PER_1K_OUTPUT_TOKENS[tier],
        prefill_ms_per_token=timing["prefill_ms_per_token"],
        decode_base_ms=timing["decode_base_ms"],
        decode_ms_per_extra_seq=timing["decode_ms_per_extra_seq"],
        max_batch_size=MEASURED_MAX_BATCH_SIZE,
        kv_capacity_tokens=PLACEHOLDER_KV_CAPACITY_TOKENS[tier],
        per_request_overhead_ms=timing["per_request_overhead_ms"],
    )


ECONOMY: Final = _profile(Tier.ECONOMY)
STANDARD: Final = _profile(Tier.STANDARD)
PREMIUM: Final = _profile(Tier.PREMIUM)

PROFILES: Final[Mapping[Tier, ModelProfile]] = MappingProxyType(
    {profile.tier: profile for profile in (ECONOMY, STANDARD, PREMIUM)}
)


_CALIBRATED_FIELDS = ("prefill_ms_per_token", "decode_base_ms", "decode_ms_per_extra_seq")
_OVERHEAD_FIELD = "per_request_overhead_ms"


def load_calibrated_profiles(path: Path) -> Mapping[Tier, ModelProfile]:
    """The standard profiles with the timing parameters from another calibration file.

    The file is what ``benchmarks/calibrate.py`` writes. Only the timing parameters are
    taken from it, and the batch limit is set to its ``max_concurrency_measured`` when it
    has one, so the simulator never batches past what that calibration measured. The
    placeholder prices and KV capacities stay as they are. ``per_request_overhead_ms`` is
    optional, since files written before it was fitted do not have it, and is then 0. A
    tier missing from the file keeps its standard profile. Raises ``ValueError`` if the
    file does not have the expected shape.
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
        measured = entry.get("max_concurrency_measured", PROFILES[tier].max_batch_size)
        if isinstance(measured, bool) or not isinstance(measured, int) or measured < 1:
            raise ValueError(
                f"{path}: profiles.{name}.max_concurrency_measured must be a positive integer"
            )
        profiles[tier] = dataclasses.replace(
            PROFILES[tier],
            prefill_ms_per_token=values["prefill_ms_per_token"],
            decode_base_ms=values["decode_base_ms"],
            decode_ms_per_extra_seq=values["decode_ms_per_extra_seq"],
            per_request_overhead_ms=float(overhead),
            max_batch_size=measured,
        )
    return MappingProxyType(profiles)
