# 19 — Usage events, the epoch source and migrations

Step 1 of the platform plan (`docs/platform-plan.md`). The ledger's charges become usage
events in a store that can outlive the process. Nothing visible changes yet: the default
server still uses an in-memory store, and no endpoint, response or number moved.

Code: `src/gateway/usage.py`, `src/gateway/database.py`, `src/gateway/migrations/`,
`EpochSource` in `src/gateway/clock.py`, changes in `src/gateway/ledger.py` and
`create_app`. Tests: `tests/test_usage_store.py`, `tests/test_migrations.py`,
`tests/test_no_content_stored.py`. Benchmark: `benchmarks/usage_store_writes.py`.

## What was decided

- **A usage event is one settled request**: application id, response id, model, prompt,
  cached and completion token counts, base and billed cost, epoch time. It has no field
  that could hold text or token ids. Today the application id is the tenant id; step 2
  makes tenants applications.
- **`UsageStore` is a protocol** with two implementations. `InMemoryUsageStore` is a ring
  of the newest 10,000 events, the default everywhere. `SqliteUsageStore` writes to the
  `usage_events` table.
- **`CostLedger` is a facade over the store.** Every charge is appended to the store first,
  then added to the current period's totals in memory, so a budget check never reads the
  store. The first time the ledger sees an application in a period it reads that period's
  totals from the store, which is how a restarted process picks up what was used before.
  Its API and all its tests are unchanged.
- **The ring loses nothing the ledger needs.** The ledger only asks a store for a period's
  totals when it first sees an application in that period, and in one process there can be
  no event of that application in that period yet.
- **Each row has its own key**, `seq`, an SQLite `AUTOINCREMENT` integer: unique, increasing,
  never reused, even after a restart or after the newest row is deleted. The response id
  (`req-N`) is unchanged and repeats across restarts. Both are tested.
- **Periods are counted in epoch time** from `period_anchor_ms` (default: when the ledger
  was created, as before). Step 3 will pass a stored anchor so a restart keeps the period.
- **Store calls are synchronous and never awaited.** `SimClock` moves time forward when the
  event loop is idle, and a write handed to a thread makes the loop look idle. The store
  protocol has no async methods, and a test checks that.

## The epoch source

`EpochSource` is a separate injected object next to `Clock`; `Clock` did not change.
`WallClock` reads `perf_counter`, which has no fixed origin, so it cannot date a row that
must outlive the process. `SystemEpoch` reads `time.time()`, in `clock.py`, still the only
module allowed to read real time; `time.time_ns` was added to ruff's banned list.
`ClockEpoch(clock, origin_ms)` is the clock's reading plus an origin: on a `SimClock` it is
as deterministic as the clock, and it is what the ledger uses when it is given no epoch, so
every existing result is computed exactly as before.

## Migrations

`MigrationRunner` applies numbered migrations (1, 2, 3, ... with no gaps), each in its own
`BEGIN IMMEDIATE` transaction together with its row in `schema_migrations` (version, name,
epoch time applied). A migration has `up`, `down` and an optional `backfill`, which runs
after `up` in the same transaction, so a failed backfill leaves the schema as it was.
`rollback(target)` reverses migrations newest first. Running `migrate` again does nothing.
The runner refuses a database newer than the code, a gap in the applied versions, and a
connection that is not in autocommit mode (Python's implicit transactions would commit
behind its back). Migration 1 creates `usage_events` and its index, and nothing else.
Tests cover apply, reverse, up-down-up, idempotence, a backfill and a failing backfill.

## SQLite settings

`open_database` turns on write-ahead logging (readers do not wait for a write), a 5 s busy
timeout (a second writer waits instead of failing), `synchronous = FULL` (a committed row
survives a power cut) and autocommit, so each append is one short transaction.

## How long a write takes

Measured with `uv run python -m benchmarks.usage_store_writes` (2000 writes after 20 warm-up
writes, per store), run three times, on the tree of the commit that added this note
(parent `47cb625`). Machine: Intel Core Ultra 7 155H, 22 logical cores, 15.4 GB RAM,
WD PC SN810 1 TB NVMe SSD, Windows 11 (10.0.26200), Python 3.12.10, SQLite 3.49.1. The
database was in the system temporary folder on that SSD. Nearest-rank percentiles; the
range is over the three runs.

| Store | mean ms | p50 ms | p99 ms | max ms |
|---|---|---|---|---|
| in-memory | 0.002 | 0.001 | 0.002 to 0.003 | 0.79 to 0.81 |
| SQLite, WAL, `synchronous = FULL` (the default) | 0.361 to 0.378 | 0.334 to 0.336 | 0.683 to 0.744 | 1.3 to 16.5 |
| SQLite, WAL, `synchronous = NORMAL` | 0.034 to 0.043 | 0.021 | 0.066 to 0.078 | 6.3 to 18.9 |

One laptop, one disk, one day; nothing was measured anywhere else.

**A synchronous write blocks the event loop** for as long as it takes. With FULL that was
about a third of a millisecond per settled request on this machine, with single writes
taking up to 16.5 ms: during such a stall no other request on that process is scheduled,
streamed or answered. That cost is only paid where a SQLite store is configured, which no
server does until step 3, and never on the protected benchmarks, which run without plans.

What would replace it later, if the stall matters: on the `WallClock` path only, a single
writer thread fed by a queue, committing in small batches, with the ledger's in-memory
totals still updated at once. That moves the write off the loop at the price of losing the
events still queued if the process dies. The `SimClock` path stays synchronous for the
reason above. A server database is for A2 or later, when more than one process writes.

## What did not change

The performance gate, the load generator and the report produce byte-identical output (the
report except its "from commit" line); the plan's section 5 lists how that is checked.
`create_app` without plans has no new code on its request path.

## What is stored, and what is not

`tests/test_no_content_stored.py` sends requests with made-up prompt words through the
whole app, once in simulator mode (where the prefix cache holds the prompt's hashed token
ids) and once in front of a mock real model server (which returns real text). It then
searches the usage database's file bytes and rows, every log record at every level, every
event, the monitoring snapshot, the Prometheus and summary metrics and the usage endpoint
for each prompt word, the response text and every token id (decimal, hex and raw bytes).
None is found. A third test plants each kind of needle and checks the search finds it.
