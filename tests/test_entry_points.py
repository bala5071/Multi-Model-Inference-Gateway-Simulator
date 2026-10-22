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
    key_checks,
    loadgen,
    metrics_example,
    overload,
    report,
    usage_retention,
    usage_store_writes,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SIMULATED = "SIMULATED on the calibrated profiles"
MECHANISM_DEMO = "MECHANISM DEMO, NOT MEASURED ON REAL DATA"


def test_baseline_limits_prints_the_table_in_the_docs(capsys: pytest.CaptureFixture[str]) -> None:
    baseline_limits.main()
    out = capsys.readouterr().out
    # The rows quoted in docs/02-baseline.md.
    assert "economy       1010.7  0.989  149.0            271.4   1.82" in out
    assert "premium       6570.0  0.152   22.2             33.5   1.51" in out


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
        for parts in (line.split() for line in capsys.readouterr().out.splitlines()[3:])
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
    # A demonstration of the mechanism, labelled so nobody quotes it; the behaviour itself
    # is pinned on the test profiles in test_overload_api.py.
    assert out.startswith(MECHANISM_DEMO)
    assert "no downgrade" in out
    assert "with downgrade" in out
    assert "state changes with downgrade:" in out


async def test_chunked_prefill_benchmark_prints_a_row_per_chunk_size(
    capsys: pytest.CaptureFixture[str],
) -> None:
    await chunked_prefill.main()
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith(MECHANISM_DEMO)
    assert [line.split()[0] for line in lines[3:]] == ["off", "4096", "1024", "256", "64", "16"]


def test_key_check_benchmark_times_checks_and_last_used_writes(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    key_checks.main(["--checks", "20", "--dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert out.startswith("MEASURED on this machine")
    names = [" ".join(line.split()[:-5]) for line in out.splitlines()[-5:]]
    assert names == [
        "check, batched",
        "check + write per request",
        "flush of 1 keys",
        "flush of 100 keys",
        "flush of 1000 keys",
    ]
    assert "gwk_" not in out  # its throwaway keys are never printed
    assert list(tmp_path.iterdir()) == []


def test_retention_benchmark_times_batched_and_unbatched_pruning(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    usage_retention.main(["--rows", "3000", "--expired", "2500", "--dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert out.startswith("MEASURED on this machine")
    rows = [line.split() for line in out.splitlines()[-2:]]
    assert [r[:3] for r in rows] == [["batch", "of", "1000"], ["all", "at", "once"]]
    assert rows[0][3] == "3"  # 1000 + 1000 + 500
    assert list(tmp_path.iterdir()) == []


def test_usage_store_write_benchmark_times_every_store(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    usage_store_writes.main(["--writes", "5", "--dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert out.startswith("MEASURED on this machine")
    rows = [line.split() for line in out.splitlines() if line.startswith(("in-memory", "sqlite"))]
    assert [" ".join(r[:-5]) for r in rows] == ["in-memory", "sqlite WAL FULL", "sqlite WAL NORMAL"]
    assert all(r[-5] == "5" for r in rows)
    # The temporary database was removed.
    assert list(tmp_path.iterdir()) == []


async def test_metrics_example_prints_the_rows_in_the_docs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    await metrics_example.main()
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith(SIMULATED)
    # The rows quoted in docs/05-metrics.md.
    assert "premium   queue_ms       23     172.9     161.9     199.1     225.0     225.0" in out
    assert "premium   total_ms       23   10614.4   11231.7   16066.3   22677.0   22677.0" in out
    assert "economy   itl_ms      22098       9.0       9.4      12.1      14.7     137.3" in out
    assert out.rstrip().endswith("cost USD is at PLACEHOLDER prices: operator-supplied, not data.")


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
    assert [line.split()[0] for line in table.splitlines()[2:-1]] == [
        "model",
        "economy",
        "standard",
        "ALL",
    ]
    assert table.splitlines()[-1].startswith("cost USD is at PLACEHOLDER prices")


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
    assert "invalid_api_key: The API key is not valid." in out
    # The demo's keys never reach its output.
    assert "gwk_" not in out
    assert "plan=enterprise  requests=4" in out


async def test_whole_report_builds_with_every_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(report, "FIGURES", tmp_path / "figures")
    monkeypatch.setattr(report, "CALIBRATION", tmp_path / "no-calibration")
    monkeypatch.setattr(report, "QUALITY_RESULTS", tmp_path / "no-results.json")

    text = await report.build(None)

    # Sections 4 and 5 have no results, and say so first.
    for section in ("## 4. ", "## 5. "):
        assert (
            text.split(section)[1]
            .split("\n\n")[1]
            .startswith("**Not measured on real data.** No results are given.")
        )
    assert "illustrative" not in text.lower()

    headings = [line for line in text.splitlines() if line.startswith("## ")]
    assert headings == [
        "## 1. Baseline vs continuous batching",
        "## 2. Prefix cache on and off",
        "## 3. Each model on the same workload",
        "## 4. Overload: admission control and downgrade",
        "## 5. Chunked prefill",
        "## 6. Simulated against real models",
        "## 7. Answer quality",
        "## Limitations",
    ]
    assert text.count("Reproduce this section: `uv run python -m benchmarks.report --only ") == 7
    assert "**Sections 1 to 3 are simulated**" in text
    assert sorted(p.name for p in (tmp_path / "figures").iterdir()) == [
        "1-batching.png",
        "2-prefix-cache.png",
    ]
    # Every figure the text points at was written.
    for name in ("1-batching", "2-prefix-cache"):
        assert f"![{name}.png](report/{name}.png)" in text
