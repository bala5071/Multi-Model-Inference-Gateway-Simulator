# 34. State interfaces: every store behind a protocol, async

Scale-out plan, pull request 1 (docs/scale-out-plan.md, section 8 and "PR 1: details settled
before building"). The seam that PRs 2 and 3 put Postgres and Redis behind. Everything still
runs on SQLite or in memory, and one behaviour changes, on purpose (Q3, below).

## What changed

- **Every store is a protocol whose methods are coroutines.** `UsageStore`, `ApplicationStore`
  and `ModeStore` already were protocols; `AccountStore`, `RoleStore`, `ProviderStore` and the
  audit log's `AuditStore` now are too, and today's classes are `SqliteAccountStore`,
  `SqliteRoleStore`, `SqliteProviderStore` and `SqliteAuditStore`. The SQLite and in-memory
  implementations are the same code with `async def`: **none of them awaits anything that can
  suspend**, so on a `SimClock` no simulated time passes inside a store call, which is what
  note 19 required of synchronous calls.
- **Counters behind protocols, over the existing in-memory classes:** `RateLimiter`
  (`RequestRateLimiter`), `FailureWindow` (`LoginLimiter`: the login limiter, the step-up session
  cap and the user-creation cap), `ConnectionTestCounter` (`InMemoryConnectionTestCounter`, moved
  out of `ProviderService`), `BreakerStore` (`Breakers`; the slow-provider warning stays
  synchronous and local), `AuditCap` (`InMemoryAuditCap`, moved out of `AuditLog`) and `Budgets`
  (`CostLedger`'s check and record; PR 2 replaces them with reserve, settle and release).
- **`write_transaction()`** replaces `immediate()`: one async context manager, `BEGIN IMMEDIATE`
  on SQLite (`gateway.database.write_transaction`, shared by every SQLite store of a
  connection), nothing in memory. **Issuing and rotating a key now count the active keys, write
  the key and write its audit entry in one transaction** (plan, section 3.6); before, the count
  and the write were atomic only because nothing awaited between them.
- **`gateway.state`**: `StateBackend`, the stores of one database, built by `sqlite_state` for
  the default server and `scripts/manage.py`; and **`run_now`**, which completes a store call
  where no event loop runs yet (`create_app`'s check of the stored plans, the default server's
  bootstrap admin, the simulated live app's seeding) and raises `StoreSuspendedError` for a call
  that suspends, so a network store cannot be used there by mistake. PR 3 moves those calls into
  the lifespan for the shared backends.
- **The egress guard's scope answer is a coroutine**: the router's answer reads the mode store.
  An answer that raises refuses the connection before the name is resolved (tested).
- **The CLI runs its store work in one `asyncio.run`.** Async tests that call it do so on a
  thread (`asyncio.to_thread`).
- **Monitoring** builds its snapshot with `await` (it reads usage through `Budgets`); it still
  decides nothing.

## Q3: concurrent wrong passwords each count (the one behaviour change)

`login` (and `step_up`) read the user, awaited the password verify (the server's thread runner
yields), then wrote `failed_logins + 1` from the row read before the await: two wrong passwords
at the same moment both wrote 1. The account store's `count_login_failure` now reads the row
and writes the count, inside one write transaction with the lock, the closing of the user's
step-up windows and the `login_locked` audit entry.

Run on this branch before the fix, with a hash runner that holds each verify until all the
logins have read the user (`tests/test_accounts.py`):

| Test | Before | After |
|---|---|---|
| two wrong passwords at once | `failed_logins` 1 | 2 |
| five wrong passwords at once | not locked | locked once, one `login_locked` entry with `failures` 5 |

The in-memory login limiter already counted both, so before the fix a sixth attempt was still
refused by the limiter; the account lockout, which survives a restart, was not reached.

## Guards against a missing `await`

A coroutine that is not awaited is truthy and does nothing, so `if not hosted_allowed():` without
its `await` would let every hosted connection through. Three guards:

- **mypy `truthy-bool`** (enabled in `pyproject.toml`) reports a coroutine used as a condition.
  Strict mode alone does not.
- **pytest turns "coroutine ... was never awaited" into an error**, and the unraisable-exception
  warning it raises into a failure of the test that created it.
- The conversion of the tests found what each catches. mypy missed coroutines compared with
  `is None` or put in a list: two leak scans were scanning coroutine objects instead of the
  stored records, and the resolver tests' `assert resolve_...(...) is not None` were passing
  without running. pytest's filter failed all of them; they are fixed.

## Not built

Postgres, Redis, reservations, the advisory lock and its timeout, `GATEWAY_STATE`, `/readyz`, the
Postgres-time windows and the skew bound: PRs 2 to 4. Store implementations other than SQLite
and memory.

## Timing

Measured, not simulated: `uv run python -m benchmarks.key_checks` (2,000 checks after 20
warm-up checks, SQLite WAL with synchronous FULL, in the system temporary folder), three runs
before the change (at `cb8021f`, the code of `main`) and three after (at `9ae964e`). Intel Core
Ultra 7 155H, 22 logical cores, 15.4 GB, Windows 11 (10.0.26200), Python 3.12.10. Ranges are
over the three runs; after the change each check is completed by `run_now`, so the time includes
creating and running the coroutine but no event loop.

| One key check, last_used batched | mean ms | p50 ms | p99 ms |
|---|---|---|---|
| before (`cb8021f`) | 0.041 to 0.042 | 0.035 to 0.036 | 0.076 to 0.087 |
| after (`9ae964e`) | 0.033 to 0.041 | 0.035 | 0.079 to 0.087 |

No difference outside the spread between runs. The perf gate (simulated, no stores in its path)
is unchanged: +0.0% on every model.
