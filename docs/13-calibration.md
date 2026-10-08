# 13 — Calibrating the simulator against real models

`benchmarks/calibrate.py` measures real models and fits the simulator's three timing
parameters to them, then checks the simulator against what it measured.

Code: `benchmarks/calibrate.py`, `load_calibrated_profiles` in `src/gateway/profiles.py`,
`simulated.profiles` in `src/gateway/backend_config.py`. Tests: `tests/test_calibrate.py`.

## Status: calibrated once, on one laptop

`benchmarks/calibration/` holds one calibration of the three Llama models:
`calibrated_profiles.json`, `samples.json` (every request measured) and a plot per tier.

**The simulator does not use it by default.** The illustrative profiles are still what
`profiles.py`, the performance gate and sections 1 to 5 of `docs/REPORT.md` run on. Loading
the calibration is opt-in (see "Using a calibration in the simulator" below).

### What was measured

| | |
|---|---|
| When | 2026-10-07 (UTC timestamp in the JSON) |
| Machine | Intel Core Ultra 7 155H (22 logical cores), 15.4 GB RAM, NVIDIA GeForce RTX 4060 Laptop GPU (8188 MiB), Windows 11 (10.0.26200), Python 3.12.10 |
| Server | **Ollama 0.12.6**, started with `OLLAMA_NUM_PARALLEL=4`, default 4096-token context. Not llama-server. |
| Models | `llama3.2:1b-instruct-q4_K_M` (1.2B), `llama3.2:3b` (3.2B), `llama3.1:8b` (8.0B); all report Q4_K_M |
| Plan | defaults: prompts of 32, 128, 512 and 1536 words; 16, 64 and 128 output tokens; 1, 2 and 4 requests together; 3 repeats |
| Requests | 42 per tier, none failed; one run, about 3 minutes in all |
| Command | `uv run python -m benchmarks.calibrate --backends examples/backends.ollama.yaml --out benchmarks/calibration` |

Other applications were not closed during the run. The 8B model did not fit entirely on the
8 GB GPU: right after the run Ollama reported 7.4 of 8.65 GB in VRAM, so **the premium tier
ran partly on the CPU**. Ollama's default `llama3.2:1b` tag is a Q8 build; the Q4_K_M tag
was used instead to match the other two.

### Fitted parameters

| Tier | Prefill ms/token | Decode base ms | ms per extra sequence | R² prefill fit | R² decode fit |
|----------|-------:|-------:|-------:|------:|------:|
| economy  | 0.2099 |  6.666 |  1.915 | 0.997 | 0.933 |
| standard | 0.3915 | 14.194 |  4.086 | 0.999 | 0.982 |
| premium  | 1.0812 | 49.992 | 15.990 | 0.995 | 0.975 |

Against the illustrative profiles from step 1:

| Tier | Prefill: illustrative / measured | Decode base: illustrative / measured | Per extra sequence: illustrative / measured |
|----------|--------------:|-------------:|--------------:|
| economy  | 0.05 / 0.21   | 6 / 6.67     | 0.2 / 1.92    |
| standard | 0.15 / 0.39   | 12 / 14.19   | 0.6 / 4.09    |
| premium  | 0.60 / 1.08   | 30 / 49.99   | 2.5 / 15.99   |

- **Single-request decode speed was guessed fairly well** for the two small models (6 vs
  6.7 ms, 12 vs 14.2 ms) and too optimistically for the 8B, which here was partly on the CPU.
- **The cost of batching was guessed far too low.** Each extra sequence added about 29% to
  32% of the base step time (1.9 of 6.7 ms, 4.1 of 14.2, 16 of 50), against the 3% to 8% the
  illustrative profiles assume. On this server and hardware, batching buys much less than
  sections 1 to 5 of the report suggest: by these fits, four requests together take about
  1.9 times as long per token as one alone, so throughput rises about 2.1 times, not 4.
- **Prefill was guessed 2 to 4 times too cheap.**
- This is concurrency up to 4 only. The illustrative batch limits of 32, 16 and 8 were not
  tested, and the 8B model would not fit more slots on this GPU anyway.

### How well the calibrated simulator matches

MAPE of the simulator, run with the fitted parameters, against the measurements:

| Tier | TTFT: fitted / held out | Total latency: fitted / held out |
|----------|----------------:|----------------:|
| economy  | 56.9% / 54.1%   | 19.8% / 18.8%   |
| standard | 42.6% / 42.2%   | 10.7% / 10.5%   |
| premium  | 35.6% / 31.4%   | 6.1% / 5.1%     |

- **Total latency is predicted to within about 5% to 20%**, best on the slowest model, where
  decoding dominates.
- **TTFT is predicted badly, 31% to 57% off.** The fitted TTFT line has an intercept of
  92 ms (economy and standard) and 139 ms (premium): a fixed cost per request, whatever the
  prompt length. The simulator has no such term; its TTFT is prefill plus one decode step,
  so it predicts first tokens far too early for short prompts. For the fast models that
  fixed cost is most of the TTFT, which is why their error is largest. This is the clearest
  thing the simulator gets wrong, and adding a per-request overhead to the cost model would
  be the first fix.
- The tool raised no warnings: no request failed and none looked queued.

The plots are `benchmarks/calibration/economy.png`, `standard.png` and `premium.png`, and
are included in section 6 of `docs/REPORT.md`.

### What this calibration is not

- Not llama-server. Ollama wraps llama.cpp but schedules and loads models its own way.
- Not repeated. It is one run; two runs of the tool on a stand-in model earlier differed by
  20% to 50% in the fitted values (see the end of this note).
- Not transferable to another machine, GPU, quantization, context size or server version.
- Not a measurement of this repo's scheduler: Ollama did the batching (`docs/12`).

The four `real_models` tests were also run against these three models through Ollama and
passed.

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

### 4. Record the hardware

`calibrated_profiles.json` carries, beside the parameters, fit statistics and errors:

- `hardware`: CPU, logical cores, RAM, GPU (from `nvidia-smi`), OS, Python;
- `servers`: for each tier, whether it is llama-server or Ollama, its version or build, the
  model, and the quantization, asked from the server itself (`unknown` where it will not
  say);
- `measured_at`, the plan that was run, and free-text `--notes`.

The numbers mean nothing without that context, which is why it is in the same file.

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

`load_calibrated_profiles(path)` returns the standard profiles with the three timing
parameters replaced for every tier in the file; a tier that is not in the file keeps its
illustrative profile. In code, pass the result as `profiles=` to
`build_continuous_registry` or `run_in_process`.

The performance gate does not use calibrated profiles. Its budgets were recorded with the
illustrative ones; switching would be a deliberate re-baseline.

## Limitations, and where the simulator diverges

Some of these are structural and some were seen in the tool check below.

**The simulator's TTFT model is too simple.**

- It prefills requests admitted together one after another. A real server processes
  several prompts in the same batches, so with concurrency the simulator predicts first
  tokens in the wrong pattern.
- It has no fixed per-request overhead. The fitted TTFT line had an intercept of 7 to 9 ms
  in the check, which the simulator can only represent as one decode step.
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
