"""The performance gate: fail when a model's p99 total latency exceeds its budget.

Run through scripts/perf_gate.py. See docs/08-ci-gate.md.

The gate runs the standard seeded load in-process on simulated time, so the same code
always gives the same numbers. The numbers come from illustrative model profiles; the gate
guards the scheduler's behaviour, not real-model performance.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from benchmarks.loadgen import LoadConfig, LoadReport, run_in_process
from gateway.profiles import PROFILES, ModelProfile, Tier

# The workload every budget refers to. Changing it invalidates the budgets.
STANDARD_LOAD = LoadConfig(seed=7, total_requests=1000, arrival_rate_per_s=5.0)

BUDGETS_PATH = Path(__file__).parent / "budgets.json"
DEFAULT_TOLERANCE = 0.02

EXIT_OK = 0
EXIT_OVER_BUDGET = 1
EXIT_USAGE = 2


def workload_fingerprint(config: LoadConfig) -> dict[str, object]:
    """The config as plain JSON data, for storing beside the budgets and comparing."""
    fingerprint: dict[str, object] = json.loads(json.dumps(dataclasses.asdict(config)))
    return fingerprint


@dataclass(frozen=True, slots=True)
class Budgets:
    # A model fails when its p99 is above budget * (1 + tolerance).
    tolerance: float
    workload: dict[str, object]
    p99_total_ms: dict[str, float]

    @classmethod
    def load(cls, path: Path) -> Budgets:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            tolerance=float(data["tolerance"]),
            workload=dict(data["workload"]),
            p99_total_ms={k: float(v) for k, v in data["p99_total_ms"].items()},
        )

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), indent=2) + "\n"

    def limit_ms(self, model: str) -> float:
        return self.p99_total_ms[model] * (1 + self.tolerance)


@dataclass(frozen=True, slots=True)
class ModelVerdict:
    model: str
    requests: int
    errors: int
    p99_ms: float | None
    budget_ms: float | None
    limit_ms: float | None
    passed: bool
    note: str

    @property
    def change(self) -> float | None:
        """p99 relative to the budget: 0.05 is 5% over, -0.05 is 5% under."""
        if self.p99_ms is None or not self.budget_ms:
            return None
        return self.p99_ms / self.budget_ms - 1


@dataclass(frozen=True, slots=True)
class GateResult:
    tolerance: float
    verdicts: tuple[ModelVerdict, ...]
    # Reasons to fail that are not about one model's latency.
    problems: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.problems and all(v.passed for v in self.verdicts)

    def to_text(self) -> str:
        def number(value: float | None) -> str:
            return "-" if value is None else f"{value:.1f}"

        limit_header = f"limit ms (+{self.tolerance:.0%})"
        lines = [
            f"{'model':10} {'requests':>8} {'p99 ms':>10} {'budget ms':>10} "
            f"{limit_header:>15} {'vs budget':>10}  status"
        ]
        for v in self.verdicts:
            # Adding 0.0 turns a rounded -0.0 into 0.0, so an exact match prints as +0.0%.
            change = "-" if v.change is None else f"{round(v.change, 3) + 0.0:+.1%}"
            lines.append(
                f"{v.model:10} {v.requests:8d} {number(v.p99_ms):>10} {number(v.budget_ms):>10} "
                f"{number(v.limit_ms):>15} {change:>10}  {'ok' if v.passed else 'FAIL'}"
            )
        lines.append("")
        for v in self.verdicts:
            if v.note:
                lines.append(f"{'note' if v.passed else 'FAIL'}: {v.model}: {v.note}")
        lines.extend(f"FAIL: {problem}" for problem in self.problems)
        if self.passed:
            lines.append("Performance gate passed.")
        else:
            lines.append("Performance gate FAILED.")
            lines.append(
                "If the change is intended, re-baseline with "
                "`uv run python scripts/perf_gate.py --update-budgets` "
                "and commit benchmarks/budgets.json."
            )
        return "\n".join(lines)


def evaluate(report: LoadReport, budgets: Budgets) -> GateResult:
    """Compare a load report with the budgets. Pure: runs nothing."""
    problems = []
    if workload_fingerprint(report.config) != budgets.workload:
        problems.append(
            "the workload is not the one the budgets were recorded for; "
            "the budgets must be re-baselined"
        )
    for model in budgets.p99_total_ms:
        if model not in report.models:
            problems.append(f"there is a budget for {model!r} but the workload has no such model")

    verdicts = []
    for model, measured in report.models.items():
        budget_ms = budgets.p99_total_ms.get(model)
        limit_ms = budgets.limit_ms(model) if budget_ms is not None else None
        p99_ms = measured.total_p99_ms
        if budget_ms is None or limit_ms is None:
            passed, note = False, "no budget is recorded for this model"
        elif measured.errors:
            passed, note = False, f"{measured.errors} of {measured.requests} requests failed"
        elif p99_ms is None:
            passed, note = False, "the workload sent this model no requests"
        elif p99_ms > limit_ms:
            passed = False
            note = (
                f"p99 total latency {p99_ms:.1f} ms exceeds its limit of {limit_ms:.1f} ms "
                f"(budget {budget_ms:.1f} ms + {budgets.tolerance:.0%})"
            )
        elif p99_ms < budget_ms * (1 - budgets.tolerance):
            passed = True
            note = (
                f"p99 total latency {p99_ms:.1f} ms is well under its budget of "
                f"{budget_ms:.1f} ms; consider re-baselining to lock the improvement in"
            )
        else:
            passed, note = True, ""
        verdicts.append(
            ModelVerdict(
                model=model,
                requests=measured.requests,
                errors=measured.errors,
                p99_ms=p99_ms,
                budget_ms=budget_ms,
                limit_ms=limit_ms,
                passed=passed,
                note=note,
            )
        )
    return GateResult(budgets.tolerance, tuple(verdicts), tuple(problems))


def measured_budgets(report: LoadReport, tolerance: float) -> Budgets:
    """Budgets that record exactly what ``report`` measured."""
    return Budgets(
        tolerance=tolerance,
        workload=workload_fingerprint(report.config),
        p99_total_ms={
            model: round(m.total_p99_ms, 3)
            for model, m in report.models.items()
            if m.total_p99_ms is not None
        },
    )


def budget_diff(old: Budgets | None, new: Budgets) -> str:
    """Human-readable description of what an update changes."""
    lines = []
    if old is None:
        lines.append("No budgets were recorded before.")
    else:
        if old.workload != new.workload:
            lines.append("workload: CHANGED (budgets below are for the new workload)")
        if old.tolerance != new.tolerance:
            lines.append(f"tolerance: {old.tolerance:.0%} -> {new.tolerance:.0%}")
    old_budgets = old.p99_total_ms if old is not None else {}
    for model in dict.fromkeys([*old_budgets, *new.p99_total_ms]):
        before = old_budgets.get(model)
        after = new.p99_total_ms.get(model)
        if before is None:
            lines.append(f"+ {model}: p99 budget {after:.3f} ms (new)")
        elif after is None:
            lines.append(f"- {model}: p99 budget {before:.3f} ms (removed)")
        elif before == after:
            lines.append(f"  {model}: p99 budget {before:.3f} ms (unchanged)")
        else:
            lines.append(
                f"~ {model}: p99 budget {before:.3f} ms -> {after:.3f} ms "
                f"({after / before - 1:+.1%})"
            )
    return "\n".join(lines)


def _slowed_profiles(spec: str) -> Mapping[Tier, ModelProfile]:
    model, _, factor = spec.partition("=")
    tier = Tier(model)
    profiles = dict(PROFILES)
    profiles[tier] = dataclasses.replace(
        PROFILES[tier],
        prefill_ms_per_token=PROFILES[tier].prefill_ms_per_token * float(factor),
    )
    return profiles


def main(
    argv: Sequence[str] | None = None,
    *,
    config: LoadConfig = STANDARD_LOAD,
    environ: Mapping[str, str] = os.environ,
) -> int:
    parser = argparse.ArgumentParser(
        prog="perf_gate.py",
        description="Fail if any model's p99 total latency exceeds its budget.",
    )
    parser.add_argument("--budgets", type=Path, default=BUDGETS_PATH)
    parser.add_argument("--output", type=Path, help="write the results as JSON to this file")
    parser.add_argument(
        "--update-budgets",
        action="store_true",
        help="record the current p99s as the new budgets instead of checking them",
    )
    parser.add_argument(
        "--simulate-regression",
        metavar="MODEL=FACTOR",
        help="demo: multiply one model's prefill_ms_per_token, e.g. premium=2",
    )
    args = parser.parse_args(argv)

    if args.update_budgets and args.simulate_regression:
        print("Refusing to record budgets from a simulated regression.")
        return EXIT_USAGE
    if args.update_budgets and environ.get("CI"):
        print(
            "Refusing to update budgets in CI. Run --update-budgets locally, "
            "review the diff and commit benchmarks/budgets.json."
        )
        return EXIT_USAGE

    profiles = _slowed_profiles(args.simulate_regression) if args.simulate_regression else PROFILES
    if args.simulate_regression:
        print(f"DEMO: simulating a regression ({args.simulate_regression} on prefill cost).")
    print("Simulated time, illustrative model profiles: not measurements of real models.")
    print(
        f"workload: seed={config.seed} requests={config.total_requests} "
        f"rate={config.arrival_rate_per_s}/s weights={dict(config.model_weights)}"
    )
    report = asyncio.run(run_in_process(config, profiles=profiles))

    old = Budgets.load(args.budgets) if args.budgets.exists() else None
    if args.update_budgets:
        new = measured_budgets(report, old.tolerance if old is not None else DEFAULT_TOLERANCE)
        print(budget_diff(old, new))
        if old == new:
            print(f"Budgets are already up to date; {args.budgets} not changed.")
        else:
            args.budgets.write_text(new.to_json(), encoding="utf-8", newline="\n")
            print(f"Wrote {args.budgets}. Review this diff and commit the file.")
        return EXIT_OK

    if old is None:
        print(f"No budgets file at {args.budgets}. Create it with --update-budgets.")
        return EXIT_USAGE
    result = evaluate(report, old)
    print(result.to_text())
    if args.output is not None:
        payload = {
            "passed": result.passed,
            "gate": dataclasses.asdict(result),
            "report": json.loads(report.to_json()),
        }
        args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")
    return EXIT_OK if result.passed else EXIT_OVER_BUDGET
