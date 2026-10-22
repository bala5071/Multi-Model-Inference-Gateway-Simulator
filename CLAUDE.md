# CLAUDE.md

## Project story

A company offers its clients several model options, priced by plan. Clients on cheaper plans
are served by cheaper, smaller models. This repo is the gateway that sits in front of those
models.

| Tier     | Model         |
|----------|---------------|
| economy  | Llama 3.2 1B  |
| standard | Llama 3.2 3B  |
| premium  | Llama 3.1 8B  |

The gateway:

- routes each request to the model for the client's plan;
- does continuous batching, one batcher per model;
- reuses shared prompt prefixes through a KV-cache **stand-in** (it models the cost saving of
  prefix reuse; it is not a real KV cache);
- reports p50/p99 latency;
- has a CI gate that fails the build on p99 regressions.

The models are simulated first. A real backend can be added later behind the same interface.

Stack: Python 3.11+, FastAPI, asyncio, pytest, ruff, mypy, uv. Free/open-source tools only.

## Architecture

Planned modules under `src/gateway/` (added step by step; most do not exist yet):

- `profiles.py` — `Tier`, `ModelProfile`, the three profiles (exists): timing is the
  six-run median of the tracked calibration (`UNSTABLE_PARAMETERS`, `HARDWARE_LIMITED`),
  batches capped at `MEASURED_MAX_BATCH_SIZE` (4); prices and KV capacities are
  `PLACEHOLDER_*` values, operator-supplied, not data. `load_calibrated_profiles` loads
  another calibration file
- `cost_model.py` — `estimate_cost`, `OutputLengthGenerator` (exists)
- `clock.py` — `Clock` protocol, `SimClock`, `WallClock`, and `PacedSimClock` (simulated
  time let through at a multiple of real time, for simulated live mode); times are
  milliseconds. Also `EpochSource`, `SystemEpoch` and `ClockEpoch`: calendar time for
  dating stored rows, injected separately from `Clock` (exists)
- `schema.py` — `GenerateRequest`, `RequestResult` (exists)
- `backends/` — abstract `Backend`, `SimulatedBackend`; a backend returns token counts and
  takes no simulated time, workers do the timing (exists)
- `registry.py` — `ModelRegistry`, `UnknownModelError`, `Worker` protocol (exists)
- `backends/llamacpp.py` — `LlamaCppBackend`: a real model behind llama-server or Ollama,
  over the OpenAI-compatible streaming API; optional `sampling` fields such as
  temperature (exists)
- `passthrough.py` — `PassthroughWorker`: forwards to a backend that does its own
  batching and only times the result; the pair for `LlamaCppBackend` (exists)
- `backend_config.py` + `backends.yaml` — which backend to use and where each tier's
  model server is (exists)
- `baseline.py` — `BaselineWorker`, the one-at-a-time FIFO reference that later steps must
  beat (exists)
- `continuous.py` — `ContinuousBatchingWorker`, the per-model iteration-level batcher;
  swappable with `BaselineWorker`; optional chunked prefill (`prefill_chunk_tokens`),
  off by default so earlier results stay reproducible (exists)
- `prefix_cache.py` — `PrefixCache`, the KV-cache stand-in: a radix tree over token ids
  with LRU leaf eviction and ref-counted leases; one per model, sharing the model's
  `kv_capacity_tokens` with running requests (exists)
- `tokenizer.py` — `Tokenizer` protocol and the `WordTokenizer` stand-in (exists)
- `routing.py` — `Plan`, `PlansConfig`, `load_plans`, and the `Router` that picks a model
  within a tenant's plan, by model name or by quality (exists)
- `plans.yaml` — the packaged plans and demo tenants (exists)
- `ledger.py` — `CostLedger`: per-tenant tokens and spend per 30-day period of epoch time,
  and the budget check; a facade over a `UsageStore` (exists)
- `usage.py` — `UsageEvent`, the `UsageStore` protocol, `InMemoryUsageStore` (bounded ring)
  and `SqliteUsageStore`; store calls are synchronous and never awaited (exists)
- `database.py` + `migrations/` — `open_database` (WAL, busy timeout, autocommit) and the
  versioned `MigrationRunner`; migration 1 is `usage_events` (exists)
- `overload.py` — `run_with_timeout`, `SloMonitor` (sliding-window p99 with hysteresis),
  `pick_downgrade`, `QueueFullError`; the queue limit itself is in the worker (exists)
- `metrics.py` — nearest-rank `percentile` and `MetricsStore`: per-model metrics in bounded
  memory, a sliding time window over a fixed-size ring (exists)
- `timeseries.py` — `TimeSeriesStore`: per-model and per-tenant series at 1 s resolution
  over a 15-minute window, in bounded memory; buckets are filled by events and reads, with
  no background task (exists)
- `events.py` — `EventBus`: in-process events with a bounded log, read with a cursor (exists)
- `monitoring.py` — `Monitor`: told by `app.py` what happened to each request, builds the
  snapshot behind `/v1/monitor/snapshot`, `/v1/monitor/stream` and Prometheus `/metrics`;
  `SchedulerView` is the protocol a worker must match for its queue, batch, KV and cache to
  be reported (exists)
- `workload.py` — `LoadConfig`, `build_workload`: the seeded request schedule, a pure
  function of its config. `benchmarks/loadgen.py` re-exports it and runs it over HTTP
  (exists)
- `loadcontrol.py` — `LoadController` and the load presets: runs that schedule inside
  the server for the dashboard, through the same handler an HTTP request reaches. Off
  unless `GATEWAY_LOAD_CONTROL=1` or simulated live mode (exists)
- `app.py` — FastAPI app: `create_app(registry, clock)` for tests, `create_default_app()`
  for the server, `create_simulated_live_app()` for simulated live mode
  (`GATEWAY_SIMULATED_LIVE=1`); JSON or server-sent-event responses; serves the built
  dashboard at `/dashboard` (exists)

Outside the package, `benchmarks/` is a package run with `python -m`:

- `benchmarks/baseline_limits.py`, `benchmarks/compare_batching.py` — the step 2 and step 3
  comparisons (exist)
- `benchmarks/loadgen.py` — seeded load generator over the HTTP API, in-process on
  `SimClock` or against a live URL; same config gives byte-identical JSON (exists)
- `benchmarks/fairness.py` — per-model isolation demo (exists)
- `benchmarks/overload.py` — p99 with and without SLO-aware downgrade (exists)
- `benchmarks/chunked_prefill.py` — ITL and TTFT tails with chunked prefill off and on (exists)
- `benchmarks/metrics_example.py` — what the metrics store reports after the default load;
  the worked example in `docs/05-metrics.md` (exists)
- `benchmarks/calibrate.py` — measures real models, fits the four timing parameters of each
  profile (the fourth is `per_request_overhead_ms`), and reports the simulator's error
  against the measurements, the spread between repeats, and how much of each model Ollama
  had on the GPU. `--compare` gives the spread between runs; `--aggregate` combines saved
  runs into the median of each parameter with its range. `benchmarks/calibration/` is
  tracked: six runs in `run1/` to `run6/` (Ollama, one laptop, 2026-10-08) and their
  aggregate, in which most parameters are marked unstable and premium hardware-limited.
  Its medians are the default profiles; `tests/test_profiles.py` keeps the two in step
  (exists)
- `benchmarks/quality_eval.py` — asks each tier's real model 100 fixed multiple-choice
  questions (an MMLU sample, MIT license, in `benchmarks/quality/`) at temperature 0 and
  scores exact match on the option letter. `benchmarks/quality/results.json` is tracked and
  holds one run (Ollama, one laptop, 2026-10-09). Small and unrepeated: always quote the
  interval with the accuracy (exists)
- `benchmarks/gate.py` + `scripts/perf_gate.py` — the CI performance gate; budgets live in
  `benchmarks/budgets.json` (exists). It runs 1000 requests at 0.8 req/s, a rate at which no
  model is saturated; it protects the scheduler logic and does not measure hardware
- `benchmarks/saturation.py` — checks that no model is saturated by a workload (offered
  above 60% of capacity, or p99 growing over 25% when the run is doubled) before its p99 is
  reported (exists)
- `benchmarks/usage_store_writes.py` — times one usage-event write on the local disk; the
  numbers in `docs/19-usage-store.md` (exists)

- `benchmarks/report.py` — generates `docs/REPORT.md` and `docs/report/*.png` from the
  benchmarks above (`make report`). Sections 1 to 3 run on the calibrated profiles
  (`GATEWAY_PROFILES` names another file) and leave out the p99 of a saturated run;
  sections 4 and 5 (overload, chunked prefill) have no results: not measured on real data;
  section 7 is measured accuracy with no cost. Never edit the report by hand; regenerate it
  (exists)

If a change moves simulated latency on purpose, re-baseline with
`uv run python scripts/perf_gate.py --update-budgets` and commit `benchmarks/budgets.json`
in the same change, with the printed diff in the PR description. Never update budgets to
make an unexplained failure pass.

`frontend/` is the monitoring dashboard: React, TypeScript, Vite, uPlot. It only reads
the monitoring API (`docs/15`); it has no logic of its own about the gateway.

- `src/api/types.ts` mirrors the API by hand. `src/test/fixtures/*.json` are snapshots the
  gateway produced; `tests/test_dashboard.py` fails when they are out of date
  (`GATEWAY_UPDATE_FIXTURES=1 uv run pytest tests/test_dashboard.py` regenerates them), and
  the frontend's type check then shows what has to follow. Never edit a fixture by hand.
- Data transforms are pure functions in `src/charts/transforms.ts`, `src/api/store.ts` and
  `src/api/derive.ts`, each with tests. Components do not compute.
- A metric with `available: false` is drawn with the `Unavailable` component, never as 0.
- Every chart goes through `ChartPanel` (units in the title, pause, table view,
  downsampling).
- `npm run build` writes to `src/gateway/dashboard/`, which is not tracked.

- The dashboard must label simulated live mode on every tab (`SimulationBanner`).
- `frontend/e2e/*.e2e.ts` are Playwright smoke tests against the gateway in simulated
  live mode (`npm run e2e`). They wait for a state, never for a time.
- `docs/img/` is taken by `scripts/capture_demo.py` in simulated live mode. Never edit
  a picture; retake it. Any caption must say it is simulated.
- The Dockerfile builds the dashboard in a Node stage and serves it from the Python
  stage; `docker-compose.yml` is the simulated live demo. Neither has been run on the
  development machine (no Docker): do not write that they work until CI has built them.

`scripts/` holds operational entry points only: `perf_gate.py`, `demo.py`,
`capture_demo.py` (retakes the dashboard pictures in `docs/img/`),
`run_local_models.sh`, and `protected_outputs.py`, which checks the outputs that must not
change against the sha256 sums in `scripts/protected_outputs.sha256` (a gate, like
`perf_gate.py`). Anything that measures or compares results goes in `benchmarks/`.
Never refresh that baseline to make a failure pass; the README says how to refresh it.

Rules:

- `app.py` is the only wiring point. It builds components and injects their dependencies.
- Logic modules never import FastAPI and never construct their own clock, RNG or backend.
- Dependencies point inward: app → routing/batching → backends/cache/metrics → clock.

Backends and workers go in pairs: `SimulatedBackend` with `ContinuousBatchingWorker` (or
`BaselineWorker`), `LlamaCppBackend` with `PassthroughWorker`. With real models the model
server does the batching and this repo's scheduler, prefix cache and chunked prefill are
not in the path. Never present a real-mode number as a result of our scheduler, and never
compare simulated and real numbers as if they measured the same thing.

Monitoring watches and decides nothing: no scheduling, routing or admission code may read
from `Monitor`, `TimeSeriesStore` or `EventBus`. A metric the gateway cannot observe (queue,
batch, KV and cache behind `PassthroughWorker`) is reported as unavailable, `null` with
`available: false`, never as zero. The schema is in `docs/15-monitoring-api.md`.

Simulated live mode (`docs/17-playground-load-replay.md`) is the simulated wiring on a
`PacedSimClock` with a load preset running. It must stay deterministic: the same seed
gives the same events and snapshots at any speed (`tests/test_live_sim.py`). Nothing in
it may read real time; `PacedSimClock` uses real time only to decide when simulated
time may move, never what it moves to. Never present anything from it as a measurement.

Tests that need a real model server are marked `real_models` and skipped unless
`GATEWAY_REAL_MODELS=1`; CI never runs them.

## Hard rules

1. **Injectable Clock.** Every component takes a `Clock`: `SimClock` in tests and CI,
   `WallClock` in the server. No `time.sleep` and no direct `time.time()` in logic. Ruff
   enforces this (`TID251` banned-api in `pyproject.toml`); only `src/gateway/clock.py` is
   exempt. Do not add `noqa` to get around it.
2. **One backend interface.** All backends implement the same abstract interface.
   `SimulatedBackend` must be fully deterministic given a seed: no global `random`, no wall
   time, no dependence on dict/set iteration order or task scheduling order.
3. **Every step ships with tests and a design note.** The note goes in
   `docs/NN-<topic>.md` and is short: what was decided and why.
4. **Never claim measured numbers that weren't measured.** Any figure in the README, docs,
   commit messages or PRs must name the command and commit that produced it. Numbers from
   the simulator are labelled as simulated. If it wasn't run, don't write a number.
5. **No invented data in results.** `TEST_PROFILES` in `tests/helpers.py` are hand-made
   fixtures, not data: only tests may import them (`tests/test_profiles.py` enforces it) and
   no document may quote a number from them. Never report a cost, saving or ratio from the
   placeholder prices, never batch past the measured 4 in a reported result, and mark every
   result that rests on an unstable calibrated parameter as such.
6. **Secrets only from environment variables.** No key, pepper or token in YAML, code,
   fixtures, docs or logs. API keys are stored only as an HMAC with a pepper from the
   environment.
7. **No prompts or responses stored, and no real data.** No runtime store, log, event or
   metric may contain prompt text, response text or token ids (including the prefix
   cache's hashed tokens). Benchmark records built from public datasets with their license
   attribution, such as `benchmarks/quality/`, are exempt from this rule because they are
   not request traffic. No customer or user data exists in the repo and none is added.
   The plan these rules came with is `docs/platform-plan.md`.

## Coding conventions

- Full type hints; mypy runs in strict mode over `src` and `tests`.
- `from __future__ import annotations` at the top of every module.
- Frozen dataclasses for value types.
- Async-first: logic that waits does so through the `Clock`.
- No module-level mutable state.
- Randomness comes from a seeded `random.Random` instance that is passed in.
- Line length 100; `ruff format` is the formatter.

## Commands

```sh
uv sync                      # create .venv and install everything
uv run ruff check .          # lint
uv run ruff format --check . # formatting (drop --check to apply)
uv run mypy                  # type check
uv run pytest                # tests
uv run python scripts/perf_gate.py   # performance gate
uv run python scripts/protected_outputs.py  # protected outputs unchanged (platform work)
uv run python -m benchmarks.report   # regenerate docs/REPORT.md
```

The `Makefile` wraps these (`make check`, `make report`).

The dashboard has its own, run in `frontend/` (Node 20):

```sh
npm ci            # install
npm run check     # eslint, prettier --check, tsc, vitest
npm run build     # build into src/gateway/dashboard
npm run dev       # dev server, proxying the API to a gateway on port 8000
npm run e2e       # browser smoke tests (PLAYWRIGHT_CHANNEL=chrome to use Chrome)
```

All of these must pass before a step is called done. CI runs the same commands on
Python 3.11 and 3.12.
