"""Every runnable entry point starts, finishes and says what it should.

These are smoke tests for the commands in the Makefile and the docs. What the numbers
mean is tested elsewhere; here the point is that a command a reader copies still works.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from benchmarks import (
    baseline_limits,
    chunked_prefill,
    compare_batching,
    fairness,
    loadgen,
    overload,
    report,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SIMULATED = "SIMULATED with illustrative profiles; not measurements of real models."


def test_baseline_limits_prints_the_table_in_the_docs(capsys: pytest.CaptureFixture[str]) -> None:
    baseline_limits.main()
    out = capsys.readouterr().out
    # The rows quoted in docs/02-baseline.md.
    assert "economy        841.0  1.189  166.7           2623.0  15.74" in out
    assert "premium       4380.0  0.228   33.3            168.4   5.05" in out


async def test_compare_batching_runs_with_small_arguments(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["compare_batching", "--requests", "30", "--seed", "3"])
    await compare_batching.main()
    out = capsys.readouterr().out
    assert SIMULATED in out
    rows = [line.split()[0] for line in out.splitlines() if line.startswith("  ")]
    # Three tiers, two arrival patterns: a header and one row per worker each time.
    assert rows == ["worker", "baseline", "continuous"] * 6


async def test_fairness_demo_shows_isolation_and_what_breaks_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    await fairness.main()
    rows = {
        (parts[0] + " " + parts[1] + (" " + parts[2] if parts[0] == "one" else ""), parts[-4]): (
            float(parts[-1])
        )
        for parts in (line.split() for line in capsys.readouterr().out.splitlines()[2:])
    }
    separate = rows[("separate workers", "premium")]
    alone = rows[("premium alone", "premium")]
    shared = rows[("one shared queue", "premium")]
    # The light model's p99 is the same with and without the heavy load, and much worse
    # once both share a queue.
    assert separate == alone
    assert shared > 2 * separate


async def test_overload_benchmark_prints_both_runs_and_the_state_changes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    await overload.main()
    out = capsys.readouterr().out
    assert SIMULATED in out
    assert "no downgrade" in out
    assert "with downgrade" in out
    assert out.count("DEGRADED") == 1
    assert out.count("recovered") == 1


async def test_chunked_prefill_benchmark_prints_a_row_per_chunk_size(
    capsys: pytest.CaptureFixture[str],
) -> None:
    await chunked_prefill.main()
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == SIMULATED
    assert [line.split()[0] for line in lines[3:]] == ["off", "4096", "1024", "256", "64", "16"]


def test_load_generator_command_line(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = [
        "loadgen",
        "--requests",
        "40",
        "--rate",
        "20",
        "--weights",
        "economy=3, standard=1",
        "--prompt-words",
        "10-20",
        "--max-tokens",
        "4",
    ]
    monkeypatch.setattr(sys, "argv", [*arguments, "--json"])
    loadgen.main()
    document = json.loads(capsys.readouterr().out)
    assert document["config"]["total_requests"] == 40
    assert document["config"]["model_weights"] == {"economy": 3.0, "standard": 1.0}
    assert document["config"]["prompt_lengths"] == [
        {"min_words": 10, "max_words": 20, "weight": 1.0}
    ]
    assert document["overall"]["requests"] == 40
    assert set(document["models"]) == {"economy", "standard"}

    monkeypatch.setattr(sys, "argv", arguments)
    loadgen.main()
    table = capsys.readouterr().out
    assert table.startswith("IN-PROCESS on simulated time")
    assert [line.split()[0] for line in table.splitlines()[2:]] == [
        "model",
        "economy",
        "standard",
        "ALL",
    ]


def test_demo_script_runs_end_to_end() -> None:
    done = subprocess.run(
        [sys.executable, "scripts/demo.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    out = done.stdout
    assert "Simulator mode: no model runs." in out
    # Each plan gets the best model it includes.
    assert "X-Model-Used: economy" in out
    assert "X-Model-Used: standard" in out
    assert "X-Model-Used: premium" in out
    assert "model_not_in_plan" in out
    assert "(cached 108)" in out
    assert out.count("   token ") == 4
    assert "unknown_tenant" in out
    assert "plan=enterprise  requests=4" in out


async def test_whole_report_builds_with_every_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(report, "FIGURES", tmp_path / "figures")
    monkeypatch.setattr(report, "CALIBRATION", tmp_path / "no-calibration")

    text = await report.build(None)

    headings = [line for line in text.splitlines() if line.startswith("## ")]
    assert headings == [
        "## 1. Baseline vs continuous batching",
        "## 2. Prefix cache on and off",
        "## 3. Cost against latency, and routing by plan",
        "## 4. Overload: admission control and downgrade",
        "## 5. Chunked prefill",
        "## 6. Simulated against real models",
        "## Limitations",
    ]
    assert text.count("Reproduce this section: `uv run python -m benchmarks.report --only ") == 6
    assert "**Sections 1 to 5 are simulated**" in text
    assert sorted(p.name for p in (tmp_path / "figures").iterdir()) == [
        "1-batching.png",
        "2-prefix-cache.png",
        "3-cost-latency.png",
        "4-overload.png",
        "5-chunked-prefill.png",
    ]
    # Every figure the text points at was written.
    for name in ("1-batching", "2-prefix-cache", "3-cost-latency", "4-overload"):
        assert f"![{name}.png](report/{name}.png)" in text
