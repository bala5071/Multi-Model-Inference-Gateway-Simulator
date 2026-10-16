"""The default profiles are the tracked calibration; the test fixtures stay in tests/."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from gateway.profiles import (
    HARDWARE_LIMITED,
    MEASURED_MAX_BATCH_SIZE,
    PLACEHOLDER_KV_CAPACITY_TOKENS,
    PLACEHOLDER_PRICE_PER_1K_INPUT_TOKENS,
    PLACEHOLDER_PRICE_PER_1K_OUTPUT_TOKENS,
    PROFILES,
    UNSTABLE_PARAMETERS,
    Tier,
)

REPO = Path(__file__).resolve().parent.parent
CALIBRATION = REPO / "benchmarks" / "calibration" / "calibrated_profiles.json"
TIMING = (
    "prefill_ms_per_token",
    "decode_base_ms",
    "decode_ms_per_extra_seq",
    "per_request_overhead_ms",
)


def _calibration() -> dict[str, dict[str, object]]:
    profiles: dict[str, dict[str, object]] = json.loads(CALIBRATION.read_text(encoding="utf-8"))[
        "profiles"
    ]
    return profiles


@pytest.mark.parametrize("tier", list(Tier))
def test_default_timing_is_the_median_in_the_tracked_calibration(tier: Tier) -> None:
    entry = _calibration()[tier.value]
    aggregate = entry["aggregate"]
    assert isinstance(aggregate, dict)
    for name in TIMING:
        assert getattr(PROFILES[tier], name) == entry[name] == aggregate[name]["median"]


@pytest.mark.parametrize("tier", list(Tier))
def test_unstable_and_hardware_flags_match_the_calibration(tier: Tier) -> None:
    entry = _calibration()[tier.value]
    unstable = entry["unstable_parameters"]
    assert isinstance(unstable, list)
    assert UNSTABLE_PARAMETERS[tier] == frozenset(unstable)
    assert (tier in HARDWARE_LIMITED) is entry["hardware_limited"]


@pytest.mark.parametrize("tier", list(Tier))
def test_batches_stop_at_the_largest_concurrency_measured(tier: Tier) -> None:
    measured = _calibration()[tier.value]["max_concurrency_measured"]
    assert PROFILES[tier].max_batch_size == MEASURED_MAX_BATCH_SIZE == measured


@pytest.mark.parametrize("tier", list(Tier))
def test_prices_and_kv_capacity_are_the_placeholders(tier: Tier) -> None:
    profile = PROFILES[tier]
    assert profile.price_per_1k_input_tokens == PLACEHOLDER_PRICE_PER_1K_INPUT_TOKENS[tier]
    assert profile.price_per_1k_output_tokens == PLACEHOLDER_PRICE_PER_1K_OUTPUT_TOKENS[tier]
    assert profile.kv_capacity_tokens == PLACEHOLDER_KV_CAPACITY_TOKENS[tier]


def _imports(path: Path) -> list[tuple[str | None, list[str]]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str | None, list[str]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            found.append((node.module, [alias.name for alias in node.names]))
        elif isinstance(node, ast.Import):
            found.extend((alias.name, []) for alias in node.names)
    return found


def test_nothing_outside_tests_imports_the_test_profiles() -> None:
    """TEST_PROFILES are hand-made fixtures, not data: only tests may use them."""
    offenders = []
    for folder in ("src", "benchmarks", "scripts"):
        for path in sorted((REPO / folder).rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            imports_tests = any(
                module is not None
                and (module in ("tests", "helpers") or module.startswith(("tests.", "helpers.")))
                for module, _ in _imports(path)
            )
            if imports_tests or re.search(r"TEST_(PROFILES|ECONOMY|STANDARD|PREMIUM)", text):
                offenders.append(path.relative_to(REPO).as_posix())
    assert offenders == []
