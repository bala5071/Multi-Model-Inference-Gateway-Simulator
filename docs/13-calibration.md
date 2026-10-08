# 13 — Calibrating the simulator against real models

`benchmarks/calibrate.py` measures real models and fits the simulator's four timing
parameters to them, then checks the simulator against what it measured.

Code: `benchmarks/calibrate.py`, `load_calibrated_profiles` in `src/gateway/profiles.py`,
`simulated.profiles` in `src/gateway/backend_config.py`. Tests: `tests/test_calibrate.py`.

## Status: the median of six runs on one laptop, most of it unstable

`benchmarks/calibration/calibrated_profiles.json` is the **median of six calibration runs**
made on 2026-10-08, with the lowest and highest value of every parameter beside it. No run
was picked: `--aggregate` takes every run it is given. The runs themselves are in
`run1/` to `run6/` (`calibrated_profiles.json` and `samples.json` each); the three plots
beside the aggregate show the median profile against all six runs' requests.

**Seven of the twelve fitted parameters are marked unstable**: their highest and lowest
fitted values are more than 25% of the median apart. **The premium tier is marked
hardware-limited**: the 8B model does not fit on this GPU. Read every calibrated figure in
this repository with that in mind.

The first calibration, a single run on 2026-10-07 with three repeats and no overhead term,
is in the history at commit `e36ce3b`.

**The simulator does not use the calibration by default.** The illustrative profiles are
still what `profiles.py`, the performance gate and the main tables of `docs/REPORT.md` run
on. Loading the calibration is opt-in (see "Using a calibration in the simulator" below).
Sections 1 and 2 of the report show a calibrated variant beside the illustrative results.

### What was measured

| | |
|---|---|
| When | 2026-10-08. Runs 1 to 3 started at 18:03, 18:07 and 18:12 UTC; Ollama was then stopped and started again; runs 4 to 6 started at 18:36, 18:40 and 18:44. Each took 3.8 to 4.6 minutes |
| Machine | Intel Core Ultra 7 155H (22 logical cores), 15.4 GB RAM, NVIDIA GeForce RTX 4060 Laptop GPU (8188 MiB), Windows 11 (10.0.26200), Python 3.12.10 |
| Server | **Ollama 0.12.6**, started with `OLLAMA_NUM_PARALLEL=4`, default 4096-token context. Not llama-server. |
| Models | `llama3.2:1b-instruct-q4_K_M`, `llama3.2:3b`, `llama3.1:8b`; all report Q4_K_M |
| Plan | defaults except repeats: prompts of 32, 128, 512 and 1536 words; 16, 64 and 128 output tokens; 1, 2 and 4 requests together; **5 repeats**, the last held out |
| Requests | 70 per tier per run, 420 per tier in all; none failed |
| Measure | `uv run python -m benchmarks.calibrate --backends examples/backends.ollama.yaml --out benchmarks/calibration/runN --repeats 5`, for N = 1 to 6 |
| Combine | `uv run python -m benchmarks.calibrate --aggregate benchmarks/calibration/run1 benchmarks/calibration/run2 benchmarks/calibration/run3 benchmarks/calibration/run4 benchmarks/calibration/run5 benchmarks/calibration/run6 --out benchmarks/calibration` |

Other applications were not closed during the runs. The tool had not been committed when
they were made: runs 1 to 3 used it as it was before it recorded where a model was
loaded, runs 4 to 6 as it is in the commit that added this section. The fitting code was
the same for all six.

### Where the models ran

Recorded by the tool from Ollama's process list straight after each tier was measured, in
runs 4 to 6. Runs 1 to 3 were made before the tool recorded it.

| Tier | Loaded size | On the GPU | On the CPU | Verdict |
|----------|--------:|-----:|---:|---|
| economy  | 2.8 GiB | 100% | 0% | fits on the GPU |
| standard | 5.0 GiB | 100% | 0% | fits on the GPU |
| premium  | 8.1 GiB |  95% | 5% | **hardware-limited** |

The premium figures are those of an 8B model that does not fit an 8 GB GPU, not of the model
on adequate hardware. The split was the same 95% / 5% in all three runs that recorded it.

### Fitted parameters

The median of the six runs, with the range over them. "Range" is highest minus lowest as a
percentage of the median; above 25% the parameter is marked unstable.

| Tier | Parameter | Median | Lowest | Highest | Range | |
|----------|---------------------------|-------:|-------:|-------:|-------:|---|
| economy  | prefill ms/token          | 0.1960 | 0.1821 | 0.2141 |  16.3% | |
| economy  | decode base ms            |  6.711 |  6.481 |  6.741 |   3.9% | |
| economy  | ms per extra sequence     |  2.676 |  2.092 |  3.686 |  59.6% | **unstable** |
| economy  | per-request overhead ms   |   90.1 |   75.4 |  113.7 |  42.5% | **unstable** |
| standard | prefill ms/token          | 0.3754 | 0.3521 | 0.4221 |  18.6% | |
| standard | decode base ms            | 15.298 | 14.747 | 22.171 |  48.5% | **unstable** |
| standard | ms per extra sequence     |  4.133 |  1.705 |  4.905 |  77.4% | **unstable** |
| standard | per-request overhead ms   |   88.4 |   65.5 |  126.1 |  68.6% | **unstable** |
| premium  | prefill ms/token          | 0.8623 | 0.7637 | 0.9577 |  22.5% | |
| premium  | decode base ms            | 45.138 | 42.767 | 48.328 |  12.3% | |
| premium  | ms per extra sequence     | 24.760 |  7.744 | 42.860 | 141.8% | **unstable** |
| premium  | per-request overhead ms   |  161.9 |   42.0 |  236.3 | 120.0% | **unstable** |

Every run, in order (`uv run python -m benchmarks.calibrate --compare` over the six files):

| Tier | Parameter | Run 1 | Run 2 | Run 3 | Run 4 | Run 5 | Run 6 |
|----------|-------------------------|------:|------:|------:|------:|------:|------:|
| economy  | ms per extra sequence   |  2.09 |  2.26 |  3.69 |  3.08 |  2.29 |  3.06 |
| economy  | per-request overhead ms |  95.4 |  85.9 |  94.3 |  75.4 |  82.9 | 113.7 |
| standard | decode base ms          | 15.30 | 14.86 | 14.75 | 15.29 | 16.01 | 22.17 |
| standard | ms per extra sequence   |  4.12 |  3.66 |  4.14 |  4.43 |  4.91 |  1.71 |
| standard | per-request overhead ms |  91.9 |  85.0 |  95.6 |  65.5 |  70.7 | 126.1 |
| premium  | ms per extra sequence   | 33.54 | 37.78 | 42.86 | 11.66 | 15.98 |  7.74 |
| premium  | per-request overhead ms | 216.4 | 230.3 | 236.3 |  59.8 |  42.0 | 107.4 |

- **What repeats:** prefill cost on every tier (16% to 23%), and single-request decode speed
  on economy and premium (4% and 12%).
- **The cost of batching does not repeat on any tier.** It is the parameter the batching
  results depend on most.
- **The premium tier changed between the two sessions.** Before Ollama was restarted it
  fitted 33.5 to 42.9 ms per extra sequence and 216 to 236 ms of overhead; after, 7.7 to
  16.0 ms and 42 to 107 ms. The command and the reported GPU share were the same. The cause
  is not known. The median of two such groups (24.8 and 161.9) describes neither.
- **Run 6 of the standard tier is a bad fit and was kept.** Its decode regression has an R²
  of 0.043: per-token latency barely tracked concurrency in that run, and the fit put the
  cost into the base step (22.2 ms) instead of the slope (1.7 ms). It is one of six values
  going into each median, and it is why two of standard's parameters are unstable.
- Against the illustrative profiles the medians say: prefill is 1.4 to 3.9 times dearer;
  each extra sequence adds 40%, 27% and 55% of the base step, not 3% to 8%; and every
  request pays a fixed overhead the illustrative profiles do not have. Concurrency was
  measured up to 4 only; the illustrative batch limits of 32, 16 and 8 were not tested.

### How well the calibrated simulator matches

The simulator on the median profile against all six runs' requests (MAPE), with the worst
R² any run's regressions had:

| Tier | TTFT: fitted / held out | Total latency: fitted / held out | Lowest R², TTFT fit | Lowest R², decode fit |
|----------|----------------:|----------------:|------:|------:|
| economy  | 12.0% / 13.5%   | 8.0% / 8.1%     | 0.960 | 0.631 |
| standard | 16.3% / 15.5%   | 7.5% / 8.0%     | 0.948 | 0.043 |
| premium  | 35.7% / 35.1%   | 19.8% / 18.7%   | 0.914 | 0.684 |

Over all requests, split by how many were sent together:

| Tier | TTFT: alone | TTFT: 2 together | TTFT: 4 together | Total: alone | Total: 2 together | Total: 4 together |
|----------|------:|------:|------:|------:|------:|------:|
| economy  |  9.3% | 14.3% | 17.4% |  7.0% | 10.9% |  8.6% |
| standard | 10.4% | 21.5% | 24.9% |  8.1% | 10.4% |  5.3% |
| premium  | 27.6% | 40.6% | 49.0% | 11.7% | 18.4% | 35.9% |

- **The overhead term fixed most of the TTFT error on the two small models.** Their
  held-out TTFT error was 54.1% and 42.2% in the 2026-10-07 calibration, which had no such
  term, and is 13.5% and 15.5% here. Those are different measurements, so this compares
  calibrations; it is not a like-for-like test of the term on the same requests.
- **It did not fix the premium tier**, which is 35% off on TTFT (31.4% held out before) and
  19% off on total latency. A median taken across two sessions that disagree fits neither.
- **TTFT with concurrency is still poor on every tier**: 17% to 49% with four together,
  against 9% to 28% alone. The simulator prefills requests that arrive together one after
  another, and a real server does not; a constant overhead cannot represent that.
- **Total latency is within 7% to 11% on the two small models.**

The plots are `benchmarks/calibration/economy.png`, `standard.png` and `premium.png`, and
are included in section 6 of `docs/REPORT.md`.

### What the calibrated profiles do to the simulated results

Sections 1 and 2 of `docs/REPORT.md` run the same simulations on the calibrated profiles,
beside the illustrative ones (`uv run python -m benchmarks.report`). Simulated in both cases.

| Tier | Throughput gain of continuous batching at 16x load: illustrative | Calibrated, batches up to the illustrative limit | Calibrated, batches of at most 4 (the measured range) |
|----------|------:|-----:|-----:|
| economy  | 12.3x | 2.3x | 1.8x |
| standard |  7.7x | 2.9x | 2.1x |
| premium  |  4.5x | 1.6x | 1.5x |

- **Within the measured range the batching gain is 1.5x to 2.1x, not 4.5x to 12.3x.** The
  middle column extrapolates the fitted cost per extra sequence to batches that were never
  measured.
- **These gains rest on the least stable parameter.** The cost per extra sequence is
  unstable on all three tiers, so the gains would differ run by run; they were computed
  from the median only.
- The prefix cache still holds median TTFT at its no-sharing level, but on the calibrated
  standard profile that level is 142 ms, most of it the 88 ms overhead, against 24 ms on
  the illustrative one.
- **Sections 3, 4 and 5 of the report were not run on calibrated profiles** and say so.
  Section 4's surge of 3 req/s is about 12 times what the calibrated premium profile can
  decode with every batch full, so that workload is saturated and a calibrated run would
  only measure its backlog.

### What this calibration is not

- Not llama-server. Ollama wraps llama.cpp but schedules and loads models its own way.
- Not independent repeats. Six runs in two sessions on one machine on one day.
- Not a measurement of the 8B model on hardware that fits it.
- Not transferable to another machine, GPU, quantization, context size or server version.
- Not a measurement of this repo's scheduler: Ollama did the batching (`docs/12`).

The four `real_models` tests were run against these three models through Ollama on
2026-10-07 and passed; they were not run again on 2026-10-08.

## What the tool does

### 1. Measure

For each tier it sends requests through the same `LlamaCppBackend` and `PassthroughWorker`
the gateway uses, timed with `WallClock`. One thing is varied at a time:

| Sweep       | Varies                | Default values       | Held fixed                         |
|-------------|-----------------------|----------------------|------------------------------------|
| prompt      | prompt length (words) | 32, 128, 512, 1536   | 8 output tokens, one at a time     |
| output      | output tokens         | 16, 64, 128          | 32-word prompt, one at a time      |
| concurrency | requests sent together | 1, 2, 4             | 32-word prompt, 64 output tokens   |

Each scenario runs `--repeats` times (default 3). For every request it records the prompt
and output token counts the server reported, time to first token, total latency, and
per-token latency (the mean gap between streamed pieces).

- **Every prompt is unique from its first word**, tagged with the run, tier, scenario and
  slot, so the server cannot serve any of it from a prompt cache.
- **One request per tier is sent first and thrown away**, to load the model.
- **A failed request is recorded in the notes** and left out of the fit.

### 2. Fit

Two ordinary least-squares fits per tier, over the samples that are not held out:

| Parameter                 | Fitted as                                                                   |
|---------------------------|-----------------------------------------------------------------------------|
| `prefill_ms_per_token`    | slope of TTFT against prompt tokens, requests sent alone                    |
| `decode_base_ms`          | intercept of per-token latency against the number of *other* requests       |
| `decode_ms_per_extra_seq` | slope of the same fit                                                       |
| `per_request_overhead_ms` | intercept of the TTFT fit, less the fitted `decode_base_ms`                 |

The overhead is the intercept less one decode step because the simulator's first token
already comes one step after prefill; only the rest of the intercept is unexplained. It
was added after the first calibration showed an intercept of 92 to 139 ms that the
simulator could not represent (`docs/01-cost-model.md` says how the simulator applies it).

Everything else in the profile (prices, `max_batch_size`, `kv_capacity_tokens`) is copied
from the illustrative profile; calibration does not measure it. A fit that comes out at or
below zero is clamped and flagged in the notes rather than passed off as a result.

### 3. Check

The same requests are replayed through the real `ContinuousBatchingWorker` on simulated
time, with the fitted profile and the token counts the model reported; requests that were
sent together are submitted together. The tool reports the **mean absolute percentage error
(MAPE)** of simulated against measured TTFT and total latency, separately for the samples
used in the fit and for the held-out ones (the last repeat of each scenario), and draws one
figure per tier: simulated against measured for TTFT and for total latency, with the
residuals of each below. Marker shape shows how many requests were sent together.

It also reports the same errors over all samples split by how many requests were sent
together (`mape_percent_by_concurrency`), since an average over mostly single requests
hides how the simulator does under concurrency.

### 4. Report the spread

- **Within a run:** every repeat is fitted on its own and the lowest, mean and highest
  value of each parameter are reported (`spread_across_repeats`), with the range as a
  percentage of the mean. The held-out repeat is included; nothing is checked against it
  here.
- **Between runs:** `uv run python -m benchmarks.calibrate --compare FILE FILE ...` prints
  the same for the fitted parameters of several `calibrated_profiles.json` files. It
  measures nothing.

### 5. Combine runs

`uv run python -m benchmarks.calibrate --aggregate DIR DIR ... --out OUT` reads the
`calibrated_profiles.json` and `samples.json` of every run folder named and writes one
calibration to `OUT`. It measures nothing.

- **Each parameter is the median over the runs**, stored with its lowest and highest
  value and every run's value (`aggregate` in the JSON). With an even number of runs the
  median is the mean of the middle two.
- **No run is chosen.** Every folder given goes in, including a run whose fit is poor.
  Dropping one after looking at it would be choosing the answer. The tool refuses to
  write the result over one of its inputs or to aggregate an aggregate.
- **A parameter is marked unstable** when highest minus lowest is more than 25% of the
  median (`unstable: true`, `unstable_parameters`, and a line in the tier's notes). The
  median is still written; the flag says it did not repeat.
- **The errors are recomputed for the median profile**: the simulator is run on it
  against every run's requests, and the plots show that. Each run's own R² is kept in
  `fit_r2_by_run`.
- A tier counts only if every run measured it. `samples.json` is not written: the
  samples stay with their runs.

### 6. Record the hardware and where the model ran

`calibrated_profiles.json` carries, beside the parameters, fit statistics and errors:

- `hardware`: CPU, logical cores, RAM, GPU (from `nvidia-smi`), OS, Python;
- `servers`: for each tier, whether it is llama-server or Ollama, its version or build, the
  model, and the quantization, asked from the server itself (`unknown` where it will not
  say);
- `measured_at`, the plan that was run, and free-text `--notes`.

The numbers mean nothing without that context, which is why it is in the same file.

**Where the model was loaded.** Straight after measuring a tier, while its model is still
loaded, the tool asks Ollama's process list (`/api/ps`, what `ollama ps` prints) for the
model's size and how much of it is in VRAM, and stores both with the GPU and CPU shares
under `servers.<tier>.placement`.

- A tier with any of its model on the CPU gets `hardware_limited: true` and a
  `hardware-limited` line in its notes. Its timings describe that model on hardware too
  small for it. The fit is not changed or dropped; it is labelled.
- If the server is not Ollama or does not list the model, the placement is `null` and
  `hardware_limited` is `null`: unknown, not assumed to be fine. llama-server is in that
  case today.
- An aggregate is hardware-limited if any run that recorded a placement was, and keeps
  every run's placement in `placement_by_run`.

## Using a calibration in the simulator

```sh
GATEWAY_PROFILES=benchmarks/calibration/calibrated_profiles.json \
  uv run uvicorn gateway.app:create_default_app --factory --port 8000
```

or in the backends file:

```yaml
backend: simulated
simulated:
  profiles: benchmarks/calibration/calibrated_profiles.json
```

`load_calibrated_profiles(path)` returns the standard profiles with the timing parameters
replaced for every tier in the file; a tier that is not in the file keeps its
illustrative profile. A file written before the overhead was fitted has no
`per_request_overhead_ms` and loads with 0. An aggregate loads the same way: the
medians are in the same fields. The loader does not act on the `unstable` or
`hardware_limited` flags; they are for the reader and the report. In code, pass the result as `profiles=` to
`build_continuous_registry` or `run_in_process`.

The performance gate does not use calibrated profiles. Its budgets were recorded with the
illustrative ones; switching would be a deliberate re-baseline.

## Limitations, and where the simulator diverges

Some of these are structural and some were seen in the tool check below.

**The simulator's TTFT model is too simple.**

- It prefills requests admitted together one after another. A real server processes
  several prompts in the same batches, so with concurrency the simulator predicts first
  tokens in the wrong pattern.
- Its per-request overhead is a constant that delays one request and does not hold up the
  model. Whether the real overhead holds up the server was not measured, and it need not
  be constant: the within-run spread of the fitted value was 9% to 22%.
- TTFT error was 28% to 49% in the check; total latency, which decoding dominates, was much
  closer. **Expect TTFT to be the weak spot.**

**Concurrency is the fragile part of the measurement.**

- If the server has fewer free slots than the requests sent together, it serves them one
  after another. Per-token latency then does not change and the extra requests just start
  late. The tool detects this (a first token that took about as long as a whole request
  does alone) and says so in the notes; `decode_ms_per_extra_seq` from such a run is
  meaningless. Give llama-server `--parallel` at least the highest concurrency (the script
  uses 4), or set `OLLAMA_NUM_PARALLEL`.
- Step time is modelled as linear in batch size. In the check the fit explained only half
  to three quarters of the variance.
- Only the measured range is known. The illustrative profiles allow batches of 32, 16 and
  8; a calibration that measured up to 4 says nothing about 32. `max_concurrency_measured`
  is in the output for this reason.

**It is one machine on one day.**

- Parameters fitted on a laptop GPU do not transfer to another GPU, a CPU, another
  quantization, another context size or another server version.
- Two runs minutes apart in the check gave parameters 20% to 50% apart. Thermal state,
  other processes and the server's own scheduling all move the numbers. Run it more than
  once before trusting a value, and raise `--repeats`.
- The sample is small: 24 to 36 requests per tier with the options used in the check.

**The held-out error is optimistic.** The held-out samples are a further repeat of the same
scenarios, not new prompt lengths or concurrency levels. It shows the fit is not chasing
noise; it does not show the model generalises.

**Token counts are the server's.** The prompt tokens used as the x-axis include whatever
template the server wraps around the prompt (a 32-word prompt was reported as 156 tokens in
the check), and per-token latency is measured between streamed pieces, which are usually but
not always single tokens.

**Not calibrated at all:** prices, batch limits, KV capacity, the prefix cache's hit
behaviour, chunked prefill, and queueing inside the model server. A calibrated simulator is
still this repo's scheduler with realistic step costs, not a model of llama-server's
scheduler (`docs/12-real-models.md`).

## Tool check on a stand-in model

These are real measurements, reported so that it is clear what the tool was seen to do.
**They are for a different model than this project's tiers and must not be used as its
calibration.** They were taken before the
Llama models were available, while the tool was being built.

| | |
|---|---|
| Machine | Intel Core Ultra 7 155H (22 logical cores), 15.4 GB RAM, NVIDIA GeForce RTX 4060 Laptop GPU (8188 MiB), Windows 11 (10.0.26200), Python 3.12.10 |
| Server  | Ollama 0.12.6 (not llama-server) |
| Model   | `deepseek-coder:latest`, reported as 1B parameters, Q4_0, standing in for the economy tier |
| Date    | 2026-10-07 |
| Options | `--tiers economy --prompt-words 32,128,512 --output-tokens 16,64 --concurrency 1,2,4` |

| Run | Server setting | Repeats | Requests | prefill ms/token | decode base ms | ms per extra seq | TTFT MAPE fit / held out | Total MAPE fit / held out |
|-----|----------------|--------:|---------:|-----------------:|---------------:|-----------------:|-------------------------:|--------------------------:|
| A | Ollama defaults | 2 | 24 | 0.2157 | 7.023 | 0 (fitted -0.346, clamped) | 49.4% / 48.5% | 23.5% / 23.5% |
| B | `OLLAMA_NUM_PARALLEL=4` | 3 | 36 | 0.2321 | 7.576 | 1.838 | 36.0% / 36.0% | 9.7% / 5.3% |
| C | `OLLAMA_NUM_PARALLEL=4` | 3 | 36 | 0.1911 | 5.941 | 2.753 | 37.9% / 28.1% | 14.7% / 8.8% |

What the three runs showed:

- **Run A:** with default settings the server did not batch. Four requests sent together got
  their first tokens after about 50, 450, 850 and 1,250 ms, each waiting for the one before,
  and per-token latency stayed near 6 ms at every concurrency. The slope fitted slightly
  negative and was clamped to 0. This run is why the tool now warns about queueing.
- **Run B:** with four parallel slots, per-token latency rose from about 7.5 ms alone to 12
  to 14 ms with four together, so batching was really being measured. R² was 0.99 for the
  prefill fit and 0.71 for the decode fit.
- **Run C:** the same command a few minutes later gave noticeably different parameters, and
  one request still waited 1.3 s for its first token; the tool flagged it.
- The TTFT fit was good in all three (R² 0.99) and its intercept was 6.6 to 9.2 ms.

Run B's files were overwritten by run C, so run B's figures here are from its printed
summary. None of the stand-in runs' files are in the repository.

## Design decisions

- **The tool measures through the gateway's own backend and worker**, so what is timed is
  what a client of the gateway would see, minus the HTTP layer.
- **Simulated values come from running the simulator**, not from evaluating the cost
  formula, so scheduler behaviour such as sequential prefill is part of the comparison.
- **Regression is ordinary least squares, written out**, with no numerical dependency.
  matplotlib is a development dependency, used only for the plots.
- **Measuring and writing are separate** (`calibrate` and `write_outputs`), so the tests can
  check the fit without touching the disk.
- **The tool reads the real date and uses `WallClock`**; it is the one file outside
  `clock.py` exempt from the rule against reading real time, because measuring real servers
  is its purpose.
- **A bug in `SimClock` was found and fixed here.** A clock whose remaining sleepers had all
  been cancelled kept yielding, which stopped a second `SimClock` on the same loop from ever
  seeing it idle. The calibration tool was the first code to use one clock after another on
  a single loop. Covered by a test in `test_clock.py`.
