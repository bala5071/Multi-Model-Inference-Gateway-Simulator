# 21 — Persistence in the default server

Step 3 of the platform plan (`docs/platform-plan.md`). The default server now keeps usage in
its SQLite database, budget periods are calendar months in UTC, old usage is pruned after a
configurable retention, and the compose file keeps the database on a volume with its secrets
in a git-ignored env file.

Code: `src/gateway/periods.py`, changes in `ledger.py`, `usage.py` and `app.py`, migration 3
(`src/gateway/migrations/m0003_usage_retention.py`); `docker-compose.yml`,
`gateway.env.example`, `Dockerfile`, `.dockerignore`. Tests: `tests/test_persistence.py`,
`tests/test_deployment_config.py`, a default-server case in `tests/test_no_content_stored.py`.

## What was decided

- **The default server stores usage events in its database**, through the existing
  `UsageStore` (`SqliteUsageStore`), dated by `SystemEpoch`. Applications and keys were
  already there (note 20). A restarted server reads the current period's usage back the
  first time it sees each application, so usage and budgets survive a restart:
  `test_usage_and_budgets_survive_a_restart` exhausts a budget, builds a new app on the same
  file with a new clock, and is still refused; `test_the_default_server_stores_usage_in_its_database`
  does the same through `create_default_app` twice.
- **Budget periods are calendar months in UTC** (`CalendarMonthsUTC`), read on the injected
  epoch source, replacing the 30 days counted from when the process started. A month
  starts at 00:00 UTC on the 1st; the instant of midnight belongs to the new month. The
  month is worked out from whole days since the epoch, not from `datetime.fromtimestamp`,
  which rounds to microseconds and could move the last instant of a month into the next.
  `Retry-After` on `budget_exhausted` is the time to the next 1st. Tested: January (31
  days), February in a leap year (29) and not (28), 2000 (a leap year) and 2100 (not),
  April (30), December to January, and the instants either side of midnight.
  `FixedPeriods` remains for tests that want short, hand-checkable periods.
- **The soft budget is unchanged:** checked when a request arrives, charged when it
  finishes. So **a request that straddles a month boundary is checked against the month it
  started in and charged to the month it finished in**:
  `test_a_request_that_straddles_the_month_is_charged_to_the_month_it_finishes_in` starts a
  long request half a second before midnight on 31 December and finds its event dated
  January and counted there, with nothing in December. Requests arriving together while
  under budget all pass and overshoot together, as before.
- **Retention:** `GATEWAY_USAGE_RETENTION_DAYS` (default 400: the current month and a little
  over a year before it). Events older than that are deleted **off the request path**: a
  task beside the server (`run_retention`, started by the app's lifespan) prunes when the
  server starts and then every hour, in **batches of 1000 events**, each one short
  transaction, yielding to the event loop between batches. No charge or request prunes.
  **Events of the current period are always kept**, whatever the retention, so a budget
  never forgets what was used this month; the delete stops at the start of the current
  month even when the retention is shorter than a month. Migration 3 adds an index on
  `usage_events (occurred_at_ms)` so a batch finds its rows without scanning the table.
  Tested on both stores: the batches (1000, 1000, then the remainder), the hourly pace,
  that a charge never prunes, and that a one-day retention leaves the current month alone.
  The in-memory store keeps its bounded ring as well.
- **Nothing new is stored.** The no-stored-content test now also runs against the default
  server's own database file, with usage, applications and keys in it.

## How long pruning takes

Measured with `uv run python -m benchmarks.usage_retention` (defaults: a table of 1,000,000
events across 50 applications, 100,000 of them past the retention, batches of 1000), run three
times, on the tree of the commit that added this text (parent `7b613d3`). Machine: Intel Core
Ultra 7 155H, 22 logical cores, 15.4 GB RAM, WD PC SN810 1 TB NVMe SSD, Windows 11
(10.0.26200), Python 3.12.10, SQLite 3.49.1; database set up as the server's (WAL,
`synchronous = FULL`, migrated) in the system temporary folder on that SSD. Nearest-rank
percentiles; ranges over the three runs. One laptop, one disk, one day.

| What | n | mean ms | p50 ms | p99 ms | max ms |
|---|---|---|---|---|---|
| one batch of 1000 (as the retention task runs it) | 101 | 2.9 to 3.3 | 2.0 to 2.2 | 11.8 to 12.6 | 12.3 to 22.1 |
| all 100,000 expired events in one delete | 1 | 126 to 135 | | | |

So a request can wait behind pruning for one batch, about 2 ms typically and 22 ms at
worst here, where an unbatched pass would block everything for about 130 ms. A million events
is, for example, about 390 days at 0.03 requests a second, or 11.5 days at 1 a second; at
steady state an hourly pass deletes about one hour's worth of old events (3,600 at 1 a
second, four batches).

## More than one process

**Not supported on one database, and refused.** Each process keeps the current period's
totals in memory and only reads the store the first time it sees an application in a
period. Two processes on one file would each check budgets against their own totals and not
see the other's charges until the next period: the budget would be enforced per process, and
could be exceeded by up to the number of processes times the budget. Sharing budgets across
processes needs a shared store read on every check, or a central counter; that belongs with
a server database (A2 or later).

**The guard:** the default server takes an exclusive lock on `<database>.lock` beside the
database (`flock` on POSIX, `msvcrt.locking` on Windows) and holds it for its lifetime. A
second server on the same database refuses to start: "… is in use by another gateway process
… Run one gateway process per database, or point GATEWAY_DATABASE at another file."
`scripts/manage.py` does not take the lock (keys can be issued while the server runs; SQLite
serialises the writes), except `restore`, which does and so refuses while a server runs.
Tested: a second lock in one process, a second default server, and a lock held by another
process that is then killed.

Limits of the guard:

- **A crashed process leaves no stale lock.** The lock belongs to the open file, and the
  operating system releases it when the process exits, however it exits (the test kills the
  holder and then takes the lock). The `.lock` file itself stays; its presence means
  nothing. **On Windows the release is not instant:** the system frees a dead process's
  locks shortly after it exits (Microsoft documents the delay as depending on system
  resources), so a server restarted in that moment may refuse once; starting it again
  works. The test waits for the lock to become free rather than expecting it at once; a
  first version expected it at once and failed now and then on this Windows machine.
- **Network filesystems:** advisory locks over NFS or SMB may not be honoured between
  machines, so two servers on two machines sharing one file over the network may both start.
  SQLite itself warns against network filesystems for the same reason. Keep the database on
  local disk.
- It guards one database file. Copies of the database (a restored backup elsewhere, a second
  `GATEWAY_DATABASE`) are separate databases with separate budgets.

## Backup and restore

The database holds applications, key HMACs and usage. Back it up with:

```sh
uv run python scripts/manage.py backup --to /backups/gateway-2025-06-01.db
```

This uses SQLite's online backup API: a consistent snapshot of one moment, safe while the
server is running and writing (copying the file instead could catch it half-written and miss
the write-ahead log). The target must be a new file; it is created owner-only like the
database. In the container: `docker compose exec gateway /app/.venv/bin/python
scripts/manage.py backup --to /data/backup.db`, then copy it off the volume.

To restore, **stop the server**, then:

```sh
uv run python scripts/manage.py restore --from /backups/gateway-2025-06-01.db
```

Restore checks the backup's integrity and that it is a gateway database before touching
anything, refuses while a gateway process holds the database (the lock above), and copies
the backup in through the same API. The server migrates it forward when it next starts.
**Updated since:** except for a migration that rebuilds a table (migration 4); for that, run
`scripts/manage.py migrate` first, which backs the database up (note 26).
`tests/test_backup.py` backs up a live database, revokes a key, adds usage and deletes an
application, restores, and finds the applications, the key and the usage exactly as they
were; it also checks refusals (a running server, a damaged file, a non-gateway file, an
existing target) and the CLI round trip.

- **A backup holds what the database holds: key HMACs and, since identity step 4, users'
  argon2id password hashes**, with the audit log and usage. **Never commit one.** `.gitignore`
  ignores `*.db` and `*.db-*` anywhere in the tree (`tests/test_database.py` checks backup
  names, the copy taken before a migration included). A backup is created owner-only like the
  database: on POSIX the file is 0600 (and a folder it creates 0700; `tests/test_backup.py`
  checks it on POSIX); on Windows it inherits the folder's permissions, so keep backups in a
  folder only you can read. `manage.py backup` has no default location (`--to` is required);
  the copy `manage.py migrate` takes before a table rebuild goes beside the database, which by
  default is in the user's data folder, outside the working tree.
- **The pepper is not in a backup and must be backed up separately** (note 20): a restored
  database without the same pepper checks no key. Keep the two apart: either alone is
  useless to whoever takes it.
- **Pruned usage is not a billing record.** Retention deletes usage events for good, and
  backups age out too. Usage here is for budgets and monitoring; anything that must be kept
  for invoicing or accounting has to be exported to a system meant to keep it, before the
  retention removes it.

## Docker

- `docker-compose.yml` publishes on `127.0.0.1:8000` only (note 20's checks), mounts the
  volume `gateway-data` at `/data`, and reads `gateway.env` through `env_file`, marked
  `required: false` so that the demo (simulated live mode, everything in memory) still runs
  without one.
- `gateway.env` is git-ignored and kept out of the build context (`.dockerignore`).
  `gateway.env.example` has placeholders only: the pepper, `GATEWAY_DATABASE=/data/gateway.db`
  and the retention. No secret is in the compose file or the image; a test checks both.
- The image creates `/data` owned by the `gateway` user, mode `0700`, and carries
  `scripts/manage.py`, so keys are issued inside the container:
  `docker compose exec gateway /app/.venv/bin/python scripts/manage.py create-key ...`.
- **None of this has been run here**: when this note was written the development machine had
  no Docker (it has Docker Desktop now; note 25 records what has been run on it). CI's
  docker job builds the image and starts the compose demo; the plain server with an env file
  and the volume has not been started anywhere.

## Changes for callers

- A budget now resets at 00:00 UTC on the 1st of each month, not 30 days after the server
  started. `period.start_ms` and `period.end_ms` in the usage response and the snapshot are
  epoch milliseconds of those instants (in simulated runs, on the simulated epoch, which
  starts at 1 January 1970).
- The dashboard fixture `snapshot-simulated.json` changed in three lines: each tenant's
  `period_end_ms` is the end of January 1970 (31 days) instead of 30 days.
- `CostLedger` takes `periods=` (default `CalendarMonthsUTC()`) and `retention_ms=` instead of
  `period_ms=` and `period_anchor_ms=`; `THIRTY_DAYS_MS` is gone.
