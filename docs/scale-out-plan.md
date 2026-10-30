# Scale-out plan: several gateway instances on Postgres and Redis

Status: **approved 2026-10-28** (PR #21), with the answers to Q1 to Q4 and three additions
folded in below (section 3.8 for time, section 3.6 for the lock, section 11 for the answers).
It needed approval because it touches authentication, sessions, step-up, the lockout, provider
keys at rest, a migration path for every table, and two new network surfaces: the gateway's
connections to Postgres and to Redis. Branch `scale-out/plan`, from `main` at `8bebbe8`. PR 1 is
being built (`scale-out/step-1`, from this branch); PRs 2 to 4 are not started. Valkey and
`asyncpg` still need the provenance check, in the PR that adds each.

**Goal.** Run several gateway instances behind a load balancer, sharing state through Postgres
(durable rows) and Redis (short-window counters), so that every rule the gateway enforces today
holds across instances exactly as it holds in one process. A single instance on SQLite, the
simulated wiring and simulated live mode keep their current code paths and their current
outputs byte for byte (section 6).

**What "without changing behaviour for one instance" means here.** Three precise statements,
each with a test (section 5):

1. With the SQLite or in-memory stores (the default server, every existing test, the perf gate,
   simulated live mode), nothing changes: the same code runs, and the protected outputs stay
   identical.
2. One instance on Postgres and Redis gives the same answers as one instance on SQLite for any
   sequence of requests that do not overlap in time (a parity test runs the same request script
   against both and compares every response and every stored row).
3. Where requests overlap, the shared backends are stricter in exactly the places listed in
   section 3, and nowhere else. The one that a user could notice is the budget edge (section
   3.1, answered by Q1).

One deliberate exception, approved as Q3: the account lockout's lost update (section 3.3) is
fixed on SQLite too, in PR 1, and named as a behaviour change there.

## 1. Every piece of state the gateway holds today

Read from the code at `8bebbe8`. "Two instances" means two processes behind a load balancer that
sends any request to either. **Safe** means the rule still holds with two instances and no
change. The **Needs** column is the plan: a Postgres row, a Redis counter, or local.

Two facts decide most rows:

- **`WallClock` is per process.** `WallClock.now_ms()` is `time.perf_counter()`: its zero is
  the process start, so two instances' readings cannot be compared. Every in-memory window
  today (rate limits, the login limiter, the step-up and user-creation caps, test-connection
  counts, the circuit breakers) is timed on that clock. Anything shared must be timed on
  calendar time: Postgres's for windows held in Postgres, the instance's `EpochSource` within a
  tested skew bound for windows held in Redis (section 3.8).
- **Many checks are atomic today only because the event loop is single-threaded,** or because
  SQLite's `BEGIN IMMEDIATE` takes a lock on the whole database. Neither holds across
  processes on Postgres. Each such check is listed below and handled in section 3.

### 1.1 Durable state (SQLite today, Postgres rows after)

| State | Where it lives today | Safe with two instances? | Needs |
|---|---|---|---|
| **Usage events** (`usage_events`, migrations 1, 3, 13) | SQLite rows, appended by `CostLedger.record` | The rows, yes. The budget built on them, no (next row) | Postgres row, same columns; `seq` an identity column |
| **Budgets** (`CostLedger._accounts`) | The current period's totals **in process memory**, read from the store only the first time an application is seen in a period; check before the request, charge after (`check_budget`, `record`) | **No.** Each instance enforces the budget on its own totals; N instances can spend up to N budgets (note 21 says this; `ProcessLock` refuses a second process for that reason) | Postgres row per (application, period): `committed_tokens`, `reserved_tokens`, plus a reservation row per admitted request (section 3.1) |
| **API key hashes** (`api_keys.key_hmac`, migration 2) | SQLite rows; HMAC-SHA256 with `GATEWAY_KEY_PEPPER` | Yes, if every instance has the same pepper | Postgres row, same columns. **Never cached** in Redis or in memory (a revocation must take effect on the next request on every instance) |
| **Key `last_used_at`** | Pending times in memory per instance (`ApiKeyService._pending`), flushed at most once a minute | Yes: each instance flushes its own, and the write already never moves the time backwards (`WHERE last_used_at_ms IS NULL OR last_used_at_ms < ?`; corrected in PR 1's plan commit, this row first said otherwise) | Postgres row, the same guarded update |
| **Applications, organizations** (migrations 2, 4) | SQLite rows | Rows yes; the per-application active-key cap (`ManagementService._check_room` then `add_key`) is a read then a write with no transaction, atomic only within one event loop | Postgres rows; the cap under the write lock (section 3.6) |
| **Users, lockout** (`users.failed_logins`, `first_failure_at_ms`, `locked_until_ms`, migration 5) | SQLite rows | **No, and not fully even in one process.** `login` reads the user, awaits the password verify (the thread runner yields), then `_count_failure` writes `failed_logins + 1` from the value read **before** the await: two concurrent wrong passwords both write 1. In one process the in-memory `LoginLimiter` still counts both; across instances it does not | Postgres row, the count made in one `UPDATE ... RETURNING` (section 3.3) |
| **Sessions** (migration 6) | SQLite rows; digests only; `last_seen_at_ms` written at most once a minute | Yes for the rows. `touch_session` is a plain `SET`, so an older touch landing after a newer one moves `last_seen` backwards, which can end a session up to a minute early (never late) | Postgres row; `last_seen` as `GREATEST` |
| **Step-up windows** (`sessions.step_up_until_ms`, migration 9) | SQLite column; consume-on-use claimed by `claim_step_up` in `BEGIN IMMEDIATE` | Only because of SQLite's database lock; a Postgres port that keeps the `SELECT` then `UPDATE` under READ COMMITTED lets two instances claim one window | Postgres column; claimed in one statement (section 3.2) |
| **Roles** (migration 8), **last-admin guard** (`_guard_last_admin` in `immediate()`) | SQLite rows; the count and the change in one `BEGIN IMMEDIATE` | Only because of SQLite's database lock | Postgres rows, under the write lock (section 3.6) |
| **Membership of an individual organization** (`count_members` then insert, in `immediate()`) | SQLite | As above | As above |
| **Audit log** (migration 7) | SQLite rows | Rows yes. The cap on unknown-user rows (`AuditLog._minute`, `_written_this_minute`, `_dropped`) is **in memory per instance**: N instances write N times the cap | Postgres rows; the cap as a Redis counter (section 3.5) |
| **Provider configs and sealed keys** (migrations 10, 11) | SQLite rows; AES-256-GCM under `GATEWAY_VAULT_KEY`, bound to organization, provider, type and URL origin | Yes, if every instance has the same vault key(s). During a rotation every instance needs `GATEWAY_VAULT_KEY_PREVIOUS` | Postgres rows, same columns; the sealed bytes as `bytea`. Never in Redis |
| **Modes** (migration 12), the application-not-looser bound | SQLite rows; the bound read and the write in `immediate()` | Only because of SQLite's database lock | Postgres rows, under the write lock (section 3.6) |
| **Schema version** (`schema_migrations`) | SQLite | Not applicable to one process | Postgres table; migrations under an advisory lock (section 2.3) |

### 1.2 Short-window counters (in memory today, Redis after)

| State | Where it lives today | Safe with two instances? | Needs |
|---|---|---|---|
| **Request rate limit** (`RequestRateLimiter`, 60 s sliding window per application) | Memory, `WallClock` | **No**: N instances allow N times the rate | Redis sorted set per application, one Lua script (section 3.4) |
| **Login limiter** (`LoginLimiter`: 5 failures in 15 min per HMAC of the typed username, known or not; LRU of `LIMITER_MAX_ENTRIES`) | Memory, `WallClock` | **No**: N times the attempts, and the only per-username control for unknown usernames | Redis sorted set per username HMAC, Lua (section 3.4); expiry instead of LRU (decision 9) |
| **Step-up failures per session** (3 in 5 min) | Memory (`_step_up_failures`) | **No**: a session can reach every instance | Redis, same script |
| **User-creation attempts per session** (`GATEWAY_USER_CREATE_ATTEMPTS` an hour) | Memory (`_creation_attempts`) | **No** | Redis, same script |
| **Test-connection counts** (per provider and per organization, per hour, both counted or neither) | Memory (`ProviderService._tests`, `_organization_tests`) | **No** | Redis, one script that checks both windows and counts both (section 3.4) |
| **Audit unknown-user cap** | Memory (above) | **No** | Redis counter per minute (section 3.5) |
| **Circuit breakers** (`Breakers`, per (organization, provider): state, failures, successes, `open_until_ms`, `probing`) | Memory, `WallClock` | **No**: each instance opens on its own failures, and in half-open each instance lets its own probe through (N probes, not one) | Redis hash per (organization, provider), Lua transitions; one probe across all instances (section 3.7) |
| **Slow-provider warning** (`misses`, `slow_since_ms`) | Memory, with the breaker | Yes: a warning on one instance is still true | **Local** (decision 11) |

### 1.3 State that stays local, by design

| State | Why it stays local |
|---|---|
| Workers' queues and batches, the prefix cache, chunked prefill (`ContinuousBatchingWorker`, `PrefixCache`) | They describe this instance's own simulated models. With real models the model server batches (`PassthroughWorker`) and there is nothing to share |
| `SloMonitor` and SLO-aware downgrade | It measures the latency this instance sees from its own workers; a downgrade is a routing choice within the caller's plan, never a security rule |
| `MetricsStore`, `TimeSeriesStore`, `EventBus`, `Monitor` | Monitoring watches and decides nothing (CLAUDE.md). Each instance reports itself; Prometheus adds instances up by its `instance` label. The dashboard shows one instance (decision 13) |
| `Redactor`'s leased key values | They are values this process decrypted; another process has nothing to scrub |
| The dummy password hash, the CSRF key (derived from the pepper), the login-limiter key (derived) | Stateless given the same pepper on every instance |
| Egress checks, the pinned TLS context | Per request and per connection |
| Playground keys, `LoadController`, `PacedSimClock` | Simulated live mode only, which stays single instance and in memory (decision 14) |
| `ProcessLock` | Kept for SQLite, which stays one process per database. Not taken on Postgres |

## 2. The storage interface, the migration from SQLite, and the rollback

### 2.1 The interface

Today each store is a class over a `sqlite3.Connection`, with a protocol for some of them
(`UsageStore`, `ApplicationStore`, `ModeStore`). The plan:

- **One protocol per store,** for every store, including the ones that have none today
  (`AccountStore`, `RoleStore`, `ProviderStore`, the audit sink), plus four new ones:
  `BudgetCounter` (reserve, settle, release, sweep), `WindowCounter` (count one event in one or
  more sliding windows, or refuse; clear), `BreakerStore` (admit, release, success, failure,
  state) and `AuditCap` (take one unknown-user slot this minute; collect the dropped count of a
  finished minute once).
- **Three implementations of each,** in a `StateBackend` bundle that `app.py` builds and
  injects (it stays the only wiring point): `memory` (today's in-memory classes), `sqlite`
  (today's SQLite classes) and `shared` (Postgres stores plus Redis counters). `memory` and
  `sqlite` keep today's code: their counters are the existing in-memory classes, unchanged.
- **Every method that changes state together is one call on the protocol,** never a read and a
  write by the caller: `claim_step_up`, `count_login_failure`, `reserve`, `admit_window`, and
  so on. The atomic rule then lives inside one implementation method, where a mutation check
  can remove it (section 5.3).
- **The write lock** (`immediate()`) becomes `write_transaction()` on the bundle: `BEGIN
  IMMEDIATE` on SQLite, a Postgres transaction that takes one transaction-level advisory lock on
  Postgres (section 3.6).
- **Async.** The store protocols become `async` (decision 1). The in-memory and SQLite
  implementations never suspend inside a method, so on a `SimClock` no simulated time can pass
  in a store call, as today; the Postgres and Redis implementations await the network. This is
  the largest mechanical change in the plan and lands on its own, with no behaviour change, in
  PR 1 (section 8).
- **Selected by `GATEWAY_STATE`:** `sqlite` (the default server, unchanged), `shared` (needs
  `GATEWAY_POSTGRES_URL` and `GATEWAY_REDIS_URL`, both from the environment only, both treated as
  secrets: they carry passwords; never logged, redacted by the same `Redactor`). Simulated live
  mode refuses `shared`.

### 2.2 Postgres schema

The same tables and columns as migrations 1 to 13, written once as a Postgres baseline
(`pg0001_baseline`), with Postgres types: `bigint GENERATED ALWAYS AS IDENTITY` for `seq`,
`double precision` for times and costs (the same values SQLite stores), `bytea` for sealed keys,
`boolean` for flags, and the same unique and foreign-key constraints. New tables:
`budget_counters (application_id, period_start_ms, committed_tokens, reserved_tokens)` and
`budget_reservations (id, application_id, period_start_ms, tokens, expires_at_ms)`. Further
migrations are numbered in both series together: a change after this plan ships as `m0014` for
SQLite and `pg0002` for Postgres in one PR, and a test checks that the two schemas describe the
same tables and columns (decision 5).

### 2.3 The migration path from SQLite

`scripts/manage.py migrate-to-postgres`, run by the operator, offline:

1. Refuses unless the SQLite database is at the latest migration, no gateway process holds its
   `ProcessLock` (it takes the lock itself for the whole copy), and the Postgres database is
   empty at `pg0001`.
2. Takes a SQLite backup through `backup_database` first (as `migrate` does today).
3. Copies every table in foreign-key order, in one Postgres transaction, preserving ids,
   `seq` values (then moves the identity past the highest), HMACs and sealed bytes byte for
   byte. Never decrypts a provider key, never prints a value.
4. Builds `budget_counters` for the current period from `usage_events` (committed = billed
   tokens summed in `seq` order; reserved = 0).
5. Verifies before committing: row counts per table, and a sha256 per table over a canonical
   dump (same columns, same order) taken from both sides. Any difference rolls back the
   Postgres transaction and prints which table differs.

Schema migrations on Postgres run at startup under `pg_advisory_lock` on a fixed key, so with
N instances starting together exactly one migrates and the others wait, then check the version.
An instance whose code is behind the schema refuses to start (it would not know the newer
columns' rules), as the SQLite runner does today.

### 2.4 Rollback

- **Before cut-over** nothing is lost: the SQLite file is untouched and backed up.
- **After running on Postgres,** `scripts/manage.py migrate-to-sqlite` copies back the same
  way (offline: every instance stopped, the Postgres database read in one repeatable-read
  transaction, verified by the same per-table sums) into a **new** SQLite file at the latest
  migration, never over an existing one. Reservations in flight are dropped (no instance is
  running, so none is in flight); counters are not copied, because the SQLite ledger rebuilds
  its totals from `usage_events`.
- **Redis is never migrated.** Its counters are windows of at most an hour; a cut-over in
  either direction starts them empty (section 4, "Redis restart").
- **Rolling back the code** while on Postgres: the previous release does not know `shared`
  and refuses to start with `GATEWAY_STATE=shared`, so a rollback of code is always also a
  rollback of state, through the script above.
- Both scripts are tested both ways on a database built in `tmp_path` with every table filled,
  and the round trip SQLite to Postgres to SQLite must give the same per-table sums.

## 3. Atomic operations

Each rule below is one statement or one Lua script, so no interleaving of instances can break
it. Each has a two-instance test and a mutation check (section 5).

### 3.1 Budgets: reserve, settle, release

Today the check is before the request and the charge after it, so in one process concurrent
requests at the edge are all admitted and can all spend the last tokens. Across instances the
totals are not even shared. The plan:

- **Reserve** at admission, in one Postgres transaction:
  ```sql
  INSERT INTO budget_counters (...) SELECT $app, $period, <billed tokens of the period's
    events>, 0 ON CONFLICT DO NOTHING;                         -- seed once per period
  UPDATE budget_counters SET reserved_tokens = reserved_tokens + $tokens
   WHERE application_id = $app AND period_start_ms = $period
     AND committed_tokens + reserved_tokens < $budget
  RETURNING committed_tokens, reserved_tokens;                 -- no row: 429 budget_exhausted
  INSERT INTO budget_reservations (...) VALUES (..., $expires_at);
  ```
  `$tokens` is the request's upper bound: its prompt tokens (cached tokens not subtracted) plus
  its completion bound, capped at the tier's context limit (Q2):
  `min(prompt + completion_bound, context_tokens)`. The completion bound is the request's
  `max_tokens`, else the plan's (which today is also used when a request asks for none), else
  the tier's `reserve_completion_tokens`. `context_tokens` and `reserve_completion_tokens` are
  per-tier settings in `backends.yaml`, operator-supplied like the placeholder prices: no
  number is shipped for them, and `shared` refuses to start while a tier lacks either
  (decision 18). The reservation is a bound for admission only: settle reconciles to the
  actual billed tokens, so nothing is ever charged from it.
- **Settle** when the request finishes, in one transaction: delete the reservation row; if it
  was there, `reserved_tokens -= tokens`; always `committed_tokens += billed`; append the usage
  event. A reservation already swept is not subtracted twice.
- **Release** on a request that ends with no output (the cases that bill nothing today): delete
  the row and subtract.
- **Sweep**: reservations past `expires_at_ms` (the tier's total timeout plus a margin) are
  released by whichever instance's retention loop runs (an instance that died mid-request
  leaks nothing for longer than that).

**The guarantee.** Two requests can never both be admitted against the same remaining tokens:
the first's reservation is counted before the second is checked, in the same row, under the
row lock Postgres takes for the `UPDATE`. The last request admitted can still go past the
budget by at most its own bound, as one request can today.

**The rule that changes.** The admission condition counts in-flight reservations
(`committed + reserved < budget`). Sequential requests get exactly today's answers (no
reservation is in flight when the next is checked). Concurrent requests at the edge are refused
where today they are admitted. Approved as Q1, over the alternative of requiring the whole
bound to fit (`committed + reserved + tokens <= budget`, never over budget, but stricter for
sequential requests too). The SQLite ledger is not changed (section 6).

### 3.2 Step-up: consume-on-use claimed once

```sql
UPDATE sessions s SET step_up_until_ms = NULL
  FROM (SELECT id_hash, step_up_until_ms FROM sessions
         WHERE id_hash = $1 AND NOT revoked AND step_up_until_ms > $now FOR UPDATE) old
 WHERE s.id_hash = old.id_hash
RETURNING old.step_up_until_ms;                                -- no row: StepUpRequiredError
```
Two instances claiming the same window: the second waits on the row lock, then finds
`step_up_until_ms` NULL and gets no row. `restore_step_up` stays one conditional `UPDATE ...
WHERE step_up_until_ms IS NULL AND NOT revoked` (already atomic). Opening a window
(`set_step_up`) and closing every window of a user (`clear_step_up`) are single statements.

### 3.3 Account lockout: one counted failure per wrong password

```sql
UPDATE users SET
  failed_logins = CASE WHEN first_failure_at_ms IS NULL OR $now - first_failure_at_ms >= $window
                       THEN 1 ELSE failed_logins + 1 END,
  first_failure_at_ms = CASE WHEN ... THEN $now ELSE first_failure_at_ms END
 WHERE id = $user RETURNING failed_logins, first_failure_at_ms;
```
then, in the same transaction, if the count reached 5: set `locked_until_ms`, reset the count,
close every step-up window of the user, and write the `login_locked` audit row. The count is
computed from the row as it is at the write, not as it was read before the password verify.

Login keeps its structure: one lookup, one verify, then the decision; every refusal takes the
same steps (the structural test of `docs/27` runs on both backends). The lockout check itself
reads `locked_until_ms` after the verify, as today.

This also fixes the lost update that exists in one process today (section 1.1). **Approved as
Q3: fixed on SQLite too, in PR 1,** as a named, deliberate behaviour change: concurrent wrong
passwords now each count. On SQLite the count is one store call, `count_login_failure`, that
reads the row and writes the new count inside `BEGIN IMMEDIATE` (atomic: SQLite admits one
writer), instead of the service writing a count computed from the row it read before the
password verify. A regression test makes two wrong-password logins both read the user before
either counts (a hash runner that waits on an event), and asserts 2; the mutation (compute
from the stale row again) gives 1 and must fail it.

### 3.4 Sliding windows in Redis

One Lua script, `window_admit(keys, limits, windows_ms, now_ms, member)`, over one sorted set per
key: for each key, drop members older than `now - window`, trim to the limit if the limit was
lowered, and if any key is at its limit return the refusal and the retry-after **without
counting anything**; otherwise add `member` to every key and set each key's expiry to its
window. Both-or-neither covers the two test-connection windows. `member` is
`<now_ms>:<instance id>:<counter>` so two instances counting in the same millisecond are two
members.

It serves the request rate limit, the login limiter (with a separate `clear`), the step-up
session cap and the user-creation cap. `now_ms` is the instance's `EpochSource`, not Redis's
`TIME` (decision 3).

### 3.5 The audit unknown-user cap

`INCR audit:unknown:<minute>` with an expiry of two minutes; at or under the cap the row is
written to Postgres, over it `INCR audit:dropped:<minute>`. The `rows_dropped` entry for a
finished minute is written by whichever instance first runs `GETDEL audit:dropped:<minute>`
and gets a number: exactly one writer, with the same row as today.

### 3.6 The write lock for management invariants

SQLite's `BEGIN IMMEDIATE` serialises every writer on the database, and five rules rely on it:
the last org_admin and last platform_admin, an individual organization's one member, a new
user's name and seat checked again after the hash, the application-mode bound, and the
provider changes with their audit rows. Two more are atomic today only because nothing awaits
between the read and the write: the active-key cap and key issuing.

On Postgres, `write_transaction()` takes one transaction-level advisory lock on a fixed key:
one global write lock for management writes, which is exactly SQLite's semantics (decision 6).
These are administrative writes, rare next to requests; none of the request path (key checks,
budgets, step-up claims, the lockout, sessions, chat admission) goes through it. The
active-key cap and key issuing move into it.

**Acquisition has a timeout and fails closed** (added on approval). The lock is taken with
`pg_try_advisory_xact_lock` in a loop with backoff on the injected clock, up to
`GATEWAY_STATE_LOCK_TIMEOUT_MS` (default 2000, decision 20); at the timeout the transaction
rolls back, nothing is written, and the request gets `503 state_unavailable`. A consume-on-use
step-up window is given back, because nothing changed (the `requires` rule of docs/32). The
holder is bounded too: `SET LOCAL statement_timeout` and `idle_in_transaction_session_timeout`
end a transaction that stalls, so a stuck holder releases the lock instead of keeping every
other management write waiting. On SQLite the existing `busy_timeout` (5 s) is the same
bound: a writer that cannot get the database lock fails with nothing written, as today; PR 1
does not change what that answers.

**A slow management write does not delay chat admission** (added on approval). Two things make
that true, and each has a test:

- Admission never takes the advisory lock (above).
- Admission never waits for a connection a management write holds: the Postgres stores use two
  connection pools, one for the request path and one for management (decision 19), so a
  management transaction that holds the lock and its connection takes nothing from requests.

The test: on two instances, a management write is held inside the lock (a test hook that
waits on an event inside the transaction, with every management connection also checked out);
while it is held, chat requests on both instances are admitted, served and settled; only then
is the event set. The test waits on states, never on times. Its mutations: take the lock in
`reserve()`, and serve admission from the management pool; each makes the requests wait for
the event, and the test fails (by a bounded wait that reports which requests had not
finished).

### 3.7 Circuit breakers

One Redis hash per (organization, provider) and one Lua script per event (`admit`, `release`,
`success`, `failure`), with the same transitions as `breaker.py` and times from `EpochSource`.
In half-open, `admit` sets `probing` only if it was unset, so exactly one request across all
instances probes. A probe whose instance dies would hold `probing` forever, so it carries an
expiry (the tier's total timeout); this is new and is decision 10. Transitions are returned by
the script and published by the instance that made them as `circuit_breaker` events on its own
`EventBus`, so monitoring stays local and decides nothing.

### 3.8 Time: whose clock decides a window (added on approval)

A window that decides a security outcome must not depend on which instance's clock read it.
Two rules, one per store:

**Windows held in Postgres use Postgres's time.** These are the account lockout (5 failures in
15 minutes, locked for 15), the step-up window (opened, checked, claimed, given back), session
idle and absolute expiry, `last_seen`, API key expiry and the rotation grace, the budget
period, and reservation expiry. Every statement that decides one of them reads the time in
SQL, from one function, `gateway_now_ms()`, never from a parameter the instance sends. A
request that needs the time for several decisions (authenticate a session, then check its
step-up window) takes it once, returned with the row it first reads, and uses that one value
throughout, so no decision in one request sees two clocks. Whatever is stored as a time
(`locked_until_ms`, `step_up_until_ms`, `expires_at_ms`) is computed from it too.

`gateway_now_ms()` is how this stays injectable: in production it returns
`clock_timestamp()` in epoch milliseconds; the test schema replaces it with a function that
reads a one-row table the test sets, which is the Postgres side of the test's `EpochSource`.
The instance's own `EpochSource` is then used for nothing these windows decide in `shared`
mode. On SQLite and in memory nothing changes: one process, one clock.

**Windows held in Redis use the instance's `EpochSource`, with a stated and tested skew
bound.** These are the request rate limit, the login limiter, the step-up session cap, the
user-creation cap, the test-connection counts, the audit cap's minute and the circuit
breakers. Redis's own `TIME` is not used: it would be a second clock outside the injected
ones. The bound, `GATEWAY_STATE_MAX_SKEW_MS`, default 1000 (decision 21):

- Each instance compares its `EpochSource` with `gateway_now_ms()` at startup and every 30
  seconds, taking the midpoint of the round trip; beyond the bound it refuses to start, or
  reports not ready and refuses every request that would use a Redis window, until a check
  passes again (fail closed).
- **What the bound tolerates, stated:** with every instance within `s` of Postgres, any two
  are within `2s` of each other. A sliding window of length `W` then behaves, across
  instances, as one between `W - 2s` and `W + 2s` long: a limit of `L` a minute can admit at
  most `L` plus the requests that arrive in a `2s` slice at the edge of the window, and a
  breaker can open or half-open up to `2s` early or late. Nothing that a Redis window decides
  is durable: the lockout itself, step-up windows and budgets are in Postgres, under the first
  rule.
- **Tested:** two instances with their epochs offset by just under the bound in opposite
  directions; the rate limit and the login limiter admit no more than the stated tolerance
  allows, and exactly the limit when requests are spread inside the window. An offset just
  over the bound: the instance refuses to start, and a running instance that drifts past it
  reports not ready and refuses at once. Mutation: disable the periodic check, and the drift
  test fails.

### 3.9 Redis restarts (Q4)

Approved: a Redis restart resets its counters and that is accepted, because the per-account
lockout and step-up consume-on-use (and budgets, sessions and key state) are in Postgres and do
not depend on Redis. The restart is detected and recorded, never silent:

- Each instance reads Redis's `run_id` (`INFO server`) at startup, on every reconnect, and with
  the 30-second skew check. A `run_id` that differs from the last one seen is a restart.
- On a restart the instance publishes a `state_store_restarted` event on its `EventBus`, which
  the existing per-type event counter counts in Prometheus:
  `gateway_events_total{type="state_store_restarted"}` (as built in PR 2; this line first named
  a separate `gateway_state_store_restarts_total` family, which was not built). Each
  instance reports what it saw; nothing decides anything from them (monitoring watches only).
  The event is also written to the audit log once per `run_id` (the first instance to `SET NX`
  a key holding the new `run_id` writes it), so the record survives the instance.
- The exposure is in the threat model (section 9).

## 4. Failure behaviour: missing is forbidden, never open

Every check that cannot be made answers as if it said no. In each case the response is the
same as a refusal of that kind would be, or `503 state_unavailable` where no refusal fits; no
answer ever reveals which store is down beyond that.

| Unavailable | Effect |
|---|---|
| **Postgres** | Every request that needs a key, a session or a budget is refused: `/v1/generate` and `/v1/chat/completions` 503 `state_unavailable` (a key cannot be checked: a missing key is not a valid one); logins fail as `LoginFailedError` after the same steps (the lookup fails closed to the dummy hash path, so the timing structure holds); management 503. `/healthz` stays up (liveness) and a new `/readyz` says not ready, so the load balancer stops sending traffic (decision 12). Nothing is queued to write later |
| **Postgres, at settle time** (the request ran, the charge fails) | The answer has been sent; the usage cannot be lost silently. The settle is retried with backoff in the instance until it succeeds or the instance stops; while any settle is pending the instance reports not ready and admits nothing new. An instance that stops with a settle pending logs the reservation id (nothing of the request) and the sweep releases the reservation; the usage of that request is lost and counted in a metric (decision 8) |
| **Redis** | Rate limit: refused (503, not 429: the caller did nothing wrong). Login limiter: the login is refused after the same steps as every other refusal. Step-up and user-creation caps: refused. Test-connection: refused. Audit unknown-user cap: the row is **dropped and counted locally** (writing it would let a flood through the cap; the local count is written as a `rows_dropped` entry once Redis is back) |
| **Redis, breakers** | Every organization provider is treated as open: skipped with `circuit_open`. That only ever narrows where a prompt can go: `local_only` is unaffected, the fallback modes fall back to the gateway's tier as their mode allows, never further |
| **Redis restart (empty)** | Accepted (Q4). Every counter it held covers at most one hour, so the loss is bounded by its window; the durable controls (the account lockout, step-up consume-on-use, budgets, sessions) are in Postgres and survive. Detected by `run_id` and recorded as an event, a metric and one audit row (section 3.9). The startup check requires `maxmemory-policy noeviction` (an evicted counter is a forgotten failure: open) and appendonly persistence, and refuses to start without them (decision 9) |
| **The management write lock** | Not acquired within `GATEWAY_STATE_LOCK_TIMEOUT_MS`: rolled back, nothing written, `503 state_unavailable`, a consume-on-use window given back (section 3.6). Chat admission does not take it |
| **Clock skew past the bound** | Refuses to start; when it happens while running, not ready, and every request that would use a Redis window is refused until a check passes (section 3.8) |
| **Either, at startup** | The instance refuses to start, as it does today without `GATEWAY_VAULT_KEY` |
| **Slow** | Every call has a timeout on the injected clock (decision 7); a timeout is unavailability, with the effects above |

Every one of these has a test with the store made unavailable (section 5.4).

## 5. Tests

### 5.1 Where they run

- Unit and contract tests run against all three backends through one parametrised fixture:
  every store protocol has one contract test suite, run on `memory`, `sqlite` and `shared`.
- `shared` tests need a real Postgres and Redis. They are marked `shared_state` and skipped
  unless `GATEWAY_TEST_POSTGRES_URL` and `GATEWAY_TEST_REDIS_URL` are set; unlike `real_models`,
  **CI runs them**, in a new job with Postgres and Valkey service containers on Linux
  (decision 4). Each test gets its own Postgres schema and Redis key prefix, dropped after.
- Time in `shared` tests is set by the test, never real: Postgres's through the test
  `gateway_now_ms()` (section 3.8), each instance's through a `ClockEpoch` over a `SimClock`
  with no sleepers, both moved explicitly together (or apart, for the skew tests): awaiting
  network I/O must not let simulated time jump.

### 5.2 Two-instance tests

Two `create_app` instances in one test process, each with its own workers, `Monitor` and
in-memory state, sharing one Postgres schema and one Redis prefix, driven through two
`httpx.ASGITransport`s with `asyncio.gather`. Plus one test with two real processes
(`python -m gateway.serve` on two loopback ports) to catch anything process-wide.

| Rule | Two-instance test |
|---|---|
| Budget | Budget with room for exactly one request's bound; 20 requests split across both instances at once: exactly one admitted, 19 `budget_exhausted`; committed equals the one request's billed tokens |
| Step-up | One window; 10 consume-on-use requests across both instances at once: exactly one changes something, 9 `step_up_required` |
| Lockout | 5 concurrent wrong passwords for one user across both: the account is locked and `failed_logins` reached 5 |
| Rate limit | Limit 10 a minute; 30 requests across both at once: exactly 10 admitted |
| Login limiter | 5 failures for an unknown username across both: the sixth is refused on either |
| Step-up cap, user-creation cap, test-connection | Exactly the cap admitted across both |
| Audit cap | Cap 60; 200 unknown-user failures across both in one minute: 60 rows and exactly one `rows_dropped` row with 140 |
| Breaker | Open the breaker from instance A; instance B skips the provider; in half-open, concurrent requests on both: exactly one probe reaches the mock provider |
| Management | Two instances revoke the last two org_admins at once: exactly one succeeds. Two create the second member of an individual organization: one succeeds. Two issue the last allowed key: one succeeds |
| Revocation | A key revoked through A is refused on B on the next request |
| Parity | The same sequential request script on one `sqlite` and one `shared` instance: every response body, status and stored row identical (statement 2 at the top of this plan) |

### 5.3 Mutation checks

Each atomic rule is mutated into its natural non-atomic form, the two-instance test must fail,
then the code is restored byte for byte and the tests pass again (shown in each PR's table):

| Mutation | Must fail |
|---|---|
| Budget: drop `AND committed_tokens + reserved_tokens < $budget` from the `UPDATE`, checking it in Python on a prior `SELECT` instead | Budget two-instance test |
| Budget: settle without deleting the reservation row first (double subtract after a sweep) | The sweep-then-settle test |
| Step-up: `SELECT` then `UPDATE` without `FOR UPDATE` | Step-up two-instance test |
| Lockout: write `failed_logins = $value_read + 1` | Lockout two-instance test |
| Rate limit: the script split into `ZCARD` then `ZADD` from Python | Rate-limit two-instance test |
| Window: count the first test-connection window even when the second refuses | Both-or-neither test |
| Audit cap: `GET` then `DEL` instead of `GETDEL` | Exactly-one `rows_dropped` test |
| Breaker: set `probing` without checking it | One-probe test |
| Write lock: `write_transaction()` without the advisory lock | Last-admin, membership and key-cap two-instance tests |
| `last_used_at` / `last_seen`: plain `SET` | Not-backwards test |
| Fail closed: each "unavailable" branch changed to admit | Its section 5.4 test |
| Lockout on SQLite (PR 1, Q3): count from the row read before the verify | The concurrent wrong-password regression test |
| Admission takes the management lock, or uses the management pool | The slow-management-write test (section 3.6) |
| Lock acquisition without the timeout | The lock-timeout test: a held lock must give 503 with nothing written, not a hang (bounded wait) |
| A Postgres-held window decided with the instance's time instead of `gateway_now_ms()` | The skewed-instance lockout and step-up tests |
| The periodic skew check disabled | The drift test (section 3.8) |
| Restart detection compares nothing | The Redis restart test: restart the container, expect one event per instance and one audit row |

The interleaving these tests need has to happen on every run, not by luck: each mutated form
has a read and a write separated by a network round trip, and the tests start all requests
behind one `asyncio.Event`. If a mutation cannot be made to fail reliably (ten runs out of ten),
that is a stop-and-ask, as the working agreement says.

### 5.4 Failure tests

Postgres or Redis stopped, or a proxy that drops or delays the connection, for each row of the
table in section 4: the refusal, the status, `/readyz`, and that no request was served. The
structural test that every login refusal takes the same steps runs with Redis unavailable too.

### 5.5 Security tests carried over

The leak scan with a planted control (the Postgres and Redis URLs carry passwords: they go
through the `Redactor`, and the scan must find the planted one in a deliberately unredacted
copy), and the existing scans run against the `shared` backend: no secret, prompt, response
or token id in any Postgres row or Redis key or value (`SCAN` over the test prefix). A missing
and a forbidden resource stay byte-identical on `shared`.

## 6. The perf gate and the protected outputs

- **The perf gate** (`benchmarks/gate.py`) runs `create_app(registry, clock)` with no
  applications: no rate limiter, no budget, no accounts. It never touches any store this plan
  changes, and it stays on `memory`. Expected: unchanged budgets, no re-baseline. If PR 1's
  async conversion moves a gate number at all, that is an unexplained change and a stop.
- **The protected outputs** (`gate.json`, `loadgen-*.json`, `docs/REPORT.md`, the budgets, the
  calibration and quality files) are all produced on `SimClock` with in-memory state. None may
  change; `scripts/protected_outputs.py --include-images` runs in every PR of this plan.
- **What shared state costs** is measured separately, not gated in CI: a new
  `benchmarks/shared_state.py` times a request's added round trips (key lookup, reserve,
  window, settle) against a local Postgres and Valkey on this machine, three runs, hardware
  recorded, for the note. No number is written before it is run. Whether to gate it is
  decision 15.

## 7. Dependencies

Nothing is added by this plan. Each PR that adds one runs the provenance check first (license
against the allowed list, metadata and author, project URL, upload age of at least two weeks,
wheel and sdist sha256, PyPI provenance attestation, the repository's declared dependencies at
the tag) and records it in `docs/licenses.md`; a doubtful result means not adding it, as with
`openai` in A5.

| Need | Candidate | Why | Excluded |
|---|---|---|---|
| Postgres driver | `asyncpg` (Apache-2.0) | Async, no C library to install | `psycopg` 3: LGPL-3.0, not on the allowed list |
| Redis client | `redis` (redis-py, MIT), its `redis.asyncio` | The reference client | - |
| Postgres server (CI, compose) | PostgreSQL (PostgreSQL License, permissive) | - | - |
| Redis-protocol server (CI, compose) | **Valkey** (BSD-3-Clause) | Redis since 7.4 is under RSALv2/SSPLv1 (and AGPLv3 from 8.0), not free/open-source in the sense CLAUDE.md requires | Redis 7.4+ (decision 4) |

The servers are not Python dependencies; they are recorded in `docs/licenses.md` under a new
"Services used in CI and compose" table.

## 8. The pull requests

In order; each starts from the previous branch once its CI is green, and must be merged in
order.

| PR | Branch | What | Behaviour change |
|---|---|---|---|
| 0 | `scale-out/plan` | This plan | None |
| 1 | `scale-out/step-1` | Every store behind a protocol, async; `StateBackend` with `memory` and `sqlite`; `write_transaction()`; the window, budget, breaker and audit-cap protocols over today's in-memory classes; the lockout fix on SQLite (Q3) | One, named: concurrent wrong passwords each count (Q3). Otherwise the same tests, the perf gate and the protected outputs unchanged |
| 2 | `scale-out/step-2` | Valkey counters (reordered on request, 2026-10-29): `redis` and the Valkey image (provenance check), the window scripts, the audit cap, the breakers, failing closed, restart detection, the skew check, the compose file and CI job, the two-instance test of a shared limit | Only with `GATEWAY_COUNTERS=valkey`; the default stays in memory |
| 3 | `scale-out/step-3` | Postgres: `asyncpg` (provenance check), `pg0001`, every durable store, the budget reservation and its two-instance edge test (Q1), the step-up claim, the lockout update, the write lock, `migrate-to-postgres` and back; the skew check moves to `gateway_now_ms()`; `GATEWAY_STATE=shared`; `/readyz` | Only with `shared` |
| 3b | `scale-out/step-3b` | Budgets on Postgres: the reservation (3.1) and its two-instance edge test (Q1), the per-tier settings (Q2); `GATEWAY_STATE=shared` allowed; Valkey's skew check against `gateway_now_ms()` | Only with `shared` |
| 3c | `scale-out/step-3c` | Each mode read once per chat request and passed on (note 36: 12 of the 20 store calls of a provider-served request) | Fewer store calls; a mode change applies from the next request, and the next attempt of one in flight, as before |
| 2b | `scale-out/step-2b` | After PR 3, recorded 2026-10-29, not built: the breaker reports off the request path, and a per-request cap on hosted attempts (below) | Chat: fewer counter calls on the path; at most 3 hosted attempts a request by default |
| 4 | `scale-out/step-4` | The two-instance tests in process and in two processes, the mutation table, the failure tests, `benchmarks/shared_state.py`, the note, the compose file for a two-instance demo on Postgres and Valkey | None |

The questions PRs 2 and 3 depended on are answered (section 11). Each PR has the template,
the definition of done, and its note (`docs/34-*` onward).

### PR 2b: two follow-ups, recorded 2026-10-29 (not built; after PR 3)

Proposed in #24 once the chat worst case was measured (note 35) and accepted as follow-ups,
without changing #24. Neither is built before PR 3 is merged.

**1. The breaker reports off the request path.**

- **What moves:** `admit` stays on the path, because it decides whether a provider is tried. The
  reports (`success`, `failure`, `release`) go through a per-instance queue that sends them in
  order for each breaker. A chat request's worst case on the path goes from 1 + 4N counter
  calls to 1 + 2N.
- **A bound:** the queue holds a bounded number of reports. One that does not fit is dropped,
  as a report that cannot be written is today. A dropped report never admits anything: it can
  only leave a breaker closed a little longer, or a half-open probe slot held until its 120 s
  expiry.
- **A shutdown flush:** the lifespan drains the queue on shutdown, within a time limit, before
  the counters' client closes. What is still queued at the limit is dropped and counted.
- **A dropped-report metric:** a Prometheus counter of reports dropped, whether the queue was
  full, the flush ran out of time, or Valkey could not answer. It is labelled by reason, and
  its name follows docs/15. It is a count of what the instance did; nothing decides anything
  from it.
- **Unchanged:** failing closed. `admit` still refuses when Valkey cannot answer.
- **Tests:**
  - the reports of one breaker arrive in order;
  - a full queue drops and counts;
  - the shutdown flush writes what is queued, and counts what it cannot;
  - a request no longer waits for its reports (counted as in
    `test_the_most_counter_calls_one_chat_request_makes`: 1 + 2N on the path).
- **Mutation checks:** reports sent out of order, the flush removed, the drop not counted, and
  `admit` moved off the path, each failing a test.
- **Timing:** measured before and after, three runs each, on this machine.

**2. A per-request cap on hosted attempts.**

- **The setting:** `GATEWAY_CHAT_MAX_HOSTED_ATTEMPTS`, set by the operator, default 3, range 1
  to 5. A value outside the range, or not a whole number, refuses the start, naming the
  variable.
- **What counts as an attempt:** each call to an organization provider within one request,
  including the one retry after invalid tool JSON. A provider skipped without a call (an open
  breaker, an unreadable key) is not an attempt.
- **At the cap:** the request's remaining hosted candidates are skipped with a fallback reason
  of their own (`attempt_cap`), recorded like the others. The gateway's own tier is still tried
  where the mode allows it, so the cap never sends a prompt anywhere the mode would not.
- **Why:** it bounds how long one request can spend on providers (attempts times the
  first-token budget, 10 s by default) and how many third parties one prompt reaches. Counter
  time is the smaller part (note 35).
- **Later, not in 2b:** the same cap as a per-application setting, which an application may set
  lower than the operator's value but never higher (refused when it is written, like the mode
  rule). The operator's value stays the ceiling.
- **Tests:**
  - the default, the range and refusal at startup;
  - four primary providers that all fail, with only three tried and the fourth skipped as
    `attempt_cap`;
  - the gateway's tier still answering after the cap;
  - the retry counting.
- **Mutation checks:** the cap not enforced, the retry not counted, and the range check
  removed, each failing a test.

### PR 2: Valkey counters, settled before building (2026-10-29)

Requested as PR 2, ahead of Postgres. Answers given before building (2026-10-29):

- **Valkey 9.0.7, pinned by digest** (`valkey/valkey:9.0.7@sha256:7e1deda01b64ece3167d370ab5be3452d29734feec22cbcbc69b3a64f6859d6d`),
  an exception to the two-week rule: every current tag was a two-day-old security release
  (GHSA-f4q9-j22m-xqq9), and the newest older one lacks the fix. Recorded in `docs/licenses.md`.
- **`redis` (redis-py) 8.1.0**, pinned exactly. No PyPI attestation (the project publishes with
  an API token); the wheel is byte-identical to the repository at tag `v8.1.0` (`e013126`),
  file for file. The evidence is in `docs/licenses.md`.
- **Budgets stay for the Postgres PR**, as section 3.1 and Q4 have it; so does the two-instance
  test of the budget edge (Q1).
- **The skew check measures against Valkey's `TIME`** until Postgres exists: read only to
  measure skew, never to decide a window. The Postgres PR moves it to `gateway_now_ms()`
  (section 3.8 otherwise unchanged).

Settled from the code:

- **`GATEWAY_COUNTERS`**: `memory` (the default: today's classes, no Valkey) or `valkey`, which
  needs `GATEWAY_VALKEY_URL` (a secret: it may carry a password; never logged, and its
  password is watched by the redactor). `GATEWAY_STATE_MAX_SKEW_MS` (default 1000).
- **What is in Valkey:** the request rate limit, the login limiter, the step-up session cap,
  the user-creation cap, the test-connection counts, the circuit breakers and the audit cap.
  Every key name is an HMAC of what it counts under a key derived from the pepper for that
  purpose alone, so no username, session digest or provider id appears in Valkey.
- **Atomic operations** are Lua scripts (sections 3.4, 3.5, 3.7), each tested on two instances
  and each with a mutation check.
- **Failing closed**, per counter: the table in note 35. Every Valkey call has a timeout on the
  injected clock (200 ms, decision 7).
- **Restart detection** (section 3.9) checks `run_id` at startup and every 30 s, with the skew
  check; the audit row is written once per `run_id`. The comparison is unit-tested with a
  scripted `INFO` and its integration with the real `INFO`; restarting a container from a test
  is not done.
- **Two-instance test:** two `create_app` instances in one process, each with its own in-memory
  stores (there is no shared durable store until Postgres), sharing one Valkey database.
- **Running against Valkey:** `docker-compose.valkey.yml` (the pinned image, append-only, no
  eviction, on 127.0.0.1 only). Tests that need it are marked `valkey` and skipped without
  `GATEWAY_TEST_VALKEY_URL`; a new CI job runs them in a service container. Locally the operator
  starts the compose file.

### PR 1: details settled before building (2026-10-28)

PR 1 is the seam, not the shared backends: everything still runs on SQLite or in memory, and
the only behaviour change is Q3. Settled from reading the code at `adaaa1e`:

- **Async store protocols.** `UsageStore`, `ApplicationStore` and `ModeStore` already are
  protocols; their methods become `async`. `AccountStore`, `RoleStore` and `ProviderStore`
  become protocols too, and today's classes are renamed `SqliteAccountStore`,
  `SqliteRoleStore` and `SqliteProviderStore`. The audit log's rows go behind an `AuditStore`
  (`SqliteAuditStore`); `AuditLog` keeps its job (the field allowlist, the cap, retention) over
  an `AuditStore` and an `AuditCap`. Every implementation is today's code with `async def`: none
  awaits anything that can suspend, so on a `SimClock` no simulated time passes in a store
  call, as today.
- **The write transaction.** Each store's `immediate()` becomes `write_transaction()`, an async
  context manager: `BEGIN IMMEDIATE` on SQLite (the stores of one database share one
  connection, so one transaction covers all of them, as today), nothing in memory. The
  active-key cap and key issuing (section 3.6) move inside it. PR 2 gives it the advisory lock.
- **Counters, over today's in-memory classes.** `RequestRateLimiter.admit`,
  `LoginLimiter.blocked`, `record_failure` and `clear` (the login limiter, the step-up session
  cap and the user-creation cap all use it), the test-connection counts (moved out of
  `ProviderService` into an `InMemoryTestCounter`), `Breakers.admit`, `release`, `success`,
  `failure` and `state`, and the audit cap (`InMemoryAuditCap`, out of `AuditLog`) become async
  behind protocols (`RateLimiter`, `FailureWindow`, `TestCounter`, `BreakerStore`, `AuditCap`).
  The slow-provider warning stays a synchronous, local part of `Breakers` (decision 11).
  Internals unchanged.
- **Budgets.** `CostLedger`'s reads and writes become async behind a `Budgets` protocol
  (`check_budget`, `record`, `usage`). PR 2 replaces check-and-record by reserve, settle and
  release; PR 1 does not change what the ledger decides.
- **`StateBackend`** (`gateway/state.py`): the bundle of stores for one database, built in one
  place (`sqlite_state(connection)`), used by `create_default_app` and `scripts/manage.py`.
  `create_app` keeps its parameters. `GATEWAY_STATE` arrives with `shared` in PR 3.
- **Sync start-up paths.** `create_app` checks every stored application's plan, and the default
  factory creates the bootstrap admin, before any event loop runs the app. They drive the
  store coroutines with `run_now`, which completes a coroutine that does not suspend and raises
  if one does: true of SQLite and memory, and the check that will fail loudly if a network store
  is ever passed there. PR 3 moves both into the lifespan for `shared`.
- **Monitoring** reads per-application usage through the ledger: `Monitor.snapshot` becomes
  async. It still decides nothing.
- **No coroutine is left un-awaited:** `pytest` turns "coroutine ... was never awaited" into an
  error (a `filterwarnings` entry), and mypy already reports an unused coroutine.
- **Q3**, the one behaviour change: `count_login_failure(user_id, now_ms)` on the account
  store reads the row and writes the new count and lock inside one write transaction, and
  closes every step-up window of a user it locks, in the same transaction; `AccountService`
  audits the result. Login and step-up both use it.
- **Benchmarks that time store calls** (`key_checks`, `usage_store_writes`, `login`,
  `session_checks`) keep their recorded numbers in notes 19, 20, 24 and 27, each tied to the
  commit that produced it. PR 1 measures the key check before and after the change on this
  machine (three runs each) and reports both in its description.
- **Mutation checks in PR 1:** Q3 (count from the row read before the verify); the
  early-return mutations of the existing structural tests for login, step-up and the key check,
  run again after the conversion; `run_now` accepting a coroutine that suspends; a dropped
  `await` caught by the warning filter.
- **The note** is `docs/34-state-interfaces.md`; CLAUDE.md's description of the stores
  ("store calls are synchronous and never awaited") is updated in the same PR.

### PR 3: Postgres, settled before building (2026-10-29)

Answers given before building (2026-10-29):

- **`asyncpg` 0.32.0**, pinned exactly: PyPI provenance attestations from `MagicStack/asyncpg`'s
  `release.yml`, and the tag's commit is the one the release run built (`docs/licenses.md`).
- **Postgres 18.6, pinned at the digest recorded on 2026-10-07**
  (`postgres:18.6-trixie@sha256:74935e72241653ca55e0414067e6d8763aceb8a810eb51b452253ec3dcfc4336`):
  it meets the two-week rule. The current tags were rebuilt 2026-10-28 for Debian base layers,
  with the same PostgreSQL package.
- **Budgets move to PR 3b.** That covers the reservation (3.1), the per-tier settings of Q2 and
  the two-instance budget-edge test of Q1. Until PR 3b lands, `GATEWAY_STATE=shared` refuses to
  start, so two instances can never each spend one budget. Section 8's row for PR 3 is narrowed
  accordingly; PR 3b follows PR 3 and comes before PR 2b. PR 3c follows PR 3b (below).

Settled from the code:

- **Stores** (`gateway/postgres.py`): a Postgres implementation of every durable store protocol
  of PR 1, over one baseline schema (`pg0001`, the tables of SQLite migrations 1 to 13). The
  schema version is applied under an advisory lock. A test checks that the two schemas have the
  same tables and columns. SQLite stays the default.
- **Two pools.** `write_transaction()` takes a connection from the management pool, and every
  call outside one uses the request pool. Inside a transaction every store uses that
  transaction's connection (a context variable).
- **The lock (3.6).** One global transaction-level advisory lock, taken with
  `pg_try_advisory_xact_lock` in a loop on the injected clock, up to
  `GATEWAY_STATE_LOCK_TIMEOUT_MS` (default 2000). At the limit nothing is written and the answer
  is `503 state_unavailable`. `statement_timeout` and `idle_in_transaction_session_timeout` bound
  the holder.
- **Time (3.8).** One SQL function, `gateway_now_ms()`, is `clock_timestamp()` in production; a
  test schema replaces it with one that reads a row the test sets.
  - The statements that decide a window read it: the step-up claim, the lockout count.
  - The services read it through a new `StoreClock` on the state bundle: one `now_ms()` per
    operation for sessions, step-up, the lockout, and key expiry. On SQLite and in memory it is
    the injected epoch, as before.
  - Valkey's skew check measures against it when Postgres is configured.
- **Q3 on Postgres:** `count_login_failure` is one `UPDATE ... RETURNING`.
- **Migration:** `scripts/manage.py migrate-to-postgres` (with `--dry-run`), and
  `migrate-to-sqlite` back into a new file (section 2.3, 2.4).
- **`/readyz`**: ready or not, nothing more.
- **Tests and compose:** tests marked `postgres` run when `GATEWAY_TEST_POSTGRES_URL` is set,
  each in a schema of its own. `docker-compose.postgres.yml` (127.0.0.1 only, data on a tmpfs)
  and a CI job run them. The operator starts the compose file locally.

### PR 3: approved with additions (2026-10-29)

Approved on the condition of three additions, made in the same PR:

- **Two-instance tests for the two rules of 3.6 that had none:** the application-mode bound, and
  provider changes with their audit rows. Each has a mutation check.
- **A limit per test across the suite**, so no test can hang on a lock or on another instance.
  `gateway_test_timeout_s` in `pyproject.toml` (60): an async test past it is cancelled and
  fails; any test still running at twice it ends the run with every thread's traceback. It is
  built in `tests/conftest.py`, without a new dependency.
- **A start-up guard on the Postgres URL:** a URL without a password is refused before any
  connection is tried, unless `GATEWAY_POSTGRES_TEST_NO_PASSWORD=1` says it is a test database.

Then, in order:

- **PR 3b: budgets on Postgres.** The reservation (3.1) with the two-instance budget-edge test of
  Q1, the per-tier settings of Q2, and `GATEWAY_STATE=shared` allowed to start. Also Valkey's
  skew check against `gateway_now_ms()` when Postgres is configured: it needs a server running
  on Postgres, which PR 3b is the first to have.
- **PR 3c: each mode read once per request.** A chat request served by an organization provider
  reads the two modes five times (12 of its 20 store calls, note 36). It reads them once and
  passes the effective mode on. A mode change still applies from the next request (docs/33),
  and from the next attempt of a request in flight.

### PR 3b: budgets on Postgres, proposed before building (2026-10-29)

Branch `scale-out/step-3b`, from `main` at `816ccbd` (#26 merged). **It needs approval before
it is built:** it is the first PR in which a server runs on Postgres. That is a new network
surface in production (the gateway's connection to Postgres, section 9) and a new path for a
secret (`GATEWAY_POSTGRES_URL` read by a server). Sessions, logins and keys then live in
Postgres. Questions B1 to B4 at the end need an answer first.

**Storage (Postgres only).** Schema version 2 adds two tables:

- `budget_counters (application_id, period_start_ms, committed_tokens, reserved_tokens)`;
- `budget_reservations (id, application_id, period_start_ms, tokens, expires_at_ms)`.

SQLite gets no migration, because the SQLite ledger does not change (section 6). The
schema-equivalence test lists these two tables as the only ones Postgres has and SQLite does
not. `migrate-to-postgres` seeds nothing: a counter is seeded from the period's usage events
the first time it is reserved against (3.1).

**One interface, two implementations.** The request path calls `reserve(application, budget,
tokens)` before it runs and gets back a `Reservation`. When the request ends it calls
`settle(reservation, result, ...)` or `release(reservation)`.

- `CostLedger` implements these as today's `check_budget` and `record`, and `release` does
  nothing. No new await suspends on SQLite or in memory, so the default path, the perf gate and
  the protected outputs do not change.
- `PostgresBudgets` runs the statements of 3.1:
  - **reserve:** seed the counter, then the conditional `UPDATE`, then the reservation row,
    in one transaction on the request pool (never the write lock, 3.6);
  - **settle:** delete the reservation, subtract it if it was there, add the billed tokens,
    and append the usage event, in one transaction;
  - **release:** delete the reservation and subtract it.

  The period and every expiry are computed from `gateway_now_ms()` inside the statement
  (3.8).

**The bound (Q2).** `tokens = min(prompt_bound + completion_bound, context_tokens)`.

- `completion_bound` is the first of: the request's `max_tokens`; the plan's `max_tokens`
  (which `/v1/generate` already applies when a request names none); the tier's
  `reserve_completion_tokens`.
- `prompt_bound` is the prompt's length in UTF-8 bytes. For chat it is the length of the
  messages as JSON. A byte-level tokenizer never produces more tokens than bytes, and neither
  does the `WordTokenizer` stand-in, so this is a bound without a tokenizer on the request
  path.
- `context_tokens` and `reserve_completion_tokens` are per-tier settings under a new `budgets:`
  key in `backends.yaml`. No numbers are shipped for them: the operator supplies them, like the
  placeholder prices. `shared` refuses to start while any tier lacks either setting, and names
  the tier.

The bound only decides admission. Settle charges the billed tokens actually used, as today.

**Which requests are settled and which released.** Anything that bills today is settled with
the same billed tokens it records today. Anything that bills nothing today is released: a queue
refusal, a timeout, a cancelled or disconnected request, and a provider error before any
output. Every exit from the handler goes through one `finally` that releases a reservation
neither settled nor released. A test covers each exit.

**Expiry and the crash case.**

- `expires_at_ms` is `gateway_now_ms()` at reserve plus the request's own total timeout plus
  60 s. That is 120 s for `/v1/generate` (`DEFAULT_OVERLOAD`) and the chat service's total
  timeout for chat.
- Every instance runs a sweep every 30 s, beside the server (never on a request path). It
  deletes expired reservations, subtracts them, and publishes a `reservation_expired` event.
  That event is counted in `gateway_events_total`, and it carries the application id, never
  anything of the request.

When an instance dies holding a reservation, that reservation keeps counting as in flight
until it expires, at most the timeout plus 60 s. Other requests from that application are
refused only if the leaked reservation is what fills the budget. The dead request's usage is
lost (decision 8), and the expiry event records it. A settle that arrives after the sweep
subtracts nothing a second time (5.3).

What the customer sees during that time: `429 budget_exhausted`, the same body as any budget
refusal. B3 asks about its `Retry-After`.

**Settle while Postgres is down** follows decision 8. The settle is retried with backoff on
the injected clock. While any settle is pending, `/readyz` answers not ready and every new
reserve is refused with 503 `state_unavailable`. If the instance stops with a settle still
pending, it logs the reservation id and nothing else, and the sweep releases the reservation.

**Shared mode allowed.** `check_state_setting` keeps refusing `shared` unless budgets are on
Postgres, which in practice means unless `GATEWAY_POSTGRES_URL` is set. The message says
exactly that. With `shared`, `create_default_app`:

- connects in the lifespan and applies the schema, under its advisory lock;
- reads the stored applications and creates the bootstrap admin there (`startup_applications`,
  never `run_now`);
- uses `postgres_state` and `PostgresBudgets`;
- takes no `ProcessLock`, since several processes are the point;
- answers `/readyz` from `PostgresDatabase.ready()`, the counters' health, and the
  pending-settle rule.

**The Valkey clock check against `gateway_now_ms()` (3.8).** With Postgres configured,
`ValkeyWatch` measures the instance's `EpochSource` against `SELECT gateway_now_ms()`, taking
the midpoint of the round trip. Without Postgres it measures against Valkey's `TIME`, as in
PR 2.

- The bound is `GATEWAY_STATE_MAX_SKEW_MS`, default 1000 ms.
- At startup, an instance past the bound refuses to start.
- Every 30 s after that it checks again. Past the bound, every Valkey-window call is refused
  with 503 `state_unavailable` and `/readyz` answers not ready, until a check passes again.
  Postgres-held windows are not affected.

The test controls both clocks: Postgres's through the test `gateway_now_ms()`, the instance's
through a `ClockEpoch` over a `SimClock`.

**Postgres down, per endpoint.** Note 36's table gains the budget rows:

| Postgres down at | Effect |
|---|---|
| reserve | 503 `state_unavailable`; no model sees the request |
| settle | retried, not ready |
| release | retried like a settle; the sweep covers it if the instance stops |
| sweep | skipped, tried again in 30 s |

Missing equals forbidden: an answer the store cannot give is a refusal.

**Tests.** The suite-wide limit from PR 3 (`gateway_test_timeout_s`, 60 s) covers everything
new. Postgres's time is set by the test throughout.

- **The edge:** a budget with room for exactly one request's bound, and 20 requests split
  across two instances at once. Exactly one is admitted and 19 get `budget_exhausted`;
  committed equals that one request's billed tokens. A second case starts one token short of
  the budget and checks that committed never passes `budget - 1 + bound`.
- **Release:** after a timeout, a queue refusal, a provider error and a cancelled stream,
  `reserved_tokens` is back to 0 on both instances.
- **The crash case:** instance A reserves and is dropped without settling. B is refused while
  the reservation counts. Postgres's time is moved past the expiry, B's sweep releases the
  reservation and B is admitted. A settle from A afterwards changes committed by its billed
  tokens only.
- **No `max_tokens`:** the tier default is reserved, capped at `context_tokens`.
- **The skew:** the start is refused past the bound; drift while running gives not ready and
  503 on the Valkey windows; back inside the bound, ready again.
- **Shared mode:** the refusal without Postgres, and a start with it (Postgres and Valkey,
  both marked).
- **Failure:** each table row with the pool closed under the app.

**Mutation checks** (5.3), one per rule, each restored byte for byte:

| # | Mutation |
|---|---|
| a | the admission condition checked in Python after a `SELECT` |
| b | settle without deleting the reservation first |
| c | release skipped on failure |
| d | release skipped on cancellation |
| e | the sweep ignores `expires_at_ms` |
| f | expiry from the instance's epoch |
| g | the period from the instance's epoch |
| h | the bound not capped at `context_tokens` |
| i | the skew measured against Valkey with Postgres configured |
| j | the periodic skew check disabled |
| k | a reserve that cannot be made admits |
| l | a pending settle does not stop admission |
| m | `shared` starts without Postgres budgets |

**Measured.** `benchmarks/durable_calls.py` gains reserve, settle and release, each timed on
this machine three times. The note records how many store calls one chat request makes now,
counted by a test, beside note 36's figure.

**Unchanged:** the perf gate and the protected outputs (`--include-images`), on the default
path.

**Questions before building:**

- **B1. TLS off loopback** (decision 2) is not built for Postgres or for Valkey.
  - Proposed: in `shared`, a non-loopback host is refused unless its URL asks for a verified
    TLS connection (`sslmode=verify-full` for Postgres, `rediss://` for Valkey, with the
    certificate checked) or `GATEWAY_STATE_PRIVATE_NETWORK=1`.
  - The alternative: `shared` refuses every non-loopback host until a separate TLS PR.
- **B2. Counters in shared mode.**
  - Proposed: `shared` requires `GATEWAY_COUNTERS=valkey`. With counters held per instance,
    each instance would grant its own rate limit and login attempts.
- **B3. `Retry-After` on a refusal caused only by in-flight reservations** (committed below the
  budget).
  - Proposed: the time until the earliest of those reservations expires, which is when room is
    certain to return. The body and code are unchanged.
  - Today it is always the end of the period, which would tell the client to wait until next
    month for room that may come back in seconds.
- **B4. The prompt bound in bytes.** It overestimates the prompt by roughly 4 times for English
  text. A request with nothing else in flight is admitted exactly as it would be with a true
  token count (Q1 compares what is in flight, not the request's own bound). Only concurrent
  requests near the edge are refused sooner.
  - The alternative: the tier's tokenizer on the request path. Only the stand-in exists today.

**Answered 2026-10-29, as proposed:**

- **B1:** verified TLS or `GATEWAY_STATE_PRIVATE_NETWORK=1` for any non-loopback host. This
  applies to the Postgres a server connects to and to the Valkey of `GATEWAY_COUNTERS=valkey`.
- **B2:** `shared` requires `GATEWAY_COUNTERS=valkey`.
- **B3:** `Retry-After` is the earliest expiry of the in-flight reservations when they alone
  fill the budget.
- **B4:** the prompt is bounded by its UTF-8 bytes.

**Settled while building (2026-10-30)**, details in note 37:

- A settle seeds a missing counter with its own event already appended, and adds its tokens to
  the counter if another instance seeded it first. The reserve and the settle each send one
  statement per decision: the take-off is one `DELETE ... RETURNING` feeding an `UPDATE`.
- The release on a hang-up is shielded. Starlette cancels the stream's task again at every
  await in its `finally`, so without the shield the reservation stayed held until it expired.
- The pool's reset of a connection given back to it is a round trip, and it is counted: 7 for
  a reserve and 9 for a settle.
- CI's `postgres` job also starts Valkey, for the shared server's end-to-end test.

## 9. Threat-model additions

- **Two new network peers** holding everything: Postgres holds key HMACs, password hashes,
  sealed provider keys and the audit log; Redis holds counters only, never a key, hash, sealed
  value, prompt or username (only HMACs). Both connections must be TLS when not on loopback, the
  certificate checked (`verify-full`), refused otherwise unless an operator flag says the
  network is private (decision 2).
- **Anyone who can write to Redis can lift rate limits and reset login counters,** within one
  window each. The account lockout and budgets are not in Redis for that reason.
- **Anyone who can write to Postgres owns the gateway**, as anyone who can write the SQLite file
  does today. Least-privilege database roles: the gateway's role can read and write its tables
  and cannot alter schema; migrations run under a separate role (decision 5).
- **A Redis restart resets every Redis counter** (accepted, Q4). After one, an attacker gets a
  fresh window on each: up to 5 more login attempts per username in the limiter (the account
  lockout in Postgres still caps a real account at 5 failures per 15 minutes), a full minute's
  rate limit per application, a fresh step-up failure cap per session, a fresh user-creation
  and test-connection allowance, a fresh audit cap minute, and every breaker closed again (one
  more round of failures before a failing provider is skipped). Someone who can restart Redis
  at will can repeat this once per restart; restarts are recorded (section 3.9), so it cannot
  happen unseen. Not exposed: the account lockout, step-up consume-on-use, budgets, sessions,
  keys, and every provider and mode setting.
- **Clock skew between instances.** Windows held in Postgres use Postgres's time and are not
  affected. Windows held in Redis tolerate a stated skew (section 3.8): each instance stays
  within `GATEWAY_STATE_MAX_SKEW_MS` of Postgres, or refuses. Instances must run NTP.
- **A load balancer is a new forwarding layer.** `LocalOnlyMiddleware` already never trusts
  forwarding headers; behind a load balancer every peer is the balancer, so the local-only
  surfaces (monitoring, `/v1/admin`, the dashboard) must not be routed through it at all. Not
  changed by this plan; stated in the note.
- **A dead instance's reservation and probe** are released by expiry (sections 3.1 and 3.7).

## 10. Decisions not in the request

Numbered so they can be objected to.

1. **Async store protocols,** with in-memory and SQLite implementations that never suspend.
   The alternative, keeping stores synchronous and running a synchronous Postgres driver on a
   thread, keeps today's call sites but blocks a pool thread per call and makes every store
   call a yield point on `SimClock`.
2. **TLS required to Postgres and Redis off loopback,** with `GATEWAY_STATE_PRIVATE_NETWORK=1`
   to allow plain connections on a network the operator vouches for.
3. **Superseded on approval by section 3.8:** windows held in Postgres use Postgres's time
   through `gateway_now_ms()`; windows held in Redis use the instance's `EpochSource` within a
   tested skew bound. Redis's `TIME` is never used.
4. **Valkey, not Redis,** in CI and compose, for the license; the gateway speaks the protocol
   and works with either.
5. **Two migration series kept in step** (`m00NN` and `pg00NN` in the same PR), with a
   schema-equivalence test, and a separate role for migrations.
6. **One global advisory lock for management writes,** reproducing SQLite's semantics exactly,
   instead of per-organization locks or SERIALIZABLE with retries: simpler to prove, and these
   writes are rare. Revisit only with a measurement.
7. **Timeouts on every store call** (Postgres 2 s, Redis 200 ms, on the injected clock),
   counted as unavailability.
8. **A settle that cannot be written is retried in the instance, which stops admitting,** and
   the usage of a request whose instance dies before settling is lost and counted, rather than
   journalled to local disk (a local journal would be per-instance state again).
9. **Redis counters expire instead of an LRU cap,** and the server refuses a Redis without
   `noeviction` and append-only persistence.
10. **A half-open probe has an expiry** (the tier's total timeout), new behaviour needed only
    because an instance can die while holding it.
11. **The slow-provider warning stays local** to each instance.
12. **A new `/readyz`** (not ready while a store is unavailable or a settle is pending);
    `/healthz` keeps its meaning. It is a new endpoint on the request surface and returns no
    detail beyond ready or not.
13. **The dashboard shows one instance;** aggregation across instances is Prometheus's job. No
    cross-instance monitoring is built.
14. **Simulated live mode stays single instance and in memory;** it refuses `shared`.
15. **The cost of shared state is measured, not gated,** in CI: CI's service containers are not
    this machine, and the gate measures the scheduler, not storage.
16. **Key records are never cached** in Redis or locally, so revocation is immediate on every
    instance, at the cost of one Postgres lookup per request (measured in PR 4).
17. **`ProcessLock` stays for SQLite only;** one process per SQLite database remains the rule.

Added with the approved answers (2026-10-28):

18. **The tier's `context_tokens` and `reserve_completion_tokens` are operator settings with
    no shipped value,** like the placeholder prices: no number is invented for them, and
    `shared` refuses to start without them. The simulated wiring never reserves, so it needs
    neither.
19. **Two Postgres connection pools,** request path and management, so a management
    transaction never holds a connection admission needs.
20. **The lock is taken by a try-lock loop on the injected clock** rather than a blocking
    `pg_advisory_xact_lock` under `lock_timeout`: the timeout is then on the same clock as
    every other timeout, and testable on simulated time.
21. **The skew bound defaults to 1000 ms** for Redis-held windows, checked every 30 s; the
    tolerance this gives is stated in section 3.8 rather than assumed.
22. **The Q3 fix goes in PR 1,** because the atomic `count_login_failure` is one of the store
    methods PR 1 introduces; it is the one behaviour change PR 1 makes, named in its
    description.

## 11. Questions, answered 2026-10-28

- **Q1. The budget edge:** admit when `committed + reserved < budget` (the proposal).
  Sequential requests unchanged; concurrent requests at the edge refused where today they are
  admitted; at most one request's bound over budget.
- **Q2. The reservation's bound:** the prompt plus a per-tier configured default, capped at
  the model's context limit, reconciled to actual usage at settle (section 3.1).
- **Q3. The lockout lost update:** fixed on SQLite too, by an atomic increment, with a
  regression test and a mutation check, named as a deliberate behaviour change (section 3.3;
  in PR 1).
- **Q4. Redis restarts:** the counter reset is accepted; the per-account lockout and step-up
  consume-on-use stay in Postgres; a restart is detected and recorded as an event and a
  metric; the exposure is in the threat model (sections 3.9 and 9).

Added on approval: windows that decide security outcomes use Postgres's time, or a stated and
tested skew (section 3.8); a test that a slow management write under the lock does not delay
chat admission, and lock acquisition with a timeout that fails closed (section 3.6).

## 12. Not built

Cross-region replication, Postgres high availability and failover, Redis Cluster or Sentinel,
autoscaling, sticky sessions (not needed: no request depends on which instance served the
last), cross-instance monitoring and dashboards, moving the prefix cache or batching across
instances, and access tokens across instances beyond what the session rows already give.
