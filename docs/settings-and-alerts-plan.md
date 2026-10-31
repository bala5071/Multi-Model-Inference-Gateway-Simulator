# Settings and alerts plan: per-application settings, alert rules and their delivery

Status: **proposed 2026-10-31, not approved.** It needs approval before anything is built: it
adds rows to the permission table and step-up rules (authentication), two new kinds of stored
secret (webhook URLs and signing keys, the SMTP password, all through the vault), and two new
outbound network surfaces (webhook deliveries and the operator's SMTP server). Branch
`settings-alerts/plan`, from `main` at `41f19f6` (#28 merged). No code in this PR.

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
an organization can cap it for all its applications. "Who" is the role allowed to change it
(section 12); every write needs step-up, consume-on-use (decision 4). "In flight" says when a
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
| S8 | `default_max_tokens` (used when a request names none; also the reservation's completion bound, Q2) | the same field as S7 does both today | integer 1 to the effective S7 | the effective S7; with no S7, today's chain (the tier's `reserve_completion_tokens`) | lower | the effective S7 | no (follows S7) | next request |
| S9 | `generate_timeout_ms` | `DEFAULT_OVERLOAD.request_timeout_ms` = 120,000, operator | integer 1,000 to the operator's | the operator's | lower | the operator's | yes | next request |
| S10 | `chat_first_token_ms` (the gateway's tier) | `DEFAULT_FIRST_TOKEN_TIMEOUT_MS` = 30,000 | integer 1,000 to the operator's, at most S11 | the operator's | lower | the operator's | yes | next request |
| S11 | `chat_total_timeout_ms` | `DEFAULT_TOTAL_TIMEOUT_MS` = 120,000 | integer 1,000 to the operator's | the operator's | lower | the operator's | yes | next request |
| S12 | `mode` (fallback mode) | migration 12, `mode.set_application`, consume-on-use | `local_only`, `local_with_hosted_fallback`, `hosted_primary_with_local_fallback` | `local_only` | stricter per `_STRICTNESS` | - | yes, today (`mode.set_organization`) | before the next hosted attempt if stricter; next request if looser (note 38) |
| S13 | `allowed_providers` | none: every enabled organization provider for the tier and role is a candidate | `all`, or a list of at most 20 provider ids of the organization | `all` | a smaller set (`[]` is none) | the organization's enabled providers | no (the organization already enables and disables its providers) | before the next hosted attempt if stricter; next request if looser |
| S14 | `max_hosted_attempts` | planned in PR 2b, not built: `GATEWAY_CHAT_MAX_HOSTED_ATTEMPTS`, default 3, range 1 to 5, operator | integer 1 to the effective ceiling | the operator's | lower | the operator's (1 to 5) | yes | before the next hosted attempt if lower; next request if higher |
| S15 | `retry_invalid_tool_json` | always one retry after invalid tool JSON (docs/33) | boolean | true | false | - | yes (an organization can force false) | before the next hosted attempt if turned off; next request if on |
| S16 | `hosted_first_token_ms` | per provider, `ProviderConfig.first_token_budget_ms`, else 10,000 | integer 1,000 to the provider's budget | the provider's | lower | each provider's own budget | yes | before the next hosted attempt if lower; next request if higher |
| S17 | `key_max_lifetime_days` | `MAX_KEY_DAYS` = 365 on the HTTP path | integer 1 to 365 | 365 | lower | 365 | yes | next key issued or rotated; existing keys keep their expiry |
| S18 | `key_default_lifetime_days` | `DEFAULT_KEY_DAYS` = 90 | integer 1 to the effective S17 | 90 | - | S17 | no (follows S17) | next key issued |
| S19 | `max_active_keys` | 10 on the HTTP path (`KeyLimits`) | integer 1 to 10 | 10 | lower | 10 | yes | next key issued; never revokes a key |

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
A stored document that fails validation at read (which should be impossible) refuses that
application's requests with 503 `state_unavailable`, logged by scope and version only: missing
is forbidden.

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
it would be, the fields that would change, and every refusal, writing nothing and needing no
step-up. It uses the same code as the write, up to the point of writing (a test runs both on
the same inputs and compares).

**What is not cached** (decision 3). Settings are read from the database on every request, in
the same query as the key (section 5), so there is no settings cache to keep in step across
instances, and a change is seen by the next request on every instance. The only memoisation is
of parsed documents keyed by (scope, version): a version never changes once written, so this
cannot be stale. It is per instance, bounded (an LRU of 4,096 entries), and holds no secret.

**The CLI** gets `settings show|set|revert` for the operator (audited, like every CLI command),
for use when the console cannot be reached (the Valkey outage runbook of note 35).

### 4. Presets

Named bundles applied as one versioned change (`via: preset:<name>`), checked like any write:
a preset value outside a cap in force is refused, never silently clamped. A preset sets only
the fields it names; every other field keeps its value. Defined in code
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

**Reading.** `lookup_key` already returns the key, its application and its organization's
state in one query (`KeyLookup`). It also returns the organization's and the application's
`settings_current` rows (two left joins, same statement, same round trip). The handler builds
`EffectiveSettings` once from them and the instance's platform values, and passes it on, as it
passes the mode. No other code on the request path reads settings.

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
- **Every other setting applies from the next request** whichever way it moves (S1 to S11, S17
  to S19). The request in flight was admitted, budgeted and timed under the values it read.
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

- **The URL is a secret** (decision 14): many receivers put a token in the path. It is sealed
  in the vault, bound to (organization, `webhook`, channel id, URL origin), and only its origin
  is ever shown (`https://hooks.example.com/...`). No answer, log, audit entry or notification
  carries the full URL.
- **The signing key** is 32 random bytes generated by the gateway, sealed in the vault with the
  same binding, and returned **once**, when the channel is created or the key rotated, with
  `Cache-Control: no-store`. It never travels to the gateway, and is never returned again.
  Rotation keeps the previous key for 24 hours and signs with both meanwhile.
- **The request:** `POST` with `Content-Type: application/json`, body `json-v1` (section 10),
  and headers `X-Gateway-Delivery` (the notification's id: receivers deduplicate on it, since
  delivery is at least once), `X-Gateway-Timestamp` (Unix seconds), and
  `X-Gateway-Signature: v1=<hex HMAC-SHA256(key, timestamp + "." + body)>` (one `v1=` per key
  during a rotation). Receivers should refuse a timestamp more than five minutes old.
- **The egress guard** (`gateway/egress.py`, as for provider URLs): https only; every resolved
  address public; the socket connects to the address that was checked (no DNS rebinding); no
  redirects (a 3xx is a failure); no proxy; the pinned TLS context. **No local destinations:**
  `GATEWAY_PROVIDER_LOCAL_ALLOW` does not apply to webhooks, so a webhook can never reach this
  machine or a private network (tests inject the resolver and transport). Checked when the
  channel is created or changed, at every test send, and at every delivery.
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
  means email channels cannot be created). The **SMTP password goes through the vault**: the
  operator sets it with `scripts/manage.py set-smtp-password` (typed at a prompt, or read from an
  environment variable for automation), sealed under a platform binding that no organization id
  can take. The server checks the certificate with the pinned TLS context. The SMTP server is
  the operator's, so the public-address rule does not apply to it; the TLS rule does.
- **Recipients:** 1 to 10 addresses per channel, each in an allowed domain (decision 15: without
  it, anyone with channel write access could make the gateway mail anyone).
- **Client:** the standard library's `smtplib` on a thread (as passwords use `ThreadHashRunner`),
  so no dependency is added. Timeouts 10 s. A 4xx answer is retried on the webhook schedule; a
  5xx is permanent.
- **The message:** plain text only (no HTML, no images, no tracking), the subject `[gateway]
  FIRING warning: <rule> on <application>`, and a `Message-ID` derived from the notification id.

**Test send** per channel (`POST .../channels/{id}/test`): one `test` notification through the
same path, with the same guard and signature, answered with the outcome (`delivered`, or the
failure's code: `egress_refused`, `timeout`, `http_4xx`, `http_5xx`, `tls`, `smtp_4xx`,
`smtp_5xx`), never the receiver's answer. Capped (section 8).

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
`rule_weakened`, `suppressed_summary` or `test`. `series` is a provider id or a key id where the
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
| Test-send caps | memory | memory | Valkey | Valkey |
| Channel secrets | vault, in memory | vault rows in SQLite | vault rows in SQLite | vault rows in Postgres |

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
| The evaluator's instance dies | the lease expires within 30 s and another instance takes it; `for` continues from the stored pending time | `alert_evaluator.last_cycle_at` in the console ("evaluated 40 s ago") |
| The durable store is down | no cycle runs (it cannot read rules or write transitions); nothing fires, nothing resolves; queued notifications wait | the console cannot load either; the operator's signal (11.5) |
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
monitor), a model's queue full or deep, KV use, the evaluator's lag, the delivery failure rate
across organizations, and a stored settings document that fails validation. Each instance
evaluates them on its **own** state, with no shared store, and sends to channels given in the
environment (`GATEWAY_OPERATOR_ALERT_WEBHOOK_URL` and `GATEWAY_OPERATOR_ALERT_WEBHOOK_KEY`,
both secrets; or `GATEWAY_OPERATOR_ALERT_EMAIL` through the same SMTP), from a bounded
in-memory queue. With N instances an outage can give N notices, one per instance, each naming
its instance; that is stated, and capped per instance. The Prometheus endpoint already exposes
most of these for an operator who runs Prometheus.

## Part C. Security and permissions

### 12. New actions in the permission table

Added to `permissions.py`, the generated `docs/permission-matrix.md`, and the documented table
in note 28 (parsed by a test, so they change together). platform_admin gets nothing inside an
application, as today.

| Action | Level | Allowed | Step-up |
|---|---|---|---|
| `settings.read` | application | org_admin, app_admin, viewer (own application, or application in own organization) | no |
| `settings.write` (set, revert, preset) | application | org_admin, app_admin | consume |
| `org_settings.read` | organization | org_admin, viewer of the organization | no |
| `org_settings.write` (set, revert) | organization | org_admin | consume |
| `alert_rules.read` (rules, the application's view of channel ids, names and types) | application | org_admin, app_admin, viewer | no |
| `alert_rules.write` (create, edit, enable, disable, delete, restore defaults) | application | org_admin, app_admin | yes |
| `alert_channels.read` (targets shown as origins and domains) | organization | org_admin | no |
| `alert_channels.write` (create, edit, delete, rotate the signing key) | organization | org_admin | consume |
| `alert_channels.test` | organization | org_admin | no (capped) |
| `incidents.read` (application) | application | org_admin, app_admin, viewer | no |
| `incidents.read_organization` | organization | org_admin, viewer of the organization | no |
| `incidents.acknowledge` | application | org_admin, app_admin | no |

Why these choices: a settings write can widen where prompts go or how much an application may
spend, so every write is consume-on-use, like the mode it sits beside (decision 4). Weakening an
alert rule weakens a defense, like `users.unlock`, so it needs step-up; not consume, because
editing several rules in one sitting is normal. A channel decides where the gateway sends data,
like a provider key, so it is consume-on-use and org_admin only. Acknowledging only stops
reminders and is easy to undo by the condition itself; viewers do not get it (decision 17).
`settings.validate` is not a separate action: the dry run is declared with `settings.write`
but claims no step-up, because it writes nothing.

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
`alert_channel_key_rotated`, `alert_channel_tested` (the outcome code), `incident_acknowledged`,
`smtp_password_set` (CLI). Every refusal (409, 429) is `management_refused` with its reason, as
today. Never in an audit row: a URL, an address, a signing key, a threshold's history (the rule
row has it), or what was typed.

### 14. Threat-model additions

- **Someone with write access silences an alert** by raising its threshold, lengthening its
  window or `for`, adding quiet hours, removing its channels, lowering its severity, or
  disabling or deleting it. Mitigations: it needs step-up; it is audited with who and when; any
  change that makes an enabled rule fire less (each field has a direction, like settings) sends
  a `rule_weakened` notice to the rule's channels **as they were before the change**, naming the
  rule and the fields, never the user; a firing incident resolves as `rule_changed` with a
  notice; the effective view of rules marks default rules that were changed; `restore-defaults`
  undoes it in one step. Not prevented: an org_admin who deletes every channel, then weakens
  rules. The audit log records it.
- **A webhook pointed at an internal address** (cloud metadata, a database, this machine): the
  egress guard at create, at test and at every delivery; the socket connects to the checked
  address; no redirects, no proxy, no local allow. A receiver that turns hostile later can only
  fail deliveries.
- **The gateway used to send mail to anyone:** recipients only in the operator's allowed
  domains, 10 per channel, test sends and notifications capped.
- **The webhook as an amplifier** against a third party: capped per rule and organization; at
  most six tries per notification; one connection at a time per notification.
- **Forged notifications to a customer's receiver:** signed with a key only the gateway and the
  customer hold, with a timestamp against replay, and a delivery id for duplicates.
- **A webhook URL with a token in it** is a secret: sealed, never shown in full, never logged.
- **Settings as a denial of service:** someone with `settings.write` can set
  `requests_per_minute` to 1 or `monthly_token_budget` to 0. It needs a fresh password each time,
  is audited and versioned, and one revert undoes it.
- **Settings as a privacy downgrade:** widening `allowed_providers` or raising
  `max_hosted_attempts` sends prompts further. Consume-on-use step-up, the caps, and the rule
  that a looser value never reaches a request in flight.
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
| `PUT /settings` (`expected_version`, `set`, `unset`) | `org_settings.write` | consume | 200 |
| `POST /settings/validate` | `org_settings.write` | | 200 |
| `POST /settings/revert` (`expected_version`) | `org_settings.write` | consume | 200 |
| `GET /settings/versions` | `org_settings.read` | | 200 |
| `GET /settings/versions/{n}` | `org_settings.read` | | 200 |
| `GET /settings/presets` | `org_settings.read` | | 200 |
| `GET /applications/{app}/settings` (the effective view) | `settings.read` | | 200 |
| `PUT /applications/{app}/settings` (`expected_version`, `set`, `unset`) | `settings.write` | consume | 200 |
| `POST /applications/{app}/settings/validate` | `settings.write` | | 200 |
| `POST /applications/{app}/settings/revert` (`expected_version`) | `settings.write` | consume | 200 |
| `POST /applications/{app}/settings/presets/{name}` (`expected_version`) | `settings.write` | consume | 200 |
| `GET /applications/{app}/settings/versions` | `settings.read` | | 200 |
| `GET /applications/{app}/settings/versions/{n}` | `settings.read` | | 200 |
| `GET /applications/{app}/alert-rules` | `alert_rules.read` | | 200 |
| `POST /applications/{app}/alert-rules` | `alert_rules.write` | yes | 201 |
| `GET /applications/{app}/alert-rules/{rule}` | `alert_rules.read` | | 200 |
| `PUT /applications/{app}/alert-rules/{rule}` (`expected_version`) | `alert_rules.write` | yes | 200 |
| `POST /applications/{app}/alert-rules/{rule}/enable` | `alert_rules.write` | yes | 200 |
| `POST /applications/{app}/alert-rules/{rule}/disable` | `alert_rules.write` | yes | 200 |
| `DELETE /applications/{app}/alert-rules/{rule}` | `alert_rules.write` | yes | 200 |
| `POST /applications/{app}/alert-rules/validate` (a rule body; also what it would have been over the last hour, from the rollups) | `alert_rules.read` | | 200 |
| `POST /applications/{app}/alert-rules/restore-defaults` | `alert_rules.write` | yes | 200 |
| `GET /applications/{app}/alert-channels` (ids, names, types) | `alert_rules.read` | | 200 |
| `GET /applications/{app}/incidents` (`state`) | `incidents.read` | | 200 |
| `GET /applications/{app}/incidents/{incident}` (with its notifications) | `incidents.read` | | 200 |
| `POST /applications/{app}/incidents/{incident}/acknowledge` | `incidents.acknowledge` | | 200 |
| `GET /incidents` (`state`, `application`) | `incidents.read_organization` | | 200 |
| `GET /alert-channels` | `alert_channels.read` | | 200 |
| `POST /alert-channels` (`type`, `name`, `url` or `recipients`, `default`) | `alert_channels.write` | consume | 201, with the signing key once for a webhook |
| `GET /alert-channels/{channel}` (health included) | `alert_channels.read` | | 200 |
| `PATCH /alert-channels/{channel}` | `alert_channels.write` | consume | 200 |
| `DELETE /alert-channels/{channel}` | `alert_channels.write` | consume | 200 |
| `POST /alert-channels/{channel}/rotate-key` | `alert_channels.write` | consume | 200, the new key once |
| `POST /alert-channels/{channel}/test` | `alert_channels.test` | | 200, the outcome code |
| `GET /alert-channels/{channel}/deliveries` | `alert_channels.read` | | 200 |

The mode endpoints (`PUT /mode`, `PUT /applications/{app}/mode`) stay as they are; the effective
settings view shows the mode from them. Status codes: those of note 30, plus 409
`settings_version_conflict`, `setting_looser_than_cap`, `no_previous_version`,
`rule_version_conflict`, `rule_limit`, `channel_limit`, `incident_not_open`; 422
`invalid_request` (with the field); 429 `test_send_limited`; 503 `state_unavailable`.

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
  a copy button and a warning); test send and its outcome; deliveries.
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
  start; a corrupt document refuses that application's requests with 503.
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
- **SSRF:** every private, loopback, link-local and metadata address refused at create, test and
  delivery; a name that resolves public at create and private at delivery refused at delivery; a
  redirect not followed; a proxy setting ignored; http refused.
- **Email:** an in-process SMTP test server written in the test suite on asyncio streams (no
  dependency): STARTTLS required, plain refused; a domain outside the allowlist refused; 4xx
  retried, 5xx not.
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
| M10 | Step-up on settings | `settings.write` without step-up | the step-up tests |
| M11 | Consume-on-use on settings and channels | `consume` off for each | the second-write-refused tests |
| M12 | Step-up on rules | `alert_rules.write` without step-up | the rule step-up tests |
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

### 19. Leak scans

Two scans, each with a planted control that must be found:

- **Channel secrets:** plant a webhook URL with a token in its path, a signing key and an SMTP
  password; create, test, deliver (succeeding and failing), rotate, delete; then scan every
  stored row (SQLite, Postgres), every Valkey key and value, every log record, every audit row,
  every event, the Prometheus output, every admin answer (except the one that returns the new
  signing key), and every outgoing payload. The scan must find the planted values in a
  deliberately unredacted copy, and nowhere else.
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
| S1 | `settings-alerts/s1` | Migration 14 and Postgres schema 3 (settings tables); `SettingsStore` on memory, SQLite and Postgres; `effective_settings`; the join into `lookup_key`; settings S1 to S11 and S17 to S19 applied on the request path; the CLI; tests; note 39 | None until a setting is set: defaults are today's values |
| S2 | `settings-alerts/s2` | The settings endpoints (application and organization), the permission rows, step-up, versions, revert, validate, presets, audit, the cross-organization tests | New endpoints |
| S3 | `settings-alerts/s3` | S12 shown, S13 to S16 added; `hosted_policy` (one statement for both modes and both settings rows); the in-flight rule for each; measured before and after | One store call fewer per hosted attempt; the in-flight rule for the new settings |
| A1 | `settings-alerts/a1` | `AlertRecorder`, `AlertRollups` (memory, Valkey), the histogram, the signals and their minimums; no rules; the recorder measured | None (nothing reads it but tests) |
| A2 | `settings-alerts/a2` | Rules: tables, endpoints, permissions, defaults (with the data step for existing applications), validate with the last-hour preview | New endpoints; nothing fires yet |
| A3 | `settings-alerts/a3` | The evaluator: the lease and its epoch, the states, incidents, dedupe, cooldown, quiet hours, acknowledge, the incident endpoints; no channels (the incident list only) | Incidents appear |
| A4 | `settings-alerts/a4` | Webhook channels: the vault binding, the signing key, the egress guard, the outbox, retries, the caps and the summary, test send, delivery visibility, `channel_delivery_failing`, both leak scans | Notifications leave the machine |
| A5 | `settings-alerts/a5` | Email through SMTP: the operator settings, the vault for the password, STARTTLS, the domain allowlist, the in-process test server | Email |
| A6 | `settings-alerts/a6` | Operator signals and channels (section 11.5) | Operator notices |
| C1 | `settings-alerts/c1` | Console: settings and organization settings | Frontend |
| C2 | `settings-alerts/c2` | Console: alert rules, channels, incidents | Frontend |

S1 to S3 and A1 can go in either order after S0; A2 needs S2's permission work; A4 needs A3.

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
   operator raises them through the CLI.
3. **No settings cache.** Settings are read in the key's own query on every request; the only
   memoisation is of immutable versions. Nothing is shared to invalidate, and a change reaches
   every instance's next request.
4. **Every settings write is consume-on-use**, not only the ones that loosen egress: one rule
   instead of a per-field split that a later field could get wrong.
5. **Modes stay in their own table and endpoints.** The effective view shows them; folding them
   into the settings documents would rewrite the merged security path of notes 33 and 38.
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

## Questions that block PR S1

- **Q1. The plan copy as ceiling** (decision 2): may a customer only lower
  `requests_per_minute`, `monthly_token_budget` and `max_tokens`, with raising left to the
  operator's CLI? Proposed: yes.
- **Q2. Consume-on-use for every settings write** (decision 4), or step-up without consume for
  settings that cannot widen egress (S1 to S11, S17 to S19) and consume only for S13 to S16?
  Proposed: every write.
- **Q3. The settings read inside `lookup_key`** (decision 3): it changes the key lookup's
  statement on every request, `/v1/generate` included, in exchange for no added store call and
  no cache. Proposed: yes, measured in S1 with `key_checks` and `request_store_calls`.
- **Q4. `default_max_tokens` as the reservation's completion bound** (S8): when a request names
  no `max_tokens`, S8 is applied as its `max_tokens`, so what is reserved is what may be
  generated. Proposed: yes; with S8 unset nothing changes.
- **Q5. The key settings S17 to S19** touch key issuing (authentication-adjacent). In S1 as
  proposed, or left out of this plan?

Questions for later PRs, not blocking S1: whether `incidents.acknowledge` should go to viewers
(decision 17); the default rules' thresholds; the caps' numbers (6 and 60 an hour); whether to
add `tzdata` for daylight saving.

## Not built

Everything in this plan. Also not planned: a Slack format, SMS or paging services, alert
routing trees or escalation policies, anomaly detection or any learned threshold, alerts across
organizations, a status page, and per-application isolation of the prefix cache stand-in.
