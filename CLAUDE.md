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

- `profiles.py` — `Tier`, `ModelProfile`, the three profiles (exists; numbers are
  illustrative), and `load_calibrated_profiles` for a calibration file
- `cost_model.py` — `estimate_cost`, `OutputLengthGenerator` (exists)
- `clock.py` — `Clock` protocol, `SimClock`, `WallClock`; times are milliseconds (exists)
- `schema.py` — `GenerateRequest`, `RequestResult` (exists)
- `backends/` — abstract `Backend`, `SimulatedBackend`; a backend returns token counts and
  takes no simulated time, workers do the timing (exists)
- `registry.py` — `ModelRegistry`, `UnknownModelError`, `Worker` protocol (exists)
- `backends/llamacpp.py` — `LlamaCppBackend`: a real model behind llama-server or Ollama,
  over the OpenAI-compatible streaming API (exists)
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
- `ledger.py` — `CostLedger`: per-tenant tokens and spend per 30-day period on the injected
  clock, and the budget check (exists)
- `overload.py` — `run_with_timeout`, `SloMonitor` (sliding-window p99 with hysteresis),
  `pick_downgrade`, `QueueFullError`; the queue limit itself is in the worker (exists)
- `metrics.py` — nearest-rank `percentile` and `MetricsRecorder` (exists)
- `app.py` — FastAPI app: `create_app(registry, clock)` for tests, `create_default_app()`
  for the server; JSON or server-sent-event responses (exists)

Outside the package, `benchmarks/` is a package run with `python -m`:

- `benchmarks/baseline_limits.py`, `benchmarks/compare_batching.py` — the step 2 and step 3
  comparisons (exist)
- `benchmarks/loadgen.py` — seeded load generator over the HTTP API, in-process on
  `SimClock` or against a live URL; same config gives byte-identical JSON (exists)
- `benchmarks/fairness.py` — per-model isolation demo (exists)
- `benchmarks/overload.py` — p99 with and without SLO-aware downgrade (exists)
- `benchmarks/chunked_prefill.py` — ITL and TTFT tails with chunked prefill off and on (exists)
- `benchmarks/calibrate.py` — measures real models, fits the three timing parameters of each
  profile, and reports the simulator's error against the measurements. Writes to
  `benchmarks/calibration/`, which is tracked and holds one calibration (Ollama, one
  laptop, 2026-10-07). The simulator does not load it by default: the profiles in
  `profiles.py` are still illustrative (exists)
- `benchmarks/gate.py` + `scripts/perf_gate.py` — the CI performance gate; budgets live in
  `benchmarks/budgets.json` (exists)

- `benchmarks/report.py` — generates `docs/REPORT.md` and `docs/report/*.png` from the
  benchmarks above (`make report`). Never edit the report by hand; regenerate it (exists)

If a change moves simulated latency on purpose, re-baseline with
`uv run python scripts/perf_gate.py --update-budgets` and commit `benchmarks/budgets.json`
in the same change, with the printed diff in the PR description. Never update budgets to
make an unexplained failure pass.

`scripts/` holds operational entry points only: `perf_gate.py`, `demo.py` and
`run_local_models.sh`. Anything that measures or compares goes in `benchmarks/`.

Rules:

- `app.py` is the only wiring point. It builds components and injects their dependencies.
- Logic modules never import FastAPI and never construct their own clock, RNG or backend.
- Dependencies point inward: app → routing/batching → backends/cache/metrics → clock.

Backends and workers go in pairs: `SimulatedBackend` with `ContinuousBatchingWorker` (or
`BaselineWorker`), `LlamaCppBackend` with `PassthroughWorker`. With real models the model
server does the batching and this repo's scheduler, prefix cache and chunked prefill are
not in the path. Never present a real-mode number as a result of our scheduler, and never
compare simulated and real numbers as if they measured the same thing.

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
uv run python -m benchmarks.report   # regenerate docs/REPORT.md
```

The `Makefile` wraps these (`make check`, `make report`).

All of these must pass before a step is called done. CI runs the same commands on
Python 3.11 and 3.12.
