# Scale-out plan: several gateway instances on Postgres and Redis

Status: **draft, waiting for approval** (it touches authentication, sessions, step-up, the
lockout, provider keys at rest, a migration path for every table, and two new network surfaces:
the gateway's connections to Postgres and to Redis). Branch `scale-out/plan`, from `main` at
`8bebbe8`. Plan only: no code, no dependency added.

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
   3.1): it needs an answer (question Q1).

## 1. Every piece of state the gateway holds today

Read from the code at `8bebbe8`. "Two instances" means two processes behind a load balancer that
sends any request to either. **Safe** means the rule still holds with two instances and no
change. The **Needs** column is the plan: a Postgres row, a Redis counter, or local.

Two facts decide most rows:

- **`WallClock` is per process.** `WallClock.now_ms()` is `time.perf_counter()`: its zero is
  the process start, so two instances' readings cannot be compared. Every in-memory window
  today (rate limits, the login limiter, the step-up and user-creation caps, test-connection
  counts, the circuit breakers) is timed on that clock. Anything shared must be timed on
  `EpochSource` (calendar time), which is what the stored rows already use (decision 3).
- **Many checks are atomic today only because the event loop is single-threaded,** or because
  SQLite's `BEGIN IMMEDIATE` takes a lock on the whole database. Neither holds across
  processes on Postgres. Each such check is listed below and handled in section 3.

### 1.1 Durable state (SQLite today, Postgres rows after)

| State | Where it lives today | Safe with two instances? | Needs |
|---|---|---|---|
| **Usage events** (`usage_events`, migrations 1, 3, 13) | SQLite rows, appended by `CostLedger.record` | The rows, yes. The budget built on them, no (next row) | Postgres row, same columns; `seq` an identity column |
| **Budgets** (`CostLedger._accounts`) | The current period's totals **in process memory**, read from the store only the first time an application is seen in a period; check before the request, charge after (`check_budget`, `record`) | **No.** Each instance enforces the budget on its own totals; N instances can spend up to N budgets (note 21 says this; `ProcessLock` refuses a second process for that reason) | Postgres row per (application, period): `committed_tokens`, `reserved_tokens`, plus a reservation row per admitted request (section 3.1) |
| **API key hashes** (`api_keys.key_hmac`, migration 2) | SQLite rows; HMAC-SHA256 with `GATEWAY_KEY_PEPPER` | Yes, if every instance has the same pepper | Postgres row, same columns. **Never cached** in Redis or in memory (a revocation must take effect on the next request on every instance) |
| **Key `last_used_at`** | Pending times in memory per instance (`ApiKeyService._pending`), flushed at most once a minute | Mostly: each instance flushes its own. A plain `SET` can move the time **backwards** when an older flush lands after a newer one | Postgres row, written as `GREATEST(last_used_at_ms, $new)` |
| **Applications, organizations** (migrations 2, 4) | SQLite rows | Rows yes; the per-application active-key cap (`ManagementService._check_room` then `add_key`) is a read then a write with no transaction, atomic only within one event loop | Postgres rows; the cap under the write lock (section 3.6) |
| **Users, lockout** (`users.failed_logins`, `first_failure_at_ms`, `locked_until_ms`, migration 5) | SQLite rows | **No, and not fully even in one process.** `login` reads the user, awaits the password verify (the thread runner yields), then `_count_failure` writes `failed_logins + 1` from the value read **before** the await: two concurrent wrong passwords both write 1. In one process the in-memory `LoginLimiter` still counts both; across instances it does not | Postgres row, the count made in one `UPDATE ... RETURNING` (section 3.3) |
| **Sessions** (migration 6) | SQLite rows; digests only; `last_seen_at_ms` written at most once a minute | Yes for the rows. `last_seen` can go backwards like `last_used_at` | Postgres row; `last_seen` as `GREATEST` |
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
  its `max_tokens` (the request's, or the plan's, which today is also used when a request asks
  for none). With no bound at all, see question Q2.
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
where today they are admitted. Q1 asks whether that is wanted, or whether to require the whole
bound to fit (`committed + reserved + tokens <= budget`, never over budget, stricter for
sequential requests too). The plan proposes the first. The SQLite ledger is not changed in
either case (section 6).

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

This also fixes the lost update that exists in one process today (section 1.1). Fixing it on
SQLite as well changes one-instance behaviour (concurrent wrong passwords count fully), so it
is asked as Q3 rather than folded in.

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

On Postgres, `write_transaction()` takes `pg_advisory_xact_lock(<one fixed key>)`: one global
write lock for management writes, which is exactly SQLite's semantics (decision 6). These are
administrative writes, rare next to requests; none of the request path (budgets, step-up
claims, the lockout, sessions) goes through it. The active-key cap and key issuing move into
it.

### 3.7 Circuit breakers

One Redis hash per (organization, provider) and one Lua script per event (`admit`, `release`,
`success`, `failure`), with the same transitions as `breaker.py` and times from `EpochSource`.
In half-open, `admit` sets `probing` only if it was unset, so exactly one request across all
instances probes. A probe whose instance dies would hold `probing` forever, so it carries an
expiry (the tier's total timeout); this is new and is decision 10. Transitions are returned by
the script and published by the instance that made them as `circuit_breaker` events on its own
`EventBus`, so monitoring stays local and decides nothing.

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
| **Redis restart (empty)** | Not detectable per key. Every counter it held covers at most one hour, so the loss is bounded by its window; the durable controls (the account lockout, budgets, step-up windows, sessions) are in Postgres and survive. The startup check requires `maxmemory-policy noeviction` (an evicted counter is a forgotten failure: open) and appendonly persistence, and refuses to start without them (decision 9) |
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
- Time in `shared` tests comes from a `ClockEpoch` over a `SimClock` with no sleepers, moved
  explicitly by the test: awaiting network I/O must not let simulated time jump.

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
| 1 | `scale-out/step-1` | Every store behind a protocol, async; `StateBackend` with `memory` and `sqlite`; `write_transaction()`; the window, budget, breaker and audit-cap protocols over today's in-memory classes | None: the same tests, the perf gate and the protected outputs unchanged |
| 2 | `scale-out/step-2` | Postgres: `asyncpg` (provenance check), `pg0001`, every durable store, the budget reservation, the step-up claim, the lockout update, the write lock, `migrate-to-postgres` and back; contract tests on Postgres; the CI service job | Only with `GATEWAY_STATE=shared`, which is not yet startable |
| 3 | `scale-out/step-3` | Redis: `redis` (provenance check), the window script, the audit cap, the breakers, the startup checks; `GATEWAY_STATE=shared` startable; `/readyz`; the failure behaviour | Only with `shared` |
| 4 | `scale-out/step-4` | The two-instance tests in process and in two processes, the mutation table, the failure tests, `benchmarks/shared_state.py`, the note, the compose file for a two-instance demo on Postgres and Valkey | None |

PRs 2 and 3 need plan approval items answered first (section 10). Each PR has the template,
the definition of done, and its note (`docs/34-*` onward).

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
- **Clock skew between instances** shifts every window by the skew (a limit can be off by the
  requests in that slice) and the step-up window's end. Instances must run NTP; the startup
  check compares the instance's epoch with Postgres's `now()` and refuses beyond 2 s (decision
  3).
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
3. **Shared windows are timed on `EpochSource`, not `WallClock` and not the server's clock**
   (`TIME` in Redis, `now()` in Postgres), so the injectable-clock rule holds and tests stay
   deterministic; with the 2 s skew check at startup.
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

## 11. Questions to answer before PR 2

- **Q1. The budget edge.** Admit when `committed + reserved < budget` (proposed: sequential
  requests unchanged, concurrent requests at the edge refused where today they are admitted;
  at most one request's bound over budget), or when `committed + reserved + tokens <= budget`
  (never over budget; a request whose bound does not fit is refused even when tokens remain)?
- **Q2. A request with no bound** (no `max_tokens` from the request or the plan): reserve the
  prompt plus a configured default, or refuse such requests under `shared`?
- **Q3. The lockout lost update in one process today** (section 3.3): fix it on SQLite too, as
  its own small PR with its mutation check (a deliberate one-instance behaviour change), or
  leave SQLite as it is?
- **Q4. Shared Redis restart:** accept the reset of at most one window (proposed), or refuse
  logins for one lockout window after a Redis restart is detected (safer, locks everyone out
  for 15 minutes)?

## 12. Not built

Cross-region replication, Postgres high availability and failover, Redis Cluster or Sentinel,
autoscaling, sticky sessions (not needed: no request depends on which instance served the
last), cross-instance monitoring and dashboards, moving the prefix cache or batching across
instances, and access tokens across instances beyond what the session rows already give.
