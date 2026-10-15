from __future__ import annotations

import json
from pathlib import Path

import pytest
from benchmarks import report

from gateway.profiles import Tier


@pytest.fixture
def figures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Send figures to a temporary folder so tests never touch docs/report."""
    target = tmp_path / "figures"
    monkeypatch.setattr(report, "FIGURES", target)
    return target


def test_report_has_the_seven_sections_in_order() -> None:
    assert list(report.SECTIONS) == [
        "batching",
        "prefix-cache",
        "cost",
        "overload",
        "chunked-prefill",
        "calibration",
        "quality",
    ]


def test_table_formats_numbers_and_text() -> None:
    table = report._table(
        ("Name", "Value"), [("a", 1234.567), ("b", 3.14159), ("c", 7), ("d", "x")]
    )
    assert table.splitlines() == [
        "| Name | Value |",
        "|---:|---:|",
        "| a | 1,234.6 |",
        "| b | 3.14 |",
        "| c | 7 |",
        "| d | x |",
    ]


async def test_a_simulated_section_is_reproducible_and_names_its_command(figures: Path) -> None:
    first = await report.build("chunked-prefill")
    picture = (figures / "5-chunked-prefill.png").read_bytes()
    second = await report.build("chunked-prefill")

    assert first == second
    assert first.startswith("## 5. Chunked prefill")
    assert (
        "Reproduce this section: `uv run python -m benchmarks.report --only chunked-prefill`"
        in (first)
    )
    assert "![5-chunked-prefill.png](report/5-chunked-prefill.png)" in first
    # The figures quoted in docs/11 appear here from the same code.
    assert "| off | 15.00 | 29.40 | 1,063.0 | 1,303.0 | 1,027.0 | 1,075.1 |" in first
    assert picture.startswith(b"\x89PNG")
    assert (figures / "5-chunked-prefill.png").read_bytes() == picture


async def test_calibration_section_says_so_when_nothing_was_measured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(report, "CALIBRATION", tmp_path / "nothing-here")

    section = await report.calibration()

    assert "**No calibration has been run.**" in section.body
    assert "illustrative model profiles" in section.body
    assert "benchmarks.calibrate" in section.body


async def test_calibration_section_reports_what_the_files_say(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = {
        "measured_at": "2026-10-07T21:00:00+00:00",
        "hardware": {
            "cpu": "Test CPU",
            "logical_cores": 8,
            "ram_gb": 16.0,
            "gpu": "Test GPU, 8188 MiB",
            "os": "TestOS 1",
        },
        "notes": "a note",
        "servers": {
            "economy": {
                "kind": "ollama",
                "version": "0.12.6",
                "model": "llama3.2:1b",
                "quantization": "Q4_K_M",
            }
        },
        "profiles": {
            "economy": {
                "prefill_ms_per_token": 0.21,
                "decode_base_ms": 7.5,
                "decode_ms_per_extra_seq": 1.8,
                "samples": 36,
                "mape_percent": {
                    "ttft_fit": 30.0,
                    "total_fit": 9.0,
                    "ttft_holdout": 36.04,
                    "total_holdout": None,
                },
                "notes": ["something the tool flagged"],
            }
        },
    }
    (tmp_path / "calibrated_profiles.json").write_text(json.dumps(document), encoding="utf-8")
    (tmp_path / "economy.png").write_bytes(b"png")
    monkeypatch.setattr(report, "CALIBRATION", tmp_path)

    body = (await report.calibration()).body

    assert "measurements of real models" in body
    assert "| CPU | Test CPU (8 logical cores) |" in body
    assert "| Model server | ollama 0.12.6 |" in body
    # Fitted value first, the illustrative one the simulator uses in brackets.
    assert "| economy | llama3.2:1b (Q4_K_M) | 0.2100 (0.05) | 7.50 (6) | 1.80 (0.2) | 36 |" in body
    assert "| 36.0% | - |" in body
    assert "- **economy:** something the tool flagged" in body
    assert "![economy](../benchmarks/calibration/economy.png)" in body
    assert "standard" not in body.split("Fitted timing parameters")[1].split("Sections 1 to 5")[0]


def test_limitations_cover_the_points_a_reader_must_not_miss() -> None:
    text = report.LIMITATIONS
    for phrase in (
        "Sections 1 to 5 are simulations",
        "Prices are illustrative",
        "Answer quality is one small measurement",
        "one seed and one workload",
        "must not be compared directly",
    ):
        assert phrase in text


# --- calibrated variants of sections 1 and 2 -----------------------------------------------


def _write_calibration(folder: Path, *, extra_ms: float = 3.0) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "calibrated_profiles.json"
    profile = {
        "prefill_ms_per_token": 0.2,
        "decode_base_ms": 7.0,
        "decode_ms_per_extra_seq": extra_ms,
        "per_request_overhead_ms": 90.0,
        "max_concurrency_measured": 4,
    }
    path.write_text(json.dumps({"profiles": {"economy": profile}}), encoding="utf-8")
    return path


def test_calibrated_profiles_come_from_the_tracked_file_or_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GATEWAY_PROFILES", raising=False)
    monkeypatch.setattr(report, "CALIBRATION", tmp_path / "nothing-here")
    assert report.calibrated_profiles() is None

    monkeypatch.setattr(report, "CALIBRATION", tmp_path / "tracked")
    _write_calibration(tmp_path / "tracked")
    found = report.calibrated_profiles()
    assert found is not None
    # Only the tier in the file, with batches of at most what was measured in the capped set.
    assert list(found.profiles) == [Tier.ECONOMY]
    assert found.profiles[Tier.ECONOMY].per_request_overhead_ms == 90.0
    assert found.profiles[Tier.ECONOMY].max_batch_size == 32
    assert found.capped[Tier.ECONOMY].max_batch_size == 4
    assert found.source == "calibrated_profiles.json"

    monkeypatch.setenv(
        "GATEWAY_PROFILES", str(_write_calibration(tmp_path / "other", extra_ms=5.0))
    )
    named = report.calibrated_profiles()
    assert named is not None
    assert named.profiles[Tier.ECONOMY].decode_ms_per_extra_seq == 5.0


async def test_batching_section_shows_the_calibrated_variant_and_the_smaller_gain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, figures: Path
) -> None:
    monkeypatch.delenv("GATEWAY_PROFILES", raising=False)
    monkeypatch.setattr(report, "CALIBRATION", tmp_path)
    _write_calibration(tmp_path)

    text = await report.build("batching")
    gains = await report.batching_gains()

    assert "### With calibrated profiles" in text
    assert "**Still simulated**" in text
    assert "![1-batching-calibrated.png](report/1-batching-calibrated.png)" in text
    assert gains is not None
    assert gains.capped[Tier.ECONOMY] < gains.calibrated[Tier.ECONOMY]
    assert gains.calibrated[Tier.ECONOMY] < gains.illustrative[Tier.ECONOMY]
    row = (
        f"| economy | 32 | {gains.illustrative[Tier.ECONOMY]:.1f}x "
        f"| {gains.calibrated[Tier.ECONOMY]:.1f}x | 4 | {gains.capped[Tier.ECONOMY]:.1f}x |"
    )
    assert row in text

    limitation = report.batching_limitation(gains)
    assert "overstate what batching buys" in limitation
    assert f"{gains.capped[Tier.ECONOMY]:.1f}x on economy" in limitation
    assert "held to the 4 requests" in limitation
    assert "simulated" in limitation


async def test_sections_say_so_when_there_is_no_calibration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, figures: Path
) -> None:
    monkeypatch.delenv("GATEWAY_PROFILES", raising=False)
    monkeypatch.setattr(report, "CALIBRATION", tmp_path / "nothing-here")

    assert "No calibration of the standard tier was found" in await report.build("prefix-cache")
    assert await report.batching_gains() is None
    assert "has not been checked against a calibration" in report.batching_limitation(None)


async def test_calibration_section_shows_fit_quality_and_both_kinds_of_spread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def spread(low: float, high: float) -> dict[str, object]:
        mean = (low + high) / 2
        return {
            "values": [low, high],
            "min": low,
            "max": high,
            "mean": mean,
            "range_percent_of_mean": 100 * (high - low) / mean,
        }

    def document(extra_ms: float, when: str) -> dict[str, object]:
        return {
            "measured_at": when,
            "hardware": {
                "cpu": "Test CPU",
                "logical_cores": 8,
                "ram_gb": 16,
                "gpu": "none",
                "os": "x",
            },
            "notes": "",
            "plan": {"repeats": 2},
            "servers": {
                "economy": {"kind": "ollama", "version": "1", "model": "m", "quantization": "Q4"}
            },
            "profiles": {
                "economy": {
                    "prefill_ms_per_token": 0.2,
                    "decode_base_ms": 7.0,
                    "decode_ms_per_extra_seq": extra_ms,
                    "per_request_overhead_ms": 90.0,
                    "samples": 10,
                    "fit": {
                        "ttft_vs_prompt_tokens": {"r2": 0.9},
                        "per_token_vs_extra_sequences": {"r2": 0.631},
                    },
                    "mape_percent": {
                        "ttft_fit": 1.0,
                        "total_fit": 1.0,
                        "ttft_holdout": 2.0,
                        "total_holdout": 2.0,
                    },
                    "mape_percent_by_concurrency": {
                        "1": {"requests": 6, "ttft": 8.0, "total": 6.0},
                        "4": {"requests": 4, "ttft": 58.9, "total": 2.5},
                    },
                    "spread_across_repeats": {
                        name: spread(1.0, 3.0)
                        for name in (
                            "prefill_ms_per_token",
                            "decode_base_ms",
                            "decode_ms_per_extra_seq",
                            "per_request_overhead_ms",
                        )
                    },
                    "notes": [],
                }
            },
        }

    (tmp_path / "run2").mkdir()
    (tmp_path / "calibrated_profiles.json").write_text(
        json.dumps(document(2.0, "day one")), encoding="utf-8"
    )
    (tmp_path / "run2" / "calibrated_profiles.json").write_text(
        json.dumps(document(4.0, "day two")), encoding="utf-8"
    )
    monkeypatch.setattr(report, "CALIBRATION", tmp_path)

    body = (await report.calibration()).body

    # A poor fit is in the table, not left out of it.
    assert "| economy | 90.0 | 0.900 | 0.631 | 8.0% | 58.9% | 6.0% | 2.5% |" in body
    assert "each of the 2 repeats fitted on its own" in body
    assert "| economy | ms per extra sequence | 1 | 2 | 3 | 100.0% |" in body
    assert "run 2 times (measured at day one, day two)" in body
    assert "| economy | ms per extra sequence | 2 | 3 | 4 | 66.7% |" in body


# --- section 4 against the calibration ------------------------------------------------------


def _write_premium(folder: Path, *, base_ms: float, extra_ms: float) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    profile = {
        "prefill_ms_per_token": 0.8,
        "decode_base_ms": base_ms,
        "decode_ms_per_extra_seq": extra_ms,
        "per_request_overhead_ms": 200.0,
        "max_concurrency_measured": 4,
    }
    (folder / "calibrated_profiles.json").write_text(
        json.dumps({"profiles": {"premium": profile}}), encoding="utf-8"
    )


def test_overload_section_says_the_workload_is_saturated_on_a_slow_calibration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GATEWAY_PROFILES", raising=False)
    monkeypatch.setattr(report, "CALIBRATION", tmp_path)
    _write_premium(tmp_path, base_ms=46.0, extra_ms=38.0)

    text = report.overload_on_calibrated()

    assert text.startswith("**How the calibrated batching gain would change this.**")
    assert "No overload numbers are given for the calibrated profiles" in text
    assert "saturated on them" in " ".join(text.split())
    # Only a capacity bound is shown: one row for the tier in the file, no latencies.
    assert text.count("| premium |") == 1
    assert "p99" not in text


def test_overload_section_says_so_when_the_bound_is_not_exceeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GATEWAY_PROFILES", raising=False)
    monkeypatch.setattr(report, "CALIBRATION", tmp_path)
    _write_premium(tmp_path, base_ms=1.0, extra_ms=0.0)

    text = report.overload_on_calibrated()

    assert "does not saturate the calibrated premium profile" in text
    assert "No overload numbers are given" not in text


# --- section 6 for a calibration that is the median of several runs --------------------------


async def test_calibration_section_marks_unstable_parameters_and_hardware_limited_tiers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def combined(median: float, low: float, high: float, unstable: bool) -> dict[str, object]:
        return {
            "median": median,
            "min": low,
            "max": high,
            "range_percent_of_median": 100 * (high - low) / median,
            "unstable": unstable,
            "values": [low, high],
        }

    offloaded = {
        "source": "ollama /api/ps",
        "size_bytes": 8 * 2**30,
        "size_vram_bytes": 7 * 2**30,
        "gpu_percent": 87.5,
        "cpu_percent": 12.5,
    }
    document = {
        "measured_at": "day one to day two",
        "aggregated_from": [
            {"run": "run1", "measured_at": "day one", "notes": ""},
            {"run": "run2", "measured_at": "day two", "notes": ""},
        ],
        "unstable_above_range_percent": 25.0,
        "hardware": {"cpu": "Test CPU", "logical_cores": 8, "ram_gb": 16, "gpu": "none", "os": "x"},
        "notes": "Median of 2 runs (run1, run2); no run was preferred or dropped.",
        "plan": {"repeats": 5},
        "servers": {
            "premium": {
                "kind": "ollama",
                "version": "1",
                "model": "m",
                "quantization": "Q4",
                "placement": offloaded,
            }
        },
        "profiles": {
            "premium": {
                "prefill_ms_per_token": 0.8,
                "decode_base_ms": 46.0,
                "decode_ms_per_extra_seq": 30.0,
                "per_request_overhead_ms": 220.0,
                "samples": 140,
                "aggregate": {
                    "prefill_ms_per_token": combined(0.8, 0.76, 0.84, False),
                    "decode_base_ms": combined(46.0, 45.0, 47.0, False),
                    "decode_ms_per_extra_seq": combined(30.0, 20.0, 40.0, True),
                    "per_request_overhead_ms": combined(220.0, 210.0, 230.0, False),
                },
                "unstable_parameters": ["decode_ms_per_extra_seq"],
                "hardware_limited": True,
                "placement_by_run": {"run1": None, "run2": offloaded},
                "fit_r2_by_run": {
                    "ttft_vs_prompt_tokens": [0.96, 0.91],
                    "per_token_vs_extra_sequences": [0.97, 0.83],
                },
                "mape_percent": {
                    "ttft_fit": 30.0,
                    "total_fit": 9.0,
                    "ttft_holdout": 31.0,
                    "total_holdout": 8.0,
                },
                "mape_percent_by_concurrency": {
                    "1": {"requests": 84, "ttft": 20.0, "total": 9.0},
                    "4": {"requests": 56, "ttft": 55.5, "total": 12.0},
                },
                "notes": ["unstable: decode_ms_per_extra_seq ranged from 20 to 40 over 2 runs"],
            }
        },
    }
    (tmp_path / "calibrated_profiles.json").write_text(json.dumps(document), encoding="utf-8")
    # A run folder beside it must not bring back the single-run spread table.
    (tmp_path / "run1").mkdir()
    (tmp_path / "run1" / "calibrated_profiles.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(report, "CALIBRATION", tmp_path)

    body = (await report.calibration()).body

    assert "**median of 2 runs** (run1, run2," in body
    assert "no run was chosen over another" in body
    assert "| premium | ms per extra sequence | 30 | 20 | 40 | 66.7% | **unstable** |" in body
    assert "| premium | decode base ms | 46 | 45 | 47 | 4.3% | yes |" in body
    # The worst fit over the runs, and the error where it is worst.
    assert "| premium | 0.910 | 0.830 | 20.0% | 55.5% | 9.0% | 12.0% |" in body
    assert "| premium | 8.0 GiB | 87.5% | 1 of 2 | **hardware-limited** |" in body
    assert "- **premium:** unstable: decode_ms_per_extra_seq ranged from 20 to 40" in body
    assert "Spread between runs" not in body


async def test_calibration_section_says_when_placement_was_not_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = {
        "measured_at": "now",
        "hardware": {"cpu": "c", "logical_cores": 1, "ram_gb": 1, "gpu": "g", "os": "o"},
        "notes": "",
        "plan": {"repeats": 1},
        "servers": {
            "economy": {
                "kind": "unknown",
                "version": "unknown",
                "model": "m",
                "quantization": "q",
                "placement": None,
            }
        },
        "profiles": {
            "economy": {
                "prefill_ms_per_token": 0.2,
                "decode_base_ms": 7.0,
                "decode_ms_per_extra_seq": 2.0,
                "samples": 5,
                "hardware_limited": None,
                "mape_percent": {
                    "ttft_fit": 1.0,
                    "total_fit": 1.0,
                    "ttft_holdout": None,
                    "total_holdout": None,
                },
                "notes": [],
            }
        },
    }
    (tmp_path / "calibrated_profiles.json").write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(report, "CALIBRATION", tmp_path)

    body = (await report.calibration()).body

    assert "| economy | not recorded | not recorded | 0 of 1 | unknown |" in body
    assert "hardware-limited** was partly on the CPU" in body


# --- section 7: answer quality ---------------------------------------------------------------


def _quality_results() -> dict[str, object]:
    def tier(correct: int, low: float, high: float, unparsed: int) -> dict[str, object]:
        return {
            "questions": 10,
            "correct": correct,
            "accuracy": correct / 10,
            "ci95": {"low": low, "high": high, "method": "Wilson score"},
            "unparsed": unparsed,
            "failed": 0,
            "latency_ms": {
                "ttft_p50": 90.0,
                "total_p50": 100.0,
                "total_p99": 150.0,
                "total_max": 160.0,
            },
            "hardware_limited": False,
            "notes": [],
        }

    def answers(chosen: str) -> list[dict[str, object]]:
        return [
            {"gold": "ABCD"[i % 4], "chosen": letter if letter != "-" else None}
            for i, letter in enumerate(chosen)
        ]

    def server(model: str) -> dict[str, object]:
        return {"kind": "ollama", "version": "1", "model": model, "quantization": "Q4"}

    return {
        "measured_at": "one afternoon",
        "hardware": {"cpu": "Test CPU", "ram_gb": 16, "gpu": "Test GPU"},
        "questions": {
            "name": "a test set",
            "license": "MIT",
            "selection": "ten of them",
            "count": 10,
        },
        "method": {
            "temperature": 0.0,
            "seed": 7,
            "max_tokens": 8,
            "scoring": "exact match on the option letter",
        },
        "servers": {
            "economy": server("small"),
            "standard": server("mid"),
            "premium": server("big"),
        },
        "tiers": {
            "economy": tier(3, 0.11, 0.60, 2),
            "standard": tier(6, 0.31, 0.83, 0),
            "premium": tier(7, 0.40, 0.89, 0),
        },
        "answers": {
            "economy": answers("AAAAAABB--"),
            "standard": answers("ABCDABCDAA"),
            "premium": answers("ABCDABCDAB"),
        },
        "differences": {
            "standard_minus_economy": {"difference": 0.3, "low": 0.05, "high": 0.55},
            "premium_minus_economy": {"difference": 0.4, "low": 0.10, "high": 0.70},
            "premium_minus_standard": {"difference": 0.1, "low": -0.15, "high": 0.35},
        },
    }


async def test_quality_section_says_so_when_nothing_was_measured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(report, "QUALITY_RESULTS", tmp_path / "nothing.json")

    section = await report.quality()

    assert section.title == "7. Answer quality against cost"
    assert "**No quality evaluation has been run.**" in section.body
    assert "benchmarks.quality_eval" in section.body


async def test_quality_section_sets_measured_accuracy_beside_simulated_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, figures: Path
) -> None:
    path = tmp_path / "results.json"
    path.write_text(json.dumps(_quality_results()), encoding="utf-8")
    monkeypatch.setattr(report, "QUALITY_RESULTS", path)

    body = (await report.quality()).body

    # What kind of number each column is, and how small the sample is, come first.
    assert body.startswith("**Accuracy here is measured on real models; cost and the first latency")
    assert "**This is a small, one-run measurement: 10 questions, each asked once.**" in body
    assert "49 to 52 points wide" in body
    assert (
        "| Questions | 10 multiple-choice questions from a test set, license MIT; ten of them |"
        in body
    )
    assert "![7-quality.png](report/7-quality.png)" in body
    assert (figures / "7-quality.png").exists()

    assert "| economy | small | 3 of 10 | 30% | 11% to 60% | 2 |" in body
    assert "| premium | big | 7 of 10 | 70% | 40% to 89% | 0 |" in body
    # Replies without an option letter are counted wrong, and that is spelled out.
    assert (
        "- **economy:** 2 replies did not open with an option letter and count as wrong. "
        "Of the 8 that did, 3 were right (38%)." in body
    )
    assert "| economy | 6 | 2 | 0 | 0 | 2 |" in body
    assert "| right answer | 3 | 3 | 2 | 2 | 0 |" in body

    assert (
        "- **premium against economy:** +40.0 points (95% interval +10.0 to +70.0). "
        "The interval excludes zero." in body
    )
    assert (
        "- **premium against standard:** +10.0 points (95% interval -15.0 to +35.0). "
        "The interval includes zero: on this sample the two cannot be told apart." in body
    )

    # The plan scenario: cost saved (simulated) against accuracy given up (measured).
    plan_rows = [line for line in body.splitlines() if line.startswith("| initech (free)")]
    assert len(plan_rows) == 1
    assert "| economy |" in plan_rows[0]
    assert plan_rows[0].endswith("| 30% | -40.0 points (-70.0 to -10.0) |")
    assert "| globex (pro) | standard |" in body
    assert "-10.0 points (-35.0 to +15.0) |" in body
    assert "| 0% | 70% | nothing: this plan gets the best model |" in body
