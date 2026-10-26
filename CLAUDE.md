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
Every runtime dependency and bundled dataset is recorded with its license in
`docs/licenses.md`; `tests/test_licenses.py` fails on anything unrecorded or not allowed.

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
- `chat.py` — the one chat format (messages, tools, tool calls, deltas, usage, capabilities
  with their source) and `ProviderError`, the fixed codes every provider failure maps to (exists)
- `secret.py` — `Secret`: a credential whose `repr`, `str` and format are redacted; `reveal()`
  only in `adapters/http.py`'s `credential_headers` (an AST test) (exists)
- `adapters/` — `OllamaAdapter`, `OpenAICompatibleAdapter`, `AnthropicAdapter`: `Backend`s that
  speak the chat format; `http.py` holds the client (no redirects, no proxy), the credential
  header and the status mapping; the credential is passed to each call, never held (exists)
- `vault.py` — `Vault`: provider keys sealed with AES-256-GCM under `GATEWAY_VAULT_KEY` (the
  server refuses to start without it; `GATEWAY_VAULT_KEY_PREVIOUS` during a rotation), bound to
  organization, provider, type and URL origin; no trial decryption; `lease()` decrypts for one
  use (exists)
- `redaction.py` — `Redactor` and `install`: a log-record factory that scrubs leased key values
  and credential patterns from every record; installed by the server's lifespan (exists)
- `egress.py` — the egress guard for organization providers: https only, every resolved address
  public, the socket connects to the checked address, no redirects, no proxy (exists)
- `providers.py` — `ProviderConfig`, `ProviderStore` over migrations 10 and 11, `ProviderService`
  (create, update, the organization's and the platform's switches, keys, test-connection with
  probing, `key_unreadable`) and `resolve_provider` (exists)
- `chatservice.py` — `ChatService`: one chat request on one tier, through the simulator or the
  tier's adapter, first-token and total timeouts on the injected clock, cancellation (exists)
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
- `routing.py` — `Plan`, `PlanLimits`, `PlansConfig` (plan templates and demo
  applications), `load_plans`, and the `Router` that picks a model within an application's
  plan, by model name or by quality (exists)
- `plans.yaml` — the packaged plan templates and demo applications; never keys (exists)
- `organizations.py` — `Organization`, `OrganizationKind` (`individual`, `business`),
  `normalize_name` (NFC, exact comparison, no control or zero-width characters, at most 100),
  and the pure `membership_decision` (an individual organization takes one member) that
  step 4 wires into user creation (exists)
- `applications.py` — `Application` (its own copy of a template's plan settings and limits,
  and its `organization_id`), `ApiKeyRecord`, `KeyLookup` (key, application and organization
  state in one query), the `ApplicationStore` protocol with organizations, in-memory and
  SQLite stores; names unique within an organization (exists)
- `apikeys.py` — `ApiKeyService`: issues keys (`gwk_<id>_<secret>`), stores only an HMAC
  with the pepper from `GATEWAY_KEY_PEPPER`, checks in constant time, rotates with a grace
  period, revokes, and batches `last_used_at`; `ApplicationAccess` is what `create_app`
  takes (exists)
- `passwords.py` — argon2id `PasswordHasher` (64 MiB, 3 iterations, 1 lane; `GATEWAY_ARGON2_*`), `InlineHashRunner` (for
  `SimClock`: no await, so simulated time cannot move) and `ThreadHashRunner` (for the server:
  a pool of 2, the loop keeps serving), `Passwords`, and `PasswordPolicy` with the bundled
  blocklist in `data/` (public data, MIT, recorded in `docs/licenses.md`) (exists)
- `users.py` — `normalize_username` (case-folded ASCII, 3 to 64), `User`, `Session` (only
  digests), `AccountStore` over migrations 5 and 6 (exists)
- `auth.py` — `AccountService`: creating users (`membership_decision`, the policy), the
  bootstrap admin from `GATEWAY_ADMIN_USERNAME`/`GATEWAY_ADMIN_PASSWORD`, login with the same
  steps for every refusal (one lookup, one verify against the stored or a dummy hash), the
  database lockout and the in-memory `LoginLimiter`, sessions (30 min idle, 12 h, `last_seen`
  at most once a minute), CSRF (an HMAC of the session id under a derived key), password
  change, retention;
  `AccountAccess` is what `create_app` takes for `/v1/auth`; step-up (`step_up`,
  `check_step_up`): a 5-minute window per session, a per-session cap, refused through
  `requires` for actions the table flags; for the management endpoints, temporary passwords
  (hashed through the runner, shown once), an admin refused on its own account, the cap on
  user-creation attempts per session (`GATEWAY_USER_CREATE_ATTEMPTS`) and the audit of every
  refusal (exists)
- `management.py` — the management endpoints' logic below HTTP: resolvers that answer a
  resource outside the path's organization as missing, `ManagementRefusal` (each 409 and 429,
  its reason the error code), and `ManagementService` (applications from a plan template, keys
  issued, rotated and revoked with an audit entry by key id; the HTTP path's `KeyLimits`),
  shared with the CLI (exists)
- `permissions.py` — `Role`, `ScopeType`, scope `Relation`s, `PERMISSIONS` (one table: action,
  level, allowed role@relation pairs, step-up, consume-on-use, wired), `Actor`, `Resource`, and `authorize`, deny
  by default; `python -m gateway.permissions --matrix` writes `docs/permission-matrix.md` (exists)
- `roles.py` — `RoleStore` over migration 8: role assignments and application-defined roles
  (exists)
- `keyderivation.py` — `derive_key(pepper, KeyPurpose)`: one key per purpose (CSRF tokens, the
  login limiter), each with its own label; API key HMACs keep the pepper itself (exists)
- `audit.py` — `AuditEvent` through a field allowlist, `AuditLog` with the per-minute cap on
  unknown-user rows and retention (exists)
- `localonly.py` — `LocalOnlyMiddleware`: monitoring, metrics, overload, load control, the
  Playground and the dashboard are served only to loopback peers (and `GATEWAY_LOCAL_NETWORKS`),
  never through forwarding headers, only for allowed Host names, never cross-site; until A8.
  Installed by both server factories (exists)
- `serve.py` — the launcher, `python -m gateway.serve`: uvicorn with proxy headers off;
  refuses a non-loopback bind unless `GATEWAY_ALLOW_NETWORK_BIND=1` (exists)
- `ratelimit.py` — `RequestRateLimiter`: per-application requests per minute, a sliding
  window on the injected clock (exists)
- `ledger.py` — `CostLedger`: per-application tokens and spend per budget period (calendar
  months in UTC by default), the budget check, and retention pruning; a facade over a
  `UsageStore` (exists)
- `periods.py` — `BudgetPeriods`, `CalendarMonthsUTC` (from whole days since the epoch),
  `FixedPeriods` for tests (exists)
- `usage.py` — `UsageEvent`, the `UsageStore` protocol, `InMemoryUsageStore` (bounded ring)
  and `SqliteUsageStore`; store calls are synchronous and never awaited (exists)
- `database.py` + `migrations/` — `open_database` (WAL, busy timeout, autocommit) and the
  versioned `MigrationRunner`; migration 1 is `usage_events`, 2 `applications` and
  `api_keys`, 3 an index for retention, 4 `organizations` and a required
  `applications.organization_id` (a table rebuild: the runner turns foreign keys off around
  it and checks them; `plan_migrations` previews without writing; the server refuses a
  rebuild on a database with data, `scripts/manage.py migrate` backs up first); 5 `users`, 6
  `sessions`, 7 `audit_log`, 8 `role_assignments` and `app_roles` (a rebuild of `users` without
  `platform_admin`), 9 `sessions.step_up_until_ms`, 10 `provider_configs`, 11 `provider_keys`. The default database is in the user's data folder,
  owner-only; `ProcessLock` allows one gateway process per database; `backup_database` and
  `restore_database` use SQLite's backup API (exists)
- `overload.py` — `run_with_timeout`, `SloMonitor` (sliding-window p99 with hysteresis),
  `pick_downgrade`, `QueueFullError`; the queue limit itself is in the worker (exists)
- `metrics.py` — nearest-rank `percentile` and `MetricsStore`: per-model metrics in bounded
  memory, a sliding time window over a fixed-size ring (exists)
- `timeseries.py` — `TimeSeriesStore`: per-model and per-application series at 1 s resolution
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
- `app.py` — FastAPI app: `create_app(registry, clock)` for tests (with `applications=` for
  API keys, usage store, epoch and retention), `create_default_app()` for the server (needs
  `GATEWAY_KEY_PEPPER`; SQLite at `GATEWAY_DATABASE`; one process per database), and
  `create_simulated_live_app()` for simulated live mode (`GATEWAY_SIMULATED_LIVE=1`,
  everything in memory, Playground with throwaway keys); JSON or server-sent-event
  responses; serves the built dashboard at `/dashboard`; with accounts, the management
  endpoints under `/v1/admin/orgs/{org}` (docs/30) and the provider endpoints (docs/32), each
  through `requires` (which claims the step-up window for consume-on-use actions), every answer
  `Cache-Control: no-store` (exists)

Outside the package, `benchmarks/` is a package run with `python -m`:

- `benchmarks/baseline_limits.py`, `benchmarks/compare_batching.py` — the step 2 and step 3
  comparisons (exist)
- `benchmarks/loadgen.py` — seeded load generator over the HTTP API, in-process on
  `SimClock` or against a live URL (key from `GATEWAY_API_KEY`); same config gives
  byte-identical JSON (exists)
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
- `benchmarks/password_hashing.py` — what argon2id costs here, alone and with logins arriving
  together; the numbers in `docs/24-password-hashing.md` (exists)
- `benchmarks/usage_retention.py` — times pruning old usage events in batches and in one
  go; the numbers in `docs/21-persistence.md` (exists)
- `benchmarks/login.py` — logins on the running server: one, 8 at once, and failed logins for
  known and unknown usernames; the numbers in `docs/24` and `docs/27` (exists)
- `benchmarks/session_checks.py` — a session check with and without the `last_seen_at`
  write; the numbers in `docs/27` (exists)
- `benchmarks/key_checks.py` — times a key check, and what writing `last_used_at` per
  request would add; the numbers in `docs/20-applications-api-keys.md` (exists)
- `benchmarks/chat_overhead.py` — the chat endpoint in front of a mock provider on the running
  server, against the mock directly; the numbers in `docs/31` (exists)
- `benchmarks/provider_requests.py` — a vault decrypt, the egress check, and provider admin
  requests on the running server; the numbers in `docs/32` (exists)
- `benchmarks/management_requests.py` — management reads and key writes on the running
  server, beside `/healthz` and `/v1/auth/me`; the numbers in `docs/30` (exists)

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
  stage; `docker-compose.yml` is the simulated live demo, with a volume and a git-ignored
  `gateway.env` for the plain server. The development machine has Docker Desktop 29.8.2
  (Windows); CI's `docker` job builds the image and starts the compose demo on Linux. What
  has been run locally is recorded in note 25; nothing else about Docker (the plain server,
  the volume, the env file) has been run: do not write that it works.

`scripts/` holds operational entry points only: `perf_gate.py`, `demo.py`,
`capture_demo.py` (retakes the dashboard pictures in `docs/img/`),
`run_local_models.sh`, `manage.py` (creates organizations and applications and issues, rotates
and revokes API keys, audited like the management endpoints; prints a new key once, never logs
it; `rotate-vault-key` and `vault-drop-unreadable` for provider keys (docs/32); also
`migrate` with `--dry-run` and a backup before a table rebuild, backup and restore), `readme_quickstart.py` (runs the README's quickstart blocks exactly as written and
checks the result; a CI job, so the README cannot go stale), `check_characters.py` (a CI
gate: tracked source, test, workflow and shell files are printable ASCII, with a short
allowlist of typographic characters for the dashboard; write anything else as an escape),
and `protected_outputs.py`, which checks the outputs that must not
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

`.gitignore` ignores `*.db` and `*.db-*` everywhere (databases and their backups hold key HMACs
and password hashes). So **no tracked file, test fixture included, may use a `.db` extension**:
it would be ignored, never committed, and a test passing locally would fail in CI. Build test
databases in `tmp_path` at run time instead.

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

## Working agreement

These apply to every task without being repeated.

**Process**

- Each step gets its own branch and PR. The first commit is the plan update. Open the PR as a
  draft with `gh`, put the reviewer-facing description in the PR body, wait for CI with `gh`,
  fix failures that are not in the stop list, then report.
- Plan approval is required only for tasks that touch authentication, secrets, provider keys,
  migrations that rebuild a table, or a new network surface. For all other tasks, write the plan
  as the first commit and continue.
- The next step may start from the previous branch once its CI is green. Say clearly which PRs
  must be merged in which order.

**Definition of done** (real output shown, never summarized): ruff, the format check, mypy, the
full pytest, `scripts/check_characters.py`, protected outputs with `--include-images`, the perf
gate, the README quickstart, the frontend checks, e2e, and every CI job green.

**Security rules**

- Every rule about who may do what, or what is refused, has a mutation check: break it, show
  which tests fail, restore it byte for byte, show the tests pass again.
- Any new path that handles a secret gets the leak scan with a planted control that must be
  found.
- Any refusal path in authentication gets a structural test that every case takes the same
  steps, plus the early-return mutation.
- A missing resource and a forbidden one get the same response, byte for byte.
- No secret in logs, errors, audit entries, test output, PR text or commits.

**Measurement rules**: performance statements are measured on this machine, with the hardware
recorded and a range over three runs. Never state an unmeasured number. Label simulated results
as simulated.

**PR description template**, sections in order: What changed; Decisions not in the request
(numbered, so they can be objected to); Mutation checks (a table); Timing (with hardware);
Threat-model additions; Not built; Checks; CI result.

**Stop and ask when**

- a check fails and the fix would change a protected output, a budget or a tolerance;
- a security semantic is ambiguous;
- a secret, key or account is needed;
- a test cannot be made to fail under mutation;
- an unexpected file is tracked, or a dependency is added (pin it, check its license, and record
  it in `docs/licenses.md` first);
- anything would require a destructive git command.

**Never** merge a PR, push to main, refresh a baseline to make a check pass, print a secret, stop
a process you did not start, or run `git reset --hard` or any command that discards work.

## Commands

```sh
uv sync                      # create .venv and install everything
uv run ruff check .          # lint
uv run ruff format --check . # formatting (drop --check to apply)
uv run mypy                  # type check
uv run pytest                # tests
uv run python scripts/perf_gate.py   # performance gate
uv run python scripts/protected_outputs.py  # protected outputs unchanged
uv run python scripts/check_characters.py   # tracked code is printable ASCII
uv run python scripts/readme_quickstart.py  # the README quickstart still works (port 8000 free)
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
