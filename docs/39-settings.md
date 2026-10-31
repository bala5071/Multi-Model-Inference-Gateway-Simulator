# 39. Per-application settings: storage and the read path

PR S1 of `docs/settings-and-alerts-plan.md`. Settings S1 to S11 of the plan, stored with every
version, decided by one function under four levels, and read with the API key in the same
statement, so a request makes no extra store call. No endpoints (PR S2), no hosted settings and
no `hosted_policy` (PR S3, after scale-out PR 2b), no alerts.

## What is stored

Migration 14 (SQLite) and schema version 3 (Postgres) add two tables, new tables only:

- `settings_current`: one row per organization or application ever set: its version, its
  document, the document format's `schema_version`, when it was last written.
- `settings_versions`: every change, with who (`changed_by_user_id`, null for the CLI), how
  (`cli`, `http`, `revert:<n>`), when (on Postgres the database's clock), the old and the new
  document, and the names of the fields changed. The latest 200 per scope are kept; the write
  that makes a newer one drops the oldest.

A document is canonical JSON: only the settings that are set, sorted keys, no whitespace. It
never holds `null`: a setting not set takes its default, so "no limit" cannot be chosen where
the plan copy has one. Every write names the version it was made from; another current version
is `settings_version_conflict` and nothing is written. Revert writes version `N + 1` with
version `N - 1`'s document; version 1 has nothing to go back to (`no_previous_version`).

Rollback: `MigrationRunner.rollback(13)` on SQLite; `rollback_schema(db, 2)` on Postgres, the one
version there with a way back (versions 1 and 2 keep `migrate-to-sqlite`). `migrate-to-postgres`
and `migrate-to-sqlite` copy and compare both tables like every other.

## Four levels, the stricter one wins

The platform (this instance's values: its tiers and their names, the generate timeout, the chat
timeouts), the plan copy (the application's own copy of its plan: models, budget,
`max_tokens`, `requests_per_minute`, changed only by the operator, Q1), the organization (caps
for all its applications), the application. `effective_settings` in `gateway/settings.py` is
the only function that decides a value: the application's (or the default) lowered to every
cap in force, each value with its source and, when a cap lowered it, which.

A write is refused (`setting_looser_than_cap`, naming the setting and the level) if a setting
it **changes** is looser than a cap in force. A value it does not change was checked when it
was written, and a cap tightened since clamps it when it is read: the stored value is kept and
comes back if the cap is loosened. The check and the write are one `write_transaction()` (the
write lock on Postgres), so a cap tightened on another instance in between waits or times out.

## On the request path: read with the key

`lookup_key` joins `settings_current` twice (the organization's row, the application's row) in
its own statement on SQLite and Postgres; the in-memory store returns its own. The rows go with
the application `authenticate` returns, and the handler decides the settings once, before
anything else (`keyed` in `app.py`):

| What the key lookup read for a scope | The request |
|---|---|
| No row (never set) | The defaults: today's values |
| A row that does not parse, fails validation, names a setting this code does not have, has a newer `schema_version`, or is not in canonical form | 503 `state_unavailable`, byte for byte the answer a store that cannot answer gives; never a looser value. Logged as `settings unreadable: <scope> <id> version <n>, check <check>`, never the document |
| The lookup fails | 503 `state_unavailable`, as before |

Either row unreadable refuses the request, on `/v1/generate`, chat and the usage endpoint
alike. A row that is not read at all is refused the same way, never taken for "no settings".
Parsed documents are kept per instance by scope, version and the text's digest (4,096 at most):
a version never changes, and a row rewritten in place has another digest.

Load control holds no keys, so it reads the two rows itself and decides them the same way; it
is not the request path. A server with a database gives `create_app` its settings store for it,
and load control is refused at start without one.

## What each setting does

| # | Setting | Applied |
|---|---|---|
| S1 | `allowed_tiers` | the models the router may pick, in the plan's order; none left: every model and quality 403 `model_not_in_plan` |
| S2 | `model_aliases` | the application's own names for its tiers, tried before the registry's; one whose tier is not allowed, or that takes a registry name, is not in force |
| S3 | `default_quality` | for a request that names neither a model nor a quality |
| S4 | `allow_downgrade_default` | for a generate request that does not say `allow_downgrade` |
| S5 to S7 | `requests_per_minute`, `monthly_token_budget`, `max_tokens` | the rate limit, the budget and the largest `max_tokens`, as the plan copy's were |
| S8 | `default_max_tokens` | see below |
| S9 | `generate_timeout_ms` | the timeout of a generate request |
| S10, S11 | `chat_first_token_ms`, `chat_total_timeout_ms` | the operator adapter's first-token and every chat attempt's total timeout; a hosted attempt keeps its provider's first-token budget (S16 is PR S3) |

Every setting applies from the next request; none changes a request already running.

## `default_max_tokens`, and `X-Max-Tokens-Applied` (Q4)

When a request names no `max_tokens` (`max_output_tokens` inside), the effective S8 is applied
as its limit: the reservation bounds it, the backend gets it (the simulator clamps its output to
it, llama-server and hosted providers are sent it as `max_tokens`), and generation stops at it
(`finish_reason: "length"`). With S8 unset it is the effective `max_tokens`, as today; with
neither, no limit applies, as today.

Every answer of `/v1/generate` and `/v1/chat/completions` that got as far as choosing a model
carries `X-Max-Tokens-Applied`: the number applied, or `none` when no limit applies. That
includes streamed answers (a header, before the first event) and the model's own failures
after the choice (`queue_full`, a timeout). Refusals made before (401, 403, 404, 422, 429 for
the rate limit or the budget, 503) do not carry it. It is the one change a client sees when no
setting is set.

A hosted provider's tokens cannot be counted as they stream: if its reported usage is over the
applied value, what it reports is settled and a warning is logged (`provider <id> answered <n>
completion tokens over the applied max_tokens <m>`); the answer is not altered.

## The CLI

`scripts/manage.py settings-show`, `settings-set` (`--set name=JSON`, `--unset name`,
`--replace` to write over a row that cannot be read) and `settings-revert`, each on
`--application` or `--organization`, each change audited (`settings_changed`,
`settings_reverted`: the version and the names of the fields, never their values) with the
operator as the actor. The CLI checks against `default_platform_settings()`, the values the
server factories run with; a test keeps the two equal.

## Store calls and time

Store calls per request, counted by the tests that count them (`tests/test_chat_routing.py`,
`tests/test_budgets_postgres.py`, which now count every settings store method too) and by
`benchmarks/request_store_calls.py`: **unchanged**. A chat request on the gateway's tier makes
the same 4 on SQLite (5 in shared mode), one an organization provider serves the same 12 (13),
and `/v1/generate` the same 2 or 3. The settings come in `lookup_key`'s statement.

MEASURED on this machine (Intel Core Ultra 7 155H, 22 logical cores, 15.4 GB, Windows 11
10.0.26200, Python 3.12.10; SQLite in the temp folder; Postgres 18.6 in Docker Desktop on the
same machine), three runs each, p50 in ms, the range over the runs:

| What | Before (`50446e3`) | After (`2576a46`) | Command |
|---|---|---|---|
| Key check, `authenticate` on SQLite | 0.019 to 0.028 | 0.024 to 0.025 | `python -m benchmarks.key_checks`, base and branch run in turn |
| `lookup_key` alone, SQLite | 0.028 to 0.033 | 0.018 to 0.029 | `python -m benchmarks.durable_calls` |
| `lookup_key` alone, Postgres | 1.253 to 1.362 | 1.110 to 1.142 | the same |
| Chat request, gateway tier, SQLite | 5.502 to 5.978 | 3.776 to 3.857 | `python -m benchmarks.request_store_calls` |
| Chat request, gateway tier, Postgres with shared budgets | 18.924 to 20.782 | 16.193 to 18.210 | the same |
| Chat request, one organization provider, SQLite | 6.373 to 6.732 | 4.177 to 5.567 | the same |
| Chat request, one organization provider, Postgres | 25.628 to 31.051 | 26.725 to 27.092 | the same |

No row's "after" range starts above its "before" range: the join costs no time these runs can
tell apart from their spread. Lower "after" figures are the machine's state, not a saving; the
whole-request rows ran at different times of the same afternoon. The first build copied the
application in `authenticate` (`dataclasses.replace`) and its key check measured 0.044 to 0.051,
above the base's range then (0.035 to 0.036, three runs earlier that afternoon), which the
plan's rule (section 20) stops on; the store now builds the application with its rows once,
and the first row is that build, measured with the base and the branch run in turn so that
both see the same machine.

## Not built here

The settings endpoints and their permissions (PR S2), the hosted settings S13 to S16 and
`hosted_policy` (PR S3), alerts. The `context_tokens` ceiling on `max_tokens` of the plan's
section 1.1 (see the PR's decisions), and the `gateway_max_tokens_exceeded_total` metric of its
section 5.1 (a log line instead, so that the monitoring API does not change in this PR).
Monitoring still shows each application's plan copy, not its effective settings.
