# 28 — Roles, the permission table, `authorize`, and declaring every route

Step 5 of the identity plan (`docs/identity-plan.md`, section 7; the plan's order is now 5
roles, 6 step-up, 7 the management endpoints). Users can hold roles, and every route of the
gateway declares what it needs. No new endpoint: the management endpoints that use `requires`
come in step 7, after step-up.

Code: `src/gateway/permissions.py` (roles, scope relations, the table, `authorize`, the
generated matrix), `src/gateway/roles.py` (the store), migration 8, the role methods of
`AccountService` in `src/gateway/auth.py`, the declarations in `src/gateway/app.py`, and the
CLI in `scripts/manage.py`. Tests: `tests/test_permissions.py`, `tests/test_roles_migration.py`,
`tests/test_manage_roles.py`. The matrix: [permission-matrix.md](permission-matrix.md).

## Roles

| Role | Held at | Covers |
|---|---|---|
| `platform_admin` | the platform | organizations and aggregate data; users in any organization; appointing org_admins (CLI) |
| `org_admin` | an organization | that organization, and every application in it |
| `app_admin` | an application | that application |
| `viewer` | an organization or an application | usage and the application list in its scope |

**Application-defined roles** (`clinic_staff`) are names an application defines (`app_roles`);
they can be held only at application scope, only once defined, and `authorize` ignores them:
they grant nothing in the gateway.

**Membership alone grants nothing.** A user holds only what is assigned. The one exception is
structural: the sole member of an individual organization is its org_admin, given when the
user is created (and by migration 8 for existing ones).

## Scope relations, and `authorize`

For each of the actor's assignments, the **relation** to the resource is:

- `platform`: a platform-scope assignment, whatever the resource;
- `own_organization`: an organization-scope assignment, on that organization;
- `application_in_own_organization`: an organization-scope assignment, on an application in it;
- `own_application`: an application-scope assignment, on that application;
- `other`: anything else, and every resource of another organization.

`authorize(actor, action, resource)` allows only when one of the actor's built-in roles, in its
relation to the resource, is listed for the action. It denies an unknown action, a resource of
the wrong level, a disabled user or organization, and everything not listed. A non-platform
assignment relates only to resources of the actor's own organization, so **every request that
crosses organizations is denied** whatever the role. platform_admin is the exception, and only
for the actions listed for it, none inside an application.

The actor's assignments are **read on every request**, with no cache: a removed role is gone at
the session's next request (tested).

## The table

`src/gateway/permissions.py` holds it; this copy is checked against it by
`test_the_documented_table_is_the_table`, every cell. Allowed pairs are `role@relation`.
**Step-up** is declared now and enforced from step 6. **Wired**: `now` (enforced today, through
the CLI's `--as`), `step 7` (the management endpoints), `A5` (provider keys and modes), `A8`
(monitoring, control and the Playground: local only until then), `later` (declared, no endpoint
planned in A2).

| Action | Level | Allowed | Step-up | Wired |
|---|---|---|---|---|
| `organizations.create` | platform | `platform_admin@platform` | yes | now |
| `organizations.list` | platform | `platform_admin@platform` | | now |
| `usage.aggregate` | platform | `platform_admin@platform` | | later |
| `organizations.disable` | organization | `platform_admin@platform` | yes | now |
| `organizations.enable` | organization | `platform_admin@platform` | yes | now |
| `users.create` | organization | `org_admin@own_organization`, `platform_admin@platform` | yes | now |
| `users.disable` | organization | `org_admin@own_organization`, `platform_admin@platform` | yes | now |
| `users.enable` | organization | `org_admin@own_organization`, `platform_admin@platform` | yes | now |
| `users.reset_password` | organization | `org_admin@own_organization`, `platform_admin@platform` | yes | now |
| `users.unlock` | organization | `org_admin@own_organization`, `platform_admin@platform` | | now |
| `users.list` | organization | `org_admin@own_organization`, `platform_admin@platform` | | step 7 |
| `roles.assign` | organization | `org_admin@own_organization`, `platform_admin@platform` | yes | now |
| `roles.revoke` | organization | `org_admin@own_organization`, `platform_admin@platform` | yes | now |
| `roles.list` | organization | `org_admin@own_organization`, `platform_admin@platform` | | now |
| `applications.create` | organization | `org_admin@own_organization` | | step 7 |
| `applications.list` | organization | `org_admin@own_organization`, `viewer@own_organization` | | step 7 |
| `usage.read_organization` | organization | `org_admin@own_organization`, `viewer@own_organization` | | later |
| `audit.read_organization` | organization | `org_admin@own_organization`, `platform_admin@platform` | | later |
| `applications.read` | application | `app_admin@own_application`, `org_admin@application_in_own_organization`, `viewer@application_in_own_organization`, `viewer@own_application` | | step 7 |
| `usage.read_application` | application | `app_admin@own_application`, `org_admin@application_in_own_organization`, `viewer@application_in_own_organization`, `viewer@own_application` | | later |
| `application_keys.issue` | application | `app_admin@own_application`, `org_admin@application_in_own_organization` | yes | step 7 |
| `application_keys.rotate` | application | `app_admin@own_application`, `org_admin@application_in_own_organization` | yes | step 7 |
| `application_keys.revoke` | application | `app_admin@own_application`, `org_admin@application_in_own_organization` | yes | step 7 |
| `application_keys.list` | application | `app_admin@own_application`, `org_admin@application_in_own_organization` | | step 7 |
| `application_roles.define` | application | `app_admin@own_application`, `org_admin@application_in_own_organization` | yes | later |
| `application_roles.delete` | application | `app_admin@own_application`, `org_admin@application_in_own_organization` | yes | later |
| `application_roles.list` | application | `app_admin@own_application`, `org_admin@application_in_own_organization` | | later |
| `audit.read_application` | application | `app_admin@own_application`, `org_admin@application_in_own_organization` | | later |
| `provider_keys.add` | application | `app_admin@own_application`, `org_admin@application_in_own_organization` | yes | A5 |
| `provider_keys.replace` | application | `app_admin@own_application`, `org_admin@application_in_own_organization` | yes | A5 |
| `provider_keys.delete` | application | `app_admin@own_application`, `org_admin@application_in_own_organization` | yes | A5 |
| `provider_keys.read` | application | `app_admin@own_application`, `org_admin@application_in_own_organization` | | A5 |
| `mode.change` | application | `app_admin@own_application`, `org_admin@application_in_own_organization` | yes | A5 |
| `monitoring.read` | platform | `platform_admin@platform` | | A8 |
| `control.write` | platform | `platform_admin@platform` | | A8 |
| `playground.use` | platform | `platform_admin@platform` | | A8 |

- **The audit log by level:** `audit.read_organization` covers organization-, user-, role- and
  login-level entries (platform_admin and the org_admin); `audit.read_application` covers
  entries about application keys and application roles (only that organization's org_admin and
  that application's app_admin). There are no transcripts: the gateway stores no prompts or
  responses (hard rule 7).
- **What platform_admin cannot do**, by construction: it holds no organization or application
  relation, so nothing at application level is ever allowed to it (application keys, roles,
  provider keys, modes, an application's usage or audit entries). A test asserts it for every
  application-level action.

## Granting, beyond the table

`roles.assign` and `roles.revoke` say who may grant at all; these rules say what:

- A role is held only within the user's own organization (the platform scope aside), and only
  in a scope its kind allows (`viewer` at an organization or an application, and so on).
- **platform_admin is granted and revoked only by the operator** at the CLI (no `--as`), never by
  a user, platform_admins included.
- A platform_admin acting as itself appoints **only `org_admin`**.
- An org_admin grants within its own organization; an app_admin cannot grant at all.
- **The last enabled org_admin of an organization, and the last enabled platform_admin, can be
  neither revoked nor disabled.** For an individual organization, whose one member is that
  last org_admin, disable the organization instead.

## Who must change their password

`create_user`, shared by the CLI and (step 7) the endpoint, makes an account start with
`must_change_password` when whoever creates it knows the password and is not of its
organization: a platform_admin creating a user in an organization it is not a member of, and
the operator at the CLI (a member of no organization). Accounts an org_admin creates in its own
organization do not. Tested on the CLI path; step 7 tests the endpoint. The creation is audited
with its actor and whether the password must be changed, for that organization's org_admin to
see. **This changes step 4's CLI:** `manage.py create-user` without `--as` now makes an account
that must change its password at first login.

## The CLI

`grant-role --username U --role R (--organization ORG | --application APP | --platform)`,
`revoke-role` with the same arguments, and `list-roles --username U`. The user and role
commands take `--as USERNAME`: the action is then checked with `authorize` against that user's
roles and recorded with that user as its actor. `--as` does not authenticate anyone: whoever
runs the CLI has the database already. It only narrows what the operator does to what that
user may do, and attributes it.

## Declaring every route

Each route of the app carries exactly one declaration:

- `requires(action, resolver)`: a FastAPI dependency that **declares and enforces**. On every
  request it reads the session (401 without one), checks the CSRF token on a state-changing
  method, refuses a password that must be changed first, reads the actor's roles, resolves the
  resource, and calls `authorize`. A route cannot be declared without being checked. **A
  resource that does not exist gets exactly the answer a forbidden one gets** (403 `forbidden`,
  byte for byte, tested). Available when the app has accounts; used by step 7.
- `local_only_route(action)`: monitoring, metrics, the overload view, control, the Playground and
  the dashboard, with the A8 action each will need. Enforced by the local-only guard (note 25)
  until then; a label only, nothing runs per request.
- `public_route(reason)`: generate and an application's own usage (an API key, checked by the
  route), health, the models, the index, and the session's own `/v1/auth` endpoints. A label
  only.

`test_every_route_of_every_app_declares_itself` builds every variant (bare, with applications,
with load control, with accounts, simulated live mode, the default server), walks `app.routes`,
and fails on any endpoint with no declaration or more than one, on a `local_only` route the
guard does not cover, and on anything outside two explicit lists: FastAPI's documentation
routes and the dashboard's static mount. The lists must match real routes, so they cannot go
stale. **The request path is unchanged**: `public_route` and `local_only_route` are attributes on
the endpoint, so `/v1/generate` and every route of the no-applications path run no new code.

## The mutation check

`authorize` was made to allow everything (its first line `return Decision(True, ...)`). The
invariant tests (no role is denied everything, a disabled user is denied everything, nothing
crosses organizations, platform_admin never reaches inside an application, a viewer never
changes anything, an app_admin never reaches another application, only platform_admin acts on
the platform), the matrix file, the HTTP tests of `requires` (another organization, a user with
no role, a viewer's change, a removed role) and the CLI's escalation tests failed: 22 of the 35
tests in `tests/test_permissions.py` and `tests/test_manage_roles.py`. With the change undone
all 35 passed and the file was byte-for-byte as before. The invariants are written apart from the table, so a wrong table
fails them too.

## Migration 8

- `role_assignments (user_id, role, scope_type, scope_id, granted_by, granted_at_ms)`, unique
  per user, role and scope, the platform scope's id `*`; `app_roles (application_id, name)`.
- **The backfill:** `platform_admin` for every user with `users.platform_admin`; `org_admin`
  for the sole member of each individual organization; nothing for anyone else. Then **`users`
  is rebuilt without the column**, through the rebuild path: foreign keys off around it,
  `foreign_key_check` before committing; `sessions` and `role_assignments` still reference the
  rebuilt table (tested with `foreign_key_list`).
- **Upgrading:** `manage.py migrate` backs the database up first (tested), and the server
  refuses to rebuild a table itself, so an existing installation runs `manage.py migrate` once
  before starting this version.
- **The reverse** puts `platform_admin` back into the column and drops the individual
  organizations' org_admin (the data already says who that is); it refuses while any other
  assignment or any application role exists.

## Measured

`uv run python -m benchmarks.session_checks`, three runs, on the tree of the commit that adds
this note; the machine of notes 24 and 27 (Intel Core Ultra 7 155H, 22 logical cores, 15.4 GB
RAM, Windows 11 10.0.26200, Python 3.12.10, SQLite 3.49.1); 2,000 each after 20 warm-up,
nearest-rank percentiles, ranges over the runs:

| | mean | p50 | p99 |
|---|---|---|---|
| Session check (no write) | 0.018 to 0.019 ms | 0.017 to 0.018 ms | 0.030 to 0.034 ms |
| Session check + roles and organization read + `authorize` | 0.032 to 0.039 ms | 0.030 to 0.037 ms | 0.080 to 0.111 ms |

Reading the assignments afresh and authorizing adds about 0.015 to 0.020 ms to a management
request. `/v1/generate` does none of it (it is `public_route`, an attribute only); the
performance gate and the protected outputs are unchanged.

## Not done here

The management endpoints (step 7), step-up (step 6), endpoints for roles and organizations
(CLI only in A2), reading the audit log, and defining application roles from the CLI.
