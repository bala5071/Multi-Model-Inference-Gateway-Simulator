# 42. Hosted settings, step-up on loosening, presets, and the hosted policy

PR S3 of `docs/settings-and-alerts-plan.md`, after scale-out PR 2b (note 41). Settings S13 to
S16, which decide where a prompt may go and how many times; step-up whenever a change loosens
one of them; the presets; the mode shown in the settings views (S12); and one statement read
before each hosted attempt.

## The settings

| # | Setting | Values | Default | Stricter | Who caps it |
|---|---|---|---|---|---|
| S13 | `allowed_providers` | `"all"`, or up to 20 provider ids of the application's organization | `"all"` | fewer providers | - (the organization enables and disables its providers) |
| S14 | `max_hosted_attempts` | 1 to 5 | the operator's cap (`GATEWAY_CHAT_MAX_HOSTED_ATTEMPTS`, 3) | lower | the operator's cap, then the organization |
| S15 | `retry_invalid_tool_json` | true or false | true | false | the organization can force false |
| S16 | `hosted_first_token_ms` | 1,000 to 600,000 | each provider's own budget | lower | the organization; each provider's budget at the attempt |

`allowed_providers` names only providers of the application's organization; one of another
organization and one that does not exist are refused alike (422). A provider not allowed is not
a candidate at all.

## Step-up when a change loosens where prompts go

A change needs step-up, consume-on-use, through `settings.loosen_egress` (an application's) or
`org_settings.loosen_egress` (an organization's caps), exactly when it loosens S13, S14, S15 or
S16 for some application it affects. That is judged by one function, `loosens_egress`, on the
effective values before and after: for an application's change, that application; for an
organization's caps, every application of the organization, each with its own stored values,
so raising a cap that an application's own value still holds down loosens nothing. A preset,
a revert and a plain change are judged the same way, from the change they make, never from
their name. Tightening, and every change to S1 to S11, needs none.

The handler runs the change as a dry run first (the validate path) and, if it loosens, claims
the step-up window for the escalated action, which the route declares beside its own
(`escalates`). The write then judges it again inside its `write_transaction()`: if the answer
differs (a cap moved between the two, on this instance or another), it is refused, 409
`settings_version_conflict`, nothing is written, and the window is given back. So a loosening
change can never be stored without a consumed step-up. The audit row says whether it loosened
(`loosened_egress`). The operator's CLI needs no step-up.

## Presets

`GET .../settings/presets` lists them with their exact values; `POST
.../applications/{app}/settings/presets/{name}` applies one as a single version (`via:
preset:<name>`, audited as `settings_preset_applied` with its name). A preset is checked like
any write: a value looser than a cap in force is refused, never clamped (`cheap` on a plan
without `economy`). A name that is not a preset is missing: 403, like any missing resource.

| Preset | Values |
|---|---|
| `cheap` | `allowed_tiers: [economy]`, `default_quality: cheapest`, `allow_downgrade_default: true`, `max_hosted_attempts: 1`, `retry_invalid_tool_json: false` |
| `fast` | `default_quality: cheapest`, `allow_downgrade_default: true`, `chat_first_token_ms: 5000`, `hosted_first_token_ms: 3000`, `max_hosted_attempts: 2`, `generate_timeout_ms: 30000`, `chat_total_timeout_ms: 30000` |
| `private` | `allowed_providers: []`, `max_hosted_attempts: 1`, `retry_invalid_tool_json: false` |

The numbers are chosen, not measured, and no saving is claimed from `cheap` (prices are
placeholders).

## Endpoints

All of note 40's, with the two preset endpoints. "Escalates to" is the action a change takes
when it loosens egress; step-up is then needed, consume-on-use.

| Method | Path | Action | Escalates to | Success |
|---|---|---|---|---|
| `GET` | `/settings` | `org_settings.read` | | 200 |
| `PUT` | `/settings` | `org_settings.write` | `org_settings.loosen_egress` | 200 |
| `POST` | `/settings/validate` | `org_settings.write` | | 200 |
| `POST` | `/settings/revert` | `org_settings.write` | `org_settings.loosen_egress` | 200 |
| `GET` | `/settings/versions` | `org_settings.read` | | 200 |
| `GET` | `/settings/versions/{n}` | `org_settings.read` | | 200 |
| `GET` | `/settings/presets` | `org_settings.read` | | 200 |
| `GET` | `/applications/{app}/settings` | `settings.read` | | 200 |
| `PUT` | `/applications/{app}/settings` | `settings.write` | `settings.loosen_egress` | 200 |
| `POST` | `/applications/{app}/settings/validate` | `settings.write` | | 200 |
| `POST` | `/applications/{app}/settings/revert` | `settings.write` | `settings.loosen_egress` | 200 |
| `GET` | `/applications/{app}/settings/versions` | `settings.read` | | 200 |
| `GET` | `/applications/{app}/settings/versions/{n}` | `settings.read` | | 200 |
| `POST` | `/applications/{app}/settings/presets/{name}` | `settings.write` | `settings.loosen_egress` | 200 |

The dry run says what a change would loosen (`loosens`) and whether it would need step-up. The
settings views show the mode (S12) from its own table; it is still set by its own endpoints
(note 33), which keep their rule.

## A request in flight, and the hosted policy

A chat request reads its mode and S13 to S16 once (the mode in the handler, the settings with
its key). Just before each hosted attempt it reads both modes and both settings rows again, in
one statement, `hosted_policy` (it replaces the two mode reads of note 38), and holds itself to
the stricter of what it read and what is now in force, field by field:

- **stricter applies before the next hosted attempt:** a stricter mode, a provider no longer
  allowed, a lower attempt cap (counted against the attempts already made: `attempt_cap`), the
  retry turned off, a lower first-token budget (for the attempt about to be made);
- **looser applies from the next request:** it never opens an attempt for a request already
  running;
- **an answer already streaming is not cut.**

A skipped attempt gives back what it was admitted for (a half-open probe's slot included). A
row that cannot be judged at the re-read refuses the attempt, as a store that cannot answer
does.

## Store calls and time

MEASURED on this machine (Intel Core Ultra 7 155H, 22 logical cores, 15.4 GB, Windows 11
10.0.26200, Python 3.12.10; a SQLite file on the local disk and the Postgres of
`docker-compose.postgres.yml`), before at `2f53618` (main after PR 2b) and after on the S3
working tree of branch `settings-alerts/s3-a1-a2`, three runs each:

- `GATEWAY_POSTGRES_URL=... uv run python -m benchmarks.request_store_calls` (note 38's
  benchmark, 300 requests a run):

  | Request | Store calls before | after | p50 ms before (3 runs) | after |
  |---|---|---|---|---|
  | gateway tier, SQLite | 5 | 5 | 3.559 to 3.902 | 3.510 to 4.093 |
  | gateway tier, Postgres | 5 | 5 | 16.530 to 17.240 | 16.928 to 17.855 |
  | org provider, SQLite | 13 | 12 | 3.967 to 4.737 | 4.271 to 4.812 |
  | org provider, Postgres | 13 | 12 | 27.599 to 28.285 | 26.839 to 28.669 |

  Postgres round trips per request with an org provider: 38 before, 36 after (one statement
  and one pool reset fewer). The gateway tier's request makes no hosted attempt and is unchanged.
- `uv run python -m benchmarks.key_checks`, the key check (its lookup carries the settings rows
  since S1, unchanged by S3): p50 0.023 to 0.039 ms before, 0.022 to 0.023 ms after.
- `GATEWAY_POSTGRES_URL=... uv run python -m benchmarks.durable_calls`, `lookup_key` alone: p50
  on SQLite 0.018 to 0.021 ms before, 0.018 to 0.026 after; on Postgres 1.235 to 1.415 before,
  1.161 to 1.277 after.

Within the run-to-run spread: one call fewer per hosted attempt does not show in the request's
time on this machine. The counts are pinned by the tests that count store calls.
