# 26 — Organizations

Step 3 of the identity plan (`docs/identity-plan.md`, sections 3 and 13). Applications now
belong to organizations. There are no members yet: users arrive in step 4.

Code: `src/gateway/organizations.py` (kinds, names, the membership rule),
`src/gateway/migrations/m0004_organizations.py`, the runner's table-rebuild support in
`src/gateway/migrations/runner.py`, the stores in `src/gateway/applications.py`, the key check
in `src/gateway/apikeys.py`, the roll-up in `src/gateway/ledger.py` and `src/gateway/usage.py`,
and the CLI in `scripts/manage.py`. Tests: `tests/test_organizations.py`,
`tests/test_organizations_migration.py`, `tests/test_manage_organizations.py`, and additions
to `tests/test_apikeys.py` and `tests/test_migrations.py`.

## What was decided

- **An organization** has an id, a name (unique), a kind (`individual` or `business`), a
  creation time and a `disabled` flag. No provider-mode or provider-key columns; those
  arrive with A5.
- **Every application belongs to one organization**, fixed when it is created.
  Application ids stay unique everywhere, because keys and usage events refer to them.
  Application **names** are unique within an organization, not across organizations.
- **The `default` organization** (kind `business`) is created by migration 4 on every
  database, new or upgraded. Existing applications go into it. `--organization` defaults to
  it, so `seed-demo`, the README quickstart and older scripts work unchanged. It is data, not
  a special case: no code checks for one organization, and the tests use two.
- **Names** (organizations' and applications') are normalized to Unicode NFC, then compared
  exactly, with no case folding, so `Acme` and `ACME` are different names. Refused:
  - empty names;
  - leading or trailing whitespace;
  - more than 100 characters;
  - any character in Unicode's "other" categories. That covers control characters, format
    characters (the zero-width ones and the bidirectional overrides included), surrogates,
    private-use and unassigned code points.

  **Look-alike names are not prevented**: `Acme` and the same word with a Cyrillic first
  letter are two different, accepted names. Names already stored before migration 4 are kept
  as they were.
- **A disabled organization** cannot get new applications, and its applications' keys stop
  working with the same 401 as any bad key. Its data stays, and `enable-organization` brings
  the same keys back.
- **The individual rule** is a pure function, `membership_decision(kind, current_members)`.
  An individual organization takes its first member, who becomes its org_admin, and refuses
  any other; a business organization takes any number. Nothing calls it yet: **step 4 wires
  it into user creation.**

## Migration 4

- **Adds** `organizations` and a required `applications.organization_id` that references
  it, with a unique index on `(organization_id, name)`. That index also serves lookups by
  organization, through its leading column.
- **A table rebuild.** SQLite cannot add a required foreign-key column to a table that has
  rows. So `applications` is rebuilt by SQLite's documented procedure:
  1. create the new table;
  2. copy the rows, in `rowid` order, with `organization_id = 'default'`;
  3. drop the old table;
  4. rename the new one;
  5. create the index.

  `api_keys` refers to `applications` by name, so afterwards it refers to the rebuilt table.
  `usage_events` has never had a foreign key; its column is `application_id TEXT NOT NULL`
  since migration 1.
- **Runner changes, for any migration marked `rebuilds_tables`:**
  - Foreign keys are turned off before the transaction and back on after it, whether it
    commits or rolls back. `PRAGMA foreign_keys` cannot change inside a transaction.
  - `PRAGMA foreign_key_check` must find nothing before `COMMIT`; otherwise the migration
    rolls back.
  - A backfill now receives `applied_at_ms`, the same value recorded in `schema_migrations`,
    taken from the injected `EpochSource`. The `default` organization's creation time is that
    value, never SQLite's clock.
- **Duplicate names refuse it.** If two existing applications share a name, the migration
  names them, rolls back and leaves the database at version 3. `migrate --dry-run` shows the
  same conflicts first. The fix, with the server stopped:

  ```sh
  uv run python scripts/manage.py backup --to before-rename.db   # works at any version
  uv run python -c "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); c.execute('UPDATE applications SET name = ? WHERE id = ?', (sys.argv[3], sys.argv[2])); c.commit(); c.close()" "$GATEWAY_DATABASE" shop-b "Shop B"
  uv run python scripts/manage.py migrate --dry-run
  uv run python scripts/manage.py migrate
  ```

  It was run as written on a version-3 database with two applications named `Shop`: the
  dry run reported the conflict and exited 1, the backup worked, and after the rename
  `migrate` backed up and migrated.
- **The reverse refuses** unless the organizations are exactly what the migration made: only
  `default`, unchanged and enabled. Once another organization exists, or `default` has
  changed, reversing would lose which application belongs where. Then restore the backup
  taken before migrating.

**Tested:**
- Apply, reverse, apply again. The reverse restores the version-3 `table_info` of each
  table and the same rows.
- Running it again applies nothing.
- The `default` organization exists even with no applications, created at the
  `applied_at_ms` recorded for version 4.
- Duplicate names refuse it, with the schema unchanged; after the rename it applies.
- A failing backfill leaves version 3 as it was.
- A rebuild that would leave dangling rows is refused by `foreign_key_check`.
- Foreign keys are back on after a success, a refusal and a failure.
- After the migration, `PRAGMA foreign_key_list(api_keys)` is still
  `applications(application_id → id)`; `usage_events` has none, before and after;
  `applications` references `organizations`. They are enforced, not only declared: an
  `api_keys` row for an unknown application, deleting an application that has keys, and an
  unknown `organization_id` are each refused. pytest's header prints the SQLite version the
  tests ran on: `SQLite 3.49.1` here, and CI prints its own.
- **A realistic database:** six applications; 36 keys (active, revoked, expired, rotated
  within and after the grace period); 5,000 usage events. After the migration, every stored
  row of `usage_events`, `api_keys` and `applications` is equal value for value, floats by
  their bits, with the same SQLite storage types. Every key authenticates as version 3's key
  check decided for its state: 18 accepted, 18 refused.

## Migrating: the CLI, the backup, the server

- **`manage.py migrate --dry-run`** reports the database's version, each pending migration's
  changes and conflicts, and whether a backup would be taken. It exits 1 if `migrate` would
  refuse. It creates no file: a missing database stays missing. An existing one is opened
  read-only, and `immutable` when there is no write-ahead log beside it (the state after a
  clean shutdown), so no `-wal` or `-shm` file appears. When a log exists, it opens read-only
  without `immutable`, so it sees rows not yet checkpointed. Tests cover both.
- **`manage.py migrate`** (and every command that opens the database, all of which migrate
  first) checks for conflicts before anything else.
  - Before a migration that rebuilds a table, on a database that has data, it takes the
    server's process lock, so a gateway still running on the old schema makes it refuse.
  - It then takes a consistent backup with the existing backup mechanism, beside the
    database as `<name>.before-migration-4-<epoch ms>.db`, verifies it, and prints its path.
  - If the backup or its check fails, nothing is migrated.
  - A new, empty database needs no backup.
- **`manage.py backup`** now works at any version, without migrating, so a copy can be
  taken before fixing a conflict.
- Checking a backup (`check_backup`, and `restore`'s source) now opens it `immutable`. A
  backup is a closed file, so this reads it exactly, and SQLite no longer leaves a `-wal` and
  a `-shm` file beside it, which it did before this step.
- **The server does not rebuild a table itself.** If its database has data and needs
  migration 4, `create_default_app` refuses, names `manage.py migrate`, and leaves the
  database and its lock as they were. A new database is still migrated by the server. This
  was not in the request: it follows from "a backup before any rebuild", which would
  otherwise be skipped whenever the server is upgraded and restarted.

## Usage and budgets

- Usage events and budgets stay per application. `/v1/applications/me/usage` keeps its
  meaning and shape; a test pins its keys.
- **The roll-up is reporting only.** `CostLedger.organization_usage(org, budgets)` adds up
  the organization's applications' current-period totals, the same ones their budget checks
  use, in application-id order, and lists each application's used tokens and budget.
  `UsageStore.usage_by_model_for(application_ids, …)` gives the same totals from the stored
  events. On SQLite it is one query, `IN` over the ids, through the existing index; a test
  shows both agree. The in-memory store answers only from what its bounded ring still holds,
  which is why the ledger's totals are the ones reported.
- **There is no organization budget.** An organization's real spending limit is the sum of
  its applications' budgets; `organization-usage` prints that sum.
- **Two organizations never mix** (tests on both stores, and over HTTP):
  - `north` and `south` each have an application named `Shop`;
  - each key reaches its own application;
  - each roll-up counts only its own applications;
  - north's Shop running out of budget (429) leaves south's Shop serving;
  - disabling north stops only north's key.

## The key check

The check now finds the key, its application and whether its organization is enabled in
**one** query, a join, instead of a key read and then an application read. A disabled
organization is decided after the comparison, together with revoked, expired and gone
applications. So every attempt, a disabled organization's included, takes the same steps:
one lookup, one HMAC, one comparison of two 64-character digests.

`test_every_attempt_takes_the_same_steps_whatever_is_wrong_with_the_key` covers the new case.
To show it would catch the mistake, the check was changed to return early for a disabled
organization, before the HMAC. Both store variants then failed, recording only `[lookup]`.
With the change undone they passed, and the file was byte-for-byte as before.

**Timing.** `uv run python -m benchmarks.key_checks` was run three times on each tree: on
`c0f4a54`, before the change (two queries), and on the tree of the commit that adds this
note (one join). Each run is 2,000 checks after 20 warm-up checks, nearest-rank percentiles,
with ranges over the three runs. The machine is the one of notes 19 and 20: Intel Core Ultra
7 155H, 22 logical cores, 15.4 GB RAM, Windows 11 (10.0.26200), Python 3.12.10, SQLite
3.49.1, database in the user's temp folder.

| Batched key check, ms | Mean | p50 | p99 | Max |
|---|---|---|---|---|
| Before (key read, then application read) | 0.017 to 0.021 | 0.017 to 0.020 | 0.024 to 0.032 | 0.103 to 0.190 |
| After (one join) | 0.019 to 0.023 | 0.019 to 0.022 | 0.027 to 0.035 | 0.039 to 0.368 |

The ranges overlap: at this size, the benchmark shows no difference it can resolve.

## The CLI

`create-organization --id --name --kind`, `list-organizations`, `disable-organization` and
`enable-organization` (`--organization ID`), and `organization-usage --organization ID`
(read-only) are new. `create-application` takes `--organization ID`, defaulting to
`default`, and `list-applications` shows each application's organization and filters by
it. Organizations are named by their id. The key commands are unchanged. The README's CLI
examples were run as written.

## Not done here

- Users, members and roles: step 4, which also calls `membership_decision`.
- Changing an organization's kind, renaming organizations or applications, moving an
  application to another organization, and deleting organizations: none has a command.
- Endpoints for any of this: step 5.
