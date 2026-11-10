# 50. Console integration: what a session may do

The plan is `docs/console-integration-plan.md` (approved on PR #45). This note covers its
first step (P1): the gateway's answer to "what can this session do", from which the console's
navigation will be drawn (P2). Nothing in it changes who may do what: `PERMISSIONS`,
`authorize`, `relation`, `ROLE_SCOPES` and `docs/permission-matrix.md` are as they were.

## The endpoint

| Method | Path | Answers |
|---|---|---|
| `GET` | `/v1/auth/capabilities` | the session's own organization |
| `GET` | `/v1/auth/capabilities?organization=<id>` | the organization named (at most 64 characters) |

```json
{
  "roles": [{"role": "org_admin", "scope_type": "organization", "scope_id": "north"}],
  "platform": {"monitoring.read": false, "control.write": false, "playground.use": false},
  "organization": {"users.list": true, "applications.list": true, "...": true},
  "applications": [{"application_id": "north-shop", "actions": {"settings.read": true, "...": true}}],
  "more_applications": false
}
```

- **Which actions:** `NAVIGATION_ACTIONS` in `permissions.py`, by level: three platform, five
  organization and five application actions, the ones the console's screens load first. None
  needs step-up, so the answer is the role table's alone; step-up is still asked when the person
  acts. `organizations.list` joins in P3 with the route that serves it.
- **Which applications:** with `applications.list` allowed in the organization, its first 100
  (`CAPABILITY_APPLICATIONS`, the console's page) from the store's page of that organization,
  the query the `applications.list` route makes, and `more_applications` when there are more.
  Otherwise the applications of the session's own application-scope assignments, each through
  `resolve_application`: one that is gone, or in another organization, is not named. Only
  applications with at least one action allowed are listed.
- **Roles:** the session's own built-in assignments, for the header to show. They decide
  nothing; an application-defined role is not shown.

## Why it cannot disagree with the routes

- It is built inside `make_requires`, beside `requires` and `escalate`, and takes the same steps:
  `authenticate` (401 `session_required` without a session), the `password_change_required`
  refusal, `actor_for` (roles read on this request), the same resolvers, and the one `authorize`
  imported there. `test_app_py_authorizes_only_inside_requires` holds unchanged.
- `tests/test_capabilities.py` pins each navigation action to the route its screen loads first
  (declared by that route, at the level the answer files it under, both directions), and checks
  for every role (the four built in, an application-defined role, no role), every organization
  (own, another, missing) and every application path (own, another organization's, missing)
  that the route answers 200 where the answer allows and 403 `forbidden` where it does not.
- The answer for each role is also written out by hand from the plan's table (section 2.3), so a
  change to the permission table shows up there as well.

## Missing equals forbidden

A missing organization, an unreadable id, and an organization the session may do nothing in give
the same bytes: every action `false`, no applications. That is why the organization asked for is
not echoed. A platform_admin sees `users.list` in an existing organization and nothing in a
missing one, which `users.list` itself already tells it.

## Other decisions

- **Not audited**, like `/v1/auth/me` and the dry runs: it changes nothing, and the console asks
  on every login, reload and navigation.
- **No CSRF token:** it is a read.
- **`Cache-Control: no-store` on every answer, refusals included:** `NoStoreMiddleware` also
  wraps `/v1/auth/capabilities`, because the `password_change_required` refusal has no header of
  its own.
- **Local only** under `/v1/auth`, like the rest, until A8.
- **Shared state:** it reads only durable rows, each request. On two instances over one Postgres,
  a role granted or revoked, and an organization disabled, through one is in the other's next
  answer (`tests/test_capabilities_postgres.py`).
- **A disabled organization** answers every action `false`: `actor_for` marks it and `authorize`
  refuses.

## Hiding is not protection

The console will hide what the answer does not allow; every action is still checked by its own
route. The monitoring endpoints answer anyone on this machine without a session until A8, so
`monitoring.read` being `false` for an org_admin hides the dashboard, nothing more.

## Not built

- The console's use of the answer, the one page, the home screens, deep links and the expired
  session (P2); the capabilities fixtures for the frontend come with P2's TypeScript type.
- The platform and organization screens, and the HTTP routes for organizations and roles (P3,
  with its own plan first).
- Timing: not measured. The answer is one organization lookup, at most one page of applications
  (or one resolve per own application assignment), and `authorize` eight times plus five times
  per application considered, in memory.
