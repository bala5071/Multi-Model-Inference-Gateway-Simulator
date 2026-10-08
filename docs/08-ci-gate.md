# 08 — CI performance gate

Step 8 adds a gate that fails the build when a model's p99 total latency goes over its
budget.

Code: `benchmarks/gate.py`, `scripts/perf_gate.py`, `benchmarks/budgets.json`, the
`perf-gate` job in `.github/workflows/ci.yml`. Tests: `tests/test_perf_gate.py`.

**What it guards.** The gate runs on simulated time with the illustrative model profiles.
It catches changes to the gateway's own logic (scheduling, batching, caching, the cost
model, the profiles) that make simulated latency worse. It says nothing about the speed of
real models or hardware. Every number below is simulated.

## Using it

```sh
uv run python scripts/perf_gate.py                      # check; exit 0 or 1
uv run python scripts/perf_gate.py --output out.json    # also save the results
uv run python scripts/perf_gate.py --update-budgets     # re-baseline; prints a diff
uv run python scripts/perf_gate.py --simulate-regression premium=2   # watch it fail
```

| Exit code | Meaning                                                              |
|-----------|----------------------------------------------------------------------|
| 0         | Every model is within its limit (or budgets were updated).           |
| 1         | At least one model is over its limit, or the run is otherwise invalid. |
| 2         | Usage problem: no budgets file, or a refused `--update-budgets`.     |

A passing run:

```
workload: seed=7 requests=1000 rate=5.0/s weights={'economy': 70, 'standard': 20, 'premium': 10}
model      requests     p99 ms  budget ms  limit ms (+2%)  vs budget  status
economy         693     1817.9     1817.9          1854.2      +0.0%  ok
standard        204     3562.3     3562.3          3633.6      +0.0%  ok
premium         103    10881.7    10881.7         11099.4      +0.0%  ok

Performance gate passed.
```

## The standard load and the budgets

The gate always runs `STANDARD_LOAD` from `benchmarks/gate.py`: seed 7, 1000 requests,
Poisson arrivals at 5 req/s, weights 70/20/10, the default prompt-length mix, half the
requests sharing a 200-word system prompt. It runs in-process through the HTTP API on
`SimClock` and takes about two seconds. At this rate all three models are busy but none is
saturated; a saturated model's p99 mostly measures the length of the run.

`benchmarks/budgets.json` holds:

- `p99_total_ms`: one budget per model, the p99 total latency recorded at the last
  re-baseline;
- `tolerance`: a model fails when its p99 is **above `budget * (1 + tolerance)`**;
- `workload`: a copy of the load configuration the budgets were recorded for.

The gate also fails if the stored workload differs from `STANDARD_LOAD`, if a model has no
budget, if a budget names a model the workload does not have, if any request failed, or if a
model received no requests. A model well under its budget passes with a note suggesting a
re-baseline.

### Why the tolerance is 2%, not 10%

The simulator has no noise, so the tolerance is not there to absorb run-to-run variation.
It exists so that small intended changes do not force a re-baseline, and to absorb any
last-bit floating-point difference between platforms. It should be as small as that allows,
because everything below it goes unnoticed.

Doubling one model's `prefill_ms_per_token` moves its p99 on the standard load by:

| Model slowed | p99 before ms | p99 after ms | Change |
|--------------|--------------:|-------------:|-------:|
| economy      |       1,817.9 |      1,909.9 |  +5.1% |
| standard     |       3,562.3 |      3,885.4 |  +9.1% |
| premium      |      10,881.7 |     11,849.2 |  +8.9% |

(From `scripts/perf_gate.py --simulate-regression MODEL=2` at commit `c7e75ac`.)
With a 10% tolerance **none of these would fail the gate**: latency on this workload is dominated by decoding, so even a doubled prefill cost
stays under 10%. At 2% all three fail. A gate is only as sensitive as its tolerance and its
workload allow; a prefill-heavy workload would be needed to catch smaller prefill
regressions.

## Updating budgets

`--update-budgets` records the current p99s as the new budgets. It is deliberately a
separate, explicit action:

- it never happens as a side effect of a check; checking never writes the file;
- it prints what changes, per model, before writing. The lines look like this (an
  illustration of the format, not output from a real re-baseline):

  ```
  ~ premium: p99 budget 10881.739 ms -> 11849.200 ms (+8.9%)
    economy: p99 budget 1817.854 ms (unchanged)
  ```

- it is **refused when `CI` is set**, so a pipeline cannot quietly re-baseline itself;
- it is refused together with `--simulate-regression`;
- the tolerance is kept as it is; change it by editing the file.

The new `budgets.json` is then an ordinary change in the pull request. The reviewer sees the
old and new numbers and approves a slowdown knowingly, or does not.

## Proof that it fails

`test_doubling_a_models_prefill_cost_fails_the_gate_with_a_readable_message` doubles one
model's `prefill_ms_per_token`, for each model in turn, and asserts exit code 1, `FAIL` on
that model's row only, and the message. `test_script_exits_non_zero_when_a_model_is_over_
budget` runs the real script in a subprocess and checks its exit code and its JSON output.

The same thing by hand, with `--simulate-regression premium=2`:

```
DEMO: simulating a regression (premium=2 on prefill cost).
workload: seed=7 requests=1000 rate=5.0/s weights={'economy': 70, 'standard': 20, 'premium': 10}
model      requests     p99 ms  budget ms  limit ms (+2%)  vs budget  status
economy         693     1817.9     1817.9          1854.2      +0.0%  ok
standard        204     3562.3     3562.3          3633.6      +0.0%  ok
premium         103    11849.2    10881.7         11099.4      +8.9%  FAIL

FAIL: premium: p99 total latency 11849.2 ms exceeds its limit of 11099.4 ms (budget 10881.7 ms + 2%)
Performance gate FAILED.
If the change is intended, re-baseline with `uv run python scripts/perf_gate.py --update-budgets` and commit benchmarks/budgets.json.
```

The other two models' numbers do not move at all, which is the per-model isolation from
step 7.

## The CI job

`perf-gate` in `.github/workflows/ci.yml` runs on every push and pull request, on Python
3.12. It runs the gate with `--output perf-gate-results.json`, tees the table to
`perf-gate.txt`, and uploads both as the `perf-gate-results` artifact whether the gate
passes or fails. The JSON contains the verdicts and the full load report.

**Not yet verified:** the workflow has not run on GitHub, because nothing has been pushed.
Locally the gate was run with `CI=true` set and passed. The budgets were recorded on Windows
with Python 3.12; the first CI run on Linux is the real check that the numbers match across
platforms (see below).

## Why a deterministic simulator makes this gate non-flaky

A performance gate is flaky when the same code can give different numbers. Here it cannot:

- **Time is simulated.** `SimClock` advances only when every task is blocked and jumps
  straight to the next wake-up. CPU speed, machine load, noisy neighbours and garbage
  collection pauses change how long the gate takes to run, not what it measures.
- **Ordering is fixed.** Simultaneous wake-ups resume in a defined order, so there are no
  races to win or lose.
- **Inputs are fixed.** The workload is a pure function of the seed; token ids and output
  lengths are hashes of the text; request ids are a counter. Nothing reads the wall clock,
  the process's hash seed or an unseeded random source.

So for a given commit the p99 is one number, not a distribution. A failure therefore always
means the code changed the result, and rerunning cannot make it pass. That removes the two
habits that make noisy gates useless: retrying until green, and widening the threshold until
nothing trips it.

Two limits on that claim:

- **Across platforms it is expected, not proven.** The only floating-point library call in
  the path is `math.log` in the arrival process, and its last bit may differ between
  platforms. Arrival gaps are rounded to a nanosecond to remove that, and the tolerance would
  absorb a stray difference. This is unconfirmed until the gate has run on the CI runner.
- **The p99 is deterministic but still coarse.** The premium model gets 103 of the 1000
  requests, so its nearest-rank p99 is its second-slowest request. A change that reorders
  two requests can move it. That is a real change in behaviour, not flakiness, but it means
  small, harmless-looking scheduling changes can need a re-baseline.

## Adapting the idea to real hardware

On real hardware the same commit gives a different p99 every run, so a single run compared
with a fixed threshold either flakes or has to be set so loose that it catches nothing.
What changes:

- **Reduce the noise first.** Dedicated runners, not shared CI machines; a pinned GPU,
  driver and model build; fixed batch and power settings; warm-up runs discarded; nothing
  else on the box. Record the environment with each result.
- **Repeat runs, and measure the noise.** Run the load several times per commit and learn
  how much the p99 varies with no code change at all. That spread, not a guess, sets what
  size of regression is detectable.
- **Compare against a baseline measured at the same time.** Run the base commit and the
  candidate interleaved on the same machine in the same job (A, B, A, B, ...), so drift in
  the machine affects both. A stored number from last week is not a fair comparison.
- **Use a statistical test, not a threshold on one number.** Compare the two sets of runs
  with a rank test such as Mann-Whitney U, or bootstrap a confidence interval for the
  difference in p99, and fail only when the candidate is worse by more than a chosen margin
  with high confidence. Tail percentiles are noisy, so they need more requests per run and
  more runs than a median does; gating on p50 and p95 as well gives an earlier, steadier
  signal.
- **Decide the error rates.** Pick the smallest regression worth blocking a merge for and
  the false-alarm rate the team will tolerate, and size the number of runs to match. With
  many metrics and models, correct for multiple comparisons or one of them will fail by
  chance.
- **Split the gate.** A cheap check on every pull request that only blocks large, clear
  regressions; a longer nightly run that tracks trends and finds small ones by bisection.
  Automatic retries are acceptable only as a second, independent sample that must also fail,
  never as "run until it passes".
- **Keep this simulator gate as well.** It is cheap, exact, and tests the scheduler in
  isolation. A hardware gate tells you that something got slower; this one tells you, with
  certainty, whether the gateway's own decisions changed.

The budgets file, the explicit update with a reviewed diff, and the readable failure table
carry over unchanged. The budget becomes a baseline distribution and the tolerance becomes
a margin plus a confidence level.

## Design decisions

- **Logic in `benchmarks/gate.py`, a thin `scripts/perf_gate.py`.** The script adds the repo
  root to the import path and calls `main`; tests call `main` and `evaluate` directly.
- **`evaluate(report, budgets)` is pure**, so the pass/fail rules are tested without
  running a load.
- **Budgets record the measured p99, with the headroom in `tolerance`**, not pre-padded
  numbers. The file then shows what was measured, and the diff on update is meaningful.
- **The workload is stored beside the budgets** and checked, so the two cannot drift apart
  silently.
- **`run_in_process` and `build_continuous_registry` take optional `profiles`**, which is
  how the tests and `--simulate-regression` slow one model down.
- **Arrival gaps are rounded to a nanosecond** in `build_workload`. The step 7 tables were
  re-run afterwards and are unchanged.
