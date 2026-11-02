# 40. The settings endpoints

PR S2 of `docs/settings-and-alerts-plan.md`. The settings of note 39 over HTTP, under
`/v1/admin/orgs/{org}`: an organization's caps and an application's settings, their versions,
one-step revert and a dry run, each through `requires` like every management endpoint (note 30):
a session, CSRF on a change, the actor's roles read afresh, a resolver, `authorize`. Local only
until A8, `Cache-Control: no-store` on every answer. The presets moved to PR S3 with the hosted
settings most of their values are (the plan's section 4).

## Endpoints

| Method | Path | Action | Step-up | Success |
|---|---|---|---|---|
| `GET` | `/settings` | `org_settings.read` | | 200 |
| `PUT` | `/settings` | `org_settings.write` | | 200 |
| `POST` | `/settings/validate` | `org_settings.write` | | 200 |
| `POST` | `/settings/revert` | `org_settings.write` | | 200 |
| `GET` | `/settings/versions` | `org_settings.read` | | 200 |
| `GET` | `/settings/versions/{n}` | `org_settings.read` | | 200 |
| `GET` | `/applications/{app}/settings` | `settings.read` | | 200 |
| `PUT` | `/applications/{app}/settings` | `settings.write` | | 200 |
| `POST` | `/applications/{app}/settings/validate` | `settings.write` | | 200 |
| `POST` | `/applications/{app}/settings/revert` | `settings.write` | | 200 |
| `GET` | `/applications/{app}/settings/versions` | `settings.read` | | 200 |
| `GET` | `/applications/{app}/settings/versions/{n}` | `settings.read` | | 200 |

A change is `{"expected_version": n, "set": {...}, "unset": [...]}`; a revert
`{"expected_version": n}`. Lists take `limit` (1 to 100, 50 by default) and `after` (the last
version shown; the next page is older), and say `next_after` while a page was full.

Who may (the permission table, note 28, and the generated `docs/permission-matrix.md`):

| Action | Allowed |
|---|---|
| `org_settings.read` | org_admin, viewer of the organization |
| `org_settings.write` | org_admin |
| `settings.read` | org_admin, app_admin of the application, viewer of the organization or of the application |
| `settings.write` | org_admin, app_admin of the application |

platform_admin gets none of them, as for everything inside an organization's applications. No
setting of S1 to S11 changes where prompts go, so no write asks for step-up. The
`org_settings.loosen_egress` and `settings.loosen_egress` rows (consume-on-use) are declared for
PR S3, which adds the hosted settings and the check that picks them; no route declares them yet.

## Missing is forbidden

The resolvers answer, as missing: another organization, an application of another organization
or one that does not exist (`resolve_application`), and a settings version that is not this
scope's or does not exist, or is not a positive number of at most nine digits
(`resolve_organization_settings_version`, `resolve_application_settings_version`). All get the
same 403 `forbidden`, byte for byte, as a resource the actor may not touch, before step-up or
anything else is looked at, and nothing is written or audited.

An organization that does not exist is covered at the resolver level, because no route can
observe that rule: `authorize` refuses every organization but the actor's own, whatever the
resolver says (`test_resolve_organization_answers_an_organization_that_does_not_exist_as_missing`).
The dry run, too, answers only after `authorize`: on another organization's path every body,
valid, invalid or malformed, gets the same 403 (`test_validate_answers_only_after_authorize`).

## What the answers say

- `GET .../settings` of an organization: its version, the settings it set, and the platform's
  caps over what it may set. Of an application: its version, what it stored, and for every
  setting the value in force, its source (`default`, `platform`, `plan`, `organization`,
  `application`), what capped it, and every cap above it by level; the aliases not in force.
- `PUT` and `revert`: the new version, the fields changed, the settings stored. Refusals, each a
  `management_refused` audit row with the action and the reason, as in note 30: 409
  `setting_looser_than_cap` (naming the setting and the level), 409 `settings_version_conflict`,
  409 `no_previous_version`; 422 `invalid_request` naming the setting.
- `validate`: the same body as `PUT`, the same code up to the point of writing (the service's
  `_prepare` and the cap check): `valid`, the refusal's code and message, the refusals by setting
  and level, the fields that would change, the settings that would be stored, for an application
  the values that would be in force, and `step_up_required` (always false in S2). It takes no
  write lock, writes nothing, and is not audited: it changes nothing.
- Versions: newest first, each with the fields it changed, who (`changed_by_user_id`, null for
  the CLI), how (`http`, `cli`, `revert:<n>`) and when; one version also with its settings and
  the previous ones.

Every change is audited: `settings_changed` and `settings_reverted`, by version and field names,
never values, with the user as the actor.

A row that cannot be judged (note 39) answers 503 here too, reads included: the console cannot
show settings it cannot read, and the operator's CLI writes over it (`settings-set --replace`).

## Store calls and time

None of this is on the request path: `lookup_key`, the handlers of `/v1/generate` and chat,
and the store calls they make are those of note 39. The tests that pin the store calls per
request still pass unchanged (`tests/test_chat_routing.py` on SQLite: 4 on the gateway's tier, 12
with an organization provider; the `postgres` tests of `tests/test_budgets_postgres.py` in CI:
5 and 13).

MEASURED on this machine (Intel Core Ultra 7 155H, 22 logical cores, 15.4 GB, Windows 11
10.0.26200, Python 3.12.10; SQLite in the temp folder): the key check through `authenticate`,
`python -m benchmarks.key_checks`, three runs each of the base (`6aa5d5b`, PR S1) and this
branch (`c79d0df`) in turn, p50 0.024 to 0.041 ms before and 0.023 to 0.041 ms after. The
spread is the machine's; the two ranges are the same. Not measured here: the Postgres rows of
`benchmarks.request_store_calls`, because Docker Desktop was not running when this was measured.
