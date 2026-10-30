# 36. The durable stores on Postgres

Scale-out plan, pull request 3 (docs/scale-out-plan.md, section 8, and "PR 3: Postgres,
settled before building"). Every durable store protocol of note 34, implemented on Postgres in
`gateway/postgres.py`, with the write lock of section 3.6, the clock of section 3.8, and a
migration from SQLite and back. SQLite stays the default, and nothing on the default path
changes: without Postgres configured, nothing connects to it.

**Shared mode is refused until PR 3b.** `GATEWAY_STATE=shared` stops the server at start (both
factories, simulated live mode included): until the budgets are shared, each instance would
enforce them on its own totals, and two instances could each spend one budget. So in this PR
Postgres is used by the tests, by `scripts/manage.py migrate-to-postgres` and
`migrate-to-sqlite`, and by `benchmarks/durable_calls.py`, never by a running server.

## What is in Postgres

| Store (note 34) | Tables | Ordered by |
|---|---|---|
| `PostgresUsageStore` | `usage_events` | `seq` (identity) |
| `PostgresApplicationStore` | `organizations`, `applications`, `api_keys` | `ord` (identity) |
| `PostgresAccountStore` | `users`, `sessions` | username; `ord` for sessions |
| `PostgresRoleStore` | `role_assignments`, `app_roles` | `ord` |
| `PostgresAuditStore` | `audit_log` | `seq` |
| `PostgresProviderStore` | `provider_configs`, `provider_keys` | as SQLite |
| `PostgresModeStore` | `modes` | as SQLite |

One baseline, `PG_BASELINE`: the tables and columns of SQLite migrations 1 to 13, in the same
order and with the same checks. Where the SQLite stores list rows in `rowid` order, the
Postgres tables have an `ord` identity column that keeps insertion order. A server-free test
compares the two schemas column by column. The schema is applied under an advisory lock
(`SCHEMA_LOCK`), so instances that start together apply it once, and a database whose schema
version is newer than the code is refused.

Not in Postgres: the counters (note 35: Valkey, or each process's memory) and the budget
totals, which the ledger keeps in memory per process until PR 3b.

## Two pools and one write lock (section 3.6)

- `write_transaction()` is for **management writes** only. It takes a connection from the
  management pool (2 by default), opens a transaction, and takes the transaction-level advisory
  lock `WRITE_LOCK` with `pg_try_advisory_xact_lock` in a loop on the injected clock. After
  `GATEWAY_STATE_LOCK_TIMEOUT_MS` (default 2000) it gives up: `StateUnavailableError`, nothing
  written, `503 state_unavailable`. The holder is bounded by `SET LOCAL statement_timeout`
  (5 s) and `idle_in_transaction_session_timeout` (10 s), so a stuck holder releases the lock.
  The seven management rules of section 3.6 hold under it on any number of instances.
- **Every other call uses the request pool** (10 by default), so a management write that holds
  the lock and every management connection takes nothing a request needs.
- `request_transaction()` (new on `AccountStore`) is for **a request's own writes**: a wrong
  password counted, with its lockout, the closing of step-up windows and the audit row. It is a
  transaction on the request pool **without** the write lock; the statement that decides locks
  its own row. On SQLite it is `BEGIN IMMEDIATE`, the same as before. Without it a wrong
  password would have waited for every management write, against section 3.6 (decision 3 of
  the PR).

The lockout count (Q3) is one statement: a CTE that reads the clock once and locks the user's
row (`FOR UPDATE`), then the `UPDATE ... RETURNING`. The step-up claim is one `UPDATE ... FROM
(SELECT ... FOR UPDATE)`: two instances claiming one window get it once.

## Time (section 3.8)

`gateway_now_ms()` is the one clock of every window held in Postgres. In production it is
`clock_timestamp()` in epoch milliseconds; a test schema replaces it with one that reads a
row the test sets.

- The lockout count and the step-up claim read it inside their statement; the `now_ms` the
  instance passes is not used.
- The services read it through `PostgresDatabase.now_ms()`, the bundle's `StoreClock`, once
  per operation: a session's check returns it in `SessionContext.now_ms`, and the step-up
  window opened in that request uses the same value.
- A key lookup returns it in the same query (`KeyLookup.now_ms`), so key expiry costs no
  extra round trip.

On SQLite and in memory the `StoreClock` is the injected epoch, as before.

## Missing is forbidden: Postgres down, per endpoint

Any connection that cannot be had, is lost, or times out (asyncpg's own limits: 2 s for a
query and for a pooled connection) raises `StateUnavailableError`, never a default. Tested
with a pool that refuses every connection (`test_every_endpoint_while_postgres_is_down_...`),
and with a real pool closed under the app:

| Endpoint | Answer while Postgres is down |
|---|---|
| `POST /v1/generate`, `POST /v1/chat/completions` (any key, real or made up) | 503 `state_unavailable`, the same bytes whatever the key; no model sees it |
| `GET /v1/auth/me`, `GET /v1/auth/sessions`, `POST /v1/auth/logout`, `POST /v1/auth/step-up` | 503 `state_unavailable` (the session cannot be checked) |
| `/v1/admin/orgs/{org}/...` (reads and writes) | 503 `state_unavailable` (the session is checked first) |
| `POST /v1/auth/login` | 401 `invalid_credentials`, the same bytes as any refused login, after the same steps: one lookup, one verify against the dummy hash. No audit row (it would go to the database that is down) |
| `GET /healthz` | 200: liveness |
| `GET /readyz` | 503 `not_ready` |
| `GET /v1/models` | 200: it holds no durable state |

A refused login takes the same steps as every other refusal:
`test_every_login_takes_the_same_steps_whatever_is_wrong` has this case, and its early-return
mutation (no verify) fails it.

## A limit on every test

`gateway_test_timeout_s` in `pyproject.toml` is 60 seconds; `-o gateway_test_timeout_s=N`
changes it for one run. It is built in `tests/conftest.py`, without a dependency:

- An async test past the limit is cancelled (`asyncio.timeout`) and fails, naming the limit.
  Every wait on a lock or on another instance is an await, so this is how such a test ends.
- Any test still running at twice the limit (a sync test, or a wait that cancelling cannot
  reach) ends the run: a watchdog thread suspends pytest's capture, prints the test's id and
  every thread's traceback, and exits with status 1.

`tests/test_suite_timeout.py` runs a test that never finishes of each kind
(`tests/hang_cases.py`, which the suite does not collect) with a limit of one second.
The Postgres tests' own waits stay bounded at 10 seconds (`STATE_BOUND_S`), so they report which
state never came, well before the suite's limit.

## Which requests touch Postgres on the chat path

Counted by `test_the_durable_store_calls_one_chat_request_makes` (store calls are the same on
SQLite and Postgres; on Postgres each is one statement and one round trip, one after another):

| Chat request | Store calls |
|---|---|
| Served by the gateway's tier, the first of a period for its application | 7: `lookup_key`, `usage_by_model` (the period's totals, once per process), the two modes twice, `append` |
| The same, after the first | 6: `lookup_key`, the two modes twice, `append` |
| Served by one organization provider | 20: `lookup_key`; the candidates twice (the modes, `list_for`, `get_key`); the modes; `get_key` twice (skip and lease); the modes three times (the egress scope); `append` |

`/v1/generate` makes `lookup_key`, the period's totals on the first request, and `append`.
None of them takes the write lock or a management connection. The batched `last_used_at`
write (note 20) is one more statement per flush, not per request. PR 3c reads each mode
once per request: 12 of the 20 calls of a provider-served request are those re-reads.

## Migration and rollback

```sh
GATEWAY_POSTGRES_URL=... uv run python scripts/manage.py migrate-to-postgres --dry-run
GATEWAY_POSTGRES_URL=... uv run python scripts/manage.py migrate-to-postgres
GATEWAY_POSTGRES_URL=... uv run python scripts/manage.py migrate-to-sqlite --to back.db
```

- **To Postgres:** the server stopped (the command takes the database's process lock), the
  SQLite database at the latest migration, and Postgres new: no schema yet, or one with nothing
  but the `default` organization. The schema is created if needed and every table copied, in
  one transaction under the write lock: ids, `seq` values, HMACs and sealed keys byte for byte,
  rows in their SQLite order. Every table is compared with SQLite's before the commit; any
  difference rolls everything back. The sequences continue after the copied `seq` values.
- **The SQLite database is not changed.** It is the rollback for as long as the gateway has not
  written to Postgres; after that, `migrate-to-sqlite` is.
- **Back to SQLite:** into a new file only, never over one, from one snapshot (repeatable read),
  migrated to the latest version and compared the same way; the file is removed if anything
  fails.
- **Dry runs** make every write and every check and keep nothing: to Postgres in a transaction
  rolled back at the end, back to SQLite into a database in memory.
- Each prints table names, row counts and sha256 digests of the rows, never a value. The
  address is never printed (it may hold a password).

## Local Postgres for the tests

```sh
docker compose -f docker-compose.postgres.yml up --detach --wait
GATEWAY_TEST_POSTGRES_URL=postgresql://postgres@127.0.0.1:5433/postgres uv run pytest -m postgres
docker compose -f docker-compose.postgres.yml down
```

**A URL without a password is refused at start-up** (`check_postgres_url`, called by
`PostgresDatabase.connect` before any connection is tried, so no caller can skip it). Only a
test database may go without one, and only when `GATEWAY_POSTGRES_TEST_NO_PASSWORD=1` says so.
The tests pass that setting explicitly (`TEST_DATABASE_ENV` in `tests/postgres_helpers.py`);
the CLI and the benchmark read it from the environment. The password must be in the URL, after
the user name or as the `password` parameter: `PGPASSWORD` and a password file do not count,
because the URL is the one secret setting. The refusal names the setting and nothing of the URL.

The image is pinned by digest (`docs/licenses.md`). It is published on 127.0.0.1 only and
trusts every connection: it has no password, so there is no credential to keep or reuse, and it
is for tests only, never exposed. Its data lives on a tmpfs at `/var/lib/postgresql`, the path
the image declares as a volume, so no volume is created and `down` leaves nothing behind
(checked on 2026-10-29 with `docker inspect`: no mounts, the tmpfs at that path, the port bound
to 127.0.0.1:5433 only). Each test works in a schema of its own (`gwtest_<random>`),
dropped afterwards; CI's `postgres` job runs them.

## Threat-model additions

- **The address is a secret** (it may hold a password): from the environment only, never in an
  error, a log record, a repr or the CLI's output. `PostgresDatabase.connect` reports only the
  error's type. A leak scan plants a password in the URL, uses and breaks the stores, and finds
  it nowhere; planted in a row, the same scan finds it.
- **No password by mistake:** a production URL that lost its password would connect to a
  server that trusts the connection. The start-up guard refuses it unless the test
  setting is on.
- **The write lock is a single point of contention for management writes.** A slow or stuck
  holder delays them by at most the lock timeout, then they are refused (503) with nothing
  written. Requests never take it.
- **A wrong password costs a request-pool connection and a row lock**, not the global lock: a
  flood of wrong passwords for one account serializes on that account's row only.

## Check, then write: every write path audited

On SQLite most writes were safe because nothing awaited between a check and its write. On a
shared store every await is a gap another instance can write into. Every service method, the CLI
and the bootstrap admin were listed by their awaited store calls (an AST pass over `auth.py`,
`management.py`, `providers.py`, `modes.py`, `apikeys.py`, `audit.py`, `ledger.py`,
`applications.py`, `valkey.py`, `app.py` and `scripts/manage.py`), and each read followed by a
write was looked at.

| Path | Check, then write | Now | Shown by (mutation) |
|---|---|---|---|
| The seven rules of section 3.6 | last org_admin and platform_admin; an individual organization's one member; a new user's name and seat; the application-mode bound; provider changes with their audit rows; the active-key cap; key issuing | one write transaction (since PR 1) | the write lock skipped (M4) fails the two-instance rules test and the bootstrap test; the mode bound and the provider audit rows each have their own two-instance test (M26, M27) |
| `create_user` (CLI, bootstrap admin) | the seat and the name, then the insert, with the hash between | checked again in one write transaction | two-instance seat test (M11, M25) |
| `bootstrap_admin` | no platform_admin yet, then the creation | checked again in the creation's transaction | two instances bootstrapping at once (M12) |
| A login with the bootstrap password | unused, then marked used, with the verify between | one statement (`use_bootstrap`) | two logins on two instances (M13; M23 for SQLite) |
| The hash upgrade at login and step-up | the hash verified, then replaced | one statement, only if still that hash | the store parity test (M14; M24 for SQLite) |
| `change_password` | the session, the account and the current password, then the new hash, with two hashes between | checked again in one write transaction | a reset made during the verify (M15) |
| A password reset | none (several writes) | one transaction | as above |
| `create_application` | the organization enabled and the name free, then the insert | one write transaction | the organization disabled from another instance (M16) |
| `manage.py disable-organization` / `enable-organization` | it exists, then the switch | one write transaction | structural (M21) |
| `rotate_key` | the key active, then the replacement | checked again in its transaction; `revoke_key` takes the lock | a revocation from another instance (M17) |
| A provider update, a provider switch | the row the request read, written back whole | applied to the row as it is now, in one transaction | the platform switch turned off meanwhile (M18, M19) |
| A connection test | the row, then the row with the capabilities | one transaction, recorded only on the destination probed | the platform switch turned off meanwhile (M20) |
| `manage.py vault-drop-unreadable` | unreadable, then deleted | one write transaction | structural (M22) |

Looked at and left as they are:

- **A successful login resets the failure count** from the row read before the verify. The
  password was right; a lock placed by wrong passwords in that moment is cleared, as it would
  be by the same login a moment later.
- **`step_up` opens a window on a session revoked meanwhile:** the session cannot be used, so the
  window cannot be either.
- **No mutable precondition:** `enable_user`, `unlock_user` (writes only), `grant_role` (the
  user's organization and an application's roles never change), `ApiKeyService.issue` (the
  application's existence is a foreign key), `seed_demo_applications`, `create-organization`
  (unique keys), `touch_session` (`GREATEST`).
- **The budget check, then the usage row:** PR 3b; shared mode is refused until then.

## Timing

MEASURED on this machine (Intel Core Ultra 7 155H, 22 logical cores, 15.4 GB, Windows 11,
Python 3.12.10), at commit `d757093`, with
`GATEWAY_POSTGRES_URL=postgresql://postgres@127.0.0.1:5433/postgres uv run python -m benchmarks.durable_calls`,
three runs of 2000 calls each after 20 warm-up calls. SQLite is a file in the temporary folder;
Postgres is the compose file's container under Docker Desktop, reached through its port on
127.0.0.1, so every round trip crosses into Docker's VM. Ranges are over the three runs.

| What | SQLite p50 ms | Postgres p50 ms | Postgres p99 ms |
|---|---|---|---|
| One read (`lookup_key`) | 0.015 to 0.016 | 1.065 to 1.081 | 2.363 to 2.693 |
| One write (`append`) | 0.353 to 0.361 | 1.087 to 1.092 | 2.427 to 2.574 |
| Chat on the gateway's tier (6 calls) | 0.405 to 0.417 | 6.508 to 6.602 | 9.531 to 9.827 |
| Chat on one organization provider (20 calls) | 0.499 to 0.524 | 21.924 to 22.315 | 27.924 to 29.183 |

So on this machine one durable call on Postgres adds about 1.05 ms to a read and about 0.73 ms
to a write at the median. The chat path adds about 6.1 ms at the median on the gateway's tier
and about 21.4 ms on an organization provider, because its calls are made one after another.
These are the store calls alone, not a whole request, and nothing on the default path uses
Postgres.
