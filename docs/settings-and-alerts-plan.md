# Settings and alerts plan: per-application settings, alert rules and their delivery

Status: **approved 2026-10-31 after changes.** The answers to Q1 to Q5 and four additions
are folded in; "Answers and changes on approval" at the end lists each one and where it went.
It needed approval because it adds rows to the permission table and step-up rules
(authentication), new kinds of stored secret (webhook URLs and signing keys, the SMTP
password, all through the vault), and two new outbound network surfaces (webhook deliveries
and the operator's SMTP server). Branch `settings-alerts/plan`, from `main` at `41f19f6` (#28
merged). No code in this PR.

**Goal.** Customers choose, per application, which gateway-level settings apply, and which
alerts they get, at what threshold, and how they are delivered. No AI agent is involved
anywhere: rules are thresholds evaluated by plain code.

**What this plan designs against** (read from `docs/scale-out-plan.md` and notes 34 to 38):

- Several instances behind a load balancer are a supported deployment: `GATEWAY_STATE=shared`
  (Postgres for durable rows, and `GATEWAY_COUNTERS=valkey` required with it, B2). SQLite stays
  one process per database (`ProcessLock`). So the combinations this plan must work on are:
  **memory** (tests, simulated live mode), **SQLite with in-memory counters**, **SQLite with
  Valkey counters** (still one process per database), and **shared** (Postgres and Valkey,
  any number of instances).
- Every store is a protocol with coroutine methods and three implementations; atomic rules are
  one store call; management invariants run in `write_transaction()` (the global advisory lock
  on Postgres, with its timeout that fails closed); request-path writes never take it.
- Windows held in Postgres use `gateway_now_ms()`; windows held in Valkey use the instance's
  `EpochSource` within `GATEWAY_STATE_MAX_SKEW_MS`. Missing is forbidden: a store that cannot
  answer is a refusal, never an admission.
- Keys are never cached (decision 16 of the scale-out plan). Modes are read once per chat
  request and again just before each hosted attempt (note 38): a stricter mode stops the next
  hosted attempt of a request in flight, a looser one applies from the next request.
- Monitoring watches and decides nothing, and each instance reports itself (decision 13).
- One chat request makes, today: on SQLite 4 store calls (gateway tier) or 12 (one organization
  provider); in shared mode 5 or 13 (note 38). This plan must not add to either.

## Part A. Settings per application

### 1. Every setting, classified

Read from the code at `41f19f6`. Three classes:

- **Gateway-level, customer-controlled:** decided by the gateway on the request path, and safe to
  let an organization or application choose within the operator's limits. These are the
  settings this plan builds.
- **Gateway-level, operator-only:** decided by the gateway, but about the platform's own safety,
  cost or secrets. Listed so they are not mistaken for customer settings; not changed here.
- **Engine-level:** belongs to a serving engine (the simulator's scheduler, or a model server
  such as llama-server or Ollama behind `PassthroughWorker`). Out of scope; listed only so the
  boundary is explicit. With real models the model server owns them and the gateway cannot set
  them at all (CLAUDE.md, backends and workers in pairs).

#### 1.1 Gateway-level, customer-controlled

Where it is today, and what it becomes. "Ceiling" is the value the operator sets for the
platform or the application's plan; the customer can only go stricter. "Org cap" says whether
an organization can cap it for all its applications. Who may change it is in section 12. A
write needs step-up, consume-on-use, **only when it loosens a setting that decides where
prompts go** (S13 to S16, and an organization cap on them that raises an effective value);
tightening, and every other setting, needs none (Q2, decision 4). "In flight" says when a
change reaches a request already running (section 5).

| # | Setting | Today | Type and range | Default | Stricter is | Ceiling | Org cap | In flight |
|---|---|---|---|---|---|---|---|---|
| S1 | `allowed_tiers` | `Plan.models`, the application's copy (note 20) | non-empty subset of the plan's models, in the plan's order | the plan's models | a smaller set | the plan's models | yes (a subset) | next request |
| S2 | `model_aliases` | none (tier names and the fixed aliases only) | map, at most 16; alias `[a-z0-9._-]{1,32}`, not a tier name or existing alias; target in the effective `allowed_tiers` | none | - (not capped; a target outside `allowed_tiers` is ignored at read and flagged in the effective view) | - | no | next request |
| S3 | `default_quality` | none: a request without `model` or `quality` is 422 | `cheapest`, `balanced`, `best`, or null | null (today's 422) | - | - | no | next request |
| S4 | `allow_downgrade_default` | per request only (`allow_downgrade`, default false) | boolean | false | - | - | no | next request |
| S5 | `requests_per_minute` | `Application.requests_per_minute`, from the plan template | integer 1 to 1,000,000, or null (no limit) | the plan copy's value | lower | the plan copy's value (none: the range only) | yes | next request (the window script already trims to a lowered limit, note 35) |
| S6 | `monthly_token_budget` | `Plan.monthly_token_budget` | integer 0 to the plan's | the plan's | lower | the plan's | yes | next reserve; a request already admitted keeps its reservation |
| S7 | `max_tokens` (largest a request may ask) | `Application.max_tokens` | integer 1 to the ceiling, or null | the plan copy's | lower | the plan copy's; with none, the tier's `context_tokens` when set (docs/37), else the range only | yes | next request |
| S8 | `default_max_tokens` (when a request names none, applied and enforced as the request's `max_tokens`, so also the reservation's completion bound; the value applied is returned in `X-Max-Tokens-Applied`, Q4 and section 5.1) | the same field as S7 does both today | integer 1 to the effective S7 | the effective S7; with no S7, today's chain (the tier's `reserve_completion_tokens`) | lower | the effective S7 | no (follows S7) | next request |
| S9 | `generate_timeout_ms` | `DEFAULT_OVERLOAD.request_timeout_ms` = 120,000, operator | integer 1,000 to the operator's | the operator's | lower | the operator's | yes | next request |
| S10 | `chat_first_token_ms` (the gateway's tier) | `DEFAULT_FIRST_TOKEN_TIMEOUT_MS` = 30,000 | integer 1,000 to the operator's, at most S11 | the operator's | lower | the operator's | yes | next request |
| S11 | `chat_total_timeout_ms` | `DEFAULT_TOTAL_TIMEOUT_MS` = 120,000 | integer 1,000 to the operator's | the operator's | lower | the operator's | yes | next request |
| S12 | `mode` (fallback mode) | migration 12, `mode.set_application`, consume-on-use | `local_only`, `local_with_hosted_fallback`, `hosted_primary_with_local_fallback` | `local_only` | stricter per `_STRICTNESS` | - | yes, today (`mode.set_organization`) | before the next hosted attempt if stricter; next request if looser (note 38) |
| S13 | `allowed_providers` | none: every enabled organization provider for the tier and role is a candidate | `all`, or a list of at most 20 provider ids of the organization | `all` | a smaller set (`[]` is none) | the organization's enabled providers | no (the organization already enables and disables its providers) | before the next hosted attempt if stricter; next request if looser |
| S14 | `max_hosted_attempts` | planned in PR 2b, not built: `GATEWAY_CHAT_MAX_HOSTED_ATTEMPTS`, default 3, range 1 to 5, operator | integer 1 to the effective ceiling | the operator's | lower | the operator's (1 to 5) | yes | before the next hosted attempt if lower; next request if higher |
| S15 | `retry_invalid_tool_json` | always one retry after invalid tool JSON (docs/33) | boolean | true | false | - | yes (an organization can force false) | before the next hosted attempt if turned off; next request if on |
| S16 | `hosted_first_token_ms` | per provider, `ProviderConfig.first_token_budget_ms`, else 10,000 | integer 1,000 to the provider's budget | the provider's | lower | each provider's own budget | yes | before the next hosted attempt if lower; next request if higher |

**Key policy is out of scope** (Q5). The key lifetime (`MAX_KEY_DAYS` 365, `DEFAULT_KEY_DAYS`
90) and the active-key cap (`KeyLimits`, 10) stay as they are. They belong with identity
steps 8 and 9 (access tokens, and the threat model and leak scan across everything), where key
issuing is reviewed as a whole. The numbers S17 to S19 are not reused.

Natural fits recorded and **not** planned for the first PRs, each with why:

| Setting | Why not now |
|---|---|
| `max_concurrent_requests` (requests of one application in flight) | Needs a new shared counter: a semaphore in Valkey with a lease per request and an expiry for a dead instance, like the breaker's probe. Its own PR, after S1 to S3 |
| Which fallback triggers apply (the five of docs/33, plus `attempt_cap`) | "Stricter" depends on the mode (fewer triggers keeps prompts local under `local_with_hosted_fallback`, keeps them hosted under `hosted_primary_...`), so no single cap direction exists. Needs its own rule first |
| A per-application usage retention shorter than the operator's | A retention pass per organization; no customer has asked |
| A per-tier `default_max_tokens` | S8 covers the common case; a per-tier map can follow without changing the storage |

Not a setting, ever: a **response cache**. It would store response text, which hard rule 7
forbids.

#### 1.2 Gateway-level, operator-only (unchanged)

`price_multiplier` and the plan templates' budgets and limits (the commercial ceiling; changed
by the operator through the plans file and the CLI); the tiers' `context_tokens` and
`reserve_completion_tokens`; the breaker's thresholds (`FAILURE_THRESHOLD` 5, `OPEN_MS`,
`MAX_OPEN_MS`, `CLOSE_AFTER`, held in Valkey per organization and provider);
`GATEWAY_PROVIDER_LOCAL_ALLOW`; the SLO targets and hysteresis of `SloMonitor` (per model,
local); the step-up window, the login limiter and the lockout; argon2 parameters; retention;
`GATEWAY_STATE_LOCK_TIMEOUT_MS`, `GATEWAY_STATE_MAX_SKEW_MS`; every secret and address. An
organization lowering the step-up window or lockout is a natural fit but changes
authentication; it is left out on purpose.

#### 1.3 Engine-level (out of scope)

The worker (`ContinuousBatchingWorker` or `BaselineWorker`), the batch cap
(`MEASURED_MAX_BATCH_SIZE`, 4), chunked prefill (`prefill_chunk_tokens`), the prefix cache
stand-in (capacity, LRU eviction, and that it is shared by every application of a model), KV
capacity, each model's queue length (`DEFAULT_MAX_QUEUE_LEN`) and its `retry_after_ms`,
`max_in_flight` for llama-server, sampling (temperature), the profiles and calibration, and the
model servers' addresses.

### 2. Levels, and the stricter one wins

Four levels, read from the most general:

1. **Platform:** the operator's settings (environment, the backends file, code defaults).
2. **Plan copy:** the application's own copy of its plan template's settings and limits (note
   20), changed only by the operator. To the customer this is part of the platform ceiling.
3. **Organization:** caps an org_admin sets for every application of the organization.
4. **Application:** values an app_admin (or org_admin) sets for one application.

One pure function, `effective_settings(platform, plan, organization, application) ->
EffectiveSettings`, in a new `gateway/settings.py`, is the only place a value is decided, as
`effective_mode` is for modes. For each setting it takes the application's value (or the
default when unset) and clamps it by every cap above it in the setting's own "stricter"
direction (a lower number, a smaller set, `false`). Each effective value carries its
**source**: `default`, `platform`, `plan`, `organization` or `application`, and, when a cap
lowered it, `capped_by`.

**At write time** an application value looser than a cap in force is refused (409
`setting_looser_than_cap`, naming the setting and the level), as an application mode looser
than its organization's is refused today (`mode_looser_than_organization`). **At read time**
the clamp still applies, because a cap can be tightened after the application's value was set:
the organization's change is always allowed (stricter is), the application's stored value is
kept, and the effective view shows it as `capped_by: organization`. Loosening a cap never
loosens any application beyond its own stored value.

The check and the write happen in one `write_transaction()` (the advisory lock on Postgres): it
is the eighth management invariant of scale-out section 3.6, next to the application-mode
bound, and gets the same two-instance test.

### 3. Storage, versions, rollback, the effective view, dry runs

**Tables** (new, no rebuild): SQLite migration 14 and Postgres schema version 3, in one PR
(scale-out decision 5), with the schema-equivalence test extended.

| Table | Columns | Notes |
|---|---|---|
| `settings_current` | `scope_type` (`organization`, `application`), `scope_id`, `version`, `document`, `schema_version`, `updated_at_ms` | One row per scope that has ever been set. Primary key (`scope_type`, `scope_id`) |
| `settings_versions` | `scope_type`, `scope_id`, `version`, `document`, `previous_document`, `changed_fields`, `changed_by_user_id` (null for the CLI), `via` (`http`, `cli`, `preset:<name>`, `revert:<n>`), `changed_at_ms` | Append-only. Primary key (`scope_type`, `scope_id`, `version`). The latest 200 versions per scope are kept; older ones are pruned by the retention pass |

The document is canonical JSON in a text column on both databases (decision 6): only the
settings that are set, sorted keys, no whitespace, validated against the catalogue before it is
written. `schema_version` lets old code refuse a document it does not know: an instance whose
code is older than the newest stored `schema_version` refuses to start, as with migrations.

**Missing, unreadable, invalid** (Q3). Three cases, on every request path (`/v1/generate`
and chat alike), decided in one function, `settings_from_rows`, that the handler calls on
what `lookup_key` returned:

| What `lookup_key` returns for a scope | What the request gets |
|---|---|
| No `settings_current` row (never set) | The platform defaults and the plan copy: today's values. Not an error |
| A row whose document fails to parse, fails validation, has a `schema_version` this instance does not know (a newer instance wrote it during a rolling deploy), or names a setting this code does not have | 503 `state_unavailable` for that application's requests. Never a looser value, never the defaults in its place: a corrupt organization cap must not lift the cap. Logged by scope, version and the failing check's name only (never the document) |
| The query fails or times out | 503 `state_unavailable`, as for a key lookup that fails today |

The organization's row and the application's row are judged separately, and either one
unreadable refuses the request. A test runs each row of this table on memory, SQLite and
Postgres, on both endpoints, and checks the answer bytes. A mutation (M37) that falls back
to the defaults on an invalid document must fail it.

**Every change is a new version** with who, when, the old and the new document, and the names
of the fields changed. `changed_at_ms` is the store clock (`gateway_now_ms()` on Postgres).

**Concurrency.** Every write names `expected_version` (the version the caller read). A
different current version answers 409 `settings_version_conflict` with the current version, and
nothing is written. Version numbers are assigned inside the write transaction, so two instances
writing at once give one success and one conflict.

**One-step revert.** `revert` with `expected_version = N` writes version `N + 1` whose document
is version `N - 1`'s. Reverting a revert goes back again. Version 1 has nothing to revert to
(409 `no_previous_version`). A revert is checked against the caps in force like any write: a
revert that would now be looser is refused (409 `setting_looser_than_cap`), listing the fields.

**The effective view** (`GET .../settings`): for every setting of section 1.1, the effective
value, its source, the application's stored value if any, every cap above it with its level,
whether it is capped, and the current version. The mode is shown from its own table (section
5). Readable by viewers (section 12).

**Dry run** (`POST .../settings/validate`): the same body as a write; runs every check
(catalogue, ranges, caps, references to providers and tiers) and returns the effective view as
it would be, the fields that would change, whether the write would need step-up (section 12),
and every refusal, writing nothing and needing no step-up. It uses the same code as the
write, up to the point of writing (a test runs both on the same inputs and compares).

**What is not cached** (decision 3). Settings are read from the database on every request, in
the same query as the key (section 5), so there is no settings cache to keep in step across
instances, and a change is seen by the next request on every instance. The only memoisation is
of parsed documents keyed by (scope, version): a version never changes once written, so this
cannot be stale. It is keyed by (scope, version, sha256 of the document text), so a row
rewritten in place by hand is parsed again and judged; only documents that passed validation
are kept. It is per instance, bounded (an LRU of 4,096 entries), and holds no secret.

**The CLI** gets `settings show|set|revert` for the operator (audited, like every CLI command),
for use when the console cannot be reached (the Valkey outage runbook of note 35).

### 4. Presets

Named bundles applied as one versioned change (`via: preset:<name>`), checked like any write:
a preset value outside a cap in force is refused, never silently clamped. A preset sets only
the fields it names; every other field keeps its value. Whether it needs step-up is decided
from the change it makes, not from its name: `fast` raising `max_hosted_attempts` from 1 to 2
loosens, so it needs step-up; applied over a value of 3 it tightens and needs none. Defined in code
(`gateway/settings.py`), listed with their exact values by `GET .../settings/presets`.

| Preset | Exact values | What it does |
|---|---|---|
| `cheap` | `allowed_tiers: [economy]`, `default_quality: cheapest`, `allow_downgrade_default: true`, `max_hosted_attempts: 1`, `retry_invalid_tool_json: false` | Serves only the smallest tier and tries at most one hosted provider. No saving is claimed: prices are placeholders (hard rule 5) |
| `fast` | `default_quality: cheapest`, `allow_downgrade_default: true`, `chat_first_token_ms: 5000`, `hosted_first_token_ms: 3000`, `max_hosted_attempts: 2`, `generate_timeout_ms: 30000`, `chat_total_timeout_ms: 30000` | Gives up sooner and falls back sooner. The numbers are chosen, not measured |
| `private` | `allowed_providers: []`, `max_hosted_attempts: 1`, `retry_invalid_tool_json: false` | No organization provider is a candidate, so no prompt leaves for a hosted provider whatever the mode, including providers added later |

`private` does not touch the mode. With no allowed provider, hosted is unreachable under any
mode, and a revert of the preset is then a revert of settings only (decision 7). `cheap` fails
on a plan without `economy` (409 `setting_looser_than_cap`, naming `allowed_tiers`).

### 5. How a change reaches running instances, and what a request in flight sees

**Reading** (Q3: yes, on every request path, `/v1/generate` included). `lookup_key` already
returns the key, its application and its organization's state in one query (`KeyLookup`). It
also returns the organization's and the application's `settings_current` rows (two left
joins, same statement, same round trip). The handler builds `EffectiveSettings` once from
them and the instance's platform values (`settings_from_rows`, section 3: a missing row is the
defaults, an unreadable or invalid one a 503), and passes it on, as it passes the mode. No
other code on the request path reads settings. The key lookup's own time is measured before
and after the join, with `benchmarks/key_checks.py` (section 20).

#### 5.1 `default_max_tokens`, applied and enforced (Q4)

When a request names no `max_tokens` (`max_output_tokens` on `/v1/generate`), the effective
S8 is **applied as the request's limit**: it is the value the reservation bounds, the value
sent to the backend or to a hosted provider, and the value generation is stopped at. A request
that names one is checked against the effective S7 as today (422 above it), and its own value
is applied.

- **Enforced, on the gateway's tiers:** the request is built with the applied value, so the
  simulator clamps its output to it (`SimulatedBackend` already takes `min` of the drawn
  length and the request's limit) and `LlamaCppBackend` sends it to the model server as
  `max_tokens`, as for a request's own value today. A test checks that no answer on these
  tiers has more completion tokens than the applied value.
- **On a hosted provider:** it is sent as the provider's limit (`max_tokens`). The gateway
  cannot count a hosted provider's tokens as they stream, so if the provider's reported usage
  exceeds it, the settle records the reported number (what was spent), the answer is not
  altered after the fact, and `gateway_max_tokens_exceeded_total{provider_type}` counts it.
  The note says so.
- **`X-Max-Tokens-Applied: <n>`** is on every answer of `/v1/generate` and
  `/v1/chat/completions`, JSON and server-sent events (a header, so before the first event),
  whether the value came from the request, from S8, or from today's chain when S8 is unset. It
  carries the application's own number and nothing else. Refusals made before a value is
  chosen (401, 403, 422, 429) do not carry it.
- **Documented** in note 39, in note 06's and note 31's endpoint descriptions (a pointer, since
  notes are snapshots), and in the README's API section. The protected outputs are bodies, not
  headers; S1 checks they stay byte-identical, and if one does not, it stops and asks.
- With S8 unset the applied value is today's, so behaviour is unchanged except for the header.

**Before each hosted attempt** the router reads both modes again today (two store calls, note
38). PR S3 replaces those two calls by one statement, `hosted_policy(organization,
application)`, returning both modes and both settings rows, and checks every hosted setting
against the request's: if the current values are stricter, the attempt is not made, exactly as
for a stricter mode (the candidate is skipped, a breaker slot it claimed is given back).

**The rule, per setting**, the existing one of note 38:

- **Stricter applies before the next hosted attempt** of a request in flight, for the settings
  that decide where a prompt goes and how many times: S12 `mode`, S13 `allowed_providers`, S14
  `max_hosted_attempts` (counted against the attempts the request already made), S15
  `retry_invalid_tool_json`, S16 `hosted_first_token_ms` (applies to the attempt about to be
  made; an attempt already waiting keeps the budget it started with).
- **Looser applies from the next request** for those same settings: it never opens an attempt
  for a request already running.
- **Every other setting applies from the next request** whichever way it moves (S1 to S11).
  The request in flight was admitted, budgeted and timed under the values it read.
  A lowered timeout does not cut a request already running.
- **An answer already streaming is not cut**, as for modes: its prompt has been sent.

**Across instances** nothing needs to be sent: every instance reads the current row on the
next request. A change committed on instance A is seen by instance B's next request (tested on
two instances, section 17).

**Store calls per request** (counted by the tests that already count them, section 20):

| Request | SQLite now | SQLite after | Shared now | Shared after |
|---|---|---|---|---|
| Chat, gateway tier | 4 | 4 | 5 | 5 |
| Chat, one organization provider | 12 | 11 (the two mode re-reads become one `hosted_policy`) | 13 | 12 |
| `/v1/generate` | 2 or 3 (`lookup_key`, the period's totals on the first request, `append`) | the same | 3 | 3 |

Settings are read once per request, inside a call the request already makes.

## Part B. Alerts

### 6. Signals

Every signal a customer sees is computed **only from that application's own requests**, or from
the organization's own providers and keys. Model-wide figures (a model's p99, its queue, KV
use, its degraded state) mix every tenant's traffic, so showing them to a customer would leak
other tenants' load: they are operator signals only (section 11.5).

**Where the numbers come from.** A new `AlertRecorder`, told by `app.py` what happened to each
request at the same points `Monitor` is told, keeps per application and per minute:
requests, outcomes by status class and error code, rejections by reason, fallbacks by reason,
hosted attempts, requests served by a hosted provider, `state_unavailable` refusals, and two
latency histograms (time to first token, total). The histogram has fixed edges at `2^(k/4)` ms
for `k = 0..71` (1 ms to about 220 s, each edge about 19% above the last) plus an overflow
bucket: histograms from several instances add up exactly, which percentiles do not. The
recorder never awaits and does no I/O on the request path (a structural test); its memory is
bounded by the applications seen in the last two minutes (section 11).

**Too few to judge.** Every signal has a minimum. Below it the signal's value is "too few to
judge": a rule in `ok` or `pending` does not fire, a rule already firing does not resolve
(section 8), and the console says "too few to judge", never 0. For a percentile the minimum is
the number of requests that puts at least five above it: `5 / (1 - p)`. For a rate it is 20
requests in the window. A window whose data is incomplete (section 11.3) is treated the same.

| Signal | Unit | Windows | Too few to judge when | Source |
|---|---|---|---|---|
| `latency_total_p50`, `_p95`, `_p99` | ms | 5, 15, 60 min | fewer than 10, 100, 500 requests | histogram |
| `latency_ttft_p50`, `_p95`, `_p99` | ms | 5, 15, 60 min | as above, counting requests with a first token | histogram |
| `error_rate` | % of requests answered 5xx (including 503 `state_unavailable` and 504) | 5, 15, 60 min | fewer than 20 requests | outcomes |
| `timeout_rate` | % answered 504 | 5, 15, 60 min | fewer than 20 | outcomes |
| `state_unavailable_rate` | % refused 503 `state_unavailable`: what a Postgres or Valkey outage looks like from the application | 5, 15 min | fewer than 20 | outcomes |
| `rejections` | count, one reason chosen in the rule: `rate_limited`, `budget_exhausted`, `queue_full` | 5, 15, 60 min | never (a count), but incomplete data is not judged | rejections |
| `rejection_rate` | % of requests, same reasons | 5, 15, 60 min | fewer than 20 | rejections |
| `fallback_rate` | % of chat requests that fell back, one reason or any (`connect_error`, `upstream_5xx`, `first_token_budget`, `circuit_open`, `invalid_tool_json`, `key_unreadable`, `attempt_cap` after PR 2b) | 15, 60 min | fewer than 20 chat requests | fallbacks |
| `hosted_share` | % of chat requests answered by a hosted provider | 15, 60 min | fewer than 20 chat requests | outcomes |
| `downgrade_rate` | % of generate requests moved to a cheaper model | 15, 60 min | fewer than 20 | outcomes |
| `requests` | count | 5, 15, 60 min | never; incomplete data is not judged | outcomes |
| `budget_used` | % of the effective `monthly_token_budget` committed this period | the period so far | the effective budget is 0 | `Budgets` (committed tokens; reservations not counted) |
| `circuit_open` | minutes the breaker of an organization provider the application may use (S13) has been open or half-open; one series per provider | now | the breaker store cannot answer | `BreakerStore.state` |
| `key_expires_in` | days until an active key of the application expires; one series per key id | now | never | the key records |
| `reservation_expired` | count of the application's budget reservations released by the sweep (shared mode only) | 60 min | never; not offered outside shared mode | the sweep |

Not offered: anything in dollars. Spend is computed from placeholder prices, and hard rule 5
forbids reporting a cost from them; budgets are in tokens.

### 7. Rules

**A rule** belongs to one application:

| Field | Type and range |
|---|---|
| `name` | 1 to 64 of `A-Z a-z 0-9 space . _ -`, no space at either end (the key label rule) |
| `signal` and its parameter | one of section 6; a parameter only where the table names one (a reason) |
| `comparison` | `>`, `>=`, `<`, `<=` |
| `threshold` | a number in the signal's unit and range (0 to 100 for %); a latency threshold is snapped to the nearest histogram edge and the snapped value is returned and stored, so the comparison is exact against bucket counts (decision 11) |
| `window` | one of the signal's windows |
| `for` | 0 to 60 min, whole minutes: how long the condition must hold before firing |
| `severity` | `info`, `warning`, `critical` |
| `channels` | 0 to 5 channel ids of the same organization (none: the incident list only) |
| `cooldown` | 5 min to 24 h, default 30 min (section 8) |
| `repeat_every` | null (default) or 1 h to 24 h: a reminder while firing and not acknowledged |
| `quiet_hours` | optional: `utc_offset` (`-12:00` to `+14:00`), `start` and `end` (`HH:MM`), `days` (a set of weekdays). Fixed offsets only, no daylight saving (decision 12) |
| `enabled` | boolean |
| `version` | assigned by the store; edits name `expected_version` |

At most 50 rules per application. A rule's channels are checked to belong to the organization
when it is written, and again when a notification is queued (a channel deleted meanwhile is
skipped, and that is recorded on the notification).

**Default rules**, created with each application (and for every existing application by the
PR that adds rules, in the migration's data step), sent to the organization's default channels
(section 9). Every number is a chosen default, not a measurement.

| Rule | On by default | Why |
|---|---|---|
| `budget_used >= 80` over the period, warning | on | Always judgeable, rarely noisy, and warns before requests are refused |
| `rejections(budget_exhausted) > 0` over 5 min, critical | on | Requests are being refused for budget: the customer must act |
| `error_rate > 5` over 5 min, for 5 min, critical | on | Catches provider failures and store outages (`state_unavailable` counts) without firing on one bad request: at least 20 requests are needed |
| `circuit_open >= 2` (minutes), warning | on | An organization provider is being skipped; only exists when the organization has providers |
| `key_expires_in <= 14` (days), warning | on | Every key issued over HTTP expires (90 days by default), and an expired key stops the application |
| `latency_total_p99 > T` | **off** | No threshold can be chosen for the customer: latency depends on the tier, the hardware and the traffic, and the gateway has no measured number to offer (hard rule 4) |
| `hosted_share > 0` over 60 min, info | off | Only meaningful under a mode with hosted fallback; useful for privacy-minded customers, who turn it on |
| `rejections(rate_limited) > 0` | off | Some clients rely on 429 as back-pressure |
| `reservation_expired > 0` | off | An instance died: the operator's problem first (section 11.5) |
| `channel_delivery_failing` (built in, section 9) | on, cannot be deleted | A failed delivery must itself be visible |

`POST .../alert-rules/restore-defaults` recreates any default rule that was deleted and resets
the edited ones to their defaults, as one audited change.

### 8. States, deduplication, cooldown and caps

**One series per (rule, series key).** The series key is empty for an application-wide signal,
the provider id for `circuit_open`, the key id for `key_expires_in`. Each series is in exactly
one state:

```
ok ---> pending ---> firing ---> acknowledged
          |            |              |
          v            v              v
          ok        resolved <--------+
```

- **ok to pending:** the condition holds at an evaluation. With `for = 0`, straight to firing.
- **pending to ok:** the condition stops holding, or the data becomes too few to judge.
  Nothing is sent.
- **pending to firing:** the condition held at every evaluation for `for`. One `firing`
  notification, subject to cooldown, quiet hours and caps.
- **firing to acknowledged:** a user with `incidents.acknowledge` acknowledges it. Reminders
  (`repeat_every`) stop; the incident still resolves on its own.
- **firing or acknowledged to resolved:** the condition is false at two consecutive evaluations
  (one minute). One `resolved` notification, if the `firing` one was sent.
- **Too few to judge while firing:** the series stays firing (or acknowledged) and is marked
  `no_data_since`. After 60 minutes with no judgeable data it resolves with reason `no_data`,
  and the notice says the rule can no longer be judged. Missing data never fires and never
  resolves quietly.
- **The rule disabled, deleted or changed** while a series is pending, firing or acknowledged:
  the series resolves with reason `rule_disabled`, `rule_deleted` or `rule_changed`, and a
  notice is sent to the rule's channels as they were before the change (section 14).

**An incident** is the stored record of one series from pending to resolved: the rule and its
version, the series key, every transition time, the value that fired, who acknowledged (a user
id), the resolve reason, `last_notified_at_ms`, and a `reopened` count. Pending is stored too,
so `for` survives the evaluator moving to another instance.

**Deduplication: one incident, one notification.**

- At most one open incident per (rule, series key): a partial unique index on the incidents
  table where the state is not `resolved`, on SQLite and Postgres alike.
- Every transition is one compare-and-set statement: `UPDATE ... SET state = $new WHERE id = $id
  AND state = $expected AND <the evaluator's lease epoch is current>` (section 11.2). The
  `firing` notification is inserted into the outbox **in the same transaction** as the
  transition to firing, so a transition made twice (two evaluators, a retried cycle) queues it
  once, and a transition rolled back queues nothing.

**Cooldown** (per rule and series). After a `firing` notification, no other `firing`
notification for that series is sent for `cooldown`. If the series resolves and fires again
within the cooldown, the resolved incident is **reopened** (`reopened + 1`) instead of a new
one being opened, and nothing is sent: the receivers were last told "resolved", and if it is
still firing when the cooldown ends, one `firing again` notification goes out. A reopened
incident that resolves again inside the cooldown sends nothing either. Compared in SQL with the
store's clock.

**Quiet hours.** `info` and `warning` notifications that fall in a rule's quiet hours are held,
not dropped: at the end of the quiet hours, one notice per incident that is still open, or that
fired and resolved meanwhile ("fired at 02:10 and resolved at 02:40, during quiet hours").
`critical` ignores quiet hours.

**Caps, so an outage cannot become a flood** (decision 9: durable, not Valkey):

| Cap | Default | Counts |
|---|---|---|
| Per rule | 6 notifications an hour | every notification of the rule: firing, resolved, reminders, weakening notices |
| Per organization | 60 notifications an hour | every notification of every rule of the organization |
| Test sends | 10 an hour per channel, 30 per organization | test sends only, in the window counters (Valkey or memory), like test-connection |

A notification over a cap is recorded as `suppressed` with its reason and not sent. When the
hour ends, the organization's default channels get **one** summary ("23 notifications of 4
rules were not sent between 10:00 and 11:00 UTC; see the incident list"), which does not count
against the caps and of which there is at most one per organization per hour. The per-rule and
per-organization counts are rows in a counter table, incremented by a conditional `UPDATE ...
WHERE sent < cap RETURNING` in the same transaction as the outbox insert, with the hour from
`gateway_now_ms()`: the same pattern as the budget reservation (scale-out 3.1), atomic on any
number of instances, and not reset by a Valkey restart.

### 9. Channels

A channel belongs to an **organization** (so one webhook can serve several applications), has a
name (the rule-name charset), a type, `enabled`, and `default` (default channels receive the
default rules and the suppression summaries). At most 20 channels per organization. An
application's admins see the organization's channels' ids, names and types, never their
targets (section 12).

**Webhook** (no third-party service needed).

- **The URL is a secret** (decision 14): many receivers put a token in the path. It is sealed in the
  vault in the **channel scope** (section 9.1), bound to (organization, channel id, `webhook_url`,
  URL origin), and only its origin is ever shown (`https://hooks.example.com/...`). No answer, log,
  audit entry or notification carries the full URL.
- **The signing key** is 32 random bytes generated by the gateway, sealed in the channel scope bound
  to (organization, channel id, `webhook_signing_key`, URL origin), and returned **once**, when the
  channel is created or the key rotated, with `Cache-Control: no-store`. It is never sent to the
  receiver, and never returned again. Rotation keeps the previous key for 24 hours and signs with
  both meanwhile.
- **The request:** `POST` with `Content-Type: application/json`, body `json-v1` (section 10),
  and headers `X-Gateway-Delivery` (the notification's id: receivers deduplicate on it, since
  delivery is at least once), `X-Gateway-Timestamp` (Unix seconds), and
  `X-Gateway-Signature: v1=<hex HMAC-SHA256(key, timestamp + "." + body)>` (one `v1=` per key
  during a rotation). Receivers should refuse a timestamp more than five minutes old.
- **The egress guard** (`gateway/egress.py`, as for provider URLs), in two places:
  - **When the URL is stored** (create, change): `check_base_url` (https only, no user info,
    no literal private address), so an obviously bad URL is refused with a field error. This
    check alone is never relied on: a name can resolve differently later.
  - **At connect time, on every connection** (test send, every delivery, every retry): the
    channel's client uses `GuardedBackend`, whose `connect_tcp` resolves the name itself,
    checks **every** resolved address with `refusal` (loopback, private, link-local, the
    metadata addresses, multicast, unspecified, reserved, and IPv4 embedded in IPv6), refuses
    if any is refused, and opens the socket **to the checked address**, never to the name
    again. So a name that resolved public when the channel was created and resolves private at
    delivery (DNS rebinding, or a record changed later) is refused at delivery with
    `egress_refused`. The client keeps no connection between deliveries (a fresh connection,
    so a fresh check, every time), and TLS is verified against the URL's host name, not the
    address, with the pinned context.
  - **No redirects followed:** the client is built with `follow_redirects=False`; any 3xx is a
    permanent failure (`http_3xx`), its `Location` is neither fetched nor stored nor logged.
  - **No proxy:** `trust_env=False`, so `HTTPS_PROXY` and the rest are ignored.
  - **No local destinations:** `GATEWAY_PROVIDER_LOCAL_ALLOW` does not apply to webhooks
    (`NO_LOCAL_DESTINATIONS` is passed, not the operator's set), so a webhook can never reach
    this machine or a private network. Tests inject the resolver and the transport.
  - **One client builder,** `webhook_client()` in `egress.py`, built like
    `organization_client` (`OrganizationTransport`, a new client per delivery, `trust_env` and
    `follow_redirects` off), is the only way a webhook is sent; an AST test fails on any other
    `httpx` client construction in the alert modules. A delivery runs under
    `egress_scope(always_hosted)`: a notification carries no prompt, so the mode's locality
    rule does not apply, and the public-address rule still does.
- **Timeouts and size:** 5 s to connect, 10 s in all; at most 4 KiB of the answer read, then
  discarded and never logged.
- **Retries:** after a connection failure, a timeout, a 5xx, a 408 or a 429 (with its
  `Retry-After`, up to an hour), at 30 s, 2 min, 8 min, 30 min and 2 h, each with up to 10%
  jitter from a seeded `random.Random`; six tries in all. Any other 4xx, an egress refusal or a
  3xx is permanent: no retry. The backoff is on the injected clock.
- **Slack-style formats** are a later webhook format (`format: slack-v1`), not now.

**Email**, through an operator-configured SMTP server.

- **Operator settings:** `GATEWAY_SMTP_HOST`, `GATEWAY_SMTP_PORT`, `GATEWAY_SMTP_TLS`
  (`starttls` or `implicit`; there is no plain option), `GATEWAY_SMTP_USERNAME`,
  `GATEWAY_SMTP_FROM`, and `GATEWAY_ALERT_EMAIL_DOMAINS` (the recipient domains allowed; empty
  means email channels cannot be created). The **SMTP password goes through the vault**, in the
  **platform scope** (section 9.1), which no organization or provider binding can open: the
  operator sets it with `scripts/manage.py set-smtp-password` (typed at a prompt, or read from
  an environment variable for automation). The server checks the certificate with the pinned
  TLS context. The SMTP server is the operator's, so the public-address rule does not apply to
  it; the TLS rule does.
- **Recipients:** 1 to 10 addresses per channel, each in an allowed domain (decision 15: without
  it, anyone with channel write access could make the gateway mail anyone), and each
  **verified** before it receives anything but its confirmation (below).
- **Verified recipients.** An address added to a channel is `unverified`. The gateway sends it
  one confirmation email: plain text, saying which organization asked to send it alerts (its
  name in quotes and its id; names already refuse control and zero-width characters, note 26,
  and the text is fixed apart from them), that nothing else will be sent unless the code is
  entered, and a 10-character code
  (base32, 50 bits, from `secrets`). The org_admin enters the code in the console
  (`POST .../alert-channels/{channel}/recipients/{recipient}/confirm`); the recipient must
  therefore have read the mail and chosen to pass the code on. Until then the address gets no
  notification, no test send, no summary. The rules:
  - the code is stored only as an HMAC under a new derived key
    (`KeyPurpose.EMAIL_CONFIRMATION`), compared in constant time, valid for 24 hours, single
    use, and void after 5 wrong entries (the recipient must be re-sent);
  - a confirmation is bound to (channel, address): the same address on another channel is
    verified again; changing an address is removing one and adding another;
  - wrong, expired, used and unknown codes, and a recipient of another organization, all get
    one answer (409 `confirmation_invalid`), byte for byte, after the same steps (one lookup,
    one HMAC, one compare against the stored or a dummy digest);
  - the code is never logged, audited, returned, or put in any event; the audit row says
    `alert_recipient_verified` with the channel and recipient ids;
  - no link is sent: the console is local only until A8, so a public confirmation endpoint
    would be a new unauthenticated surface. A link can follow with A8 (decision 26).
- **The confirmation emails are capped**, so the gateway cannot be used to mail a stranger
  repeatedly, even inside an allowed domain:

  | Cap | Default | Held in |
  |---|---|---|
  | Per address | 3 in 24 hours, across every channel and organization | a durable row keyed by an HMAC of the normalised address (its own purpose, `KeyPurpose.EMAIL_ADDRESS`), a conditional `UPDATE ... WHERE sent < cap` on the store clock, so a Valkey restart cannot release it |
  | Per channel | 10 an hour | the window counters (Valkey or memory), like test sends |
  | Per organization | 20 an hour | the window counters |
  | Per instance, all organizations | 200 an hour | memory: a backstop against a bug |

  A send over a cap answers 429 `confirmation_limited` and sends nothing; missing is
  forbidden (a counter that cannot answer refuses). The per-address digest lets the cap
  hold across organizations without storing the address twice in clear; the recipient row
  itself holds the address, which the org_admin entered and sees.
- **Client:** the standard library's `smtplib` on a thread (as passwords use `ThreadHashRunner`),
  so no dependency is added. Timeouts 10 s. A 4xx answer is retried on the webhook schedule; a
  5xx is permanent.
- **The message:** plain text only (no HTML, no images, no tracking), the subject `[gateway]
  FIRING warning: <rule> on <application>`, and a `Message-ID` derived from the notification id.

**Test send** per channel (`POST .../channels/{id}/test`): one `test` notification through the
same path, with the same guard and signature, answered with the outcome (`delivered`, or the
failure's code: `egress_refused`, `timeout`, `http_3xx`, `http_4xx`, `http_5xx`, `tls`,
`smtp_4xx`, `smtp_5xx`, `smtp_credential_unreadable`, `no_verified_recipient`), never the
receiver's answer. An email test goes to the channel's verified recipients only. Capped
(section 8).

**A failed delivery is visible:**

- every notification has a state (`queued`, `sending`, `delivered`, `failed`, `suppressed`),
  its attempts, and the last failure's code, shown on the incident and on the channel;
- every channel has `last_delivered_at`, `last_failed_at` and `consecutive_failures`;
- the built-in rule `channel_delivery_failing` (`consecutive_failures >= 3` on any channel of
  the organization, warning) is on for every organization, sends to every default channel
  **except the failing one**, and shows in the incident list whatever happens to its own
  delivery;
- Prometheus: `gateway_alert_deliveries_total{channel_type, result}` per instance (no
  organization label, no target);
- an `alert_delivery_failed` event on the instance's `EventBus`, carrying the channel id and
  the code.

**Channel secrets** (the webhook URL, the signing key, the SMTP password) are decrypted only for
one use, by `Vault.lease()`, and only in two places: the webhook signer and request builder,
and the SMTP login. `Secret.reveal()` is allowed today only in `adapters/http.py`'s
`credential_headers` (an AST test); that test's allowlist gains exactly these two call sites,
and nothing else (decision 16). Leased values go to the `Redactor` like provider keys.

#### 9.1 Vault scopes

Today every sealed value is an organization's provider key, with associated data
`vault-v1|organization|provider|type|origin|key_id` under the master key itself. The vault
gains a **scope**, so that a value sealed for one purpose cannot be opened as another, by
binding or by key:

| Scope | Holds | Associated data (fields joined by a bar) | Encryption key |
|---|---|---|---|
| `provider` (today's, unchanged) | organization provider keys | `vault-v1`, organization, provider, type, origin, key id | the master key, as today (no reseal of existing rows) |
| `channel` | webhook URLs, webhook signing keys | `vault-channel-v1`, organization, channel, kind (`webhook_url`, `webhook_signing_key`), origin, key id | the master key |
| `platform` | the SMTP password (and the operator alert webhook's secrets, A6) | `vault-platform-v1`, `smtp`, host and port, username, key id | a **subkey**, `HMAC-SHA256(master, "gateway vault scope platform")`, never used for anything else |

- `Binding` becomes three frozen dataclasses (`ProviderBinding`, today's `Binding` renamed
  with an alias; `ChannelBinding`; `PlatformBinding`), and `seal`, `open` and `lease` take
  their union. The associated data's first field names the scope, and the `platform` scope's
  values are fields no organization can name (`smtp`, not an organization id), so no binding
  of one scope equals one of another.
- The platform scope also uses its own key, so even a bug that built the right associated
  data under the wrong scope could not open it. Its label differs from the key id label
  (`gateway vault key id`), and a test checks the two derived values differ.
- **Stored apart:** platform secrets in a `platform_secrets` table (migration and Postgres
  schema in A5), with no organization column, read only by `SmtpSender`; no provider or channel
  store query reads that table (a test greps the SQL of every store).
- **Rotation:** `rotate-vault-key` reseals all three scopes, the platform subkey derived from
  the new master. A platform secret whose master is not loaded is `key_unreadable`: email
  deliveries fail with `smtp_credential_unreadable`, shown as any failed delivery is
  (section 9) and as an operator signal.
- **The test that no organization or provider key can decrypt it** (A5): seal an SMTP
  password; then try to open its ciphertext with every `ProviderBinding` and `ChannelBinding`
  of two test organizations (including one whose provider id, type and origin are set to
  `smtp`, the SMTP host and its port, and one whose organization id is `smtp`), under the
  master key and under the platform subkey; copy a provider key's ciphertext and a channel
  secret's into the `platform_secrets` row and open them as the SMTP password; and copy the
  SMTP ciphertext into a provider key row and run test-connection. Every attempt must fail
  with `VaultIntegrityError` (or `key_unreadable` for the row copied to a provider), and the
  provider path must never send it. The control: the same calls with the right
  `PlatformBinding` open it.
- The two protections are each tested on their own, since either alone still makes the test
  above pass: **associated data apart** (the bytes of every scope's associated data differ
  from every other scope's, for the crafted field values above) and **key apart** (the
  platform ciphertext, opened directly with `AESGCM(master)` and the right associated data,
  bypassing `Vault`, gives `InvalidTag`). Mutations: M41 (the platform scope's prefix dropped
  and its fields laid out like a provider binding) fails the first; M42 (the subkey replaced
  by the master) fails the second; M43 (both at once) fails the end-to-end test.

### 10. What an alert contains, and must not

**`json-v1`** (the email carries the same fields as text):

```json
{"version": 1, "delivery_id": "ntf_...", "kind": "firing", "sent_at": "2026-11-02T10:00:00Z",
 "organization": {"id": "org_..."}, "application": {"id": "app_...", "name": "Acme"},
 "rule": {"id": "rul_...", "name": "Errors", "signal": "error_rate", "comparison": ">",
          "threshold": 5, "unit": "%", "window_s": 300, "for_s": 300, "severity": "critical"},
 "incident": {"id": "inc_...", "state": "firing", "series": null, "value": 7.5,
              "fired_at": "...", "resolved_at": null, "resolve_reason": null},
 "console_path": "/v1/admin/orgs/org_.../applications/app_.../incidents/inc_..."}
```

`kind` is `firing`, `firing_again`, `reminder`, `resolved`, `quiet_hours_summary`,
`rule_weakened`, `channel_removed`, `suppressed_summary` or `test`. (The recipient
confirmation is not a notification: it has its own fixed text and carries no incident.) `series` is a provider id or a key id where the
signal has series.

**Built from an allowlist** at the moment the notification is queued (`AlertPayload`, a frozen
dataclass with only these fields, like `AuditEvent`), and stored in the outbox as built; a
delivery sends exactly what was stored. Never in it: prompt or response text, token ids or
hashed tokens, API keys or their HMACs, provider keys, webhook URLs or signing keys, provider
base URLs, usernames, email addresses (other than the email's own `To`), IP addresses, session
ids, or a user who acknowledged (the console shows that, the notification does not). Names are
the customer's own labels (the application and rule names); the gateway adds no personal data.

### 11. Evaluation

#### 11.1 Where the data lives, on each combination

| State | memory | SQLite, memory counters | SQLite, Valkey counters | shared (Postgres and Valkey) |
|---|---|---|---|---|
| Settings and their versions | memory | SQLite | SQLite | Postgres |
| Rules, channels, incidents, outbox, cap counters, lease | memory | SQLite | SQLite | Postgres |
| Per-minute rollups (section 6) | the recorder itself | the recorder itself | Valkey | Valkey |
| Test-send and per-channel, per-organization confirmation caps | memory | memory | Valkey | Valkey |
| Per-address confirmation cap, recipients and their code digests | memory | SQLite | SQLite | Postgres |
| Evaluator heartbeat (in the lease row), and each instance's last read of it | memory | SQLite; memory | SQLite; memory | Postgres; memory |
| Channel secrets (channel scope) and the SMTP password (platform scope, `platform_secrets`) | vault, in memory | vault rows in SQLite | vault rows in SQLite | vault rows in Postgres |

**Rollups in Valkey** (`AlertRollups`, a new counter protocol with a memory and a Valkey
implementation): every 10 s each instance adds its closed deltas with one pipelined
`HINCRBY` per (application, minute), key names an HMAC under a new derived purpose
(`KeyPurpose.ALERT_ROLLUPS`, note 35's rule), values counts only, expiry two hours. Off the
request path entirely. If Valkey cannot take a flush, the deltas are kept in the instance for up
to 10 minutes, then dropped and counted (`gateway_alert_rollups_dropped_total`); the evaluator
sees those minutes as incomplete. With SQLite and Valkey, separate deployments sharing one
Valkey would add up an application id they both have, as today's rate limit would (note 35):
the operator must not share a Valkey between deployments.

#### 11.2 Which instance evaluates, and how often

**One evaluator at a time,** chosen by a lease row in the durable store: an instance takes it
with a conditional `UPDATE ... WHERE expires_at_ms < gateway_now_ms() OR holder = $me`, holds it
for 30 s and renews every 10 s. Taking it over increments an **epoch**, and every incident
transition and outbox insert includes `AND epoch = $mine` (a fencing token), so an evaluator
that paused past its lease writes nothing. On SQLite the one process takes it; on Postgres any
instance can. A test runs three instances and kills the holder.

**Every 30 s** (on the injected clock) the evaluator:

1. reads the enabled rules (one query; paged);
2. reads the rollups of the minutes it does not yet hold (completed minutes are immutable; it
   keeps the last hour in memory, so a cycle reads about one new minute per application with
   rules);
3. reads the durable signals in one query each: committed tokens for the applications with
   `budget_used` rules, key expiries, breaker states (one `BreakerStore.state` per provider in
   use);
4. computes each series' value, or "too few to judge";
5. makes the transitions of section 8, each its own transaction, with its outbox rows.

**A minute is complete** 25 s after it ends: the 10 s flush interval, twice the skew bound, and
a margin. Until then it is not judged. So a condition is noticed at most about 55 s after its
minute ends, plus `for`.

**Delivery** runs on every instance: a worker claims queued notifications with `FOR UPDATE SKIP
LOCKED` and a 60 s claim (Postgres; on SQLite the one process), at most 4 at a time per
instance, and records the outcome. A claim whose instance died expires and is claimed again,
so delivery is at least once, and `X-Gateway-Delivery` lets the receiver drop a repeat.

#### 11.3 Incomplete data

A minute counts as incomplete when Valkey restarted during it or after it (a new `run_id`,
section 3.9 of the scale-out plan: the restart already publishes `state_store_restarted`), when
an instance reported dropped deltas for it, or when it is older than the evaluator's memory
and could not be read. A window with an incomplete minute is "too few to judge". An instance
that dies loses at most its last 10 s of deltas without saying so; a `requests < N` rule can
therefore read low by up to that, and the note will say so.

#### 11.4 Failures

| What fails | What happens | How it is seen |
|---|---|---|
| The evaluator's instance dies | the lease expires within 30 s and another instance takes it; `for` continues from the stored pending time | the heartbeat (11.6) goes `stale` on every instance after 90 s if no instance takes over; the console shows "evaluated 40 s ago" |
| The durable store is down | no cycle runs (it cannot read rules or write transitions); nothing fires, nothing resolves; queued notifications wait | the console cannot load either; the heartbeat is `unknown` on every instance (11.6) |
| Valkey is down | rollups are kept up to 10 min, then dropped; those minutes are incomplete; durable signals are still evaluated | "too few to judge" on the affected rules; `gateway_alert_rollups_dropped_total`; the operator's signal |
| A delivery fails | retried (section 9), then `failed` | the notification's state, the channel's health, `channel_delivery_failing`, a metric, an event |
| Every channel of an organization fails | `channel_delivery_failing` cannot be delivered either | it stays in the incident list; the console shows a banner on every page of the organization |
| A cycle takes longer than 30 s | the next starts when it ends; no overlap on one instance | `gateway_alert_evaluation_ms`, the lag in the console |

The evaluator never blocks a request: it runs beside the server, like the reservation sweep,
on the request pool's transactions (never the management write lock), and a slow cycle delays
only alerts.

#### 11.5 Operator signals (a later PR)

Signals about the platform itself go to the **operator**, not to customers, and must work when
the stores do not: Postgres or Valkey unavailable, a Valkey restart, clock skew past the bound,
a settle kept waiting, write-lock timeouts, audit rows dropped, a model degraded (the SLO
monitor), a model's queue full or deep, KV use, the evaluator heartbeat (11.6, in health and
metrics from A3; a notification from A6), the delivery failure rate
across organizations, and a stored settings document that fails validation. Each instance
evaluates them on its **own** state, with no shared store, and sends to channels given in the
environment (`GATEWAY_OPERATOR_ALERT_WEBHOOK_URL` and `GATEWAY_OPERATOR_ALERT_WEBHOOK_KEY`,
both secrets; or `GATEWAY_OPERATOR_ALERT_EMAIL` through the same SMTP), from a bounded
in-memory queue. With N instances an outage can give N notices, one per instance, each naming
its instance; that is stated, and capped per instance. The Prometheus endpoint already exposes
most of these for an operator who runs Prometheus.

#### 11.6 The evaluator heartbeat (an operator signal, in A3)

Alerts that silently stop being evaluated are worse than no alerts: nothing fires and
nothing says so. So the evaluator's liveness is an operator signal of its own, built with the
evaluator in A3 rather than with the other operator signals in A6.

- **The heartbeat** is written in the lease row (section 11.2): `last_cycle_completed_at_ms`
  (the store clock) and `last_cycle_epoch`, set in the same conditional statement that renews
  the lease at the end of each completed cycle, so a fenced evaluator cannot write it either.
- **Every instance reads it** with the lease attempt it already makes every 10 s (the same
  statement returns the row), and keeps the last answer in memory. Health and metrics read
  that memory: they make no store call.
- **States**, judged against the store clock read with the row:

  | State | When |
  |---|---|
  | `ok` | a cycle completed within the last 90 s (three cycles) |
  | `stale` | the last completed cycle is older than 90 s, or none has ever completed 120 s after an instance started (no evaluator is running, or every cycle fails) |
  | `unknown` | this instance could not read the lease row for 30 s (the durable store is down): never `ok` |
  | `disabled` | no evaluator is configured on this deployment (simulated live mode, decision 18) |

- **In health:** `/healthz` gains one field, `alert_evaluator: "ok" | "stale" | "unknown" |
  "disabled"`, and nothing else (no times, no instance names, no epoch: `/healthz` is public).
  `/readyz` does **not** depend on it: a stale evaluator does not stop an instance from
  serving requests, and taking every instance out of the load balancer for it would turn an
  alerting fault into an outage (decision 27).
- **In metrics** (Prometheus `/metrics`, local only, each instance reporting itself):
  `gateway_alert_evaluator_state{state}` (1 for the current state),
  `gateway_alert_evaluator_last_cycle_age_seconds` (from the row; absent while `unknown`, never
  0), `gateway_alert_evaluator_holder` (1 on the instance holding the lease),
  `gateway_alert_evaluation_ms` and `gateway_alert_evaluation_failures_total`. The monitoring
  snapshot (`/v1/monitor/snapshot`, docs/15) carries the same with `available: false` when
  `unknown`.
- **An event** `alert_evaluator_stale` (and `alert_evaluator_recovered`) on the instance's
  `EventBus` and a log line at each change of state, so an operator without Prometheus sees it.
  In A6 it also becomes an operator notification, sent by each instance on its own state.
- **The console** shows "alerts not evaluated since 10:14 UTC" as a banner on every alert
  page while the state is `stale` or `unknown`.
- Monitoring decides nothing: the heartbeat is read, never used to choose which instance
  evaluates (the lease does that).

## Part C. Security and permissions

### 12. New actions in the permission table

Added to `permissions.py`, the generated `docs/permission-matrix.md`, and the documented table
in note 28 (parsed by a test, so they change together). platform_admin gets nothing inside an
application, as today.

| Action | Level | Allowed | Step-up |
|---|---|---|---|
| `settings.read` | application | org_admin, app_admin, viewer (own application, or application in own organization) | no |
| `settings.write` (set, revert, preset, when the change loosens nothing of S13 to S16) | application | org_admin, app_admin | no |
| `settings.loosen_egress` (the same writes, when the change loosens S13, S14, S15 or S16) | application | org_admin, app_admin | consume |
| `org_settings.read` | organization | org_admin, viewer of the organization | no |
| `org_settings.write` (set, revert, when no application's effective S13 to S16 becomes looser) | organization | org_admin | no |
| `org_settings.loosen_egress` (a cap raised so that some application's effective S14, S15 or S16 becomes looser) | organization | org_admin | consume |
| `alert_rules.read` (rules, the application's view of channel ids, names and types) | application | org_admin, app_admin, viewer | no |
| `alert_rules.write` (create, edit, enable, disable, delete, restore defaults) | application | org_admin, app_admin | no (audited; weakening sends a notice, section 14) |
| `alert_channels.read` (targets shown as origins and domains) | organization | org_admin | no |
| `alert_channels.write` (create, change its target, recipients, type or `default`, rotate the signing key) | organization | org_admin | consume |
| `alert_channels.disable` (disable, delete, remove a recipient) | organization | org_admin | no (audited; sends a notice, section 14) |
| `alert_channels.confirm_recipient` (enter a code, re-send a confirmation) | organization | org_admin | no (capped, one answer for every refusal) |
| `alert_channels.test` | organization | org_admin | no (capped) |
| `incidents.read` (application) | application | org_admin, app_admin, viewer | no |
| `incidents.read_organization` | organization | org_admin, viewer of the organization | no |
| `incidents.acknowledge` | application | org_admin, app_admin | no |

**Step-up, as answered (Q2):** only for loosening a setting that decides where prompts go,
and for creating or changing an alert channel; tightening needs none; consume-on-use is kept
for both. Key policy is out of scope (section 1.1), so it adds no row.

- **Loosening is decided from the change, not the endpoint.** One pure function,
  `loosens_egress(before, after)`, compares the effective S13 to S16 of every application the
  write affects, before and after, using the same "stricter" directions as
  `effective_settings`. For an application write that is one application; for an
  organization cap, every application of the organization (one query in the same
  transaction). Any field looser on any application means the write is
  `*.loosen_egress`.
- **How `requires` takes it:** the handler first runs the write as a dry run (the validate
  path, no write), which returns `loosens_egress` for the version named by
  `expected_version`; then it declares `settings.write` or `settings.loosen_egress` and
  `requires` claims the step-up window for the second. The write itself runs with the same
  `expected_version` in `write_transaction()` and recomputes `loosens_egress` inside it; if
  the answer differs from the dry run's (a cap moved between the two), the write is refused
  with 409 `settings_version_conflict` and the claimed window is given back. So a loosening
  write can never commit without a consumed step-up, on any number of instances.
- **The route walk** declares both actions on each settings write route; the synthetic-route
  AST check of note 29 is extended so a route that can reach `loosen_egress` must declare it.
- **Alert rules need no step-up.** Weakening a rule is the threat of section 14; it is
  mitigated by the `rule_weakened` notice to the rule's channels as they were, the audit, and
  `restore-defaults`, not by a password prompt.
- **Disabling or deleting a channel** narrows where data goes, so it is not "creating or
  changing" and needs no step-up; it is audited, and the organization's other default
  channels get a `channel_removed` notice (decision 25).
- **The modes are unchanged:** `mode.set_application` and `mode.set_organization` keep their
  merged rule (consume-on-use for every change, tightening included). Bringing them in line
  with this answer would change the merged security path of notes 33 and 38, so it is left for
  its own PR if wanted (decision 5).

Acknowledging only stops reminders and is easy to undo by the condition itself; viewers do not
get it (decision 17). `settings.validate` is not a separate action: the dry run is declared
with `settings.write` and claims no step-up, because it writes nothing.

**Every endpoint goes through `requires(action, resolver)`.** Resolvers answer a rule, incident,
channel, notification or settings version outside the path's organization or application as
missing: 403 `forbidden`, byte for byte the same as a forbidden one. A rule may name only its
own organization's channels; a channel id from another organization in a rule body is refused
as `invalid_request` with the same body as a channel id that does not exist.

### 13. Audit

Through `AuditEvent`'s allowlist, new actions: `settings_changed`, `settings_reverted`,
`settings_preset_applied` (scope, version, the names of the fields changed; the values are in the
version table, not the audit row), `alert_rule_created`, `alert_rule_changed`,
`alert_rule_enabled`, `alert_rule_disabled`, `alert_rule_deleted`, `alert_rules_restored`,
`alert_channel_created`, `alert_channel_changed`, `alert_channel_deleted`,
`alert_channel_key_rotated`, `alert_channel_disabled`, `alert_channel_tested` (the outcome
code), `alert_recipient_added`, `alert_recipient_confirmation_sent`, `alert_recipient_verified`,
`alert_recipient_removed` (channel and recipient ids only, never the address or the code),
`incident_acknowledged`, `smtp_password_set` (CLI). A settings write records whether it was a
`loosen_egress` write and that the step-up was consumed. Every refusal (409, 429) is
`management_refused` with its reason, as today. Never in an audit row: a URL, an address, a signing
key, a threshold's history (the rule row has it), or what was typed.

### 14. Threat-model additions

- **Someone with write access silences an alert** by raising its threshold, lengthening its
  window or `for`, adding quiet hours, removing its channels, lowering its severity, or
  disabling or deleting it. Mitigations (no step-up, as answered for Q2): it is audited with
  who and when; any
  change that makes an enabled rule fire less (each field has a direction, like settings) sends
  a `rule_weakened` notice to the rule's channels **as they were before the change**, naming the
  rule and the fields, never the user; a firing incident resolves as `rule_changed` with a
  notice; the effective view of rules marks default rules that were changed; `restore-defaults`
  undoes it in one step. Not prevented: an org_admin who deletes every channel, then weakens
  rules. The audit log records it.
- **A webhook pointed at an internal address** (cloud metadata, a database, this machine):
  refused at create by the URL check, and, the check that matters, at connect time on every
  connection: every resolved address checked, the socket opened to the checked address, a
  fresh connection per delivery. Covered variants: a name that resolves public at create and
  private later (rebinding, or a changed record); a name with several addresses of which one is
  private (refused, not "pick the public one"); IPv4 embedded in IPv6; a literal address; a
  trailing dot.
- **A redirect to an internal address**, from a public receiver that answers 3xx: not
  followed (`follow_redirects=False`); the 3xx is a permanent failure and `Location` is never
  fetched or logged. A receiver that turns hostile later can only fail deliveries.
- **A proxy in the environment** that would carry the request past the guard: ignored
  (`trust_env=False`).
- **The gateway used to send mail to anyone:** recipients only in the operator's allowed
  domains, 10 per channel, and only **verified** recipients get notifications or test sends.
- **The confirmation email itself as the spam vector** (an org_admin adds a stranger's address
  in an allowed domain and has it re-sent): the confirmation is plain, says who asked and that
  nothing follows without the code; it is capped per address (3 a day across every
  organization, in a durable row), per channel, per organization and per instance.
- **Guessing a confirmation code** instead of receiving it: 50 bits, 24 hours, void after 5
  wrong entries, the confirm endpoint capped; every refusal gives one answer after the same
  steps. Proving control of a mailbox by guessing is not feasible inside those limits.
- **A confirmation code leaked** through logs, audit or answers: it is stored only as an HMAC
  and is in the channel-secrets leak scan as a planted value.
- **The SMTP password opened by an organization's path**: it lives in the vault's platform
  scope (its own associated data and its own subkey) and its own table; no organization or
  provider binding can open it, and no organization store reads its table (section 9.1, with
  the test).
- **A channel secret opened as a provider key, or the reverse** (a ciphertext copied between
  rows): the channel scope's associated data differs from the provider scope's.
- **Alerts silently not evaluated** (the evaluator dead, fenced forever, or failing every
  cycle): the heartbeat goes `stale` within 90 s on every instance, in `/healthz`, in metrics,
  as an event and a log line, and on the console's alert pages (section 11.6).
- **The heartbeat as an oracle** on the public `/healthz`: it gives one word, no times, no
  instance, no organization.
- **A loosening disguised as a tightening** (a preset, a revert, or an organization cap raised
  so applications' stored values come back into force): decided from the effective values
  before and after, inside the write transaction, not from the endpoint or the field named.
- **The webhook as an amplifier** against a third party: capped per rule and organization; at
  most six tries per notification; one connection at a time per notification.
- **Forged notifications to a customer's receiver:** signed with a key only the gateway and the
  customer hold, with a timestamp against replay, and a delivery id for duplicates.
- **A webhook URL with a token in it** is a secret: sealed, never shown in full, never logged.
- **Settings as a denial of service:** someone with `settings.write` can set
  `requests_per_minute` to 1 or `monthly_token_budget` to 0. Tightening needs no step-up (Q2),
  so a stolen session can do this without the password; it is audited and versioned, and one
  revert undoes it (the revert loosens rate and budget, which are not egress settings, so it
  needs no step-up either).
- **Settings as a privacy downgrade:** widening `allowed_providers` or raising
  `max_hosted_attempts` sends prompts further. Consume-on-use step-up for any loosening of S13
  to S16, the caps, and the rule that a looser value never reaches a request in flight.
- **A corrupt or newer settings row read as "no settings"**: refused with 503, never the
  defaults (section 3), since the defaults may be looser than a corrupt cap.
- **`X-Max-Tokens-Applied` as a leak:** it carries only the application's own applied value,
  which the caller could compute from its own settings.
- **Cross-tenant leakage through signals:** customers see only their own application's
  numbers; model-wide numbers are the operator's (section 6).
- **Alert content as a leak:** built from an allowlist; the leak scan (section 19).
- **Different ceilings on different instances** (a rolling deploy, a mistake): each instance
  clamps by its own ceilings, so a request can see a different ceiling on different instances.
  Each instance writes a sha256 of its ceilings to a durable row at start and reports a mismatch
  as an event, a log line and an operator signal (decision 19); it does not refuse, so a rolling
  change of a ceiling stays possible.
- **The evaluator as a single writer:** a stale evaluator is fenced by the lease epoch; a
  partial unique index keeps one open incident per series whatever happens.
- **Two new outbound surfaces:** webhook deliveries (the egress guard) and the SMTP server (the
  operator's, TLS required, certificate checked).

## Part D. API and console

### 15. Endpoints

All under `/v1/admin/orgs/{org}`, local only until A8 like every admin endpoint, `Cache-Control:
no-store`, CSRF and the Origin check on every state change, lists paginated with `limit` (1 to
100, default 50) and `after` as in note 30. Writes take `expected_version` where the resource
has one.

| Method and path | Action | Step-up | Success |
|---|---|---|---|
| `GET /settings` (the organization's caps, effective) | `org_settings.read` | | 200 |
| `PUT /settings` (`expected_version`, `set`, `unset`) | `org_settings.write`, or `org_settings.loosen_egress` | consume, only if it loosens | 200 |
| `POST /settings/validate` | `org_settings.write` | | 200 |
| `POST /settings/revert` (`expected_version`) | `org_settings.write`, or `org_settings.loosen_egress` | consume, only if it loosens | 200 |
| `GET /settings/versions` | `org_settings.read` | | 200 |
| `GET /settings/versions/{n}` | `org_settings.read` | | 200 |
| `GET /settings/presets` | `org_settings.read` | | 200 |
| `GET /applications/{app}/settings` (the effective view) | `settings.read` | | 200 |
| `PUT /applications/{app}/settings` (`expected_version`, `set`, `unset`) | `settings.write`, or `settings.loosen_egress` | consume, only if it loosens | 200 |
| `POST /applications/{app}/settings/validate` | `settings.write` | | 200 |
| `POST /applications/{app}/settings/revert` (`expected_version`) | `settings.write`, or `settings.loosen_egress` | consume, only if it loosens | 200 |
| `POST /applications/{app}/settings/presets/{name}` (`expected_version`) | `settings.write`, or `settings.loosen_egress` | consume, only if it loosens | 200 |
| `GET /applications/{app}/settings/versions` | `settings.read` | | 200 |
| `GET /applications/{app}/settings/versions/{n}` | `settings.read` | | 200 |
| `GET /applications/{app}/alert-rules` | `alert_rules.read` | | 200 |
| `POST /applications/{app}/alert-rules` | `alert_rules.write` | | 201 |
| `GET /applications/{app}/alert-rules/{rule}` | `alert_rules.read` | | 200 |
| `PUT /applications/{app}/alert-rules/{rule}` (`expected_version`) | `alert_rules.write` | | 200 |
| `POST /applications/{app}/alert-rules/{rule}/enable` | `alert_rules.write` | | 200 |
| `POST /applications/{app}/alert-rules/{rule}/disable` | `alert_rules.write` | | 200 |
| `DELETE /applications/{app}/alert-rules/{rule}` | `alert_rules.write` | | 200 |
| `POST /applications/{app}/alert-rules/validate` (a rule body; also what it would have been over the last hour, from the rollups) | `alert_rules.read` | | 200 |
| `POST /applications/{app}/alert-rules/restore-defaults` | `alert_rules.write` | | 200 |
| `GET /applications/{app}/alert-channels` (ids, names, types) | `alert_rules.read` | | 200 |
| `GET /applications/{app}/incidents` (`state`) | `incidents.read` | | 200 |
| `GET /applications/{app}/incidents/{incident}` (with its notifications) | `incidents.read` | | 200 |
| `POST /applications/{app}/incidents/{incident}/acknowledge` | `incidents.acknowledge` | | 200 |
| `GET /incidents` (`state`, `application`) | `incidents.read_organization` | | 200 |
| `GET /alert-channels` | `alert_channels.read` | | 200 |
| `POST /alert-channels` (`type`, `name`, `url` or `recipients`, `default`) | `alert_channels.write` | consume | 201, with the signing key once for a webhook |
| `GET /alert-channels/{channel}` (health included) | `alert_channels.read` | | 200 |
| `PATCH /alert-channels/{channel}` | `alert_channels.write` | consume | 200 |
| `POST /alert-channels/{channel}/disable` | `alert_channels.disable` | | 200 |
| `DELETE /alert-channels/{channel}` | `alert_channels.disable` | | 200 |
| `POST /alert-channels/{channel}/recipients` (an address) | `alert_channels.write` | consume | 201, the recipient `unverified`; the confirmation sent (capped) |
| `POST /alert-channels/{channel}/recipients/{recipient}/resend` | `alert_channels.confirm_recipient` | | 200 (capped: 429 `confirmation_limited`) |
| `POST /alert-channels/{channel}/recipients/{recipient}/confirm` (the code) | `alert_channels.confirm_recipient` | | 200; any refusal 409 `confirmation_invalid` |
| `DELETE /alert-channels/{channel}/recipients/{recipient}` | `alert_channels.disable` | | 200 |
| `POST /alert-channels/{channel}/rotate-key` | `alert_channels.write` | consume | 200, the new key once |
| `POST /alert-channels/{channel}/test` | `alert_channels.test` | | 200, the outcome code |
| `GET /alert-channels/{channel}/deliveries` | `alert_channels.read` | | 200 |

The mode endpoints (`PUT /mode`, `PUT /applications/{app}/mode`) stay as they are; the effective
settings view shows the mode from them. Status codes: those of note 30, plus 409
`settings_version_conflict`, `setting_looser_than_cap`, `no_previous_version`,
`rule_version_conflict`, `rule_limit`, `channel_limit`, `incident_not_open`,
`confirmation_invalid`; 422 `invalid_request` (with the field); 429 `test_send_limited`,
`confirmation_limited`; 503 `state_unavailable`. A step-up refusal is note 29's, unchanged.

### 16. Console screens (a later PR; frontend only)

The dashboard today only reads the monitoring API and has no logic about the gateway (CLAUDE.md).
These screens are a separate admin console, local only until A8; the rule that components do
not compute still holds, and its data transforms are pure functions with tests.

- **Settings** (per application): every setting with its effective value, its source and its
  caps; the edit form, which calls validate on every change and shows the result before
  "Save" asks for the password; the presets with their exact values; the version history with
  the fields each version changed, and "Revert to the previous version".
- **Organization settings:** the caps, the applications each cap currently limits, history.
- **Alert rules** (per application): the list with each rule's state and last value (or "too
  few to judge"); the editor, with the validate preview "over the last hour this would have
  fired at 10:14 and resolved at 10:31"; defaults marked, changed defaults marked.
- **Channels** (organization): the list with health; create (the signing key shown once, with
  a copy button and a warning); email recipients with their state (`unverified`, `verified`),
  the code entry and re-send; test send and its outcome; deliveries.
- **Evaluator state:** the banner of section 11.6 on every alert page while the heartbeat is
  `stale` or `unknown`.
- **Incidents:** open and recent, per application and for the organization; each with its
  timeline, its notifications and their states, and Acknowledge.

## Part E. Tests, mutations, measurement

### 17. Tests, per rule

Every store test runs on memory, SQLite and Postgres through the existing parametrised fixture;
Valkey tests are marked `valkey`, Postgres ones `postgres`.

- **Settings:** each setting's range and type refused outside it; `effective_settings` against
  a table of cases (every level, every source, a cap tightened after a value was set); a write
  looser than a cap refused at each level; a cap tightened with applications above it (stored
  values kept, effective clamped); versions (who, when, old and new); revert, revert of a revert,
  revert refused by a cap, version 1; `expected_version` conflict, and the same on two instances
  at once (one success); validate equals write, field for field, without writing; presets with
  their exact values, refused by a cap; a document with an unknown `schema_version` refuses the
  start; every row of section 3's missing, unreadable and invalid table on both endpoints,
  on memory, SQLite and Postgres (a missing row gives today's values; each unreadable or
  invalid case 503 with the same bytes, and never a looser value: a test sets an organization
  cap, corrupts the organization's row, and checks that no request is served at all, rather
  than served without the cap).
- **`default_max_tokens`:** applied as the request's limit when none is named, on
  `/v1/generate` and chat, JSON and SSE; the reservation bound equals it; no answer on the
  gateway's tiers exceeds it; a hosted provider over it is counted, not altered;
  `X-Max-Tokens-Applied` on every answer that chose a value (from the request, from S8, from
  today's chain), absent on early refusals.
- **Step-up only when loosening:** for each of S13 to S16, a loosening write without step-up
  refused, the same with step-up accepted and the window consumed (a second loosening
  refused); a tightening write accepted with no step-up and the window untouched; a preset and
  a revert judged by their effect (both ways); an organization cap raised so an application's
  stored value comes back into force needs step-up, one that affects no application does not;
  a cap moved between the dry run and the write (two instances) refused with the window given
  back; a write to S1 to S11 never asks for step-up.
- **Propagation:** a change on instance A seen by B's next request; each setting of section 5 in
  flight (stricter before the next hosted attempt, looser from the next request), with the same
  scripted providers as note 38's tests; an answer already streaming not cut.
- **Store calls:** the counts of section 5, by the tests that already count them.
- **Recorder:** never awaits (structural), bounded memory, and two instances' rollups adding up
  to exactly one instance's for the same requests; histogram edges and snapping.
- **Signals:** each signal's value from scripted rollups; each minimum, at one below and at the
  minimum; incomplete minutes (a scripted restart, dropped deltas); no model-wide number reaches
  an application signal.
- **States:** every transition; `for`; too few while pending and while firing; `no_data`
  after 60 min; rule disabled, deleted, changed while firing; acknowledge; reminders.
- **Dedupe, cooldown, caps:** two evaluators on two instances making the same transition (one
  notification); a retried cycle (one); flapping inside the cooldown (one notification, then
  "firing again" after it); quiet hours held and summarised, critical not held; per-rule and
  per-organization caps on two instances at once (exactly the cap sent, the rest suppressed,
  one summary).
- **Evaluation:** the lease on three instances, the holder killed, `for` continuing; a fenced
  stale evaluator writing nothing; durable store down (no transition); Valkey down (incomplete,
  durable signals still judged).
- **Delivery:** at least once with the claim expired; retries on the schedule, on the injected
  clock; no retry on a permanent failure; signature verified by an independent implementation
  in the test; rotation signing with both; the answer never logged.
- **SSRF, at connect time:** every private, loopback, link-local, metadata, multicast,
  reserved and embedded-IPv4 address refused at create (literal) and at connect (resolved), on
  test send, first delivery and each retry; a name that resolves public at create and private
  at delivery refused at delivery, with the socket never opened (the injected backend records
  every `connect_tcp`); a name resolving to one public and one private address refused; the
  socket opened to the checked address, not the name (the injected backend sees an address);
  a second delivery to the same channel resolves again (no pooled connection); a 301, 302,
  307 and 308 to a private address and to a public one each a permanent `http_3xx` with no
  second request made; `HTTPS_PROXY` set in the environment and ignored; http refused.
- **Email:** an in-process SMTP test server written in the test suite on asyncio streams (no
  dependency): STARTTLS required, plain refused; a domain outside the allowlist refused; 4xx
  retried, 5xx not.
- **Verified recipients:** an unverified address gets only its confirmation (no notification,
  no test send, no summary: the test server's mailbox is checked); the right code verifies;
  wrong, expired, used, voided and another organization's codes give one answer, byte for
  byte, after the same steps (the structural test of note 27's refusals, applied here, and the
  early-return mutation M47); five wrong entries void it; the per-address cap holds across two
  organizations and two instances and survives a Valkey restart; the per-channel and
  per-organization caps; a counter that cannot answer refuses the send.
- **Vault scopes:** the test of section 9.1 that no organization or provider binding opens
  the SMTP password, the associated-data-apart and key-apart tests, the channel scope against
  the provider scope both ways, and rotation resealing all three scopes.
- **Heartbeat:** `ok`, `stale` (the evaluator killed with no taker; every cycle failing; a
  fenced evaluator renewing nothing), `unknown` (the durable store down) and `disabled`, on
  three instances, each reporting the same state from its own read; `/healthz` carries the one
  word and nothing else; `/readyz` unaffected; the metrics and the snapshot (`available:
  false` when `unknown`, the age absent, never 0); the events at each change; health and
  metrics make no store call (counted).
- **Permissions:** the generated matrix covers every new action; **every new endpoint** gets the
  cross-organization test (a resource of `south` through `/orgs/north/...`: 403, the same bytes
  as a missing one) and the cross-application test inside one organization for app_admin;
  step-up and consume-on-use where the table says, and the window given back when nothing
  changed; viewers refused every write.
- **The route walk** (`app.routes` of every app variant) still finds every route declared.

### 18. Mutation checks

Each is made, the named tests shown failing, then restored byte for byte and the tests shown
passing, in the PR that builds the rule. If one cannot be made to fail ten runs out of ten, that
is a stop-and-ask.

| # | Rule | Mutation | Must fail |
|---|---|---|---|
| M1 | Ceiling at write | skip the cap check for application writes | the looser-than-cap tests at each level |
| M2 | Ceiling at read | `effective_settings` returns the stored value without the clamp | the cap-tightened-later test |
| M3 | The cap check under the write lock | check outside `write_transaction()` | the two-instance cap-and-write test |
| M4 | Version conflict | ignore `expected_version` | the conflict test, on one and on two instances |
| M5 | One-step revert | revert to version 1 instead of `N - 1` | the revert-of-a-revert test |
| M6 | Validate writes nothing | validate commits | the validate test |
| M7 | Read once | read settings again inside the handler | the store-call count tests |
| M8 | Stricter before the next hosted attempt | drop the settings check in `hosted_policy` | the in-flight tests for S13 to S16 |
| M9 | Looser never opens an attempt | apply looser values at the re-read | the looser-in-flight tests |
| M10 | Step-up when loosening | `loosens_egress` returns false for one of S13 to S16 (one mutation per setting) | that setting's loosening-without-step-up test |
| M11 | Consume-on-use on loosening writes and channel writes | `consume` off for `settings.loosen_egress`, `org_settings.loosen_egress` and `alert_channels.write`, each | the second-write-refused tests |
| M12 | An organization cap raised is a loosening | `loosens_egress` compares the caps only, not the applications' effective values | the cap-raised-brings-back-a-stored-value test |
| M13 | Cross-organization, per resolver | each resolver returns a resource of another organization (one mutation per resolver) | that resolver's endpoints' cross-organization tests |
| M14 | Missing equals forbidden | a missing rule answers 404 | the byte-identical tests |
| M15 | Channel of another organization in a rule | skip the check | the rule-channel test |
| M16 | SSRF at create | skip the guard at create | the create tests |
| M17 | SSRF at delivery | skip the guard at delivery | the rebinding test |
| M18 | Connect to the checked address | resolve again at connect | the rebinding test |
| M19 | No redirects | follow redirects | the redirect test |
| M20 | Dedupe, by the index | drop the partial unique index | the two-evaluator test |
| M21 | Dedupe, by compare-and-set | transition without `AND state = $expected` | the retried-cycle test |
| M22 | Outbox in the transition's transaction | queue after the commit | the rolled-back-transition test |
| M23 | Fencing | drop `AND epoch = $mine` | the stale-evaluator test |
| M24 | Cooldown | ignore `last_notified_at_ms` | the flapping test |
| M25 | Caps | check the count in Python after a `SELECT` | the two-instance caps test |
| M26 | One summary | the summary counted as exempt more than once | the one-summary test |
| M27 | Too few never fires | judge below the minimum | the minimum tests |
| M28 | Missing data never resolves | resolve on no data at once | the no-data test |
| M29 | Rule weakened notice | skip it | the weakening tests |
| M30 | Quiet hours | send `warning` during quiet hours | the quiet-hours test |
| M31 | Payload allowlist | add the webhook URL (or a username) to the payload | the leak scan (section 19) |
| M32 | Signature | sign the body without the timestamp | the independent verifier test |
| M33 | Permanent failures not retried | retry a 400 | the retry tests |
| M34 | Recorder never awaits | an `await` in the recorder | the structural test |
| M35 | No model-wide number in application signals | feed the model's histogram to an application signal | the tenant-isolation test |
| M36 | Reveal only at the two new sites | a third `reveal()` | the AST test |
| M37 | Unreadable settings never loosen | `settings_from_rows` returns the defaults for an invalid document | the corrupt-organization-cap test and the 503 table tests |
| M38 | Loosening decided inside the write | skip the recomputation of `loosens_egress` in `write_transaction()` | the cap-moved-between-dry-run-and-write test |
| M39 | `default_max_tokens` applied | the reservation and the backend use today's chain when S8 is set | the reservation-bound and no-answer-exceeds tests |
| M40 | `X-Max-Tokens-Applied` | omit it on SSE answers | the header test |
| M41 | Vault scopes: associated data apart | the platform prefix dropped, fields laid out like a provider binding | the associated-data-apart test |
| M42 | Vault scopes: key apart | the platform subkey replaced by the master key | the key-apart test |
| M43 | No organization or provider key opens the SMTP password | M41 and M42 together | the end-to-end test of section 9.1 |
| M44 | Every resolved address checked | check only the first address | the mixed-address test |
| M45 | A fresh check per delivery | reuse one pooled client across deliveries | the second-delivery-resolves-again test |
| M46 | No proxy | `trust_env=True` on the webhook client | the proxy test |
| M47 | One answer for every confirmation refusal | return early for an unknown recipient, before the HMAC | the structural same-steps test |
| M48 | Only verified recipients | skip the verified filter on notifications (and, separately, on test sends) | the mailbox tests |
| M49 | Per-address confirmation cap, durable and atomic | the cap in a Valkey window instead (and, separately, checked in Python after a `SELECT`) | the Valkey-restart test; the two-instance cap test |
| M50 | The code never stored or logged in clear | store the code instead of its HMAC; log it on send | the channel-secrets leak scan |
| M51 | The heartbeat is fenced | write `last_cycle_completed_at_ms` in its own statement, without the epoch | the fenced-evaluator-goes-stale test |
| M52 | No heartbeat is `ok` without a fresh read | keep the last `ok` while the lease row cannot be read | the `unknown` test |
| M53 | `/healthz` says one word | add the age to `/healthz` | the health-body test |

### 19. Leak scans

Two scans, each with a planted control that must be found:

- **Channel secrets:** plant a webhook URL with a token in its path, a signing key, an SMTP password
  and an email confirmation code (the code generator injected so the planted value is the one sent);
  create, test, deliver (succeeding, failing, redirected), rotate, add a recipient, re-send, confirm
  (wrong, then right), delete; then scan every stored row (SQLite, Postgres), every Valkey key and
  value, every log record, every audit row, every event, the Prometheus output, `/healthz`, every
  admin answer (except the one that returns the new signing key), every outgoing payload, and every
  mail the test SMTP server received (except the confirmation itself, which must carry the code).
  The scan must find the planted values in a deliberately unredacted copy, and nowhere else.
- **Alert contents:** run requests with a planted prompt, response, API key, provider key and
  username through to a firing and a resolved notification; scan every payload in the outbox,
  every delivered body (webhook and email) and the rollups. Nothing planted may appear; the
  control is a payload with the planted prompt added on purpose, which the scan must flag.

### 20. The perf gate, the protected outputs, and added latency

- **The perf gate** runs `create_app(registry, clock)` with no applications: no settings, no
  recorder, no evaluator. Expected unchanged; any movement is an unexplained change and a stop.
- **The protected outputs** (`--include-images`) are produced on `SimClock` with in-memory
  state and default settings, which are today's values. Expected byte-identical in every PR. The
  load generator's applications have no settings stored, so they take the defaults.
- **Simulated live mode** gets settings in memory and no evaluator in these PRs (decision 18);
  its determinism test is unchanged.
- **Added latency on the request path, measured:** `benchmarks/request_store_calls.py` (note
  38) and `benchmarks/key_checks.py` (note 20), three runs before and three after, on this
  machine with the hardware recorded, on SQLite and on Postgres, in the PR that adds the join
  (S1) and in the one that adds `hosted_policy` (S3). A new `benchmarks/alert_recorder.py` times
  one recorder call. No number is written before it is run.
- **Not allowed to regress:** the store-call counts of section 5 are pinned by tests (CI fails
  on one more call). The time is not gated in CI (scale-out decision 15); if the after range's
  lowest p50 is above the before range's highest p50, the PR stops and asks before going on.
- **Evaluation cost** is measured beside the server, not on the request path: one cycle's time
  for 1, 100 and 1,000 applications with the default rules, on SQLite and on Postgres and
  Valkey.

## Part F. Order of work

Each PR is small, reviewable alone, starts from the previous branch once its CI is green, and
must be merged in order. PR 2b of the scale-out plan (the operator's cap on hosted attempts)
must be merged before S3.

| PR | Branch | What | Behaviour change |
|---|---|---|---|
| S0 | `settings-alerts/plan` | This plan | None |
| S1 | `settings-alerts/s1` | Migration 14 and Postgres schema 3 (settings tables); `SettingsStore` on memory, SQLite and Postgres; `effective_settings`; the join into `lookup_key` on every request path, `/v1/generate` included; `settings_from_rows` (a missing row the defaults, unreadable or invalid a 503, with its table of tests); settings S1 to S11 applied on the request path; S8 applied and enforced as the request's limit, with `X-Max-Tokens-Applied` and its documentation; the CLI; the key lookup and `request_store_calls` measured before and after; tests; note 39 | The `X-Max-Tokens-Applied` header on every answer; otherwise none until a setting is set: defaults are today's values |
| S2 | `settings-alerts/s2` | The settings endpoints (application and organization), the permission rows (with `*.loosen_egress` declared; no setting of S1 to S11 reaches it, so no write needs step-up yet), versions, revert, validate, presets, audit, the cross-organization tests | New endpoints |
| S3 | `settings-alerts/s3` | S12 shown, S13 to S16 added; `loosens_egress` and step-up on loosening (M10 to M12, M38); `hosted_policy` (one statement for both modes and both settings rows); the in-flight rule for each; measured before and after | One store call fewer per hosted attempt; the in-flight rule for the new settings; loosening S13 to S16 asks for the password |
| A1 | `settings-alerts/a1` | `AlertRecorder`, `AlertRollups` (memory, Valkey), the histogram, the signals and their minimums; no rules; the recorder measured | None (nothing reads it but tests) |
| A2 | `settings-alerts/a2` | Rules: tables, endpoints, permissions, defaults (with the data step for existing applications), validate with the last-hour preview | New endpoints; nothing fires yet |
| A3 | `settings-alerts/a3` | The evaluator: the lease and its epoch, the heartbeat in `/healthz`, metrics, the snapshot and events (section 11.6), the states, incidents, dedupe, cooldown, quiet hours, acknowledge, the incident endpoints; no channels (the incident list only) | Incidents appear; `/healthz` gains `alert_evaluator` |
| A4 | `settings-alerts/a4` | Webhook channels: the vault's channel scope, the signing key, `webhook_client` with the connect-time check and no redirects, the outbox, retries, the caps and the summary, test send, delivery visibility, `channel_delivery_failing`, both leak scans | Notifications leave the machine |
| A5 | `settings-alerts/a5` | Email through SMTP: the operator settings, the vault's platform scope and `platform_secrets` for the password (with the test that no organization or provider key opens it), STARTTLS, the domain allowlist, verified recipients and the confirmation caps, the in-process test server; the leak scan extended to the code | Email, to verified recipients only |
| A6 | `settings-alerts/a6` | Operator signals and channels (section 11.5) | Operator notices |
| C1 | `settings-alerts/c1` | Console: settings and organization settings | Frontend |
| C2 | `settings-alerts/c2` | Console: alert rules, channels, incidents | Frontend |

S1 to S3 and A1 can go in either order after S0; A2 needs S2's permission work; A4 needs A3.

**S1, as agreed when it started (2026-10-31).** Exactly the row above. `hosted_policy` and
the in-flight rule stay in S3, after scale-out PR 2b. The mutations S1 makes are M1 to M5,
M7, M37, M39 and M40; M6 (validate writes nothing) goes to S2 with the validate endpoint.
The four levels of section 2. Every new test runs under the suite's existing
`gateway_test_timeout_s`. Where S1 departs from a detail of this plan, note 39 says so and
the PR lists it as a numbered decision.

**Dependencies.** None planned. Webhooks use `httpx` (already a dependency, through the egress
guard); signatures `hmac` and `hashlib`; email `smtplib`; the test SMTP server is written in the
suite. Quiet hours use fixed offsets so that `tzdata` is not needed on Windows (decision 12). If
any PR finds it needs one, it stops and runs the provenance check first.

## Decisions not in the request

Numbered so they can be objected to.

1. **A third class, operator-only gateway settings** (section 1.2), so that "gateway-level" does
   not read as "customer-controllable" for the pepper, the vault, retention or the breaker.
2. **The application's plan copy counts as the platform ceiling** for the customer: the
   customer can lower `requests_per_minute`, the budget and `max_tokens`, never raise them; the
   operator raises them through the CLI. (Approved, Q1.)
3. **No settings cache.** Settings are read in the key's own query on every request; the only
   memoisation is of immutable versions. Nothing is shared to invalidate, and a change reaches
   every instance's next request. (Approved, Q3, with the missing, unreadable and invalid rule.)
4. **Step-up, consume-on-use, only for loosening S13 to S16 and for creating or changing a
   channel** (answered, Q2; it replaces "every settings write"). Loosening is judged from the
   effective values of every affected application, recomputed inside the write transaction,
   so a per-field split cannot be bypassed by a preset, a revert or an organization cap.
5. **Modes stay in their own table and endpoints**, and keep their merged rule (consume-on-use
   for every change, tightening included), although Q2's answer would let tightening go
   without. The effective view shows them; changing them would rewrite the merged security
   path of notes 33 and 38. A separate PR can align them if wanted.
6. **Settings documents are canonical JSON in a text column** on both databases, so
   `migrate-to-postgres` and back compare them byte for byte, with a `schema_version` so older
   code refuses newer documents.
7. **The `private` preset clears `allowed_providers`** instead of setting the mode, so it is
   revertible as settings alone and also covers providers added later.
8. **200 versions kept per scope.**
9. **Notification caps are durable counter rows**, not Valkey windows, so a Valkey restart
   cannot release a flood; test-send caps are Valkey windows like test-connection.
10. **Rollups in Valkey off the request path**, flushed every 10 s, instead of a Valkey call
    per request (about 1 ms each, note 35) or Postgres writes per instance.
11. **Latency thresholds are snapped to histogram edges**, so "p99 above T" is exact on merged
    histograms; the snapped value is shown.
12. **Quiet hours in fixed UTC offsets,** without daylight saving, to avoid `tzdata`.
13. **One evaluator at a time by a lease with an epoch** in the durable store, not a Valkey
    lock: the transitions it fences are in the same store.
14. **The whole webhook URL is a secret,** sealed and shown only as its origin.
15. **Email recipients only in operator-allowed domains.**
16. **`reveal()` allowed at exactly two new call sites,** the webhook signer and the SMTP login.
    The confirmation code is not a vault secret: it is generated, mailed once and kept only as
    an HMAC.
17. **Viewers can read settings, rules and incidents but not acknowledge.**
18. **No evaluator in simulated live mode** in these PRs: it stays deterministic and sends
    nothing.
19. **Different ceilings on different instances are reported, not refused,** so a rolling
    change of a ceiling is possible.
20. **No dollars in any alert,** while prices are placeholders.
21. **Model-wide signals are operator-only,** to keep tenants' traffic apart.
22. **The `max_concurrent_requests` setting waits** for its own PR and shared counter.
23. **Default rules are real rows** created with each application, not virtual, so they can be
    edited and their changes audited like any rule.

Added on approval (2026-10-31), with the four additions and the answers:

24. **Webhook deliveries are outside the mode's locality rule** (`egress_scope(always_hosted)`):
    a notification carries no prompt, and an organization under `local_only` still wants its
    alerts. The public-address rule, the connect-time check and no redirects all still apply.
25. **Disabling or deleting a channel, or removing a recipient, needs no step-up:** it narrows
    where data goes. It is audited and sends a `channel_removed` notice to the organization's
    other default channels.
26. **Recipients are verified by a code entered in the console, not a link:** the console is
    local only until A8, and a public confirmation endpoint would be a new unauthenticated
    surface. A link can be added with A8.
27. **`/readyz` does not depend on the heartbeat,** and `/healthz` carries one word: a stale
    evaluator should not take instances out of the load balancer, and `/healthz` is public.
28. **The platform scope has its own subkey as well as its own associated data;** the
    provider scope keeps the master key, so no existing row is resealed.
29. **The per-address confirmation cap is durable and spans organizations,** keyed by an HMAC
    of the normalised address, so two organizations cannot each send a stranger three a day,
    and a Valkey restart releases nothing.
30. **A hosted provider that exceeds the applied `max_tokens` is counted, not cut:** the
    gateway cannot count a hosted provider's tokens as they stream.
31. **The heartbeat is built with the evaluator (A3),** not with the other operator signals
    (A6), so alerts never run without it.

## Answers and changes on approval (2026-10-31)

| Item | Answer | Where it went |
|---|---|---|
| Q1, the plan copy as ceiling | Yes | Section 1.1 (ceilings), decision 2 |
| Q2, step-up | Only for loosening where-prompts-go settings and for creating or changing alert channels (and key policy, which is out of scope); tightening needs none; consume-on-use kept | Section 1.1, section 4 (presets by effect), section 12 (`*.loosen_egress`, `loosens_egress`), section 14, M10 to M12 and M38, decisions 4, 5 and 25 |
| Q3, settings in `lookup_key` | Yes, on every request path including `/v1/generate`; a missing row is the platform defaults, unreadable or invalid a 503, never a looser value, with a test; the key lookup measured | Section 3 (the table), section 5, section 17, section 20, M37, PR S1 |
| Q4, `default_max_tokens` | Applied and enforced as the request's `max_tokens`, with `X-Max-Tokens-Applied`, documented | Section 5.1, section 17, M39 and M40, PR S1, decision 30 |
| Q5, key policy | Out of scope; with identity steps 8 and 9 | Section 1.1 (S17 to S19 removed) |
| Addition 1, verified recipients with a cap on confirmations | Added | Section 9 (email), section 12, section 13, section 14, section 15, section 17, section 19, M47 to M50, PR A5, decisions 26 and 29 |
| Addition 2, the resolved-address check at connect time, no redirects | Added | Section 9 (webhook), section 14, section 17, M16 to M19 and M44 to M46, PR A4, decision 24 |
| Addition 3, the evaluator heartbeat in health and metrics | Added | Section 11.6, section 11.4, section 14, section 17, M51 to M53, PR A3, decisions 27 and 31 |
| Addition 4, a distinct vault scope for the SMTP password, with the test | Added | Section 9.1, section 14, section 17, M41 to M43, PR A5, decision 28 |

Nothing blocks S1.

Questions for later PRs, not blocking S1: whether `incidents.acknowledge` should go to viewers
(decision 17); the default rules' thresholds; the caps' numbers (6 and 60 an hour); whether to
add `tzdata` for daylight saving; the confirmation caps' numbers (3 a day per address, 10
and 20 an hour); whether the mode endpoints should also drop step-up for tightening (decision
5).

## Not built

Everything in this plan. Also not planned: a Slack format, SMS or paging services, alert
routing trees or escalation policies, anomaly detection or any learned threshold, alerts across
organizations, a status page, and per-application isolation of the prefix cache stand-in.
