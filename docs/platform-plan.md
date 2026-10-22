# Platform plan, A1: applications, API keys and persistent usage

Status: approved 2026-10-21. No code has changed yet; this note is the plan for the
`platform/foundation` branch. Steps 1 to 6 each ship with tests and their own design note
(`docs/19-*` onwards), as every step in this repo does.

A1 turns the simulator's trust-based tenants into applications that identify themselves
with an API key, and the in-memory ledger into usage events that survive a restart. Later
phases are referred to by name only: A2 (users, roles, sessions and authenticated admin
endpoints), A3 (organizations), A5 (provider configs and the audit log), A8 (protecting
monitoring and load control), A9 (the console). Nothing in A1 builds any of them.

## Rules this effort adds

- **Secrets come only from environment variables.** No key, pepper or token in YAML,
  code, fixtures, docs or logs.
- **The gateway stores no prompt text, response text or token ids** at runtime: not in a
  store, a log, an event or a metric. Benchmark records built from public datasets with
  their license attribution (`benchmarks/quality/`) are exempt; they are not request
  traffic.
- **No real data.** Public benchmark data with its license and our own measurements stay.
  No customer or user data exists in the repo and none is added.

## 1. Concept map

| Today | Becomes | Where it is today |
|---|---|---|
| Tenant (`initech`, `globex`, `acme`) | **Application**: id, name, its plan settings, the HMACs of its API keys | `plans.yaml` `tenants:`, `PlansConfig.tenants` |
| `plans.yaml`: named plans plus a tenant to plan map | **Per-application plan settings** (`models`, `price_multiplier`, `monthly_token_budget`) held on each application. The named plans remain as **templates** an application is created from | `routing.py` `Plan`, `PlansConfig` |
| `CostLedger`: in memory, per process, periods counted from process start | **Persistent usage events**: one append-only row per settled request (application id, request key, model, prompt, cached and completion token counts, base and billed cost, epoch time). Usage and the budget check are computed from them | `ledger.py` |
| `tenant_id` in the request body, taken on trust | **Identity from an API key**: `Authorization: Bearer <key>` resolves to an application. A `tenant_id` field in the body is a 422 | `app.py` `GenerateBody`, the generate handler |
| `GET /v1/tenants/{id}/usage`, readable by anyone for anyone | `GET /v1/applications/me/usage`, scoped to the caller's key | `app.py` |
| `unknown_tenant` (404), `tenant_required` (401) | `invalid_api_key` (401), `api_key_required` (401) | `app.py` |
| Monitoring's `tenants` group, `tenant_*` series, `tenant_id` on events | `applications`, `application_*`, `application_id` | `monitoring.py`, `timeseries.py`, `events.py` |

Unchanged: `Plan` as a value type; `Router`'s model and quality rules; `pick_by_quality`;
`pick_downgrade`; the soft budget (check before, charge after); the 429
`budget_exhausted` body; and the path of `create_app` without plans, which has no
tenancy at all.

**No single-organization assumption.** A3 puts an Organization (with a `kind` field) above
Application. Steps 1 to 5 must not assume there is one organization: no global
"the application" lookups, no unique constraints that would have to be scoped later, no
code that lists all applications as if they belonged to one owner. No organization column
is added now; A3 adds it with its own migration.

## 2. Storage

- **stdlib `sqlite3`**, behind two protocols: `UsageStore` and `ApplicationStore`. Each has
  an in-memory implementation (tests, simulated runs) and a SQLite one (the default
  server). No new dependency.
- **Versioned migrations**: a hand-written runner, a `schema_migrations` table, each
  migration a numbered module with `up`, `down` and an optional backfill. Tests apply every
  migration up, then down, then up again, and check a backfill against seeded rows.
  SQLAlchemy and Alembic are reconsidered at A2 if this becomes painful.
- **A1 creates only its own tables**: `applications`, `api_keys`, `usage_events`. Users,
  roles, sessions, organizations, provider configs and the audit log are **not** created;
  each arrives with its own migration in A2, A3 and A5. No speculative columns.
- **Store calls are synchronous.** `SimClock` advances time when the event loop is idle; a
  write offloaded to a thread makes the loop look idle and lets simulated time jump. Writes
  are short and local; if they ever need to move off the loop, that is a decision with its
  own test against `SimClock`.
- **Epoch time comes from a second injected source**, added to `clock.py` (the only file
  allowed to read real time). `WallClock` is `perf_counter`, not epoch, so it cannot date a
  row that must outlive the process. `Clock` itself does not change, so no timing path does.
- **Request keys are unique across restarts.** The response `id` (`req-N`, a per-process
  counter) stays as it is; the usage row gets its own key.
- **One writer.** SQLite serialises writes and the budget stays soft; more than one gateway
  process on one database is not supported in A1, and the docs say so.

## 3. API keys

- Random, at least 128 bits of entropy (`secrets.token_bytes(32)`), with a short
  non-secret prefix so a key can be recognised and listed.
- Stored only as `HMAC-SHA256(pepper, key)`; the pepper comes from `GATEWAY_KEY_PEPPER`.
  Comparison uses `hmac.compare_digest`. The plaintext key is never stored, logged or put
  in an event.
- **The default server refuses to start without the pepper.** `create_app` with plans in
  tests receives its stores and pepper by injection.
- **Pepper rotation** (documented in the step's design note): each stored hash records
  the pepper version it was made with; the server accepts `GATEWAY_KEY_PEPPER` (current)
  and `GATEWAY_KEY_PEPPER_PREVIOUS` (optional); a key verified under the previous pepper is
  re-hashed under the current one; once no row uses the old version, the previous variable
  is removed. Keys themselves do not change.
- **Issuance is a CLI under `scripts/`**, a bootstrap only: it creates an application or a
  key in the database and prints the new key once to the terminal. It never writes it to a
  log or file. There is no admin token endpoint; A2 replaces the CLI with authenticated
  endpoints.
- **Demo and Playground**: in simulated live mode and in `scripts/demo.py`, throwaway keys
  are generated in memory at startup and handed to the in-process pieces (load controller,
  demo script, the dashboard's Playground through the server) without being printed,
  logged or written anywhere. The load controller sends through the handler with an
  already-resolved application, as it now sends with a tenant id.

## 4. What is touched

**Modules**
- `routing.py`: `PlansConfig.tenants` goes; `Router.route` takes an `Application`.
- `plans.yaml`: `plans:` stays, as templates. `tenants:` becomes demo application
  definitions, with no keys.
- `ledger.py` becomes `usage.py`: `UsageEvent`, `UsageStore`, in-memory and SQLite stores.
- New: `applications.py` (`Application`, `ApplicationStore`, key hashing and checking),
  `migrations/` (runner and numbered migrations).
- `clock.py`: the epoch time source.
- `app.py`: the auth dependency; `GenerateBody` without `tenant_id`; `settle()` writes a
  usage event; the usage endpoint; `send_planned` uses internal application identities;
  `create_default_app` reads the new environment variables and refuses to start without
  the pepper.
- `monitoring.py`, `timeseries.py`, `events.py`: the tenant to application rename.
- **Not touched**: `loadcontrol.py`, `workload.py`, `continuous.py`, `baseline.py`,
  `prefix_cache.py`, `tokenizer.py`, `cost_model.py`, `profiles.py`, `metrics.py`,
  `overload.py`, `registry.py`, `schema.py`, `passthrough.py`, `backends/`.

**Tests**
- Rewritten: `test_routing.py`, `test_ledger.py` (becomes `test_usage_store.py`),
  `test_tenancy_api.py` (becomes `test_auth_api.py`).
- Edited for the rename: `test_app.py`, `test_streaming.py`, `test_overload_api.py`,
  `test_monitoring_api.py`, `test_timeseries.py`, `test_events.py`, `test_live_sim.py`,
  `test_dashboard.py` (fixtures regenerated with `GATEWAY_UPDATE_FIXTURES=1`, never edited),
  `test_entry_points.py`, `test_real_backend_api.py`, `test_real_models.py`.
- New: `test_migrations.py` (up, down, up; backfill), `test_applications.py`, a restart
  test (a second app on the same database sees the first one's usage and keys),
  `test_no_content_stored.py`:
  - sends requests with distinctive prompt words through the full app, and checks that no
    prompt text, response text or token id appears in any store table, any log record
    captured at every level, any event, any snapshot or the Prometheus output;
  - checks the same for the `PrefixCache`'s hashed token ids (`WordTokenizer.encode` of
    the prompt), which live in memory only and must never reach a store, log or event.

**Benchmarks and scripts**
- `benchmarks/loadgen.py`: `--tenant` becomes a key read from an environment variable, in
  live mode only. The in-process path does not change.
- `benchmarks/report.py`: reads the plan templates for the section 7 table; its output does
  not change.
- `scripts/demo.py`: creates applications and throwaway keys in-process.
- New: the key issuance CLI in `scripts/`.
- `scripts/capture_demo.py`: retakes the pictures after the dashboard rename.
- **Not touched**: `benchmarks/gate.py`, `scripts/perf_gate.py`, `benchmarks/overload.py`
  and every other benchmark.

**Frontend** (rename only, no polishing; A9's console extends or replaces it):
`src/api/types.ts`, `store.ts`, `derive.ts`, `playground.ts`, `tabs/Tenants.tsx` (becomes
`Applications.tsx`), `tabs/Events.tsx`, `App.tsx`, their tests, `e2e/demo.capture.ts`,
`e2e/demo.tour.ts`.

**Docs**: notes 06, 09, 15, 16, 17 and 18 get an "updated by" line (they are snapshots);
`docs/README.md`, `README.md`, `CLAUDE.md` (architecture list), `docker-compose.yml` and
Dockerfile comments; new notes from 19.

## 5. What must stay byte-for-byte unchanged

These all run through `create_app(registry, clock)` **without plans**, so that path gets
no new code on the request path: no store, no auth, no extra awaits.

| Output | How it is produced |
|---|---|
| `benchmarks/budgets.json` | tracked |
| Performance gate results | `uv run python scripts/perf_gate.py --output gate.json` |
| Load generator JSON, gate config and default config | `uv run python -m benchmarks.loadgen --output ...` (in-process) |
| `docs/REPORT.md`, `docs/report/*.png` | `uv run python -m benchmarks.report` |
| `benchmarks/calibration/**`, `benchmarks/quality/**` | tracked |

The first action of step 1 records the sha256 of each on the tree this plan is committed
on. After every step they are produced again and compared; any difference stops the step.
The check is `uv run python scripts/protected_outputs.py`, against the sums committed in
`scripts/protected_outputs.sha256` (recorded at `d1ad230`; identical in content to the
`4a35700` outputs, with line endings normalised so any checkout gives the same sums).
The one planned exception is step 6: `report.py` says "tenant" in four sentences, and
renaming them changes `REPORT.md`. That diff must contain those words and nothing else, and
it gets new recorded sums.

## 6. Migration order

| Step | What | Pull request |
|---|---|---|
| 0 | This plan; CLAUDE.md rules; README security note | this commit |
| 1 | sha256 baseline. Migration runner. Epoch time source. `UsageStore` (in-memory, SQLite). `CostLedger`'s API kept as a façade over it, its tests passing unchanged. `test_no_content_stored.py` against the ledger and events as they are. No API change | own PR |
| 2 | Applications and API keys **together**: `ApplicationStore`, per-application plan settings, templates, `Router` on `Application`, HMAC keys, the pepper, the issuance CLI, the auth dependency; `tenant_id` leaves the body; `/v1/applications/me/usage` | **one PR**. An application id taken on trust from the body is insecure and must never be merged on its own |
| 3 | Persistence in the default server: database path and pepper from the environment, refusal to start without the pepper, the restart test, default bind confirmed as `127.0.0.1` | own PR |
| 4 | Monitoring rename (snapshot, Prometheus, events, time series); fixtures regenerated; the dashboard follows (rename only) | own PR |
| 5 | Load control, simulated live mode, demo and Playground with in-memory throwaway keys; loadgen live mode; e2e; pictures retaken | own PR |
| 6 | Docs, CLAUDE.md architecture list, README; report prose regenerated (wording-only diff) | own PR |

## 7. Riskiest steps

1. **Simulated time and database writes** (steps 1 and 3): a write that yields to a thread
   lets `SimClock` jump. Synchronous store calls, and `test_live_sim.py` must pass
   unchanged.
2. **Epoch against monotonic time** (step 1): periods and rows must be dated by the new
   epoch source, injected, with a simulated one in tests.
3. **Request identity across restarts** (step 1): `req-N` repeats after a restart, so usage
   rows need their own key.
4. **Keeping the sha256 outputs identical** while `create_app` changes (steps 2 to 5).
5. **The snapshot rename** (step 4): fixtures, `types.ts`, e2e and pictures move together.
6. **Keys in the demo** (step 5): every in-process key must stay in memory; a key in a log,
   a picture or the e2e output is a failure.
7. **One SQLite writer**: documented, not solved.

## 8. Current conflicts with the rules

**Secrets only from environment variables**
- No secret exists in the repo. Every `GATEWAY_*` setting is read in
  `create_default_app` only, which is the pattern to keep.
- `LlamaCppBackend` sends no auth header. If a model server ever needs a key, it comes from
  the environment, never from `backends.yaml`.
- `plans.yaml` must never gain keys. `.gitignore` already ignores `.env`.

**No prompts or responses stored**
- `benchmarks/quality/results.json` holds real model outputs for 100 MMLU questions. It is
  a benchmark record from a public dataset, so it is exempt and stays.
- The `PrefixCache` holds an unkeyed blake2b hash of every word of every prompt, in memory
  (`tokenizer.py`). A dictionary reverses it, so it is prompt content: it must never be
  persisted, logged or exported. Step 1's test checks this.
- Prompt and output text are held in memory while a request runs and returned to the
  caller; nothing logs them, and events carry no text. Load generator and calibration
  outputs store counts only.

**No real data**
- `benchmarks/quality/mmlu_sample.json` is public (MIT, attributed): stays.
- Calibration and quality files record the development laptop's CPU and GPU: our own
  measurement, not personal: stays.
- Demo tenants are fictional; docs hold outputs of local simulator runs.

## 9. Known gap: unauthenticated monitoring and load control

`/v1/monitor/*`, `/metrics`, `/metrics/summary`, `/v1/overload`, `/v1/control/*` and the
dashboard stay **unauthenticated in A1**; they show every application's spend and usage.
A2 and A8 protect them with roles. Until then:

- the default server binds to `127.0.0.1` (uvicorn's default; the `Makefile` and README
  commands do not change it);
- `GATEWAY_LOAD_CONTROL` stays off by default;
- **the server must not be exposed to a network until A8.** The Docker image binds
  `0.0.0.0` inside the container, as it must to be reachable at all; `docker-compose.yml`
  will publish the port on `127.0.0.1` only (step 3).
