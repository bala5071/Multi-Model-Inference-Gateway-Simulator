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
  over a year before it, for invoices). The ledger deletes events older than that, at most
  once an hour, from the first charge after it is due. **Events of the current period are
  always kept**, whatever the retention, so a budget never forgets what was used this month.
  Migration 3 adds an index on `usage_events (occurred_at_ms)` so the delete does not scan
  the table. Tested on both stores, including "at most hourly" and "current period kept".
  The in-memory store keeps its bounded ring as well.
- **Nothing new is stored.** The no-stored-content test now also runs against the default
  server's own database file, with usage, applications and keys in it.

## More than one process

**Not supported on one database.** SQLite serialises writers (WAL mode and the 5 s busy
timeout make a second writer wait, not fail), but each process keeps the current period's
totals in memory and only reads the store the first time it sees an application in a period.
Two processes on one file would each check budgets against their own totals and not see the
other's charges until the next period: the budget would be enforced per process, and could
be exceeded by up to the number of processes times the budget. Running one gateway process
per database is the supported setup. Sharing budgets across processes needs a shared
store read on every check, or a central counter; that belongs with a server database (A2 or
later).

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
- **None of this has been run here**: the development machine has no Docker (note 18). CI's
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
