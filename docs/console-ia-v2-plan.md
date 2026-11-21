# Console information architecture v2: clients, applications, costs

Status: **approved** (PR #48, 2026-11-10), with the answers in section 13, which override any
recommendation above that differs. Two new permission rows are approved: the owner's
metrics-only read of a client's applications (Q1, PR 3) and the price rows (Q4, PR 4); each lands
in its own clearly marked commit. The act-as mode (section 9) still needs its own plan. This file replaces P3 onward of `docs/console-integration-plan.md`
(its section 15 says what is superseded).

Builds on what PRs #45 to #47 shipped and keeps it: one React page at `/dashboard/`; navigation
only from `GET /v1/auth/capabilities`, which is answered inside `make_requires` with the same
`authorize` the routes use; no role name in `src/`; a missing resource and a forbidden one
answer the same bytes; `/v1/admin` and `/v1/auth` local only (`LOCAL_ONLY_PATHS`); the page under
the console's content security policy; no secret in browser storage; the CSRF token in memory.

## 1. Facts from the code (main at fdf4afc)

Each was read from the code; the plan turns on them.

| # | Fact | Where |
|---|---|---|
| F1 | platform_admin is **denied** `applications.list`, `applications.read`, `usage.read_organization`, `usage.read_application`, `incidents.read_organization` and every application-level action. docs/28: "nothing at application level is ever allowed to it (... an application's usage ...)", and a test asserts it for every application-level action. | `permissions.py`, `docs/permission-matrix.md`, `docs/28-roles.md` |
| F2 | `usage.aggregate` is `platform_admin@platform`, wired `later`, no route. The identity plan defines it as "see aggregate usage **per organization**". | `permissions.py:131`, `docs/identity-plan.md:181` |
| F3 | `usage.read_organization` (org_admin, viewer at the organization) and `usage.read_application` (app_admin, org_admin, both viewers) exist, wired `later`, no route. | `permissions.py:149,199` |
| F4 | `organizations.create` (step-up) and `organizations.list` are wired `now`: the CLI only (`create-organization`, `list-organizations`). No HTTP route. `roles.assign` is CLI only too (`test_there_is_no_endpoint_for_roles`). | `scripts/manage.py`, `tests/test_management_api.py` |
| F5 | Applications and keys over HTTP **exist**: `POST/GET /v1/admin/orgs/{org}/applications`, `GET .../applications/{app}`, `POST/GET .../keys`, `.../rotate`, `.../revoke`. `applications.create` has **no** step-up; `application_keys.issue` has step-up. The new key is returned once (`issued_view`). Both write audit rows (`APPLICATION_CREATED`, `KEY_ISSUED`). Tests: `test_creating_an_application_from_a_plan_template`, `test_a_key_is_shown_once_works_and_is_listed_without_secrets`. | `app.py:1807-1878`, `management.py:268,430` |
| F6 | No route lists the plan templates. The only way to learn them over HTTP is the 422 text of an unknown plan. | `management.py:289-291` |
| F7 | `POST /v1/admin/orgs/{org}/users` exists and lets platform_admin create a user in **any** organization (`users.create`, step-up); the account must change its password at first login. | `app.py:1713`, `docs/28-roles.md` |
| F8 | **The A1 recorder** keeps, per application and per minute: requests by kind, status classes, 504s, error codes, rejections, fallbacks by reason, hosted attempts and answers, downgrades, and the TTFT and total-latency histograms (72 fixed edges, each about 19% above the last). **No tokens, no model, no organization id.** Rollups are kept **120 minutes**, in memory (default server, simulated live) or in Valkey (shared state). Read only by the evaluator and the rule preview. | `alertrecorder.py`, `alertrollups.py` (`KEEP_MINUTES = 120`) |
| F9 | **Usage events** already hold, per settled request: application, model (the routed tier's), prompt, cached and completion tokens, and who answered (`source.provider`: `gateway` or an organization provider id). On SQLite and Postgres (index on application and time; retention 400 days by default); in memory a ring of 10,000 events. Refused and failed requests have no event. **So tokens per model per application are already kept; costs need no new write on the request path.** | `usage.py`, migrations 1, 3, 13, `app.py:5368` |
| F10 | The stored `base_cost_usd` and `billed_cost_usd` come from the **placeholder prices** in `profiles.py`; chat records 0. Hard rule 5 forbids reporting a cost from them. The CLI's `organization-usage` prints `billed_cost_usd` today. | `cost_model.py`, `profiles.py:71`, `app.py:4537`, `scripts/manage.py:251` |
| F11 | **Platform settings are not editable.** `PlatformSettings` is built at start from the environment and the registry (`platform_settings`, `app.py:3370`); no store, no route. | `settings.py:322` |
| F12 | Applications have no enabled or disabled state. An application's mode is in the modes store (migration 12) with a `PUT` and no read route. | `applications.py:45`, `app.py:1955` |

## 2. The product

### 2.1 Front door

Login, the forced password change and step-up stay as #47 left them. After login the shell asks
`/v1/auth/capabilities` and picks the landing screen from the answer, never from a role name:

| The answer allows | Landing | Who that is today |
|---|---|---|
| any platform-level action | **Clients** | platform_admin |
| `applications.list` in the session's organization | **Applications** | org_admin, viewer (organization) |
| application-level actions on exactly one application | that **application's page** | app_admin or viewer of one application |
| application-level actions on several | a short **list of those applications** | app_admin or viewer of several |
| nothing | **No access yet**: "This account has no access to any part of the gateway yet. Ask your organization's administrator." and Log out | no role, application-defined role only |

A session that is both (platform_admin who is also org_admin of the default organization) lands on
Clients and also sees its own organization's tabs.

### 2.2 Routes (hash)

| Route | Screen | Shown when the answer allows |
|---|---|---|
| `#/clients` | Clients | `organizations.list` and `usage.aggregate` (platform) |
| `#/clients/<org>` | one client's applications and their metrics, as the platform owner (Q1 (b)) | `usage.read_client_applications` (new row, section 13) |
| `#/monitor/<tab>` | Platform monitoring (unchanged) | `monitoring.read` |
| `#/apps` | Applications of the organization | `applications.list` |
| `#/apps/new` | Add application | the refused-body probe on `POST .../applications` (2.4) |
| `#/app/<id>/overview`, `/settings`, `/rules`, `/incidents`, `/keys` | the application page's tabs | `usage.read_application`, `settings.read`, `alert_rules.read`, `incidents.read`, `application_keys.list` |
| `#/channels` | Channels | `alert_channels.read` |
| `#/organization` | Organization settings | `org_settings.read` |
| `#/home` | resolves to the landing of 2.1 | always |

The old hashes keep working: `#/application/<id>` reads as `#/app/<id>/settings`, `#/rules/<id>`
as `#/app/<id>/rules`, `#/incidents/<app>/<incident>` as that application's incidents tab, and
`#/incidents` as the organization's incident list (kept as a link from Applications). `routes.ts`
stays the one parser and printer, pure and tested. Unknown and forbidden routes draw the landing
with "That page is not available to this account.", the same words for both, as now.

**Platform settings is left out.** The settings API has no platform level to read or write
(F11); the platform's values are environment variables. The Clients page says so in one line.

### 2.3 Navigation per session (a test expectation, not a frontend table)

| Item | platform_admin | org_admin | viewer (org) | app_admin | viewer (app) | no role |
|---|---|---|---|---|---|---|
| Clients | yes | no | no | no | no | no |
| Platform monitoring | yes | no | no | no | no | no |
| Applications (organization tab) | no | yes | yes, read | no | no | no |
| Channels (organization tab) | no | yes | no | no | no | no |
| Organization settings (organization tab) | no | yes | yes, read | no | no | no |
| One client's applications, metrics only (`#/clients/<org>`) | yes (Q1 (b)) | no | no | no | no | no |
| Application page: Overview, Settings, Alert rules, Incidents | no | yes | yes, read | its own | its own, read | no |
| Application page: API keys | no | yes | no | its own | no | no |

"Read" means the screen's write controls stay hidden because the screen's own probe said
`forbidden`, as since note 47. Hiding is a convenience; every route still checks.

### 2.4 Screens

- **Clients** (platform). One row per organization, a page of 50 at a time: name and id, kind,
  enabled, applications, requests, error rate, fallback rate, open incidents (Q2), usage (tokens)
  and estimated cost (PR 4), health dot. Above it a **platform health strip**: the evaluator's
  heartbeat (`/healthz` `alert_evaluator`), the stores (`/readyz`), and platform totals over the
  same window. The **health dot** is derived from the gateway's own states, no new threshold: red
  when the organization has a firing incident, amber when pending or acknowledged, green when
  neither, grey "unknown" when the evaluator is not `ok` or the window is incomplete.
- **One client, as the platform owner** (`#/clients/<org>`). A banner, "Viewing as platform
  owner: metrics only". One row per application of that client: id, name, requests, error rate,
  fallback rate, p50, open-incident counts by state, tokens and estimated cost (PR 4), status dot;
  a row opens that application's metrics over time. Nothing editable, no link to settings, rules,
  channels, incidents or keys (Q1 (b), section 13).
- **Applications** (organization). "Add application" on top when the probe allows. One row per
  application, a page of 50: name, mode (Q8), requests, error rate, p50, open incidents, status
  (the same dot as Clients, per application; "no requests" when the window is empty), usage and
  estimated cost (PR 4). Clicking a row opens its Overview.
- **Add application**: name and plan (the plan list from a new read, 4.1). Create, then "Issue the
  first key", which asks for the password (the table's step-up on `application_keys.issue`). The
  key is shown once in a read-only field with a copy button and the words "Shown once. It cannot
  be shown again."; it lives only in that component's state, is dropped on navigation or unmount,
  never written to storage, a URL or the console log. If the key step is cancelled or refused,
  the application exists and its API keys tab offers the issue again.
- **Application page**: a header with the name and id, and tabs **Overview** (that application's
  monitoring: requests per minute, status classes, error rate, rejections, fallbacks by reason,
  hosted share, p50/p95/p99 TTFT and total latency, then usage and cost in PR 4), **Settings**,
  **Alert rules**, **Incidents** (the existing screens, moved under the tab), **API keys** (list,
  issue, rotate, revoke over the existing routes; a new key shown once as above).
- **Channels**, **Organization settings**: the existing screens, as organization tabs.
- **Viewer sessions** see the same pages with every edit control hidden. Each new write control
  appears only after the gateway said yes, never from a role: "Add application" after the
  refused-body probe on `POST .../applications` answers `invalid` rather than `forbidden` (the
  note 49 pattern); "Issue key", "Rotate", "Revoke" after the same probe on their routes answers
  `step_up_required` or `invalid` rather than `forbidden` (`requires` authorizes before step-up).

### 2.5 Every metric is honest

- A window with no requests shows "no requests", never 0% errors or a 0 ms p50.
- A minute the rollups mark `incomplete` (an instance dropped deltas, Valkey restarted) makes its
  window "incomplete" on screen, never a number that silently lacks counts.
- Percentiles come from the fixed histogram: the bucket's upper edge, labelled "p50 (to within
  about 19%)". Never a nearest-rank figure the recorder does not have.
- After a restart of the default server (in-memory rollups), the windows that reach before the
  process started are "since restart HH:MM", not a full window.
- Nothing returned is a prompt, a response, a token id, a key or free text: ids, minutes, fixed
  field names and counts (F8, F9).

## 3. Windows and the recorder (question Q3)

The rollups hold 120 minutes (F8). "Requests 24 h" and 24-hour rates cannot come from them.

- **Option (a), no recorder change:** rates and latency over the last 60 minutes; tokens and cost
  over the chosen range from usage events (F9). The Clients page then reads every application of
  every organization for 60 minutes: bounded by pages, but it grows with applications.
- **Option (b), recommended:** the recorder also adds each request to **hour** keys and to
  **organization** keys (`record` takes the organization id `app.py` already has):
  - per application per hour, and per organization per minute and per hour, the same fixed fields;
  - kept 26 hours (hours) and 120 minutes (minutes): in Valkey by TTL, in memory by the same
    `_forget` rule, so memory stays bounded by the applications and organizations seen;
  - the Clients page reads organizations x 24 hour keys for a page of 50, never every application;
  - **the request path changes** in the in-memory case (the sink absorbs at once): measured
    before and after with `benchmarks/alert_recorder.py`, three runs each, hardware recorded. In
    Valkey the extra keys ride the flusher's pipeline every 10 s, off the request path; the
    flush is timed before and after too.
  - Postgres is not involved (rollups are never in Postgres); `postgres`-marked tests show the
    shared app reading Valkey's hours and organization keys from two instances.
- **Option (c):** a durable hourly table in SQLite/Postgres. Another write path and migration; not
  needed for 24 hours.

Ranges offered, all bounded: **60 min** and **24 h** for rates and latency; **24 h, 7 d, 30 d,
and the current budget period** (calendar month, UTC) for tokens and cost; nothing longer than
31 days. Each read's cost is measured in its PR (section 8).

## 4. Backend inventory

Every new route is under `/v1/admin` (local only), goes through `requires()` with one action,
answers `Cache-Control: no-store`, refuses a missing and a forbidden resource with the same bytes
(a byte-for-byte test per role), returns no content, and is added to the route-pin and agreement
tests of P1.

### 4.1 Items

| Item | Status | Route | Action | Tests |
|---|---|---|---|---|
| Per-application monitoring read | **new** | `GET /v1/admin/orgs/{org}/applications/{app}/metrics?window=60m\|24h` | `usage.read_application` | new: per role, missing = forbidden, incomplete minutes, no-requests, fixed fields only |
| Per-organization monitoring read (summary rows for a page of applications, and organization totals) | **new** | `GET /v1/admin/orgs/{org}/metrics?window=&after=&limit=` | `usage.read_organization` | new, as above |
| Application listing | **exists** | `GET /v1/admin/orgs/{org}/applications` | `applications.list` | `test_applications_are_listed_a_page_at_a_time`; adds `mode` (effective) to each row if Q8 says yes, fixtures regenerated |
| Open incidents per application, by state | **new** (the list route pages at 100 and cannot count) | `GET /v1/admin/orgs/{org}/incidents/counts` | `incidents.read_organization` | new |
| Plan templates for the Add form | **new** (F6) | `GET /v1/admin/orgs/{org}/plans` (names, models, limits; never keys) | `applications.create` | new |
| Create an application | **exists** | `POST /v1/admin/orgs/{org}/applications` (no step-up, per the table) | `applications.create` | exists; adds the audit-row assertion and the leak scan |
| Its first key | **exists** | `POST .../applications/{app}/keys` (step-up) | `application_keys.issue` | exists; adds the e2e "shown once" check |
| Organization list | **new** | `GET /v1/admin/orgs?after=&limit=` (id, name, kind, enabled, application count) | `organizations.list` | new |
| One client's applications, metrics only | **new**, PR 3 | `GET /v1/admin/platform/orgs/{org}/applications/metrics?window=&after=&limit=` and `GET /v1/admin/platform/orgs/{org}/applications/{app}/metrics?window=` | **new row** `usage.read_client_applications` (section 13) | new: fixed fields, every refusal pinned |
| Clients summaries | **new** | `GET /v1/admin/platform/usage?window=&after=&limit=` (per organization: requests, error rate, fallback rate, open-incident counts (Q2), tokens and cost (PR 4)) | `usage.aggregate` | new |
| Platform health strip | **exists** | `/healthz`, `/readyz` | public, local | exists |
| Price table | **new** | `GET /v1/admin/platform/prices`, `POST /v1/admin/platform/prices` (append one row) | **new rows** `prices.read`, `prices.write` (Q4) | new |
| Cost and usage reads | **new** | `GET .../applications/{app}/usage?range=`, `GET /v1/admin/orgs/{org}/usage?range=`, and the Clients read above | `usage.read_application`, `usage.read_organization`, `usage.aggregate` | new |

### 4.2 The permission table file

- **Who may do what does not change** in PRs 1 to 3. They wire existing rows
  (`organizations.list`, `usage.aggregate`, `usage.read_organization`, `usage.read_application`).
  Their `wired` column moves from `later`/`now` to a new `Wired.CONSOLE` ("console"), so
  `permissions.py` and the generated `docs/permission-matrix.md` change in that column only; a
  test compares every role and relation cell before and after.
- `NAVIGATION_ACTIONS` gains those four reads (none has a step-up flag; P1's test keeps it so).
- **PR 4 adds rows** only if Q4 says so. **PR 5 adds a relation** only after its own plan.

## 5. Costs (PR 4)

### 5.1 The price table

- **What:** USD per million tokens, a prompt price and a completion price, per model, as usage
  events name the model (F9). Effective-dated rows: `model_prices (id, model,
  prompt_micro_usd_per_mtok, completion_micro_usd_per_mtok, effective_from_ms, created_at_ms,
  created_by)`. Prices are integers in micro-dollars per million tokens (0.15 USD = 150000), so no
  floating-point drift in storage; a null pair means "no price from then on".
- **Append only.** No update, no delete. A correction is a new row. `effective_from_ms` must be
  at or after now on the store clock (`StoreClock`; `gateway_now_ms()` on Postgres): **no
  backdating**, checked inside the write transaction.
- **Where:** SQLite migration 19 (a new table, no rebuild), Postgres schema version 8, an
  in-memory store for simulated live mode (empty: every cost there reads "no price set").
  Valkey holds nothing: prices are read at read time, never on the request path.
- **Who edits:** the platform owner (Q4), in the console with step-up (consume-on-use) and
  audited (`price_set`: model, the two prices, effective from; never free text), and on the CLI
  (`manage.py prices-set`, `prices-show`), audited like `settings-set`.
- **Per-organization override: no**, as you recommend. It would make one "estimate" depend on
  contract terms, double every lookup, and belongs with invoicing, which this is not.

### 5.2 History when a price changes

**Effective-dated lookup, recommended.** A request is priced by the row in effect at its
`occurred_at_ms`, the same instant that puts it in a budget period. Because rows are append-only
and never backdated, the rows that covered a past instant never change, so **a past period's
cost never changes when a new price is set**: the same property as a snapshot at settle time.

Snapshot at settle time was considered: two new columns on `usage_events` (an `ADD COLUMN`
migration on SQLite and Postgres), a price lookup on every settle (a per-process cache that can
disagree between instances for its lifetime), and a new write on the request path to measure. It
buys nothing the lookup does not give once backdating is refused. Old periods are never
recomputed with a new price under either.

### 5.3 What is shown

- **Cost = prompt tokens x prompt price + completion tokens x completion price**, per model, summed.
  Cached prompt tokens are priced as prompt tokens (your formula; there is no cached price).
- **A model with no row in effect** shows its tokens and "no price set", never 0; a total that
  includes such tokens says "partial: N tokens without a price".
- **Local models** (every gateway tier today: simulated, or Ollama/llama-server) show tokens and
  "n/a" unless the operator sets a price.
- **Chat answered by an organization's own provider** (`source.provider` not `gateway`): Q5.
- **Every cost** carries "estimate, not an invoice".
- **The stored `base_cost_usd` / `billed_cost_usd`** (placeholder prices, F10) are never shown by
  the console. Budgets are unchanged. The CLI's `organization-usage` printing them is noted as a
  rule-5 risk and left for you to decide (not in these PRs).

### 5.4 Reads, stores, cost

- Usage reads group events by model and price interval over a bounded range (section 3):
  per application (exists as `usage_by_model`), per organization (new: a join to `applications`
  by `organization_id`), per page of organizations for Clients (new).
- **SQLite and Postgres:** the same SQL; the existing index on (application, time).
- **In memory** (ring of 10,000): a range that reaches before the oldest event the ring dropped is
  "incomplete", never a short total.
- **Valkey:** not involved.
- **Request path:** unchanged by PR 4 (no new write; F9). Stated with the code, and the p99 gate run.
- **Measured in PR 4:** each read on SQLite and Postgres at 10k, 100k and 1M events in range,
  three runs, hardware recorded. Target (a target, not a measurement): a screen's reads under
  250 ms at 1M events. If the 30-day read misses it, PR 4 stops and proposes a daily summary
  computed off the request path, with prices then effective at UTC day starts.

## 6. Add application, in detail

Two calls, each the existing route: `POST .../applications` (no step-up; the table has none),
then `POST .../keys` (step-up). Audit rows: `application_created` and `key_issued` (both exist;
PR 1 asserts both). The key is never logged (the request log records no body; the leak scan
proves it), never stored in the browser, and gone from the DOM after navigation. One form keeps
both steps visible: "1. Create" and "2. First key".

## 7. The owner and client data

(Answered by Q1 (b): see section 13. The text below is the analysis it answered.)

The owner sees **metrics only**: counts, rates, latency buckets, tokens and estimated cost; never
a prompt, a response, a key or a setting. Under today's table (F1, F2) that is: the organization
list (`organizations.list`) and **per-organization** aggregates (`usage.aggregate`). Clicking a
client therefore cannot open that client's Applications page without widening platform_admin.
Q1 asks which of these you want; the recommendation keeps the table.

## 8. Organization creation from Clients (question Q6)

| Option | What | Security cost |
|---|---|---|
| **A. Stays on the CLI** | `create-organization`, `create-user`, `grant-role` as today | None new. Operational: needs a shell on the machine. |
| **B. Organization only, over HTTP** | `POST /v1/admin/orgs` (`organizations.create`, step-up) | One new route for an existing row (local only until A8). A stolen owner session plus password creates empty organizations; the first admin still needs the CLI, so it is half a flow. |
| **C. Organization with its first org_admin, one call** (recommended) | `POST /v1/admin/orgs` with the organization and the first admin's username; in one write transaction: create the organization, create the user (must change password), grant org_admin. Refused unless the organization is new. Temporary password shown once. Step-up consume-on-use. Audited in the platform trail and the new organization's trail. | One new route, existing rows only (`organizations.create`; `users.create` and the appointment of org_admin are already the owner's). The owner knows the temporary password until first login: the risk docs/28 already accepts for `users.create`, made visible by the audit rows. It reaches **only a brand-new organization**: no general roles route exists, so it cannot appoint an admin in an existing client. |
| **D. A general roles route** | `POST /v1/admin/orgs/{org}/roles` (`roles.assign`) | Lets an owner who can already create a user in any organization (F7) appoint it org_admin there: a path into every existing client's settings that bypasses the act-as audit design. Not recommended. |

**Recommendation: C**, in PR 3, with its own mutation checks (an existing organization accepted;
the grant made outside the transaction; step-up not consumed) and the leak scan on the temporary
password.

## 9. "Act in organization X" (PR 5, plan-first)

Outline only; its own draft plan PR comes before any code, because it adds a relation
(`platform_admin@acting_in`) to the table. What it must have, from your request: a fresh password
(consume-on-use step-up), one organization, a reason from a fixed list, a time limit (proposed
15 minutes, no extension without a new password), a banner on every screen while it lasts, ended
by logout, expiry or by hand, the window held in the session row so every instance sees it
(SQLite, Postgres, two-instance tests), and audit rows in **both trails** for its start, its end
and every action taken in it. Which actions it allows is that plan's main question (I would
exclude provider keys, egress loosening, channel targets and keys).

## 10. PR split

Stacked; each starts from the previous once its CI is green; merge in this order. Base of the
first is `main` after this plan merges.

1. **Applications list and Add application.** The shell's new routes and landing (2.1, 2.2), the
   Applications tab with requests, error rate, p50, status and open incidents over 60 minutes,
   the per-organization metrics read, the incident counts read, the plan list, Add application,
   the API keys tab. `Wired.CONSOLE` for `usage.read_organization`.
2. **The application page and its Overview.** Tabs, the per-application metrics read, the old
   console screens moved under the tabs, old hashes mapped. `usage.read_application` wired.
3. **Clients.** The organization list, the Clients summaries, the health strip, the owner's
   client view (Q1), the recorder's hour and organization keys if Q3 says (b) (request path,
   measured), and Q6's answer. `organizations.list`, `usage.aggregate` wired.
4. **Costs.** The price table (stores, migration 19, Postgres schema 8, CLI, console editor if Q4),
   usage and estimated cost on Overview, Applications and Clients, the read timings of 5.4.
5. **Act-as, plan-first.** A docs-only draft first; code only after approval.

### 10.1 What every PR carries

- Per-role navigation tests (vitest, from the gateway-produced capabilities fixtures; no role
  table in the test).
- Direct URL to a screen the role cannot use: the landing with the not-available words, and no
  request for that screen's data.
- Deep-link login (logged out on the new route, log in, that screen), expired session (401 on the
  new screen's read and on a write behind `guarded`: login with the notice, hash kept, CSRF token
  forgotten).
- One e2e spec per role (platform_admin, org_admin, viewer at the organization, app_admin, viewer
  of an application, no role), waiting for states, never times.
- Mutation checks, each restored byte for byte, sha256 before and after in the table.
- The leak scan with planted controls: every URL, console message, request URL, storage dump,
  server log and audit row scanned for the run's passwords, session cookie, CSRF token, **every
  API key issued** (PR 1) and temporary password (PR 3); a planted copy of each in
  `sessionStorage` and in a URL fragment must be found, or the run fails.
- Before/after timings, three runs, hardware recorded, where the request path is touched (PR 3
  if Q3 is (b); PR 4 shows it is not touched).
- Per-role screenshots under `docs/pr-console-v2-screenshots/`; `docs/img/` retaken, captions
  saying simulated.
- The full definition of done of CLAUDE.md.

### 10.2 Mutation checks planned (each PR adds its own rows)

| PR | Mutation | Expected to fail |
|---|---|---|
| 1 | metrics read skips `requires` for one application of the page | per-role test, cross-organization test |
| 1 | a missing organization answers 404 on the metrics read | missing = forbidden byte test |
| 1 | an empty window answers error rate 0 | no-requests test |
| 1 | an incomplete minute is summed as complete | incomplete test |
| 1 | "Add application" drawn without the probe | vitest per role, viewer e2e |
| 1 | the issued key written to `sessionStorage` | leak scan (planted control proves the scan) |
| 2 | application metrics answered for an application of another organization | cross-organization test |
| 2 | an old hash mapped to the wrong tab | `routes.ts` round-trip test |
| 3 | Clients read answers an organization's per-application rows | fixed-fields test |
| 3 | the recorder adds organization counts under the wrong organization | recorder test |
| 3 | org creation accepts an existing organization | Q6 test |
| 4 | a model with no price summed as 0 | no-price test |
| 4 | a price row accepted with `effective_from` in the past | no-backdating test |
| 4 | a past period priced with the newest row | history test |
| 4 | `prices.write` allowed without step-up | step-up test |

A mutation no test catches stops the work and is reported, unless it is dead code or a duplicate
check, which is fixed and listed.

## 11. Out of scope

Remote access and authenticated monitoring (A8); Team and user screens (users stay on
`scripts/manage.py`); vLLM; invoices; changing budgets to the new prices; any permission change
not approved here (Q1, Q4) or in PR 5's own plan.

## 12. Questions

**Q1. What the owner sees on one client.** Today's table gives platform_admin per-organization
aggregates only (F1, F2).
(a) The client's own row over time, organization totals only, no application list, nothing
editable. No table change. (b) A per-application metrics view (ids, names, counts) under a new
platform row: a widening that contradicts docs/28 and its test. (c) Per-application views only
inside act-as (PR 5).
**Recommendation: (a) now, (c) later.**

**Q2. Open incidents and the health dot on Clients.** platform_admin is denied
`incidents.read_organization`. (a) Serve **counts by state only** (pending, firing, acknowledged:
no rule, application or text) inside the `usage.aggregate` read, as aggregate data per
organization; the docs/28 note says so. (b) Leave incidents off Clients; the dot then shows only
"traffic / no traffic".
**Recommendation: (a).**

**Q3. 24-hour windows.** (a) 60 minutes only, no recorder change. (b) Hour and organization keys in
the recorder, kept 26 hours, measured before and after (section 3). (c) A durable hourly table.
**Recommendation: (b), in PR 3**; PRs 1 and 2 show 60 minutes and gain 24 hours then.

**Q4. Who edits prices.** (a) Two new rows, `prices.read` and `prices.write`, both
`platform_admin@platform`, write with consume-on-use step-up; the console editor and the CLI.
Organizations see prices only through their cost reads. (b) CLI only, no new row; the owner sees
prices only in cost figures.
**Recommendation: (a)**, approved here as the one permission change of PRs 1 to 4.

**Q5. Chat answered by an organization's own hosted provider.** (a) Tokens shown under "your
providers" with "billed by your provider", no estimate. (b) An estimate from the prices the
organization entered on its provider (`price_input_per_mtok`, `price_output_per_mtok`), labelled
as theirs.
**Recommendation: (a)**: the operator's table should not appear to price someone else's bill.

**Q6. Creating an organization from Clients** (section 8). A, CLI; B, organization only; C,
organization with its first org_admin in one audited, step-up call, new organizations only;
D, a general roles route.
**Recommendation: C.**

**Q7. What this supersedes.** The old plan's P3 (users screen, applications-and-keys screen,
organizations and roles routes), Q5 (a) (the three roles routes) and addition 1 (a fresh install
end to end in the browser) are replaced: users stay on the CLI, no roles route (section 8, D),
and addition 1 holds only if Q6 is C. (a) Yes, superseded as written. (b) Keep the users screen too.
**Recommendation: (a).**

**Q8. The mode column.** The mode (S12) has its own table and no read route at all (F12); the
settings reads do not include it. (a) Add the effective mode (`local_only` or the hosted modes,
an enum, nothing else) to each row of the existing `applications.list` answer: the organization's
viewer then reads it for the first time, a setting it can see but not change. (b) Drop the column.
**Recommendation: (a)**: the mode says where prompts may go, which a reader of the organization
should be able to see.

**Q9. The plan's file.** (a) This new file, with section 15 of the old plan pointing here. (b) All
of it folded into `docs/console-integration-plan.md`.
**Recommendation: (a).**

## 13. Answers (approved 2026-11-10)

- **Q1: (b).** The owner sees per-application metrics for a client, through **one new
  platform_admin row**, metrics only:
  - `usage.read_client_applications`, level organization (the client), allowed
    `platform_admin@platform` only, no step-up, wired console. It lands in PR 3 **in its own
    commit**, marked "permission row", holding only `permissions.py`, the regenerated matrix,
    docs/28's table and the tests that pin it, so it can be reviewed alone.
  - Its two routes (4.1) answer only from a fixed allowlist: application id and name, the window,
    and the counts, rates, histogram percentiles, open-incident counts by state, tokens and
    estimated cost. **Never** a key or key id, a setting, a mode, a channel or its target, a rule,
    an incident's text or id, a provider, a prompt or a response. A test compares each answer's
    field names with the allowlist exactly.
  - An application is looked up inside the client named in the path; a missing application, one
    of another organization and a missing organization answer the same 403 bytes as a forbidden
    one, for every role (test).
  - **Every other refusal stays and is pinned** by a test that asks each as platform_admin, on an
    existing client, through the real routes: `applications.list`, `applications.read`,
    `application_keys.list`/`issue`/`rotate`/`revoke`, `settings.read`/`write`,
    `org_settings.read`/`write`, `alert_rules.read`/`write`, `alert_channels.read`/`write`,
    `incidents.read`, `incidents.read_organization`, `incidents.acknowledge`,
    `usage.read_organization`, `usage.read_application`, `mode.set_*`, `provider_keys.*`. Each
    answers 403 `forbidden`. docs/28's "nothing at application level is ever allowed to it" stays
    true: the new row is at organization level and its test is kept.
  - Mutation checks for the row: allowed for org_admin; the allowlist gains a field; a missing
    application answers differently from a forbidden one; the row granted step-up-free write.
  - **Act-as stays the only way** for the owner to change anything in a client.
- **Q2: (a).** Open incidents on Clients and the client view as counts by state only.
- **Q3: (b), in PR 3,** with before/after timings. PRs 1 and 2 show 60 minutes, labelled "last 60
  minutes" on every figure.
- **Q4: (a).** `prices.read` and `prices.write` in PR 4, in their own marked commit.
- **Q5: (a).** **Q6: C.** **Q7: (a).** **Q8: (a).** **Q9: (a).**
- The ten decisions listed in #48's description are accepted.

## 14. PR 1: decisions made from the code, before its code (2026-11-11)

Each is listed in PR 1's description so it can be objected to. None changes who may do what.

1. **"Add application" follows the capabilities answer, not a refused-body probe.** The rule
   that a direct URL to a screen a session cannot use draws the landing with no request for
   that screen's data (10.1) needs the shell to know before the screen asks anything. So
   `applications.create` joins `NAVIGATION_ACTIONS` (no step-up flag), pinned to
   `GET /v1/admin/orgs/{org}/plans`, the Add form's first read, which declares that action. The
   answer is the gateway's `authorize`, as for every navigation item.
2. **Issue, Rotate and Revoke follow the key list's answer** (`application_keys.list`), pinned
   equal in `tests/test_console_probes.py`'s `SIBLINGS`. A write probe of a step-up action is
   refused `step_up_required`, and every such refusal is an audit row
   (`test_a_channel_write_probe_would_be_audited`); Revoke has no body to refuse.
3. **The window is the last 60 complete minutes**, the evaluator's own (a minute is complete
   25 s after it ends), and the figures are `signals.evaluate`'s: requests, `error_rate` (5xx
   over requests) and `latency_total_p50`, with the alerts' minimums, so the list and the
   alerts never disagree. Below a minimum the figure is "too few to judge", never 0.
4. **A p50 above the histogram's last edge** is answered `above_range`, not a number: JSON has
   no infinity.
5. **The organization metrics read answers one page of application rows** (the list's paging,
   the same order), with no organization totals: those come with the organization keys of PR 3.
6. **`window` accepts only `60m`** in PR 1; `24h` comes with PR 3.
7. **Since a restart:** with in-memory rollups, a window that starts before the process started
   carries `since_ms`, shown "since HH:MM UTC (restart)".
8. **The status dot is a pure frontend function** of the incident counts, the evaluator's
   heartbeat and the request count, always with its word beside it (firing, attention, ok, no
   requests, unknown); it decides nothing.
9. **Incident counts leave out the built-in `channel_delivery_failing` incidents** (kept under
   the empty application id): they belong to no application; the Incidents screen shows them.
10. **Correction to F12:** an application's settings read already returns its mode
    (`mode.effective`), so Q8 shows the organization's readers nothing new. The list reads the
    mode per application of the page (at most 100 reads), timed in PR 1.
11. **Landing in PR 1:** an organization session lands on Applications. A platform session and
    an application-only session keep Home until PR 3 (Clients) and PR 2 (the application page).
12. **Until PR 2:** a row's name opens the application's settings, and "API keys" joins the
    navigation with the application chooser (`#/keys`, then `#/app/<id>/keys`), as Alert rules
    has; PR 2 moves both under the application page.
13. **The e2e drives real traffic:** the org_admin spec issues a key through the page, sends
    requests with it, and waits for the next complete minute to show them. The key is in the
    leak scan's list, with its own planted control.

## 15. PR 3a (Clients): decisions made from the code, before its code (2026-11-19)

PR 3 is split: **3a** here (Clients, the owner's client view, the recorder's hour and
organization keys, the organization list); **3b**, plan-first, creates an organization (Q6 C) and
is not in 3a. This section is 3a's plan; **it adds routes and a permission row, so no code is
written before it is approved.**

### 15.1 Routes (all under `/v1/admin`, local only, `requires()` with one action, `no-store`)

| Route | Action | Answers (fixed fields, nothing else) |
|---|---|---|
| `GET /v1/admin/orgs?after=&limit=` | `organizations.list` (exists; `wired` to `console`) | id, name, kind, enabled, application count; a page at a time |
| `GET /v1/admin/platform/usage?window=60m\|24h&after=&limit=` | `usage.aggregate` (exists; `wired` to `console`) | per organization of the page: requests, error rate, fallback rate, open-incident counts by state (Q2), the window and whether it is incomplete; and the platform's totals over the same window |
| `GET /v1/admin/platform/orgs/{org}/applications/metrics?window=60m\|24h&after=&limit=` | **new row** `usage.read_client_applications` | per application of that client: id, name, requests, error rate, fallback rate, p50, open-incident counts by state |
| `GET /v1/admin/platform/orgs/{org}/applications/{app}/metrics?window=60m\|24h` | the same row | that application over time: per minute (60m) or per hour (24h), the same fields |

- **The new row** (section 13, Q1): `usage.read_client_applications`, level organization (the
  client), allowed `platform_admin@platform` only, no step-up, wired console. In **its own
  commit**: `permissions.py`, the regenerated `docs/permission-matrix.md`, docs/28's table, the
  `WHO_MAY_DO_WHAT` pin (59 rows to 60, a new sha256) and the tests that pin it.
- **Its tests** (section 13): each answer's field names equal the allowlist exactly; never a key or
  key id, setting, mode, channel or target, rule, incident text or id, provider, prompt or
  response. A missing application, one of another organization and a missing organization answer
  the same 403 bytes as a forbidden one, for every role. **Every other refusal stays and is
  pinned**: platform_admin asks each action listed in section 13 through the real routes on an
  existing client, and each answers 403 `forbidden`.
- `NAVIGATION_ACTIONS` gains `organizations.list` and `usage.aggregate` (no step-up), so the
  capabilities answer can show **Clients**; the route pins and the agreement tests take the four
  new routes.

### 15.2 The recorder (Q3 (b))

`AlertRecorder.record` takes the organization id `app.py` already has. Each request also adds to
the application's **hour** key, and to the organization's **minute** and **hour** keys: the same
fixed fields. Hours are kept 26 hours, minutes 120 minutes: in memory by the same `_forget` rule
(bounded by the applications and organizations seen), in Valkey by TTL through the flusher's
pipeline (off the request path). Postgres holds no rollups. Measured before and after with
`benchmarks/alert_recorder.py` (the in-memory record, on the request path) and the flush (Valkey),
three runs each, hardware recorded.

**What it makes possible:** the application hour keys give each application a 24-point hourly
series, and the minute rollups already give 60 per-minute points. That would make per-application
series possible for the Overview tab through a per-application read. **3a does not add that read
or the chart**: it says so in its report, as you asked.

### 15.3 The pages (lazy chunks)

- **Clients** (`#/clients`): the platform landing (2.1) and a sidebar item under Platform, shown
  from `organizations.list`. A health strip (the evaluator's heartbeat from `/healthz`, the stores
  from `/readyz`, the platform's totals), then one row per organization a page of 50: name and id,
  kind, enabled, applications, requests, error rate, fallback rate, open incidents by state, the
  health dot (2.4's rule: red firing, amber pending or acknowledged, green neither, grey unknown
  when the evaluator is not ok or the window incomplete). A window switch: last 60 minutes, last
  24 hours. "Platform settings are environment variables" in one line.
- **One client** (`#/clients/<org>`): the banner "Viewing as platform owner: metrics only", one row
  per application with the fields above, and a row opens that application's metrics over time
  (table view; whether a chart is drawn is yours to say, 15.2). Nothing editable, no link to the
  client's settings, rules, channels, incidents or keys.
- Both load on demand; the bundle gate is kept; the initial download and every chunk before and
  after are reported.

### 15.4 What 3a carries

Section 10.1's list, the mutation checks of section 13 for the row (allowed for org_admin; the
allowlist gains a field; a missing application answers differently from a forbidden one; the row
granted step-up-free write) plus one per new route and the recorder's forgetting, the leak scan
with planted controls, per-role e2e and direct addresses, axe in both themes, CI-image visual
baselines each looked at, screenshots per role.

### 15.5 Open, asked before any code

1. Approve 15.1 to 15.4 (routes, the row, the recorder, the pages).
2. **Valkey timings.** The flush's before/after timing needs a Valkey on this machine
   (`docker-compose.valkey.yml`); Docker Desktop is not running here, and starting servers is
   yours. Either you start it and I time it here, or the Valkey numbers come from CI's `valkey` job,
   labelled as CI's hardware, not this machine's.

### 15.6 Answers (approved 2026-11-20)

Sections 15.1 to 15.4 are approved as written, with these conditions:

1. **Bounds**: the Valkey key count and memory for the hourly counts at 5,000 applications over
   26 hours are stated, old hours are shown to expire, and the same bound is stated for the
   in-memory recorder.
2. **The evaluator cycle** is timed again after the recorder change (it was 0.25 to 0.40 s at 1,000
   applications), before and after reported.
3. **Windows from memory** are labelled "since the gateway restarted"; a 24-hour figure never looks
   complete when it is not.
4. **The organization list and the Clients summaries** have a page size and a cap, with tests at the
   cap.
5. **Same bytes** for missing and forbidden on all four new routes, for every role, plus the leak
   scan with planted controls.

**Valkey timing** comes from CI's `valkey` job, labelled as CI's hardware; the request path is timed
here, on SQLite and in memory, against the perf gate. No server and no Docker are started here.

## 16. PR 3b (create an organization): the plan, before any code (2026-11-21)

**Status: proposed. No code is written until this section is approved.** It adds routes, touches
authentication and adds a migration. It implements Q6 C (section 8): one audited call creates a
new organization and its first org_admin, needs a fresh password, works for new organizations only,
and the owner knows the temporary password until the first login.

Every fact below was read from the code at **7dd7f63** (the tree of 3fcc60a, PR 3a). Results marked
*run* came from an in-process script on that commit's test world (no server, no password printed).
Nothing about Postgres concurrency here has been run yet: those are the tests of 16.8.

### 16.1 What the code showed, and what it changes

| # | Finding | Where | Consequence |
|---|---|---|---|
| A1 | **PR 3a is not on `main`.** #57 merged into `console-ia/pr3a-clients` at 01:45:17, after #56 had merged that branch to `main` at 01:44:09. `main` (9fe94f9) has the 3a plan but no Clients code, row, recorder change or docs/57. | `git merge-base`, `gh pr view` | Q1. 3b's code cannot start until it lands. |
| A2 | **The owner can already take over any existing client's admin.** *Run:* platform_admin reset `alice` (north's org_admin) through the existing route, got the temporary password, logged in, changed it, and that session read north's settings (200), which platform_admin itself is refused (403). Alice's own session was ended. | `users.reset_password`, `permissions.py:139`, `app.py:1775` | "Act-as stays the only way" (section 13) and "cannot appoint an admin in an existing client" (section 8) are **not true today**. Q2. |
| A3 | **A second path to the same place.** *Run:* the existing `POST .../users` on an existing, empty *individual* organization made the owner's new user its org_admin. | `users.create`, `_add_user`, `auth.py:586` | The same claim. Q2. |
| A4 | `organizations.create` exists, with step-up but **not** consume-on-use. Q6 C needs consume. | `permissions.py:130` | One changed row and a new `WHO_MAY_DO_WHAT` pin. |
| A5 | `organizations.create` is "wired now" but nothing enforces it: the CLI's `create-organization` calls the store directly, outside the write lock, with **no audit row**. | `scripts/manage.py`, `_store_command` | The CLI moves onto the same service (G4). |
| A6 | `new_organization` accepts any non-empty id. *Run:* `..`, `.` and `a/b` are accepted, and the audit id pattern also allows `.` and `..`. A browser normalizes `..` out of a URL path, so such an id would address a different route. | `organizations.py`, `audit.py` | Ids are made by the server (Q3). |
| A7 | `_add_user` already grants org_admin in an individual organization. A second grant raises a unique violation. | `auth.py:586` | Business grants once here; individual must not grant twice (G2). |
| A8 | `create_user_with_temporary_password` opens its own write transaction, and Postgres refuses a nested one. | `auth.py:548`, `postgres.py:895` | The new call uses `_add_user` inside **one** transaction. |
| A9 | A reset does not re-read the user after hashing. Used for a reissue, it could overwrite a password the admin set meanwhile. | `auth.py:751-762` | G10. |
| A10 | Login creates its session without re-checking the hash it verified, so a login in flight during a reissue ends with a session the reissue did not revoke. It can do nothing (must change password, and the old password no longer verifies) but it should not exist. | `auth.py:1026` | G12. |
| A11 | *Run:* malformed JSON answers 422 **before** authorization, even with no session. It reveals no resource, but it is not the forbidden bytes. | FastAPI reads a declared body first | The new routes parse the body after `requires` (16.2). |
| A12 | A temporary password has no expiry. | `users.py` | Q5. |
| A13 | `test_no_navigation_action_needs_step_up` keeps step-up actions out of the capabilities answer. | `tests/test_capabilities.py` | The button follows `organizations.list`, the same role row, pinned in `test_console_probes.py` as the key buttons are. |
| A14 | The audit log has no organization column and no read route (`audit.read_*` is wired later). An existing `password_reset` row does not name the organization. | `audit.py`, `permissions.py:195` | The new rows name it in their details (16.3). |

### 16.2 Routes and permission rows

| Route | Action | Who | Step-up | Cap | Answer |
|---|---|---|---|---|---|
| `POST /v1/admin/orgs` (**new route**) | `organizations.create` (**changed row**: consume-on-use, wired `console`) | `platform_admin@platform` | consume | 10 attempts an hour per user | 201: the organization, the admin (id, username), `temporary_password`, its expiry |
| `POST /v1/admin/platform/orgs/{org}/first-admin/reissue` (**new route, if Q4 (a)**) | `organizations.reissue_first_admin` (**new row**, organization level) | `platform_admin@platform` | consume | the same counter | 200: username, `temporary_password`, its expiry |
| `GET /v1/admin/orgs` (exists) | `organizations.list` | unchanged | none | page of 50, at most 100 | one more allowlisted field: `first_admin_pending`, `{username, expires_at_ms}` or `null` |

Authorization, in order, for both new routes: session (401), CSRF, password-change gate, roles read
now, resource resolved, `authorize`, then the step-up claim. **Only then** is the body read
(at most 4 KiB) and validated, so a caller without the right gets the same refusal whatever it
sent (A11). The service checks the action again behind `requires`, as the management endpoints do;
inside the transaction it also checks `users.create` and the org_admin grant on the new organization.

The row changes land in **their own commit**: `permissions.py`, the matrix, docs/28, the pin, and
the tests that pin them. Both routes are local only and `no-store`.

**Rate limit.** The existing `user_creation` window (in Valkey when counters are shared), under a
key of its own built from the **user id**. Today's user-creation cap is per session, and a new login
resets it. Every attempt counts, whatever its outcome. Past it: 429
`organization_creation_limited`, audited. A refused attempt gives its step-up window back (existing
rule), so this cap is what bounds probing for taken names and usernames.

### 16.3 Audit rows

No row ever holds the password, its hash, or a refused name or username.

| Event | Rows, in the transaction of the change | Owner's trail (`actor_user_id`) | New organization's trail |
|---|---|---|---|
| Created | `organization_created` (**new action**); `user_created` (`reason: temporary_password`, `must_change_password`); `role_granted` (org_admin) | all three | `organization_id` or `scope_id` on all three |
| Reissued | `password_reset` (`reason: first_admin_reissued`, **new**; `step_up_consumed`, `organization_id`); `sessions_revoked` if any | yes | `organization_id` on both (the second gains it) |
| First password change | `password_changed`; `first_admin_activated` (**new**): from here the owner no longer knows the password | no: the actor is the new admin | `organization_id` |
| Refused | `management_refused` (reason, action) or the existing `step_up_required`, after the rollback | yes | none: nothing was created |

### 16.4 The temporary password

- **Made** on the server by the existing `_temporary_password`: 18 bytes from `secrets.token_bytes`
  (144 bits), 24 base64url characters, drawn again until the password policy accepts it. Nobody
  types or chooses it. Only its argon2id hash is stored.
- **Shown once**, in the body of that one `no-store` POST response, built after the commit. Never in
  a URL, header, log, audit row, error, event, metric or browser storage; no route reads it back.
- **In the browser** it lives only in the dialog's React state, in a read-only text field with
  autocomplete and spellcheck off (so no password manager or cloud spellchecker sees it). It is
  cleared when the dialog closes or the session ends.
- **Forced change at first login** is the existing gate: until the password is changed, every
  `requires` route answers `password_change_required`. The change needs the temporary password,
  a different new one, and passes the policy.
- **If the owner leaves before copying it**, it is gone. The organization stays; its row on Clients
  shows "first admin has not logged in" and a **Reissue** action (Q4): a fresh password, the old one
  dead at once, any session it opened revoked, the rows of 16.3. Reissue works only while the marker
  below exists. Once the admin has changed the password it answers as missing.
- **The marker.** Migration 19 and Postgres schema 8 add one new table (no rebuild),
  `first_admin_pending` (`organization_id` primary key, `user_id` unique, `issued_by`,
  `issued_at_ms`, `expires_at_ms`), written in the create transaction. It is deleted in the same
  transaction as the first password change, and by any other reset of that user. `must_change_password`
  cannot serve as the marker: it is also true for a real admin whose password was reset, and a
  reissue on that account would be a takeover.

### 16.5 Atomicity and races

The create call: count the attempt; validate; read-only pre-check (so a collision costs no hash);
hash **outside** any transaction; then one `write_transaction()` (on Postgres, the global advisory
write lock) that re-checks everything and writes the organization, user, grant, marker and audit
rows; `token.changed()` inside it; commit; only then build the response.

| Gap | What could go wrong | How it is closed | Two-instance test (Postgres) |
|---|---|---|---|
| G1 | Organization created, user not | One transaction for all five writes; any failure rolls all back | Fail each write in turn: no row of any kind remains |
| G2 | Double grant in an individual organization | Grant only for `business` | Both kinds hold exactly one org_admin row |
| G3 | Two owners, the same name | Re-check under the lock, then the `UNIQUE` index; the loser gets 409 `organization_name_taken` | Both instances at once: one 201, one 409, one organization |
| G4 | The CLI writes outside the lock, unaudited (A5) | `create-organization` moves onto the service, in the transaction, audited | CLI path against HTTP, at once |
| G5 | Names differing only by case slip past a check (Q6) | The comparison runs under the write lock, and every writer takes it (G4) | `Acme` and `acme` at once: one wins |
| G6 | Two calls, the same username | Re-check in the transaction, then `UNIQUE`; the loser's whole transaction rolls back, so no empty organization is left | Different names, same username: one organization |
| G7 | Crash mid-way | The transaction rolls back; nothing exists | Kill the connection before commit |
| G8 | Crash or lost response **after** commit | The organization exists and the password is lost. A retry gets 409, never a second organization; **Reissue** recovers | Answer dropped after commit, then reissue |
| G9 | Double submit, or a window used twice | `claim_step_up` is one atomic claim; `changed()` inside the transaction, so a failed commit never gives a used window back | Same session, both instances: one proceeds |
| G10 | Reissue overwrites a password just set (A9) | Inside the transaction: the marker still exists **and** the hash is the one read before hashing; otherwise 409 | Reissue against password change: the admin's password survives or the change is refused, never a mix |
| G11 | Two reissues at once | The same compare: one wins, the other gets 409 and shows nothing | Both instances: exactly one password works |
| G12 | Login in flight during a reissue (A10) | The session is written in a request transaction that re-reads the hash; if it moved, the same `LoginFailedError` through the same steps | Login held at its verify, reissue commits: no live session |
| G13 | Reissue against disable, or the organization disabled | Re-read in the transaction; refused | Both orders |
| G14 | Write lock times out | 503, nothing written, window given back | Hold the lock past the timeout |

### 16.6 Input rules

| Field | Rule | What the form says |
|---|---|---|
| Organization name | `normalize_name`: NFC, 1 to 100 characters, no leading or trailing space, no control, format, zero-width or bidirectional characters. Unique; case-insensitive for new names if Q6 (a). Reserved (case-folded): `default`, `platform`, `admin`, `system`, `root`. | "Up to 100 characters. Shown to you and to this client's admin." |
| Kind | `business` (default) or `individual` | "An individual organization has exactly one member." |
| First admin's username | `normalize_username`: ASCII, 3 to 64 of letters, digits, `.`, `-`, `_`, case-folded. Unique **across all organizations**. Reserved: `admin`, `root`, `system`, `operator`, `platform`. | "3 to 64 letters, digits, dots, dashes or underscores. Usernames are unique across all clients." |
| Organization id | Made by the server, `org_<16 hex>` if Q3 (a); never an input | Not shown as a field |

Unknown fields are refused. A refusal names the field and the rule and never echoes the value.
Look-alike names across scripts are still not prevented (a limit `normalize_name` documents).

**Missing equals forbidden.** Every role but the owner gets the same 403 bytes from both routes,
whatever the body. On reissue, a missing organization, one with no pending admin, one whose admin
has activated and a disabled one all answer those bytes, **for the owner too**.

### 16.7 The console

- **"Create organization"** on Clients, shown when the gateway's answer allows `organizations.list`
  (A13). It opens a native `<dialog>` through `ui/Dialog`: name, kind, username, Cancel, Create.
- **Create** asks for the password first (`guarded(..., true)`, the existing step-up dialog stacked
  over this one) and is disabled while the call runs.
- **The reveal panel** replaces the form in the same dialog:
  - the username and temporary password in read-only fields, each with **Copy**;
  - the warning: **"You will not see this password again. Copy it now and give it to the admin
    over a secure channel. It must be changed at first login."**;
  - the expiry if Q5 (a), and "Until then, you know this admin's password";
  - **Done**, enabled only after ticking "I have copied the password". If the clipboard is refused,
    the panel says so and the field stays selectable.
- **Escape and backdrop do not close the panel** (the `cancel` event is held), and leaving the page
  while it is open raises the browser's warning. Closing it clears the value and reloads the list.
- **Reissue** on a pending row: a confirmation that names the consequence, the step-up, then the same panel.
- The client's heading shows its **name**, since ids are generated (one allowlisted field on the
  client's metrics answer). Under the existing CSP; inside the Clients chunk; the bundle gate is kept.

### 16.8 Tests

- **Per role and direct address:** every role of the test world on both routes, in pytest and e2e;
  the button is absent for all but the owner; `#/clients` by address as in PR 3a.
- **Same bytes:** every role, with valid, invalid and malformed bodies and no body, and each missing
  case of 16.6.
- **Structural (authentication):** every login refusal, the new ones included, takes the same steps,
  plus the early-return mutation.
- **Leak scan with planted controls.** The real password and a planted copy in each place must be
  handled: the real one **absent**, the planted one **found**, so a blind scan fails. Places: every
  other response body, server output and logs, audit rows, `localStorage`, `sessionStorage`,
  IndexedDB, `document.cookie`, the address and its fragment, request URLs, console messages.
- **Postgres, two instances:** one test per row of 16.5, in CI's `postgres` job, on `pg_world(2)`
  with a barrier, so each pair really overlaps. Parity with SQLite for the new store methods.
- **Migration:** 19 up, schema 8 up and rollback, `pg_migrate` copies the new table.
- **e2e, the whole path:** the owner creates an organization, passes step-up, copies the password
  and closes the panel; the new admin logs in, is forced to change the password, reaches
  Applications; the old password fails; Reissue is gone. A second path: leave before copying,
  reissue, the first password is dead. Keyboard, 390 px, axe in both themes, CSP, CI-image baselines
  each looked at, screenshots per role.
- **Timing:** the create call and one login, before and after the marker read, on SQLite, three runs,
  hardware recorded; the perf gate.

**Mutation checks**, each restored and shown by sha256:

| # | Mutation | Must be caught by |
|---|---|---|
| 1 | An existing organization accepted | create tests |
| 2 | The grant made outside the transaction | G1 |
| 3 | Step-up not consumed | row and route tests |
| 4 | `organizations.create` allowed for org_admin | per-role, pin |
| 5 | Body read before `requires` | same-bytes |
| 6 | Individual granted twice | G2 |
| 7 | The name re-check removed from the transaction | G3, G5 |
| 8 | The username re-check removed | G6 |
| 9 | `changed()` moved after the commit | G9 |
| 10 | Reissue allowed with no marker | missing equals forbidden |
| 11 | Reissue's hash compare removed | G10, G11 |
| 12 | Marker not deleted at first password change | reissue after activation |
| 13 | Old sessions not revoked on reissue | reissue test |
| 14 | Login's hash re-check removed | G12 |
| 15 | Temporary password added to an audit row | leak scan |
| 16 | Temporary password logged | leak scan |
| 17 | Cap keyed by session instead of user | cap test |
| 18 | Reserved name accepted | input tests |
| 19 | Expired password accepted, if Q5 (a) | login test |
| 20 | `must_change_password` false on the new admin | forced-change test |
| 21 | Panel closes on Escape | frontend test |

### 16.9 Risks

- **The owner knows the first admin's password until it is changed.** Accepted by Q6 C, made visible
  by `first_admin_activated`, and bounded by Q5. There is no email, so the handover is by hand.
- **A password copied to the clipboard** stays there, under the operating system's control. The panel cannot clear it reliably.
- **Global usernames:** a taken-username answer reveals that the name exists elsewhere. The owner can
  already list users; the cap bounds it.
- **More than 1,000 organizations** turn the platform totals to `above_cap` (PR 3a's cap). Creating
  organizations makes that reachable; nothing breaks.
- **This touches login** (G12, Q5), the most sensitive path here, hence the structural test and two mutations.

### 16.10 Out of scope

A users screen; the roles routes (option D stays rejected); deleting, renaming, disabling or
enabling an organization from the console; inviting by email; act-as (PR 5); prices (PR 4); bulk
creation; preventing look-alike names across scripts.

### 16.11 Questions, before any code

**Q1. Landing PR 3a.** Its code is on `console-ia/pr3a-clients`, not `main` (A1). (a) Open a PR
from that branch to `main`, wait for CI, you merge it, then 3b builds on `main`. (b) Stack 3b's code on
that branch. **Recommendation: (a).**

**Q2. The owner's existing path into clients (A2, A3).** (a) A small separate PR **before** 3b's code:
remove `platform_admin@platform` from `users.reset_password` and `users.create`, keeping the
operator's CLI as the recovery path, and add both to section 13's pinned refusals. (b) The same
inside 3b, in its own commit. (c) Keep today's behavior and correct sections 8 and 13 to say the owner
can take over a client's admin, audited. **Recommendation: (a)**: it is a security fix to shipped
behavior, and Reissue is only "narrow" if the broad route is gone.

**Q3. The organization's id.** (a) Made by the server, `org_<16 hex>`, as application ids are. (b)
Typed by the owner, as a checked lowercase slug: readable URLs, one more field, reserved words and a
collision answer. **Recommendation: (a).**

**Q4. Reissue.** (a) The narrow new row and route, only while the first admin is pending, with the
marker table. (b) Reuse `users.reset_password`: no new row or table, but it depends on the broad
power Q2 removes. (c) No reissue: a lost password needs the operator's CLI. **Recommendation: (a).**

**Q5. Does an unused temporary password expire?** (a) After 72 hours; Reissue makes a new one.
Adds one check to login. (b) Never, as today, until the first login. **Recommendation: (a)**: an
org_admin credential the owner knows should not live forever.

**Q6. Name uniqueness and reserved words.** (a) New names unique ignoring case, plus the reserved
lists of 16.6. Existing names are untouched. (b) Exact match only, as today, with the reserved lists.
**Recommendation: (a)**: `Acme` and `acme` as two clients is an operator's mistake waiting to happen.

**Q7. Kinds.** (a) Both `business` and `individual`. (b) `business` only for now.
**Recommendation: (a)**: the code handles both, and the test for G2 covers it.

### 16.12 The approval, and the order of work

Approved on 2026-11-21: Q1 to Q7, each as recommended, with these additions.

1. **PR 3a lands first** (#59, merged).
2. **Then a small separate PR, before any 3b code:** remove `platform_admin@platform` from
   `users.reset_password` and `users.create`, which also takes both away over HTTP. The operator's
   CLI stays the recovery path. It updates `docs/28-roles.md` and the who-may-do-what pin, tests
   that the owner is refused with the same bytes as a forbidden request, and tests that the CLI
   still recovers a locked-out admin. Nothing else.
3. **Only then 3b's code**, as a draft on `main`.
4. The two-instance Postgres tests for the 14 gaps must run in CI's `postgres` job, and the PR must
   show them running and passing. For an individual organization, the two-instance test must cover
   the seat rule.
5. Every PR lists every check run on its head commit from both workflows, with ids, before it is
   reported, and is marked ready only when all are green. Nothing is merged by the agent.

**What the owner keeps after step 2.** Removing the two rows does not make the owner metrics-only
in a client. Over HTTP it can still list, disable, enable and unlock a client's users, read a
provider's status and disable or enable a provider. None of these gives it a credential or a
session in the client, and changing them is outside step 2. `docs/28-roles.md` says so.
