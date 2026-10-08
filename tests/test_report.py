from __future__ import annotations

import json
from pathlib import Path

import pytest
from benchmarks import report


@pytest.fixture
def figures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Send figures to a temporary folder so tests never touch docs/report."""
    target = tmp_path / "figures"
    monkeypatch.setattr(report, "FIGURES", target)
    return target


def test_report_has_the_six_sections_in_order() -> None:
    assert list(report.SECTIONS) == [
        "batching",
        "prefix-cache",
        "cost",
        "overload",
        "chunked-prefill",
        "calibration",
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
        "Answer quality is not modelled",
        "one seed and one workload",
        "must not be compared directly",
    ):
        assert phrase in text
