# 57. Clients: the platform owner's view, and the recorder's hours (PR 3a)

PR 3a of `docs/console-ia-v2-plan.md`, planned in its section 15 and approved with the conditions
of 15.6. It adds four reads, one permission row, the recorder's hour and organization counts, and
two console pages. Creating an organization (Q6 C) is PR 3b, planned first, and is not here.

## The routes

All under `/v1/admin`: local only, `Cache-Control: no-store`, each through `requires()` with one
action and no step-up. A missing organization, a missing application and an application of
another organization answer the same 403 bytes as a forbidden one, for every role.

| Method | Path | Action | Step-up | Success |
|---|---|---|---|---|
| `GET` | `/v1/admin/orgs` | `organizations.list` | | 200 |
| `GET` | `/v1/admin/platform/usage` | `usage.aggregate` | | 200 |
| `GET` | `/v1/admin/platform/orgs/{org}/applications/metrics` | `usage.read_client_applications` | | 200 |
| `GET` | `/v1/admin/platform/orgs/{org}/applications/{app}/metrics` | `usage.read_client_applications` | | 200 |

- `organizations.list` and `usage.aggregate` existed (wired `now` and `later`); both are now wired
  `console` and are navigation actions, so the capabilities answer shows **Clients**.
- **`usage.read_client_applications`** is new (section 13, Q1): organization level, allowed
  `platform_admin@platform` only. It landed in its own commit with the matrix, docs/28's table and
  the `WHO_MAY_DO_WHAT` pin (60 rows). Every other read and write in a client stays refused to
  platform_admin, each asked through its route (`tests/test_client_usage.py`).
- **Fixed fields.** Each answer holds the field names of `usagefigures`' allowlists and nothing
  else (a test compares them exactly): ids, names, an organization's kind and whether it is
  enabled, its application count, counts, rates, a percentile, and open incidents counted by
  state. Never a key or key id, setting, mode, channel or target, rule, incident text or id,
  provider, prompt or response.
- **A page and a cap.** Every list is a page of 50 by id, at most 100 (`limit` past 100 is 422),
  with the `after` cursor; the organization list and the usage page share the order and cursor.
  The platform's totals sum at most 1,000 organizations (`TOTALS_CAP`), read 100 at a time; past
  that every total says `above_cap` instead of counting part of the platform.

## The figures

- `60m`: the last 60 complete minutes; `24h`: the last 24 complete hours. A bucket is complete
  25 s after it ends, as the evaluator's minutes are.
- An application's 60 minutes are its minute rollups (the Applications list's figures, the same
  values); its 24 hours are the recorder's new hour keys. An organization's are its own minute and
  hour keys. The platform's totals are its organizations' buckets summed.
- Each figure is the alert signals' own value and minimum (`signals.evaluate_counts`, `_value`'s
  code with no rule window): below the minimum "too few to judge", never 0; a bucket that lost
  counts makes the window `incomplete`; rollups that cannot be read are `unavailable`.
- **From when** (15.6, condition 3). The window says `since_ms` and `since_reason` when its counts
  begin inside it: `restarted` for this process's in-memory counts (the page says "since HH:MM UTC,
  when the gateway restarted"), `counting_began` when the shared store began counting that kind
  then. A 24-hour figure with either is never drawn as a whole 24 hours.

## The recorder

`AlertRecorder.record` takes the organization id `app.py` already has. Each request adds its fixed
fields to its application's minute (as before), its application's hour, and its organization's
minute and hour. Minutes are kept 120 minutes, hours 26 hours: in memory by `_forget` on the
newest minute, in Valkey by each key's expiry, set in the flusher's pipeline (off the request
path).

What this makes possible: the application hour keys give each application a 24-point hourly
series, and the minute rollups already give 60 per-minute points. The client view's one
application uses them as a table. **The Overview tab's chart is not added** here.

## Not built

Creating an organization (PR 3b); a chart on the Overview tab; tokens and estimated cost on the
client view (section 13 allows them; 15.1 did not list them).
