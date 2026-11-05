# 45. Alert evaluation: the lease, incidents, the heartbeat

PR A3 of `docs/settings-and-alerts-plan.md` (sections 8, 11.2 to 11.6, 12 and 15), as agreed when
it started. The evaluator turns the rules of note 44 and the signals of note 43 into incidents.
No channel exists yet (A4): **the incident list is the only channel**, and each notification an
incident would send is recorded on it. No outbound connection, no new secret.

## One evaluator at a time: the lease and its epoch

A lease row in the durable store (`alert_lease`, SQLite migration 16, Postgres schema 5):

- **Every instance attempts it every 10 s** with one conditional statement: taken when it is
  free or expired, renewed when the instance holds it; either way the row comes back with the
  store's time. The lease lasts 30 s.
- **Taking it over bumps its epoch.** Every write the evaluator makes is fenced: the statement
  that changes an incident, records a notification or writes the heartbeat carries `AND the
  lease is still held by me at my epoch and not expired`. An evaluator that paused past its
  lease (a stop-the-world pause, a partition) writes nothing when it wakes, its heartbeat
  included.
- **The holder runs a cycle every 30 s** (the injected clock); on SQLite the one process holds
  it, on Postgres any instance can. If the holder dies, another takes the lease within 30 s and
  carries on: a pending incident keeps its `pending_since`, so `for` continues.

## A cycle

1. The open incidents: one whose rule was deleted, disabled or changed (a new version) resolves
   with that reason (`rule_deleted`, `rule_disabled`, `rule_changed`).
2. The enabled rules (paged by id), and for each its signal: from the rollups over the window
   ending at the last complete minute (a minute is complete 25 s after it ends), or a durable
   value: `budget_used` (the period's billed tokens against the effective monthly budget),
   `circuit_open` (one series per enabled provider the application may use, S13; the minutes
   this evaluator has seen its breaker open or half-open), `key_expires_in` (one series per
   active key with an expiry). `reservation_expired` is not judged in A3: only each instance's
   own sweep counts it, so no instance knows the whole count (its default rule is off).
3. Each series' transition, decided by one pure function (`evaluator.decide`), each its own
   fenced compare-and-set.
4. Quiet hours over: one `quiet_hours_summary` per incident with held notifications.
5. The heartbeat, with the renewal, in one conditional statement.

Valkey down: the rollup minutes cannot be read and are incomplete (too few to judge); durable
signals are still judged. The durable store down: no lease, no cycle, no transition.

## States and incidents

As in the plan's section 8: `ok` (no incident), `pending`, `firing`, `acknowledged`,
`resolved`. An incident is one series of one rule from pending to resolved, stored with every
transition time; at most one is open per (rule, series key), by a partial unique index on SQLite
and Postgres alike. Every change compares the state and the revision it was read at, so a
change made twice (two evaluators, a retried cycle) is made once.

| From | To | When | Recorded |
|---|---|---|---|
| ok | pending | the condition holds (`for` > 0) | |
| ok | firing | the condition holds, `for` = 0 | `firing` |
| pending | firing | it held for `for` | `firing` |
| pending | resolved (`pending_cleared`) | it stopped holding, or became too few to judge | |
| firing, acknowledged | resolved (`cleared`) | false at two consecutive evaluations | `resolved` |
| firing, acknowledged | (marked `no_data_since`) | too few to judge | |
| firing, acknowledged | resolved (`no_data`) | 60 min with nothing judgeable | `resolved` |
| firing | acknowledged | a user acknowledges | |
| firing | (stays) | `repeat_every` passed since the last | `reminder` |

**Cooldown.** After a `firing` notification no other is recorded for that series for the
rule's cooldown. A series that fires again inside it reopens the resolved incident (`reopened +
1`) and records nothing; if it is still firing when the cooldown ends, one `firing_again`. A
reopened incident that resolves inside the cooldown records nothing either.

**Quiet hours.** An `info` or `warning` notification inside a rule's quiet hours (a fixed UTC
offset, `start` to `end` on its days; a span past midnight belongs to the day it starts on) is
recorded `held`; when they end, one `quiet_hours_summary` per incident, and the held ones become
`released`. `critical` is never held.

**Notifications are recorded, not sent.** Each is a row (`kind`, `state`, time, reason) written
in the same transaction as the change it belongs to, so a change rolled back records nothing.
A4 adds the outbox that delivers them, with the payload of the plan's section 10, the retries,
the caps and the suppressed summary.

## Endpoints

Under `/v1/admin/orgs/{org}`, each through `requires`, with resolvers that answer an
application outside the path's organization, or an incident outside the path's application
(or an id not in an incident id's form), as missing: 403, the same bytes as forbidden. No
step-up. Lists take `state`, `limit` (1 to 100) and `after`.

| Method | Path | Action | Success |
|---|---|---|---|
| `GET` | `/incidents` | `incidents.read_organization` | 200 |
| `GET` | `/applications/{app}/incidents` | `incidents.read` | 200 |
| `GET` | `/applications/{app}/incidents/{incident}` | `incidents.read` | 200 |
| `POST` | `/applications/{app}/incidents/{incident}/acknowledge` | `incidents.acknowledge` | 200 |

`incidents.read`: org_admin, app_admin of the application, viewer of the organization or of the
application. `incidents.read_organization`: org_admin, viewer of the organization.
`incidents.acknowledge`: org_admin, app_admin of the application; viewers read but never
acknowledge (decision 17). Acknowledging an incident that is not firing is 409
`incident_not_open`. Audited: `incident_acknowledged` (the incident, its application, who).

## The heartbeat

The lease row's `last_cycle_completed_at_ms` (the store's clock) and `last_cycle_epoch`, written
with the renewal at the end of each completed cycle, only while the lease is held at that epoch.
Every instance keeps the row its last attempt read; health, metrics and the snapshot judge it
from that memory, with no store call:

| State | When |
|---|---|
| `ok` | a cycle completed within the last 90 s |
| `stale` | the last completed cycle is older than 90 s, or none completed 120 s after this instance started |
| `unknown` | this instance's last successful read is 30 s old or more: never `ok` without a fresh read |
| `disabled` | no evaluator on this deployment (simulated live mode, and `create_app` without an incident store) |

- **`/healthz`** gains `alert_evaluator`, one of those four words, and nothing else. `/readyz`
  does not depend on it.
- **Metrics:** `gateway_alert_evaluator_state{state}`, `..._last_cycle_age_seconds` (absent while
  unknown), `gateway_alert_evaluator_holder`, `gateway_alert_evaluation_ms`,
  `gateway_alert_evaluation_failures_total`.
- **The snapshot** (`alert_evaluator`, docs/15): the same, `available: false` while unknown.
- **Events** `alert_evaluator_stale` (to `stale` or `unknown`) and `alert_evaluator_recovered`,
  each with a warning log line.

The console's banner is C2's. The heartbeat chooses nothing: the lease does.

## Store calls and time

The evaluator runs beside the server, off the request path; `/healthz`, the metrics and the
snapshot read its memory. MEASURED on this machine (Intel Core Ultra 7 155H, 22 logical cores,
15.4 GB, Windows 11 10.0.26200, Python 3.12.10; a SQLite file and the Postgres of
`docker-compose.postgres.yml`), before at `64765fb` (main after A2) and after on the A3 tree,
three runs of `GATEWAY_POSTGRES_URL=... uv run python -m benchmarks.request_store_calls` (each
three passes of 300 requests) and three of `uv run python -m benchmarks.key_checks`:

| Request | Store calls before | after | p50 ms before (9 passes) | after |
|---|---|---|---|---|
| gateway tier, SQLite | 5 | 5 | 3.446 to 4.147 | 3.440 to 3.867 |
| gateway tier, Postgres | 5 | 5 | 16.099 to 17.277 | 16.090 to 16.764 |
| org provider, SQLite | 12 | 12 | 4.053 to 4.901 | 4.049 to 4.560 |
| org provider, Postgres | 12 | 12 | 25.029 to 26.832 | 24.778 to 26.335 |

Postgres round trips per request unchanged (22 and 36). The key check: p50 0.024 to 0.026 ms
before, 0.024 to 0.029 ms after. Nothing of A3 adds a store call to a request; a cycle's own
time is `gateway_alert_evaluation_ms`, measured below.

## One cycle's time (added before A4)

MEASURED on the same machine, at `208b54c` (main after A3), three runs of
`GATEWAY_POSTGRES_URL=... uv run python -m benchmarks.alert_evaluation`. Each application has
the default rules (five enabled) and one key; one in ten fails, one in twenty is refused for
budget; rollups in memory; the server's own durable signals. The first cycle opens the
incidents; then three steady cycles each run.

| Applications | Store | First cycle ms (3 runs) | Steady cycle ms (9 cycles) | Store calls (first, steady) |
|---|---|---|---|---|
| 1 | SQLite | 1.7 to 2.0 | 0.3 to 0.8 | 21, 18 |
| 1 | Postgres | 30.9 to 33.8 | 15.4 to 20.1 | 21, 18 |
| 100 | SQLite | 31.8 to 38.7 | 21.4 to 29.6 | 1,621, 1,491 |
| 100 | Postgres | 1,496.8 to 1,539.5 | 1,312.5 to 1,340.4 | 1,621, 1,491 |
| 1,000 | SQLite | 334.1 to 366.6 | 256.1 to 328.1 | 16,179, 14,879 |
| 1,000 | Postgres | 14,713.5 to 16,262.0 | 13,165.6 to 15,379.7 | 16,179, 14,879 |

A cycle makes about 15 store calls per application, one at a time (the application, both
settings rows, the period's usage, the keys, the last resolved incident of each series without
an open one), so its time grows with the applications: on Postgres about 0.9 ms a call, 13 to
16 s for 1,000. That is half of the 30 s lease, which a cycle does not renew while it runs
(the renewal is the attempt every 10 s, between cycles, and `complete_cycle` at the end), and
the reads go through the request pool. At some 2,000 applications a cycle would outlive its
own lease and be fenced out every time: no evaluation, a stale heartbeat. See the A4 PR for
what this asks of the design before delivery is built on top.
