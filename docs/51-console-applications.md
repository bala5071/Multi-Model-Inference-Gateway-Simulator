# 51. The console's Applications list and Add application

PR 1 of `docs/console-ia-v2-plan.md` (approved on PR #48; its section 14 lists the decisions
made from the code before this step). An organization's session now lands on its Applications:
one row per application with its figures over the last 60 minutes, open incidents and status,
"Add application" for whoever may create one, and each application's API keys. Nothing changes
who may do what: the only change to the permission table file is the `wired` column of
`usage.read_organization` (`later` to `console`); every cell of every role is as it was.

## The endpoints

Each goes through `requires` with one action; a missing organization is refused with the bytes
of a forbidden one; every answer is `Cache-Control: no-store` and local only (`/v1/admin`).

| Method | Path | Action | Step-up | Success |
|---|---|---|---|---|
| `GET` | `/plans` | `applications.create` | | 200 |
| `GET` | `/metrics` | `usage.read_organization` | | 200 |
| `GET` | `/incidents/counts` | `incidents.read_organization` | | 200 |

Paths are under `/v1/admin/orgs/{org}`. Creating an application and issuing, rotating and
revoking its keys are note 30's endpoints, unchanged; the application rows they and the list
answer now carry `mode`.

- **`GET /plans`**: the plan templates an application can be created from, in the plans file's
  order: name, allowed models, monthly token budget, `max_tokens`, `requests_per_minute`. It
  declares `applications.create`, so whoever may create an application may see what from, and
  nobody else; it is the Add form's first read.
- **`GET /metrics?window=60m&after=&limit=`**: one page of the organization's applications, the
  list's own paging and order (so a list page and a metrics page with the same `after` and
  `limit` name the same applications), each with three figures, and the window:

  ```json
  {"window": {"minutes": 60, "first_minute_ms": 0, "end_ms": 3600000, "since_ms": null},
   "applications": [{"application_id": "north-shop",
     "requests": {"value": 42.0, "why_not": null},
     "error_rate": {"value": 2.38, "why_not": null},
     "latency_total_p50": {"value": 9.51, "why_not": null}}],
   "next": null}
  ```

  `window` takes only `60m` until PR 3 adds the recorder's hour keys. No organization totals:
  those need the organization keys of PR 3, and summing every application here would not be
  bounded by a page.
- **`GET /incidents/counts`**: `{"applications": [{"application_id", "pending", "firing",
  "acknowledged"}]}`, the applications with an open incident only. One `GROUP BY` on
  `incidents_by_organization`, the same statement on SQLite and Postgres (`OPEN_COUNTS_SQL`).
  The built-in `channel_delivery_failing` incidents, kept under the empty application id, are
  not counted: they belong to no application, and the Incidents screen lists them.

## The figures

They are the alert signals' own: `usagefigures.figures` calls `signals.evaluate` for
`requests`, `error_rate` (5xx over requests) and `latency_total_p50`, over the minutes the
evaluator takes (the last complete ones; a minute is complete 25 s after it ends). So a figure
on the list and the value a rule judges are the same number, and each has the same minimum:

| Why there is no value | When |
|---|---|
| `too_few` | under 20 requests for a rate, under 10 for the p50 (`signals.py`) |
| `incomplete` | a minute of the window lost counts (an instance dropped deltas, Valkey restarted) |
| `unavailable` | the rollups could not be read (Valkey down): every figure, never a guess |
| `above_range` | the p50 is in the histogram's overflow bucket: JSON has no infinity |

The p50 is the upper edge of its bucket (edges about 19% apart), labelled so on the page. With
this process's own rollups (the default server), a window that starts before the process did
carries `since_ms`, and the page says "since HH:MM UTC (restart)".

## Mode

Each application row carries `mode`, the mode in force (`effective_mode` of the organization's
and the application's, the one a request is held to). An application's settings read already
answered `mode.effective` to everyone who may read its settings, so the list shows its readers
nothing new (a correction to the plan's F12). It is read per application of the page: at most
100 reads, timed below.

## Add application, and keys

- **Add application** (`#/apps/new`) is shown when the capabilities answer allows
  `applications.create` in the organization: that action joined `NAVIGATION_ACTIONS`, pinned to
  `GET /plans`. A direct address to the form from a session that may not create draws the
  landing with "That page is not available to this account." and asks nothing else.
- Two steps on one screen: **create** (`POST .../applications`, no step-up, as the table says),
  then **the first key** (`POST .../keys`, step-up: the page asks for the password). The key is
  shown once, in a read-only field, with "Shown once. It cannot be shown again."; it is held in
  that component's state only, gone when the screen is left, never in storage, an address or the
  console log. If the key step is cancelled, the application exists and its API keys screen
  issues one.
- **API keys** (`#/app/<id>/keys`, and `#/keys` with the application chooser): the list, and
  Issue, Rotate (a new key shown once, the old one kept for the grace hours) and Revoke.
  Those three follow the list's own answer, `application_keys.list`: a write probe of a step-up
  action would be refused `step_up_required`, an audit row each time, and Revoke has no body
  to refuse. `tests/test_console_probes.py` pins the three rows equal to the list's.
- The audit trail is note 30's: `application_created` and `key_issued` (by key id, never the
  key).

## The page

- **Landing:** a session the capabilities answer allows `applications.list` lands on
  Applications (`#/home` draws it); a platform session and an application-only session keep
  Home until PR 3 (Clients) and PR 2 (the application page).
- **Applications** (`#/apps`): name (to its settings until PR 2), mode, requests, error rate,
  p50, open incidents, status, and "API keys" where the answer names that application with
  `application_keys.list`; a page of 50 with "Next page". The status is a pure function
  (`applicationStatus`) of the incident counts, the evaluator's heartbeat and the request count,
  always written beside its dot: firing, attention (pending or acknowledged), ok, no requests,
  unknown (the evaluator not `ok`, or a figure that cannot be judged as complete).
- Every figure says "last 60 minutes".

## Timing

MEASURED with `uv run python -m benchmarks.console_reads` (3 runs of 300 reads each, after 20
warm-up reads) at commit 8c13589, on an Intel Core Ultra 7 155H (22 logical cores), 15.4 GB RAM,
Windows 11, Python 3.12.10; SQLite on the local disk, rollups in memory, every read through the
whole app in process (session, `requires`, handler). The range is over the three runs.

| Read | p50 (ms) | p99 (ms) |
|---|---|---|
| `GET /metrics`, page of 50 | 16.0 to 16.8 | 87.9 to 99.8 |
| `GET /metrics`, page of 100 | 32.7 to 34.8 | 126.7 to 140.4 |
| `GET /incidents/counts` (10 applications with a firing incident of 100) | 0.98 to 1.10 | 2.0 to 3.1 |
| `GET /applications` with modes, page of 50 | 2.05 to 2.11 | 4.3 to 5.1 |
| `GET /applications` with modes, page of 100 | 2.89 to 2.98 | 6.0 to 6.7 |
| the mode reads alone, 50 applications | 0.31 to 0.32 | 0.6 to 0.8 |
| the mode reads alone, 100 applications | 0.61 to 0.63 | 1.5 to 1.8 |

The figures cost about a third of a millisecond an application: each application's hour is summed
once (`signals.evaluate_many`, `evaluate`'s own code, pinned equal to it by a test) and the event
loop is given back between applications, as the evaluator does, so a page never holds up the
requests being served. The request path is not touched: the recorder, the generate and chat
handlers are unchanged, and the perf gate is at +0.0% on every model. Valkey and Postgres were not
measured: neither runs on this machine.

## Not built here

The application page and its Overview (PR 2), Clients and 24-hour windows (PR 3), usage and
costs (PR 4), act-as (PR 5). No organization totals. No figure on Valkey or Postgres was timed:
neither runs on this machine.
