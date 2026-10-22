from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from benchmarks import report

from gateway.profiles import PROFILES, UNSTABLE_PARAMETERS, Tier


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
        "each-model",
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


async def test_a_simulated_section_is_reproducible_and_names_its_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GATEWAY_PROFILES", raising=False)
    first = await report.build("each-model")
    second = await report.build("each-model")

    assert first == second
    assert first.startswith("## 3. Each model on the same workload")
    assert "Reproduce this section: `uv run python -m benchmarks.report --only each-model`" in first
    assert "**Simulated** on the calibrated profiles" in first
    assert "**Unstable parameters**" in first
    assert "**Hardware-limited** (model partly on the CPU): premium." in first
    # The saturation check behind the numbers is shown, and nothing is saturated.
    assert "| premium alone | premium |" in first
    assert "**saturated**" not in first
    # No cost: prices were never measured.
    assert "**Cost and routing by plan are not reported.**" in first
    assert "USD" not in first


async def test_calibration_section_says_so_when_nothing_was_measured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(report, "CALIBRATION", tmp_path / "nothing-here")

    section = await report.calibration()

    assert "**No calibration has been run.**" in section.body
    assert "nothing in this report has been calibrated" in section.body
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
    # The fitted values only: nothing else is set beside them.
    assert "| economy | llama3.2:1b (Q4_K_M) | 0.2100 | 7.50 | 1.80 | 0.0 | 36 |" in body
    assert "| 36.0% | - |" in body
    assert "- **economy:** something the tool flagged" in body
    assert "![economy](../benchmarks/calibration/economy.png)" in body
    assert "standard" not in body.split("Fitted timing parameters")[1].split("Sections 1 to 3")[0]


def test_limitations_cover_the_points_a_reader_must_not_miss() -> None:
    text = " ".join(report.LIMITATIONS.split())
    for phrase in (
        "Sections 1 to 3 are simulations",
        "Seven of the twelve fitted parameters are unstable",
        "The premium tier is hardware-limited",
        "Batches stop at 4",
        "Never measured: prices, `kv_capacity_tokens`, and the prefix cache's behaviour",
        "placeholders, operator-supplied, not data",
        "Overload (section 4): not measured on real data",
        "Chunked prefill (section 5): not measured on real data",
        "Saturated runs are not reported as latency",
        "Answer quality is one small measurement",
        "one seed and one workload",
        "must not be compared directly",
    ):
        assert phrase in text
    assert "illustrative" not in text


# --- the profiles the simulated sections run on ---------------------------------------------


def _write_calibration(folder: Path, *, extra_ms: float = 3.0) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "calibrated_profiles.json"
    profile = {
        "prefill_ms_per_token": 0.2,
        "decode_base_ms": 7.0,
        "decode_ms_per_extra_seq": extra_ms,
        "per_request_overhead_ms": 90.0,
        "max_concurrency_measured": 2,
        "unstable_parameters": ["per_request_overhead_ms"],
        "hardware_limited": False,
    }
    path.write_text(json.dumps({"profiles": {"economy": profile}}), encoding="utf-8")
    return path


def test_the_default_is_the_tracked_calibration_and_the_environment_can_name_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GATEWAY_PROFILES", raising=False)
    found = report.calibration_in_use()
    assert found.profiles is PROFILES
    assert found.source == "benchmarks/calibration/calibrated_profiles.json"
    assert found.unstable == UNSTABLE_PARAMETERS
    assert found.hardware_limited == {Tier.PREMIUM}
    # The tracked file is an aggregate, so the measured range of the batching cost is known.
    assert set(found.batching_cost_range) == set(Tier)
    for tier, (low, high) in found.batching_cost_range.items():
        assert low < PROFILES[tier].decode_ms_per_extra_seq < high

    monkeypatch.setenv("GATEWAY_PROFILES", str(_write_calibration(tmp_path, extra_ms=5.0)))
    named = report.calibration_in_use()
    assert named.profiles[Tier.ECONOMY].decode_ms_per_extra_seq == 5.0
    # Batches stop at what that file measured.
    assert named.profiles[Tier.ECONOMY].max_batch_size == 2
    assert named.unstable[Tier.ECONOMY] == {"per_request_overhead_ms"}
    assert named.batching_cost_range == {}


async def test_batching_section_reports_no_p99_for_a_saturated_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, figures: Path
) -> None:
    monkeypatch.setenv("GATEWAY_PROFILES", str(_write_calibration(tmp_path)))
    monkeypatch.setattr(report, "LOAD_LEVELS", (0.5, 4.0))
    found = report.calibration_in_use()
    only_economy = dataclasses.replace(found, profiles={Tier.ECONOMY: found.profiles[Tier.ECONOMY]})
    monkeypatch.setattr(report, "calibration_in_use", lambda: only_economy)

    text = await report.build("batching")

    rows = [line for line in text.splitlines() if line.startswith("| economy | ")]
    calm, flooded = rows[0], rows[1]
    assert "saturated" not in calm
    # Four times the baseline's capacity: neither worker's p99 is a latency any more.
    assert flooded.count("| saturated |") == 2
    # The gain table: the largest batch is the measured one; no aggregate, so no range.
    gain_row = next(line for line in rows if line.startswith("| economy | 2 |"))
    assert gain_row.endswith("| - | - |")
    assert (figures / "1-batching.png").exists()


async def test_batching_gain_moves_with_the_measured_cost_per_extra_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GATEWAY_PROFILES", raising=False)
    gains = await report.batching_gains(report.calibration_in_use())
    for tier in Tier:
        assert gains.max_batch[tier] == 4
        assert gains.highest_cost[tier] <= gains.median[tier] <= gains.lowest_cost[tier]


async def test_prefix_cache_section_leaves_out_a_saturated_row(
    monkeypatch: pytest.MonkeyPatch, figures: Path
) -> None:
    monkeypatch.delenv("GATEWAY_PROFILES", raising=False)
    monkeypatch.setattr(report, "PREFIX_FRACTIONS", (1.0,))
    monkeypatch.setattr(report, "PREFIX_RATE_PER_S", 6.0)

    text = await report.build("prefix-cache")

    assert "| 100% | saturated | saturated |" in text
    assert "| 100% sharing, cache off | standard |" in text
    assert "**saturated**" in text
    assert "**The prefix cache's behaviour was never measured.**" in text


async def test_overload_and_chunked_prefill_have_no_results() -> None:
    for name in ("overload", "chunked-prefill"):
        body = await report.build(name)
        assert "**Not measured on real data.** No results are given." in body
        assert "|" not in body  # no table
        assert "![" not in body  # no figure
        assert " ms" not in body


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

    assert section.title == "7. Answer quality"
    assert "**No quality evaluation has been run.**" in section.body
    assert "benchmarks.quality_eval" in section.body


async def test_quality_section_reports_measured_accuracy_and_no_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, figures: Path
) -> None:
    path = tmp_path / "results.json"
    path.write_text(json.dumps(_quality_results()), encoding="utf-8")
    monkeypatch.setattr(report, "QUALITY_RESULTS", path)

    body = (await report.quality()).body

    # What kind of number this is, and how small the sample is, come first.
    assert body.startswith("**Measured on real models.** No simulated number is in this section.")
    assert "**This is a small, one-run measurement: 10 questions, each asked once.**" in body
    assert "49 to 52 points wide" in body
    assert (
        "| Questions | 10 multiple-choice questions from a test set, license MIT; ten of them |"
        in body
    )
    assert "![7-quality.png](report/7-quality.png)" in body
    assert (figures / "7-quality.png").exists()

    assert "| economy | small | 3 of 10 | 30% | 11% to 60% | 2 | 150.0 |" in body
    assert "| premium | big | 7 of 10 | 70% | 40% to 89% | 0 | 150.0 |" in body
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

    # What each plan gives up in accuracy (measured), and no cost: prices were never measured.
    assert "| free | economy | 30% | -40.0 points (-70.0 to -10.0) |" in body
    assert "| pro | standard | 60% | -10.0 points (-35.0 to +15.0) |" in body
    assert "| enterprise | premium | 70% | nothing: this plan includes the best model |" in body
    assert "USD" not in body
    assert "saved" not in body.lower()
