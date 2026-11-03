# 44. Alert rules

PR A2 of `docs/settings-and-alerts-plan.md` (sections 7, 12, 13 and 15), as agreed when it
started: the rules, their table, their endpoints and permission rows, the default rules (with
the data step for the applications that already exist), and the dry run with the last hour's
preview. Nothing evaluates a rule yet (A3) and nothing is sent (A4). No outbound connection, no
new secret.

## What A4 adds, and what a rule's channels are until then

Channels do not exist before A4, so these move there with their tests and mutations (M15, M29):
the check that a rule's channel ids belong to the organization; `GET
.../applications/{app}/alert-channels`; the built-in rule `channel_delivery_failing`; and the
`rule_weakened` notice. Until A4 a rule's `channels` is `[]` (the incident list only) or
`["organization defaults"]`, stored as a plain list with no link to a table; anything else is
refused (422, naming `channels`).

## A rule

`gateway/alertrules.py`, `validate_rule`, field by field; the first field wrong is named (422
`invalid_request`, with the field):

| Field | Values |
|---|---|
| `name` | 1 to 64 of `A-Z a-z 0-9 space . _ -`, no space at either end |
| `signal`, `parameter` | a signal of note 43; a parameter only where the signal takes one, and one of its values |
| `comparison` | `>`, `>=`, `<`, `<=` |
| `threshold` | in the signal's unit: 0 to 100 for %; a latency is snapped to the nearest histogram edge, and the snapped value is stored and returned |
| `window` | one of the signal's windows; none for a signal judged now or over the period |
| `for_minutes` | 0 to 60 |
| `severity` | `info`, `warning`, `critical` |
| `channels` | `[]` or `["organization defaults"]` until A4 (default the latter) |
| `cooldown_minutes` | 5 to 1440, default 30 |
| `repeat_every_minutes` | none (default) or 60 to 1440 |
| `quiet_hours` | none, or `utc_offset` (`-12:00` to `+14:00`), `start`, `end` (`HH:MM`), `days` |
| `enabled` | true (default) or false |

At most 50 rules an application (409 `rule_limit`). Every change names the version it was made
from (409 `rule_version_conflict` otherwise, nothing written).

## Default rules

Real rows, created with each application in its creation's transaction (over HTTP and from the
CLI), and for every application that already exists by migration 15's data step (Postgres
schema 4: the same in SQL). Their ids come from the application and the default's key
(`default_rule_id`), so the data step, a new application and `restore-defaults` agree. All with
`["organization defaults"]`; every number is a chosen default, not a measurement.

| Key | Rule | On |
|---|---|---|
| `budget_used` | `budget_used >= 80`, warning | yes |
| `budget_exhausted` | `rejections(budget_exhausted) > 0` over 5 min, critical | yes |
| `error_rate` | `error_rate > 5` over 5 min, for 5 min, critical | yes |
| `circuit_open` | `circuit_open >= 2` (minutes), warning | yes |
| `key_expires` | `key_expires_in <= 14` (days), warning | yes |
| `hosted_share` | `hosted_share > 0` over 60 min, info | no |
| `rate_limited` | `rejections(rate_limited) > 0` over 5 min, warning | no |
| `reservation_expired` | `reservation_expired > 0` over 60 min, warning | no |

Not rows, against the plan's table: `latency_total_p99 > T`, which has no threshold anyone can
choose for the customer (so it is a rule the customer writes, not a row with an empty
threshold), and `channel_delivery_failing` (A4). `circuit_open` is created for every
application, providers or not: with none, it has nothing to judge. Applications created in
memory without the management service (the demo applications of simulated live mode and the
tests' seeded ones) get no rows until `restore-defaults`. A rule's view says whether it is a
default changed from what ships (`changed_from_default`), so a weakened default is visible;
`restore-defaults` puts every default back as it ships, a deleted one recreated, an edited one
reset (a new version), as one audited change.

## Endpoints

Under `/v1/admin/orgs/{org}`, each through `requires`, with a resolver that answers an
application outside the path's organization, or a rule outside the path's application (or an id
not in a rule id's form), as missing: 403, the same bytes as forbidden. No step-up (the plan's
section 12: weakening a rule is audited, and the notice is A4's). Writes take
`expected_version`.

| Method | Path | Action | Success |
|---|---|---|---|
| `GET` | `/applications/{app}/alert-rules` | `alert_rules.read` | 200 |
| `POST` | `/applications/{app}/alert-rules` | `alert_rules.write` | 201 |
| `POST` | `/applications/{app}/alert-rules/validate` | `alert_rules.read` | 200 |
| `POST` | `/applications/{app}/alert-rules/restore-defaults` | `alert_rules.write` | 200 |
| `GET` | `/applications/{app}/alert-rules/{rule}` | `alert_rules.read` | 200 |
| `PUT` | `/applications/{app}/alert-rules/{rule}` | `alert_rules.write` | 200 |
| `POST` | `/applications/{app}/alert-rules/{rule}/enable` | `alert_rules.write` | 200 |
| `POST` | `/applications/{app}/alert-rules/{rule}/disable` | `alert_rules.write` | 200 |
| `DELETE` | `/applications/{app}/alert-rules/{rule}` | `alert_rules.write` | 200 |

`alert_rules.read`: org_admin, app_admin of the application, viewer of the organization or of
the application. `alert_rules.write`: org_admin, app_admin of the application.

**The dry run** (`validate`, under `alert_rules.read`: it writes nothing) checks a body as a
create would and returns the rule as it would be stored (the threshold snapped), and what it
would have been over the last hour of complete minutes (a minute is complete 25 s after it
ends), from the rollups: for each minute, the signal's value over the window ending there, or
why there is none (`too_few`, `incomplete`), and whether the condition held. A signal not
computed from the rollups (`budget_used`, `circuit_open`, `key_expires_in`,
`reservation_expired`) has no preview.

## Audit

`alert_rule_created`, `alert_rule_changed`, `alert_rule_enabled`, `alert_rule_disabled`,
`alert_rule_deleted` (target: the rule id; details: the application and the names of the fields
changed, `rule_fields`, never their values: the row has them), and `alert_rules_restored`
(target: the application). A refusal (409) is `management_refused` with its reason.

## Store calls and time

Nothing of A2 is on the request path: no store call is added (the counts are pinned by the tests
that count them). MEASURED on this machine (Intel Core Ultra 7 155H, 22 logical cores, 15.4 GB,
Windows 11 10.0.26200, Python 3.12.10; a SQLite file and the Postgres of
`docker-compose.postgres.yml`), `GATEWAY_POSTGRES_URL=... uv run python -m
benchmarks.request_store_calls`, p50 ms over each run's three passes of 300 requests:

- Store calls per request unchanged: 5 on the gateway tier, 12 with an organization provider;
  Postgres round trips 22 and 36.
- A first run after A2 was slower on SQLite than note 43's runs after A1 (gateway tier 5.172 to
  5.468 against 3.567 to 4.025), past the plan's stop rule, so A1's commit (`cb8446c`) and A2's
  tree were then run interleaved, A1, A2, A1, A2, on the same state of the machine:

  | Request | A1 (`cb8446c`), 2 runs | A2, 2 runs |
  |---|---|---|
  | gateway tier, SQLite | 5.361 to 6.505 | 4.322 to 5.731 |
  | org provider, SQLite | 6.262 to 7.056 | 5.673 to 6.535 |
  | gateway tier, Postgres | 18.213 to 23.148 | 16.789 to 19.692 |
  | org provider, Postgres | 27.149 to 31.029 | 26.665 to 29.986 |

  The machine was slower than during note 43's runs, for both; A2 is not slower than A1.
- `uv run python -m benchmarks.key_checks`, the key check, three runs: p50 0.024 to 0.054 ms
  (0.023 to 0.029 after A1, an earlier state of the machine).
