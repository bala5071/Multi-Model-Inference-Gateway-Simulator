# Release notes

What an operator upgrading the gateway should know: changes in behaviour, not a feature list.
Newest first. Each entry names its PR and its note.

## v0.1.0 (not yet tagged)

Everything merged since the first release, the tag `v0.2-platform-foundation` (PR #1,
2026-10-22), up to PR #43. That tag holds the simulator, the scheduler, the dashboard,
simulated live mode and migration 1 (usage events); applications, API keys and persistence in
the default server arrived after it, in PR #2.

### Upgrading: read first

- **A SQLite database from any earlier version needs `uv run python scripts/manage.py migrate`
  before this version starts.** Migrations 4, 8 and 18 rebuild a table, and the server refuses
  to run a pending rebuild on a database that has data, naming the command, rather than rebuild
  without a backup. The CLI backs the database up first, beside it, and prints where;
  `--dry-run` shows what it would do. A new, empty database is migrated by the server itself
  ([operating.md](operating.md#upgrading), notes 26 and 48).
- **Three secrets are now required at start, from the environment:** `GATEWAY_KEY_PEPPER`
  (PR #2, note 20), a first admin in `GATEWAY_ADMIN_USERNAME` and `GATEWAY_ADMIN_PASSWORD`
  (PR #9, note 27), and the vault's master key `GATEWAY_VAULT_KEY` (PR #18, note 32). The server
  refuses to start without them. Losing the pepper invalidates every API key; losing the master
  key, every sealed provider key and channel secret.
- **Every request needs an application's API key** (`Authorization: Bearer`); `tenant_id` is
  refused with 422 (PR #2, note 20).
- **The server binds `127.0.0.1` only** unless `GATEWAY_ALLOW_NETWORK_BIND=1`, and monitoring,
  `/metrics`, load control, the Playground, the dashboard, console logins and the admin
  endpoints answer only loopback peers (and `GATEWAY_LOCAL_NETWORKS`), never through forwarded
  headers (PR #5 and later, note 25).
- **Provider calls, the operator's own adapters and webhooks are held to TLS 1.2 or higher**
  (PR #39, note 46): a provider that offers only TLS 1.0 or 1.1 now fails its handshake. No
  setting turns this off.
- **The admin console** is served at `/dashboard/console.html` once the dashboard is built
  (PRs #41 and #43, notes 47 and 49). It adds no endpoint: it uses the existing ones, and is
  local only like them.

### Migrations 14 to 18

SQLite migrations, and the Postgres schema versions that match them in shared mode (Postgres
is brought to the current schema at start; `migrate-to-postgres` and `migrate-to-sqlite` copy
between them, note 36).

| Migration | Postgres | What it adds | Runs | PR, note |
|---|---|---|---|---|
| 14 | schema 3 | `settings_current`, `settings_versions`: per-application and per-organization settings with their versions | by the server | #31, note 39 |
| 15 | schema 4 | `alert_rules`, with a data step that gives every existing application the default rules | by the server | #34, note 44 |
| 16 | schema 5 | `alert_lease`, `incidents`, `incident_notifications` | by the server | #36, note 45 |
| 17 | schema 6 | `alert_channels`, `channel_secrets`, `notification_deliveries`, `notification_caps`, `suppression_summaries`, `instance_ceilings`, `incidents.notify_channels`; refused while an application has an empty id | by the server | #39, note 46 |
| 18 | schema 7 | rebuilds `alert_channels` (type webhook or email); `notification_deliveries.recipient_id`; `alert_recipients`, `confirmation_caps`, `platform_secrets`. Its rollback is refused while email data exists | **by `scripts/manage.py migrate` only** on an existing database | #42, note 48 |

Because migration 18 rebuilds a table, a database at any version from 1 to 17 needs the CLI
once; it then runs every pending migration, 14 to 17 included.

### Every behaviour change, by PR

PRs that changed only plans, documentation or tests are listed at the end.


### PR #43 (C2: the console's alert screens)

- **The console gains the alert rules, channels and incidents screens** and the evaluator
  banner. Frontend only: no endpoint, setting or stored value changed (note 49).

### PR #42 (A5: email channels and verified recipients)

- **An existing SQLite database needs `scripts/manage.py migrate` before the server starts.**
  Migration 18 rebuilds `alert_channels` (SQLite changes a check only by rebuilding a table), so
  the server refuses to run it on an existing database and names the command. The CLI backs the
  database up first, beside it; `--dry-run` shows what it would do. Every channel keeps its id.
  A new database is migrated by the server as before (note 48).
- **Postgres is brought to schema 7 at start, as before;** its rollback is refused while an email
  channel or a recipient's delivery exists (note 48).
- **Four more cap variables,** `GATEWAY_EMAIL_CONFIRMATIONS_PER_*`, with defaults; a value out of
  range refuses the start, as A4's do. They join the ceilings row, so instances with different
  values report it (note 48).
- **Email is sent only when the operator sets it up:** with no `GATEWAY_SMTP_HOST` or no
  `GATEWAY_ALERT_EMAIL_DOMAINS`, email channels cannot be created and nothing changes. With them,
  mail goes over STARTTLS or implicit TLS only, to verified recipients only (note 48).
- **The monitoring snapshot's `alert_deliveries` gains `email` and `smtp_lookups_abandoned`,**
  `/metrics` gains `gateway_alert_smtp_lookups_abandoned` and the email results of
  `gateway_alert_deliveries_total`, and there is one more event type, `alert_smtp_resolver_busy`
  (note 15).

### PR #41 (C1: the console's settings screens)

- **The admin console at `/dashboard/console.html`**, with a content security policy on the
  built page. Frontend only (note 47).

### PR #39 (A4: alert channels and their webhook delivery)

- **Migration 17 / Postgres schema 6.** Webhook channels and their delivery: notifications now
  leave the machine, to the organization's channels, through the outbox with retries and caps.
  **An empty application id is refused everywhere** (stores, the CLI, the plans file), and
  migration 17 is refused while one exists (note 46).

- **Provider calls are now held to TLS 1.2 or higher.** The pinned TLS context
  (`gateway.egress.provider_tls_context`, note 33) used to take its lowest protocol version from
  the platform's OpenSSL defaults: TLS 1.2 on some systems, lower on others (CI's Linux runner
  allowed any). It now sets TLS 1.2 itself, for organization providers, the operator's own
  adapters and webhooks alike. A provider that offers only TLS 1.0 or 1.1 now fails its
  handshake, as a provider with a certificate that does not verify does. No setting turns
  this off (note 46).
- **The HTTP libraries no longer log webhook attempts.** `httpx`'s INFO request line and
  `httpcore`'s DEBUG trace are dropped for webhook attempts only, since they carry the webhook
  URL (a secret) and the receiver's headers. Other requests, provider calls among them, log as
  before (note 46).
- **The alert evaluator's Postgres pool is 3 connections** (it was 2), shared with the webhook
  delivery worker (note 46).

### PR #37 (the alert evaluator, measured and bounded)

- **An evaluation cycle reads 200 applications a page, stops after 20 s and resumes fairly**,
  and gives the event loop back between applications; on Postgres it has its own pool of 2
  connections (3 since PR #39). Incidents and notifications behave as before (note 45).

### PR #36 (A3: the alert evaluator and incidents)

- **Migration 16 / Postgres schema 5.** The default server and shared mode now run an alert
  evaluator: one instance at a time holds a lease and evaluates every 30 s; incidents open,
  fire, resolve and are listed and acknowledged under `/v1/admin/orgs/{org}/...incidents`
  (note 45).
- **`/healthz` gains one field, `alert_evaluator`** (`ok`, `stale`, `unknown` or `disabled`);
  `/readyz` does not depend on it. `/metrics`, the snapshot and the event bus gain the
  evaluator's state, holder, cycle time and failures (notes 15 and 45).

### PR #34 (S3, A1, A2: hosted settings and presets, the alert recorder, alert rules)

- **Settings S13 to S16** (allowed providers, hosted attempts, the retry after invalid tool
  JSON, the hosted first-token budget). **A change that loosens any of them for an application
  it affects needs the password again**, consume-on-use, judged again inside the write; presets
  and their endpoints (note 42).
- **One store call fewer per hosted attempt** (`hosted_policy`: both modes and both settings rows
  in one statement); a stricter value applies before the next hosted attempt, a looser one from
  the next request (note 42).
- **Every request is recorded per application and minute** by the alert recorder (in memory,
  or flushed to Valkey every 10 s in shared mode); it never awaits (note 43).
- **Migration 15 / Postgres schema 4**: alert rules with defaults for every application, their
  endpoints, versions, restore-defaults and a last-hour preview (note 44).

### PR #33 (scale-out 2b: breaker reports and the hosted-attempt cap)

- **At most `GATEWAY_CHAT_MAX_HOSTED_ATTEMPTS` (default 3) calls to organization providers per
  chat request.** Circuit-breaker reports go through a bounded queue off the request path (a
  half-open probe's at once); a dropped report is counted in a metric; a probe's slot frees
  itself after 120 s (note 41).

### PR #32 (S2: the settings endpoints)

- **New endpoints** for an organization's caps and an application's settings: read, change,
  dry run, versions, one-step revert, each audited (note 40).

### PR #31 (S1: settings)

- **Migration 14 / Postgres schema 3.** Settings S1 to S11 apply on the request path, read with
  the key in the same statement. **A settings row that cannot be judged answers 503
  `state_unavailable`**, never a looser value (note 39).
- **Every answer of `/v1/generate` and `/v1/chat/completions` that chose a limit carries
  `X-Max-Tokens-Applied`** (a number, or `none`); an application's `default_max_tokens` applies
  when a request names none (note 39).

### PR #28 (scale-out 3c: the mode read once per request)

- **The organization's and the application's modes are read once per chat request, and again
  just before each hosted attempt**; a stricter mode found then refuses the attempt. An answer
  already streaming is not cut (note 38).

### PR #27 (scale-out 3b: budgets on Postgres, shared mode)

- **`GATEWAY_STATE=shared`** (with `GATEWAY_POSTGRES_URL` and `GATEWAY_COUNTERS=valkey`) starts
  the gateway on Postgres and Valkey, several instances at once, no process lock. Token budgets
  there are reserved before a request and settled after it; a store off this machine needs
  verified TLS or `GATEWAY_STATE_PRIVATE_NETWORK=1`. SQLite is unchanged (note 37).

### PR #26 (scale-out 3: Postgres stores)

- **Every durable store on Postgres**, with `scripts/manage.py migrate-to-postgres` and
  `migrate-to-sqlite`, every table compared before anything is kept. Shared mode was refused
  until PR #27 (note 36).

### PR #24 (scale-out 2: counters in Valkey)

- **`GATEWAY_COUNTERS=valkey` with `GATEWAY_VALKEY_URL`** shares the rate limit, the login
  limiter, the step-up and user-creation caps, test-connection counts, breakers and the audit
  cap. Off by default. **A counter that cannot answer refuses the request (fails closed)**, and a
  Valkey restart is detected (note 35).

### PR #23 (scale-out 1, onto main from #22)

- **Concurrent wrong passwords each count toward the lockout**, where some could be lost
  before. Every store is behind an async protocol; no other change in behaviour (note 34).

### PR #20 (provider step 3 follow-up)

- **`X-Provider: gateway`** names the gateway's own tier (it said `local`); usage rows stored
  before this PR still say `local`. An answer already streaming from a hosted provider is not cut
  when the mode becomes stricter (note 33).

### PR #19 (provider step 3: fallback modes)

- **Migrations 12 and 13.** Chat can be served by an organization's own providers when the
  organization and the application both choose a mode other than `local_only` (the default).
  **Such a mode sends prompts, tool definitions and results to a third party the organization
  chose.** Answers carry `X-Provider`, `X-Provider-Class: local | hosted`, and after a fallback
  `X-Fallback-From` and `X-Fallback-Reason` (note 33).

### PR #18 (provider step 2, onto main from #17)

- **Migrations 10 and 11; `GATEWAY_VAULT_KEY` required.** An organization's providers and their
  keys (sealed with AES-256-GCM, never returned), consume-on-use step-up for each change, the
  egress guard on every call, test-connection (which spends tokens on the organization's
  account) (note 32).

### PR #16 (provider step 1: the chat endpoint)

- **`POST /v1/chat/completions`** in the OpenAI format, with tools and streaming, on the
  gateway's own tiers; unsupported fields are refused with 422 naming them (note 31).

### PR #15 (identity step 7: management endpoints)

- **Users, applications and keys over HTTP** under `/v1/admin/orgs/{org}`, within the session's
  roles; a missing resource is answered exactly as a forbidden one; temporary passwords instead
  of chosen ones; every conflict audited (note 30).

### PR #11 (identity step 6: step-up)

- **Migration 9.** Sensitive actions need a fresh password: `POST /v1/auth/step-up` opens a
  5-minute window for that session (note 29).

### PR #10 (identity step 5: roles)

- **Migration 8, a rebuild of `users`: run `scripts/manage.py migrate`.** Roles
  (`platform_admin`, `org_admin`, `app_admin`, `viewer`) granted from the CLI; membership alone
  grants nothing (note 28).

### PR #9 (identity step 4: users and sessions)

- **Migrations 5 to 7; a first admin required** from `GATEWAY_ADMIN_USERNAME` and
  `GATEWAY_ADMIN_PASSWORD`, who must change the password at first login. Console logins under
  `/v1/auth`, sessions with CSRF, lockout after 5 failures, the audit log (note 27).

### PR #8 (identity step 3: organizations)

- **Migration 4, a table rebuild: run `scripts/manage.py migrate`.** Applications belong to
  organizations (existing ones to `default`); application names are unique within an
  organization; a disabled organization's keys stop working (note 26).

### PRs #5 and #6 (identity steps 1 and 2)

- **argon2id password hashing** (64 MiB, 3 iterations by default) with a blocklist (note 24).
- **Local only:** monitoring, `/metrics`, `/v1/overload`, load control, the Playground and the
  dashboard answer only loopback peers (and `GATEWAY_LOCAL_NETWORKS`), refuse forwarded headers,
  foreign Host names and cross-site requests; `python -m gateway.serve` refuses a non-loopback
  bind unless `GATEWAY_ALLOW_NETWORK_BIND=1`. The compose demo counts Docker's bridge network as
  local (note 25).

### PR #2 (platform steps 2 to 6)

- **Applications and API keys:** every request carries an application's key; keys are stored
  only as an HMAC with `GATEWAY_KEY_PEPPER`, which the server now requires; `tenant_id` is a 422
  (note 20).
- **Usage in the default server's SQLite database** (migrations 2 and 3), budgets per calendar
  month in UTC, usage kept `GATEWAY_USAGE_RETENTION_DAYS` (default 400), backup and restore
  from the CLI; **one gateway process per database**, a second one refuses to start (note 21).
- **Tenants became applications** in the monitoring snapshot, Prometheus labels, events and the
  dashboard; rate-limit refusals are a rejection reason (note 22).

### No behaviour change

Plans, notes, CI, tests or documentation only: #3, #4, #7, #12, #13 and #14 (the step 7 plan), #17 and #22 (merged on the way to #18 and #23), #21, #25, #29, #30, #35,
#38 and #40.
