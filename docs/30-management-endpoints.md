# 30. The management endpoints

Identity step 7 (docs/identity-plan.md, section 7). Users, applications and application keys
can now be managed over HTTP, by a logged-in user, within that user's roles. Roles and
organizations are still managed only through the CLI.

Code: `src/gateway/management.py` (resolvers, refusals, applications and keys),
`src/gateway/auth.py` (users: temporary passwords, the own-account rule, the creation cap,
auditing refusals), wiring in `src/gateway/app.py`. Tests: `tests/test_management_api.py`,
`tests/test_manage_management.py`, `tests/test_secret_leaks.py`.

## The endpoints

All of them are under `/v1/admin/orgs/{org}`. Each one is declared and enforced by
`requires(action, resolver)` (note 28), so the session, the CSRF token on a change, roles read
again, `authorize` and step-up (note 29) are all checked in one place, before the handler
runs. A test compares this table with the app's routes and with the permission table, so a
route declared with a weaker action fails.

| Method | Path under `/v1/admin/orgs/{org}` | Action | Step-up | Success |
|---|---|---|---|---|
| `POST` | `/users` | `users.create` | yes | 201 |
| `GET` | `/users` | `users.list` | | 200 |
| `POST` | `/users/{username}/disable` | `users.disable` | yes | 200 |
| `POST` | `/users/{username}/enable` | `users.enable` | yes | 200 |
| `POST` | `/users/{username}/reset-password` | `users.reset_password` | yes | 200 |
| `POST` | `/users/{username}/unlock` | `users.unlock` | yes | 200 |
| `POST` | `/applications` | `applications.create` | | 201 |
| `GET` | `/applications` | `applications.list` | | 200 |
| `GET` | `/applications/{app}` | `applications.read` | | 200 |
| `POST` | `/applications/{app}/keys` | `application_keys.issue` | yes | 201 |
| `GET` | `/applications/{app}/keys` | `application_keys.list` | | 200 |
| `POST` | `/applications/{app}/keys/{key_id}/rotate` | `application_keys.rotate` | yes | 201 |
| `POST` | `/applications/{app}/keys/{key_id}/revoke` | `application_keys.revoke` | yes | 200 |

Bodies: `POST /users` takes `username` only; `POST /applications` takes `name` and `plan`;
issuing a key takes `label` and `expires_in_days`; rotating takes `grace_hours` and
`expires_in_days`. Lists take `limit` and `after`. Any other field is refused (422).

`/v1/admin` is local only until A8 (note 25): it is in `LOCAL_ONLY_PATHS`, the README's and
note 25's tables and the launcher's warning.

## Missing is forbidden

A **resolver** turns the path into the resource `authorize` checks. It answers nothing when the
organization, the user, the application or the key does not exist, **or is not inside the
organization the path names**: a user of `south` reached through `/orgs/north/users/sam`, an
application of `south` through `/orgs/north/applications/south-shop`, or a key of another
application through north's application, are all missing. Missing and not permitted get the
same 403 `forbidden`, byte for byte (tested on every endpoint, with and without an open
step-up window: authorization comes before step-up, so a window never tells them apart).

Behind `requires`, the services check the record itself again with `authorize`, and a refusal
there gets the same answer. That second check is defense in depth; the resolvers are what the
mutation checks break (see the PR), and the tests catch each one directly and over HTTP.

## Passwords never travel to the server here

`POST /users` and `POST /users/{username}/reset-password` accept no password. The server draws
a temporary one (18 random bytes, 24 characters of base64url), checks it against the policy
(drawn again if it were refused, for example for containing the username), hashes it through
the `HashRunner`, and returns it once, in that response, as `temporary_password`. The account
must change it at its first login. A reset also revokes every session of the user and closes
any step-up window, and clears a lockout. The temporary password is never logged, stored
(only its argon2 hash) or shown again; the leak scan treats it like a new API key: allowed in
that one response, found anywhere else, with a planted control.

The CLI keeps its prompt: the operator types the password, twice.

## Users

- **Usernames are global**, so `409 username_taken` tells an org_admin that a name exists in
  some organization. This is a username oracle, recorded in the threat model (plan, section
  12). It is narrowed, not closed: every attempt to create a user counts toward a cap per
  session (10 an hour, `GATEWAY_USER_CREATE_ATTEMPTS`, 1 to 1000), refused past it with
  `429 user_creation_limited`, and every 409 is audited.
- **Not your own account**: disabling or resetting the account you are logged in as is
  `409 own_account`. Unlocking it is not possible either in practice: step-up is refused
  while the account is locked, and a lockout closes every window.
- **The last org_admin** of an organization, and the last platform_admin, cannot be disabled
  (`409 last_admin`), in one transaction with the count (note 28).
- **An individual organization** takes one member (`409 organization_full`); a disabled
  organization takes none (`409 organization_disabled`).
- **`users.unlock` needs step-up** from this step: unlocking lifts a lockout, which is a
  defense. The permission table, note 28's copy of it, the generated matrix and note 29's list
  changed together.
- `GET /users` shows each user's id, name, whether it is disabled, locked or must change its
  password, and when it was made; never roles (roles are CLI-only in A2) or anything about
  its password.

## Applications and keys

- **Applications** are created from a plan template's name in the plans file; never from raw
  numbers (any other field is refused). The server makes the id, `app_` and 16 hex
  characters; the name is unique within the organization (`409 application_name_taken`) and
  may repeat across organizations. The CLI keeps `--id`.
- **A new key** is returned once, in the body of the issue or rotate response, never logged,
  stored (only its HMAC) or shown again. A list shows key ids, labels, state and times, never a
  key or its HMAC.
- **Limits on the HTTP path:** at most 10 active keys per application (`409 key_limit`; a
  rotation counts the key it replaces out, so rotating at the limit works); a label of 1 to 64
  of `A-Z a-z 0-9 space . _ -`, no space at either end; every key expires, 1 to 365 days,
  90 unless asked; a grace period of 0 to 168 hours, 24 unless asked. Rotating a revoked or
  expired key is `409 key_not_active`. The CLI, being the operator, keeps its behaviour: a key
  expires only if asked, and there is no limit on how many.
- platform_admin reaches none of these: the table gives it nothing inside an application.

## Lists

`limit` is 1 to 100 (50 unless given); `after` is the last id of the previous page, and the
answer's `next` is the cursor for the next page, or null on the last. Users are ordered by
username, applications and keys by id. A cursor is only compared with ids in the
organization's own rows, never looked up, so it cannot reveal anything about another
organization.

## Answers

Every answer under `/v1/admin` carries `Cache-Control: no-store`, errors, validation failures
and the local-only guard's refusals included (a small ASGI middleware, installed only with
accounts, outermost). Status codes:

| Status | Code | When |
|---|---|---|
| 401 | `session_required` | no live session |
| 403 | `csrf_failed` | a change without the session's CSRF token |
| 403 | `password_change_required` | the session's password must be changed first |
| 403 | `forbidden` | not permitted, or not found: one body |
| 403 | `step_up_required` | a flagged action without an open window |
| 409 | `username_taken`, `organization_full`, `organization_disabled`, `application_name_taken`, `last_admin`, `own_account`, `key_limit`, `key_not_active` | a conflict, audited |
| 422 | `invalid_request` | a body or query the endpoint does not take |
| 429 | `user_creation_limited` | past the session's cap on creating users, audited |

## Audit

New entries: `application_created` (the application, its organization and plan),
`key_issued`, `key_rotated` (the old key's id as the target, the new key's id and the grace
period in the details) and `key_revoked`, from the endpoints and the CLI alike; the operator is
recorded as no user. Every 409 and 429 above is `management_refused`, naming the organization,
the action and the reason, never anything that was typed (a refused username is not written).
A key appears in an entry only by its id, the visible part of the key; the entry types pass
through the allowlist of note 27, which checks each id's form.

## Not built

- Endpoints for roles and organizations (CLI only in A2).
- Bearer access tokens on these endpoints (step 8).
- Reading the audit log over HTTP (declared `later` in the table).
- Any limit per source address (A8).

## Measured

MEASURED on this machine (Intel Core Ultra 7 155H, 22 logical cores, 15.4 GB RAM, Windows 11
10.0.26200, Python 3.12.10): `uv run python -m benchmarks.management_requests --rounds 500
--runs 3`, on the code of identity step 7's code commit. The real server (`gateway.serve`, on a
throwaway SQLite database), real time, HTTP from a client on the same machine, the requests
interleaved round by round. The p50 of each run, in ms, as the range over the three runs (500
rounds each, after 20 warm-up rounds):

| Request | p50, ms |
|---|---|
| `GET /healthz` (no session) | 1.60 to 1.62 |
| `GET /v1/auth/me` (a session check, no roles) | 1.67 to 1.70 |
| `GET .../users` (5 users) | 1.91 to 1.94 |
| `GET .../applications/{app}` | 1.79 to 1.87 |
| `GET .../applications/{app}/keys` (a page of up to 50: revoked keys pile up during the run) | 2.18 to 2.25 |
| issue a key, then revoke it (two writes with step-up, timed together) | 6.49 to 6.55 |

So a management read costs 0.1 to 0.3 ms more at p50 than a session check: `requires`
(roles read again, the resolver, `authorize`) and the handler's query. Logins and step-up hash
with argon2 and are not timed here (notes 24 and 27).

`/v1/generate`, the performance gate and the protected outputs are unaffected: they run through
`create_app` without accounts, where none of this is installed, and both checks pass unchanged.
