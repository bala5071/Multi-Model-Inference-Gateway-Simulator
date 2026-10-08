from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from pathlib import Path

import pytest
from benchmarks.gate import (
    BUDGETS_PATH,
    EXIT_OK,
    EXIT_OVER_BUDGET,
    EXIT_USAGE,
    STANDARD_LOAD,
    Budgets,
    budget_diff,
    evaluate,
    main,
    measured_budgets,
    workload_fingerprint,
)
from benchmarks.loadgen import LoadConfig, LoadReport, ModelReport

REPO_ROOT = Path(__file__).resolve().parent.parent
# Small enough to run in a fraction of a second; used where the real budgets do not matter.
QUICK = LoadConfig(seed=7, total_requests=120, arrival_rate_per_s=5.0)


def _model(p99_ms: float | None, *, requests: int = 10, errors: int = 0) -> ModelReport:
    return ModelReport(
        requests=requests,
        errors=errors,
        ttft_p50_ms=1.0,
        ttft_p99_ms=2.0,
        total_p50_ms=3.0,
        total_p99_ms=p99_ms,
        throughput_rps=1.0,
        completion_tokens_per_s=1.0,
        cache_hit_rate=0.0,
        cache_token_hit_rate=0.0,
        cost_usd=0.0,
    )


def _report(**p99_by_model: float | None) -> LoadReport:
    config = LoadConfig(model_weights=dict.fromkeys(p99_by_model, 1.0))
    return LoadReport(
        config=config,
        models={name: _model(p99) for name, p99 in p99_by_model.items()},
        overall=_model(None),
    )


def _budgets(report: LoadReport, tolerance: float = 0.10, **p99: float) -> Budgets:
    return Budgets(tolerance, workload_fingerprint(report.config), dict(p99))


# --- the real gate -------------------------------------------------------------------------


def test_committed_budgets_pass_on_the_standard_load(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == EXIT_OK

    out = capsys.readouterr().out
    assert "Performance gate passed." in out
    assert "FAIL" not in out
    for model in ("economy", "standard", "premium"):
        assert model in out


def test_committed_budgets_describe_the_standard_load() -> None:
    budgets = Budgets.load(BUDGETS_PATH)
    assert budgets.workload == workload_fingerprint(STANDARD_LOAD)
    assert set(budgets.p99_total_ms) == {"economy", "standard", "premium"}
    assert 0 < budgets.tolerance <= 0.10


@pytest.mark.parametrize("slowed", ["economy", "standard", "premium"])
def test_doubling_a_models_prefill_cost_fails_the_gate_with_a_readable_message(
    slowed: str, capsys: pytest.CaptureFixture[str]
) -> None:
    # --simulate-regression MODEL=2 doubles that model's prefill_ms_per_token.
    assert main(["--simulate-regression", f"{slowed}=2"]) == EXIT_OVER_BUDGET

    out = capsys.readouterr().out
    rows = {line.split()[0]: line for line in out.splitlines() if line.strip()}
    for model in ("economy", "standard", "premium"):
        assert rows[model].endswith("FAIL" if model == slowed else "ok")
    assert f"FAIL: {slowed}: p99 total latency" in out
    assert "exceeds its limit of" in out
    assert "Performance gate FAILED." in out
    assert "--update-budgets" in out  # tells the reader what to do if it is intended


def test_script_exits_non_zero_when_a_model_is_over_budget(tmp_path: Path) -> None:
    results = tmp_path / "results.json"
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/perf_gate.py",
            "--simulate-regression",
            "premium=2",
            "--output",
            str(results),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == EXIT_OVER_BUDGET
    assert "FAIL: premium: p99 total latency" in completed.stdout
    saved = json.loads(results.read_text(encoding="utf-8"))
    assert saved["passed"] is False
    assert [v["model"] for v in saved["gate"]["verdicts"] if not v["passed"]] == ["premium"]
    assert set(saved["report"]["models"]) == {"economy", "standard", "premium"}


# --- evaluate ------------------------------------------------------------------------------


def test_p99_exactly_at_the_limit_passes_and_just_above_fails() -> None:
    at_limit = _report(a=110.0)
    over = _report(a=110.01)
    budgets = _budgets(at_limit, tolerance=0.10, a=100.0)
    exact_limit_ms = budgets.limit_ms("a")

    assert evaluate(_report(a=exact_limit_ms), budgets).passed
    result = evaluate(over, budgets)
    assert not result.passed
    (verdict,) = result.verdicts
    assert (verdict.budget_ms, verdict.limit_ms) == (100.0, exact_limit_ms)
    assert verdict.change == pytest.approx(0.1001)
    assert "exceeds its limit of 110.0 ms (budget 100.0 ms + 10%)" in verdict.note


def test_only_the_model_over_budget_fails() -> None:
    report = _report(a=100.0, b=300.0, c=50.0)
    result = evaluate(report, _budgets(report, a=100.0, b=200.0, c=50.0))

    assert not result.passed
    assert [(v.model, v.passed) for v in result.verdicts] == [
        ("a", True),
        ("b", False),
        ("c", True),
    ]
    text = result.to_text()
    assert "FAIL: b: p99 total latency 300.0 ms exceeds its limit of 220.0 ms" in text
    assert "Performance gate FAILED." in text


def test_much_faster_than_budget_passes_with_a_note() -> None:
    report = _report(a=50.0)
    result = evaluate(report, _budgets(report, a=100.0))

    assert result.passed
    assert "consider re-baselining" in result.verdicts[0].note
    assert "note: a:" in result.to_text()


def test_model_without_a_budget_fails() -> None:
    report = _report(a=100.0, b=100.0)
    result = evaluate(report, _budgets(report, a=100.0))

    assert not result.passed
    assert "no budget is recorded" in result.verdicts[1].note


def test_budget_for_a_model_the_workload_lacks_fails() -> None:
    report = _report(a=100.0)
    result = evaluate(report, _budgets(report, a=100.0, gone=100.0))

    assert not result.passed
    assert "budget for 'gone'" in result.problems[0]


def test_failed_requests_fail_the_gate_even_if_latency_is_fine() -> None:
    report = _report(a=10.0)
    broken = dataclasses.replace(report, models={"a": _model(10.0, requests=10, errors=2)})
    result = evaluate(broken, _budgets(report, a=100.0))

    assert not result.passed
    assert "2 of 10 requests failed" in result.verdicts[0].note


def test_model_that_got_no_requests_fails() -> None:
    report = _report(a=None)
    result = evaluate(report, _budgets(report, a=100.0))
    assert not result.passed
    assert "no requests" in result.verdicts[0].note


def test_budgets_recorded_for_another_workload_fail() -> None:
    report = _report(a=100.0)
    other = dataclasses.replace(report, config=dataclasses.replace(report.config, seed=99))
    result = evaluate(other, _budgets(report, a=100.0))

    assert not result.passed
    assert "re-baselined" in result.problems[0]


# --- updating budgets ----------------------------------------------------------------------


def test_update_budgets_writes_the_file_and_prints_a_diff(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "budgets.json"
    args = ["--budgets", str(path)]

    # No file yet: checking is a usage error, updating creates it.
    assert main(args, config=QUICK, environ={}) == EXIT_USAGE
    assert "--update-budgets" in capsys.readouterr().out
    assert main([*args, "--update-budgets"], config=QUICK, environ={}) == EXIT_OK
    created = capsys.readouterr().out
    assert "No budgets were recorded before." in created
    assert "+ economy: p99 budget" in created
    recorded = Budgets.load(path)
    assert recorded.workload == workload_fingerprint(QUICK)

    # The file now passes, and updating again changes nothing.
    assert main(args, config=QUICK, environ={}) == EXIT_OK
    capsys.readouterr()
    assert main([*args, "--update-budgets"], config=QUICK, environ={}) == EXIT_OK
    unchanged = capsys.readouterr().out
    assert "(unchanged)" in unchanged
    assert "already up to date" in unchanged
    assert Budgets.load(path) == recorded


def test_update_budgets_shows_old_and_new_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "budgets.json"
    args = ["--budgets", str(path), "--update-budgets"]
    main(args, config=QUICK, environ={})
    recorded = Budgets.load(path)
    stale = dataclasses.replace(
        recorded, p99_total_ms={**recorded.p99_total_ms, "premium": 1000.0, "retired": 5.0}
    )
    path.write_text(stale.to_json(), encoding="utf-8")
    capsys.readouterr()

    assert main(args, config=QUICK, environ={}) == EXIT_OK

    out = capsys.readouterr().out
    assert f"~ premium: p99 budget 1000.000 ms -> {recorded.p99_total_ms['premium']:.3f} ms" in out
    assert "- retired: p99 budget 5.000 ms (removed)" in out
    assert "  economy: p99 budget" in out
    assert Budgets.load(path) == recorded


def test_update_budgets_is_refused_in_ci(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "budgets.json"
    code = main(["--budgets", str(path), "--update-budgets"], config=QUICK, environ={"CI": "true"})

    assert code == EXIT_USAGE
    assert "Refusing to update budgets in CI" in capsys.readouterr().out
    assert not path.exists()


def test_update_budgets_is_refused_for_a_simulated_regression(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "budgets.json"
    code = main(
        ["--budgets", str(path), "--update-budgets", "--simulate-regression", "premium=2"],
        config=QUICK,
        environ={},
    )

    assert code == EXIT_USAGE
    assert "simulated regression" in capsys.readouterr().out
    assert not path.exists()


def test_checking_never_modifies_the_budgets_file(tmp_path: Path) -> None:
    path = tmp_path / "budgets.json"
    main(["--budgets", str(path), "--update-budgets"], config=QUICK, environ={})
    before = path.read_bytes()

    main(["--budgets", str(path)], config=QUICK, environ={})
    main(["--budgets", str(path), "--simulate-regression", "premium=3"], config=QUICK, environ={})

    assert path.read_bytes() == before


def test_budget_diff_reports_workload_and_tolerance_changes() -> None:
    report = _report(a=100.0)
    old = _budgets(report, tolerance=0.10, a=100.0)
    new = dataclasses.replace(
        measured_budgets(dataclasses.replace(report, models={"a": _model(120.0)}), 0.02),
        workload={"changed": True},
    )

    diff = budget_diff(old, new)

    assert "workload: CHANGED" in diff
    assert "tolerance: 10% -> 2%" in diff
    assert "~ a: p99 budget 100.000 ms -> 120.000 ms (+20.0%)" in diff
