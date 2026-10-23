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
**Step-up** is enforced by `requires` since step 6 (docs/29): a flagged action is refused with
`step_up_required` unless the session's step-up window is open. **`consume`** (A5, docs/32): step-up that one successful
call uses up, so each such change needs a fresh password. **Wired**: `now` (enforced today, through
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
| `users.unlock` | organization | `org_admin@own_organization`, `platform_admin@platform` | yes | now |
| `users.list` | organization | `org_admin@own_organization`, `platform_admin@platform` | | step 7 |
| `roles.assign` | organization | `org_admin@own_organization`, `platform_admin@platform` | yes | now |
| `roles.revoke` | organization | `org_admin@own_organization`, `platform_admin@platform` | yes | now |
| `roles.list` | organization | `org_admin@own_organization`, `platform_admin@platform` | | now |
| `applications.create` | organization | `org_admin@own_organization` | | step 7 |
| `applications.list` | organization | `org_admin@own_organization`, `viewer@own_organization` | | step 7 |
| `usage.read_organization` | organization | `org_admin@own_organization`, `viewer@own_organization` | | later |
| `providers.list` | organization | `org_admin@own_organization` | | A5 |
| `providers.status` | organization | `org_admin@own_organization`, `platform_admin@platform` | | A5 |
| `providers.create` | organization | `org_admin@own_organization` | yes | A5 |
| `providers.update` | organization | `org_admin@own_organization` | yes | A5 |
| `providers.disable` | organization | `org_admin@own_organization`, `platform_admin@platform` | yes | A5 |
| `providers.enable` | organization | `org_admin@own_organization`, `platform_admin@platform` | yes | A5 |
| `providers.delete` | organization | `org_admin@own_organization` | consume | A5 |
| `providers.test` | organization | `org_admin@own_organization` | | A5 |
| `provider_keys.set` | organization | `org_admin@own_organization` | consume | A5 |
| `provider_keys.delete` | organization | `org_admin@own_organization` | consume | A5 |
| `mode.set_organization` | organization | `org_admin@own_organization` | yes | A5 |
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
| `mode.set_application` | application | `app_admin@own_application`, `org_admin@application_in_own_organization` | yes | A5 |
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
  last org_admin, disable the organization instead. The count of the other admins and the
  revocation or disabling run in **one write transaction** (`BEGIN IMMEDIATE`), so another
  connection (the CLI beside the server) cannot remove the last other admin in between.
  `tests/test_last_admin.py`: two revocations back to back leave one and refuse the second;
  and a revocation (or a disabling) from a second connection, attempted while the first is
  between its count and its change, is held off (the database is locked) and, run again, is
  refused as the last admin.

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
roles. `--as` does not authenticate anyone: whoever runs the CLI has the database already.
So the audit entry records **the operator as the actor** (no user id) and the user in a
separate allowlisted field, `acting_as` (a user id), never as the actor; a role granted this
way records no user as `granted_by` either. **`--as` only narrows**:
`test_as_only_narrows_what_the_operator_may_do` runs nine commands, each in two identical
fresh databases, as the operator and `--as` each of four users: nothing succeeds with `--as`
that fails without it, every command succeeds for the operator, and every one of the four
users is refused some.

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

## The mutation checks

Each rule about who may do what is enforced in one place, and each place was broken on purpose
to show its tests fail. Every time, the file was restored and found byte-for-byte as before,
and the tests of `tests/test_permissions.py`, `tests/test_manage_roles.py` and
`tests/test_last_admin.py` (39, then 40 with the scope test) passed again. The scope check (a role
is held only within the user's own organization, in `_assignment`) is a fourth mechanism, apart
from the granting rules: the granting-rules mutation's 3 failures were all those two rules
(platform_admin granted only by the operator; a platform_admin appointing only org_admins) and
the narrowing test that relies on them.

| Broken on purpose | Tests that failed |
|---|---|
| `authorize` allows everything | 23 of the 36 in the first two files: every invariant, the role anchors, the matrix file, the three HTTP tests of `requires`, and four CLI tests (who must change their password, an app_admin granting org_admin, an org_admin granting outside its organization, application roles granting nothing) |
| The granting rules (`_check_granting`) allow everything | 3: platform_admin granted only by the operator; a platform_admin appointing only org_admins; `--as` only narrowing |
| The last-admin guard does nothing | 6: the last org_admin, the last platform_admin, the individual organization's member, and the three of `tests/test_last_admin.py` |
| No transaction around the count and the change | 2: a revocation and a disabling from another connection slipping in between |
| The scope check, organization branch (any organization accepted) | 2: `test_grant_list_and_revoke`, `test_a_role_is_held_only_within_the_users_own_organization` |
| The scope check, application branch (any organization's application accepted) | 1: `test_a_role_is_held_only_within_the_users_own_organization`, added in this review: before it, nothing caught this mutation |

**The 13 that still pass with `authorize` allowing everything**, and why none of them depends
on it:

- `test_the_documented_table_is_the_table`: compares the documented table with the code's;
  no decision is made.
- `test_requires_waits_for_a_password_change`: `requires` refuses a password that must be
  changed before it calls `authorize`.
- `test_requires_refuses_an_undeclared_action`: checked when the route is declared.
- `test_every_route_of_every_app_declares_itself`, `test_a_route_without_a_declaration_is_found`:
  the structure of the routes.
- `test_the_creation_is_audited_with_the_operator_acting_as_a_user`: what the audit entry
  records.
- `test_grant_list_and_revoke`: the actions it performs are allowed anyway; the refusals it
  checks (twice, outside the user's organization) are the scope checks of the assignment.
- `test_platform_admin_is_granted_only_by_the_operator`,
  `test_a_platform_admin_appoints_org_admins_and_nothing_else`,
  `test_as_only_narrows_what_the_operator_may_do`: the granting rules, enforced apart from
  `authorize`; they fail when those rules are broken (above).
- `test_the_last_org_admin_cannot_be_revoked_or_disabled`,
  `test_the_last_platform_admin_cannot_be_revoked_or_disabled`,
  `test_an_individual_organizations_member_is_its_org_admin`: the last-admin guard; they fail
  when it is broken (above).

In the first version of this step one more passed: the role anchors checked only what roles
may do, so allowing everything satisfied them. They now check what roles may not do as well,
and fail with the mutation.

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

The management endpoints (step 7), endpoints for roles and organizations
(CLI only in A2), reading the audit log, and defining application roles from the CLI.
