# Console integration plan: one product, one login, navigation by role

Status: **approved** (PR #45, 2026-11-10), with the answers and additions in section 13. It
touches authentication, sessions and authorization, so it was plan-first (CLAUDE.md, working
agreement). The design note that ships with the code is `docs/50-console-integration.md`.

Nothing in this plan changes who may do what. `src/gateway/permissions.py` and
`docs/permission-matrix.md` stay byte for byte as they are in every PR below unless the answer to
question Q1 says otherwise, and Q1 is asked, not assumed.

## 1. Current state, read from the code (main at ee5c04a)

### 1.1 Pages and login paths

| URL | What it is | Login | Guard |
|---|---|---|---|
| `/` | JSON index of the API (`app.py`, `index`) | none, public | none |
| `/dashboard/` (`index.html`, `src/App.tsx`) | the monitoring dashboard: Overview, Latency, Capacity, Applications, Events, Playground tabs (`#/<tab>`) | **none**: no session is read | `LocalOnlyMiddleware` (loopback, no forwarding headers, Host allowlist) |
| `/dashboard/console.html` (`src/console/ConsoleApp.tsx`) | the admin console: Application settings, Organization settings, Alert rules, Channels, Incidents (`#/application/<id>`, `#/organization`, `#/rules/<id>`, `#/channels`, `#/incidents[/<app>/<incident>]`) | its own `LoginScreen` (`POST /v1/auth/login`); a reload asks `GET /v1/auth/me` | local only, plus the session, CSRF and `requires` on every API call; a content security policy on the built page (`vite.config.ts`, `CONSOLE_CSP`) |
| `/docs` | FastAPI's OpenAPI page | none | none |

There is one login path for a person: the console's. The dashboard has no link to the console and
the console none to the dashboard (note 47, "Not built"). The CLI (`scripts/manage.py`, `--as`)
is the operator's path and is not part of this work.

Facts the plan rests on, each checked:

- **The session** is the `gw_session` cookie: HttpOnly, `Path=/`, `SameSite=Strict`, `Secure` only
  with `GATEWAY_COOKIE_SECURE=1` (`auth.py`, `session_cookie`); 30 minutes idle, 12 hours absolute.
  The CSRF token is an HMAC of the session id, returned by login and `/v1/auth/me`, held by the
  console in a closure in memory (`api.ts`, `sessionApi`), never in storage.
- **Every admin action** goes through `requires(action, resolver)` (`app.py`, `make_requires`):
  session, CSRF on a state change, `must_change_password` refusal, `actor_for` (roles read afresh),
  resolver, `authorize`, then step-up. A missing and a forbidden resource both answer 403
  `forbidden`, "Not permitted, or not found.", byte for byte. `authorize` is imported and called
  in `app.py` only inside `make_requires` (`tests/test_step_up.py`,
  `test_app_py_authorizes_only_inside_requires`).
- **The console holds no rule.** Write controls appear only after the gateway said yes: a
  settings dry run (`probeWrite`), a refused-body probe (`probeRefusedBody`), or a sibling
  answer of the same role row pinned by `tests/test_console_probes.py`.
- **The console's navigation is not role-aware**: all five items are drawn for every logged-in
  account (`ConsoleApp.tsx`), and each screen shows the gateway's refusal when it gets one.
- **A session that expires** while a screen is open is not sent back to login: the screen shows
  "Your session has ended. Log in again." (`derive.ts`, `refusalText`) and the shell still
  believes it is logged in until a reload.
- **Deep links inside the console already survive login**: the login screen is drawn in place
  and the hash is not touched, so after login the hash's screen is drawn. There is no way to
  come back to a dashboard tab after logging in, because the dashboard has no login.
- **Organizations and roles have no HTTP endpoints.** `organizations.create`, `.list`,
  `.disable`, `.enable` and `roles.assign`, `.revoke`, `.list` are wired `now`, meaning the CLI
  with `--as`. Over HTTP a platform_admin reaches only `/v1/admin/orgs/{org}/users*` and
  `/v1/admin/orgs/{org}/providers/status|disable|enable`.
- **Monitoring has no roles.** `monitoring.read`, `control.write` and `playground.use` are
  `platform_admin@platform` in the table but wired `A8`: today they are enforced only by the
  local-only guard, so anyone with a browser on this machine reads every organization's
  monitoring without logging in.
- **Simulated live mode has no accounts** (`create_simulated_live_app` passes no `accounts`), so
  `/v1/auth/*` answers 404 there.
- **A role is held only in the user's own organization** (`auth.py`, `_assignment`), the
  platform scope aside. The bootstrap admin is created in `DEFAULT_ORGANIZATION_ID`.

### 1.2 What each role sees today

The roles are the four built-in ones in `permissions.py` (`Role`): `platform_admin` (platform
scope), `org_admin` (organization scope), `app_admin` (application scope), `viewer`
(organization or application scope). Application-defined roles (`app_roles`, such as
`clinic_staff`) grant nothing to `authorize` and are treated as no role. "No role" is a real
case: a user created without a grant.

| Role | Dashboard | Console navigation | What the console's screens answer |
|---|---|---|---|
| platform_admin | everything, no login (local only) | all five items | all five: "Not permitted, or not found." (no org-level or application-level row allows it); its HTTP-only powers (users, provider status) have no screen |
| org_admin | everything, no login | all five | all five work, with write controls per probe |
| viewer, organization scope | everything, no login | all five | application settings, organization settings, rules and incidents read only; channels refused |
| viewer, application scope | everything, no login | all five | its application's settings, rules and incidents read only (application id typed: `applications.list` refused); organization settings, channels and the organization-wide incident list refused |
| app_admin | everything, no login | all five | its application's settings, rules, incidents with write and acknowledge; the rest refused |
| no role | everything, no login | all five | everything refused |

## 2. The product after this work

### 2.1 One page, one shell

The dashboard and the console become **one single-page app at `/dashboard/`**, with one shell:
a header (product name, "Simulated" badge where it applies, the user name, the organization id,
the role(s) the gateway reports, theme, Log out) and one navigation. The dashboard's tabs become
the shell's monitoring section (`#/monitor/<tab>`); the console's screens keep their hash routes.
`/dashboard/console.html` stays as a stub that sends the browser to `/dashboard/` with the same
hash (a `<meta http-equiv="refresh">` and a link, no inline script), so existing links keep
working. The old dashboard hashes (`#/latency` and so on) are read as `#/monitor/latency`.

Why one page and not two pages with a shared header: one session check, one CSRF token in one
closure, one step-up dialog, one expired-session path; a second page would be a second copy of
each, which is where they drift. The cost is that the dashboard comes under the console's content
security policy (Q3).

### 2.2 The front door

- **Plain server (accounts on):** `/dashboard/` asks `GET /v1/auth/me`. 401: the login screen,
  drawn in place, hash untouched. Logged in with `must_change_password`: the change-password
  screen, nothing else. Logged in: the home screen for the role (2.4), or the hash's screen if
  the URL named one.
- **Simulated live mode (no accounts):** `/v1/auth/me` answers 404. The shell then shows the
  monitoring section only, with the `SimulationBanner` on every tab, as today; no login, no
  console items. The shell decides this from that one answer, not from a build flag.
- `/` stays the JSON index (Q4).

### 2.3 Navigation per role

Each item is shown only when the gateway's capabilities answer (section 3) says the item's
action is allowed. This table is what that answer produces from today's permission table; it is
a test expectation, not a table the frontend holds.

| Item (route) | Action asked | platform_admin | org_admin | viewer (org) | viewer (app) | app_admin | no role |
|---|---|---|---|---|---|---|---|
| Home (`#/home`) | none | yes | yes | yes | yes | yes | yes |
| Monitoring (`#/monitor/*`) | `monitoring.read` (platform) | yes | no (Q2) | no | no | no | no |
| Playground tab, load control | `playground.use`, `control.write` | yes | no | no | no | no | no |
| Users of an organization (`#/users`, new screen, P3) | `users.list` (organization) | yes, any organization | yes, own | no | no | no | no |
| Organizations (`#/organizations`, new screen, P3, Q5) | `organizations.list` (platform) | yes | no | no | no | no | no |
| Organization settings (`#/organization`) | `org_settings.read` | no | yes | yes (read) | no | no | no |
| Applications and keys (`#/applications`, new screen, P3) | `applications.list`; keys per application by `application_keys.list` | no | yes | list only | no | its application's keys | no |
| Application settings (`#/application/<id>`) | `settings.read` on an application it can name | no | yes | yes (read) | yes (read) | yes | no |
| Alert rules (`#/rules/<id>`) | `alert_rules.read` on an application it can name | no | yes | yes (read) | yes (read) | yes | no |
| Channels (`#/channels`) | `alert_channels.read` | no | yes | no | no | no | no |
| Incidents (`#/incidents`) | `incidents.read_organization`, or `incidents.read` on an application it can name | no | yes | yes (read) | its application (read) | its application | no |

"Read" means the item is shown and the screen's write controls stay hidden because the screen's
own probe says `forbidden`, as today. Nothing about write controls moves into the navigation.

The platform_admin row is what the table gives: organizations and users, provider status, and
monitoring. It sees none of an organization's settings, rules, channels or incidents. Q1 is about
that.

### 2.4 Home screens

One component, drawn from the capabilities answer, never from a role name:

- **Platform** (any platform-level capability): organizations (once P3 and Q5 land), the users
  screen with an organization chooser, monitoring, and a line saying that organization settings
  are managed by that organization's own admins.
- **Organization** (any organization-level capability): the organization id, links to the items
  above that are shown, the evaluator banner (`/healthz`, as on the console today), and the
  number of open incidents if `incidents.read_organization` is allowed.
- **Application only** (only application-level capabilities): the applications the session can
  name (its own assignments), each with links to settings, rules and incidents.
- **Nothing**: "This account has no access to any part of the gateway yet. Ask your
  organization's administrator." and Log out.

A session can be more than one of these (an org_admin of the default organization who is also
platform_admin); the home shows each section that applies.

### 2.5 Deep links, expired sessions, password change, step-up

- **Deep link while logged out:** the hash is the return target. Login is drawn in place and the
  hash is never rewritten, so after login the shell draws that route. No `?next=` parameter and
  no redirect exist, so there is nothing to make an open redirect from. A hash naming a route
  the shell does not know, or an item the capabilities answer does not allow, draws Home with
  "That page is not available to this account." (the same words for an unknown route and a
  forbidden one).
- **Expired session:** one place, the API client, sees 401 `session_required` on any call
  (including the monitoring stream's reconnect `/v1/auth/me` check), forgets the CSRF token,
  closes any step-up dialog (rejecting its promise), and draws login with "Your session has
  ended. Log in again." The hash stays, so after login the person is where they were. A form's
  unsaved contents are not kept: keeping them would mean browser storage, which the console does
  not use (note 47). The note records this; the login notice does not promise otherwise.
- **Forced password change:** after login or a reload, `must_change_password` draws only the
  change-password screen, as today; the capabilities endpoint refuses 403
  `password_change_required` like `requires` does, so a stale shell cannot draw navigation for
  such an account. After the change, the shell asks `/v1/auth/me` and capabilities again.
- **Step-up:** unchanged. The shell owns the one `StepUpDialog`; `guarded` is passed to every
  screen as now. The password is in the field until sent, then emptied; never kept.
- **Logout:** from any section, including monitoring. `POST /v1/auth/logout` with the CSRF
  token, then login in place; the hash is cleared to `#/home` so the next person does not land on
  the previous person's screen.
- **No token or password in a URL, a log or storage:** the session stays an HttpOnly cookie, the
  CSRF token stays in memory, routes carry only ids already in the API paths (organization,
  application, incident ids, as today), and the shell writes nothing to `localStorage`,
  `sessionStorage` or IndexedDB except the existing theme preference (`useTheme`), which holds
  "light" or "dark".

## 3. Backend: the capabilities answer

### 3.1 The endpoint

`GET /v1/auth/capabilities` and `GET /v1/auth/capabilities?organization=<id>`.

```json
{
  "organization_id": "acme",
  "platform": {"organizations.list": true, "monitoring.read": true, "...": false},
  "organization": {"users.list": true, "org_settings.read": false, "...": false},
  "applications": [
    {"application_id": "app_...", "actions": {"settings.read": true, "...": false}}
  ]
}
```

- **Which actions:** a fixed tuple `NAVIGATION_ACTIONS` in `permissions.py`, grouped by level:
  every action named in the navigation table of 2.3, and nothing else. It answers whether the
  role table allows the action, never whether step-up is open; step-up is still asked when the
  person acts. Every action in it is a read with no step-up flag in today's table (checked:
  `users.list`, `organizations.list`, `applications.list`, `org_settings.read`,
  `settings.read`, `alert_rules.read`, `alert_channels.read`, `incidents.read_organization`,
  `incidents.read`, `application_keys.list`, `monitoring.read`; `control.write` and
  `playground.use` have none either), and a test keeps it so.
- **Which organization:** the session's own when the parameter is absent. With the parameter,
  the organization is resolved with the same `resolve_organization` the admin routes use.
- **Which applications:** those the session can name, read through the same resolvers the
  admin routes use: the organization's applications (first 100, the console's page size) when
  `applications.list` is allowed there; otherwise the applications of the session's own
  application-scope assignments, each resolved with `resolve_application`, a missing one left
  out. An application-scope assignment that names a deleted application is therefore invisible,
  the same as one the session never had.
- **Missing equals forbidden:** an organization that does not exist, and one the actor has no
  allowed action on, answer the same bytes: every action `false`, `applications: []`, and
  `organization_id` echoing the asked id. A test compares the two answers byte for byte, for
  each role. (A platform_admin sees `true` on an existing organization and `false` on a missing
  one; that is the same knowledge `users.list` already gives it, and `organizations.list` is its
  own.)
- **No answer for a disabled user or organization:** login already refuses a disabled user;
  `actor_for` marks a disabled organization, so `authorize` answers `false` for everything.
- **Headers and guard:** `Cache-Control: no-store` (`_NO_STORE`), under `/v1/auth`, so local only
  like the rest. It reads, so it needs no CSRF token.

### 3.2 How it cannot disagree with `requires`

1. **The same function, the same actor.** The answer is built inside `make_requires`, next to
   `requires` and `escalate`, as `vars(requires)["capabilities"]`: the same `service.authenticate`,
   the same `must_change_password` refusal, the same `service.actor_for` (roles read on this
   request), the same resolvers, and the same `authorize` imported once at the top of
   `make_requires`. `test_app_py_authorizes_only_inside_requires` keeps holding without change:
   no other code in `app.py` imports or calls `authorize`.
2. **The same resource.** Each navigation item names the route its screen loads first (for
   example Channels: `GET /v1/admin/orgs/{org}/alert-channels`). A new test walks the app's
   routes with `declaration_of`, finds that route, and asserts that its declared action is the
   item's action and its level the item's level. If someone changes a route's action, or adds a
   navigation item without a route, that test fails.
3. **The same answer, proven on every pair.** A test runs, for every role (the four built-in, an
   application-defined role, no role), every scope it can hold, every navigation action and
   every resource relation (own, other organization, missing): the capabilities answer and the
   real route's answer (`200` versus `403 forbidden`; none of these actions needs step-up, so
   nothing masks the answer). They must agree on every pair. This is the test that makes "never disagree" a
   fact rather than a design intention.
4. **No table in the frontend.** The frontend reads `capabilities[level][action]` for an action
   name it gets from a single map from item to action (`src/shell/navigation.ts`). It has no
   role names: a lint test (`tests/test_dashboard.py` style, reading the frontend source) fails on
   `platform_admin`, `org_admin`, `app_admin` or `viewer` appearing in `src/` outside fixtures and
   the header's display of the gateway's own role list (Q6).

### 3.3 Audit

Not audited, like `GET /v1/auth/me`, the settings dry runs and the probes: it changes nothing,
the shell calls it on every login, reload and return from an expired session, and an audit row
for each would bury the rows that matter. The note records the decision. If Q1 picks the
"act in organization" option, its start, end and every action taken in it are audited (section 5).

### 3.4 Shared state

The answer reads only durable state (users, organizations, role assignments, applications), each
request, through the stores both server factories already use; no counter is involved. Tests:

- In process, SQLite: a role revoked through the CLI path is gone from the next answer.
- `postgres`-marked, two instances (`create_shared_app` twice over one schema): a role revoked on
  instance A, the next capabilities answer from instance B no longer has the action; an
  organization disabled on A, B answers all `false`. The same pair for a role granted.

### 3.5 Backend changes not made

No cookie change, no session-lifetime change, no new login path, no change to `LocalOnlyMiddleware`'s
paths, no change to `PERMISSIONS`, `authorize`, `relation` or `ROLE_SCOPES`. Wiring
`organizations.list` and the roles actions over HTTP is a new network surface for existing rows
and is asked separately (Q5).

## 4. Frontend changes

- **`src/shell/`** (new): `Shell.tsx` (session states: checking, out, must-change, in, no
  accounts), `Header.tsx` (user, organization, roles, theme, Log out), `Navigation.tsx` (items
  from `navigation.ts` filtered by the capabilities answer), `Home.tsx`, `routes.ts` (one parser
  and printer for every hash: the console's `parseRoute`/`routeHash` extended with `home`,
  `monitor/<tab>`, `users`, `organizations`, `applications`; pure, tested).
- **`src/console/`**: screens unchanged except that they take the shell's context; the shell's
  `api.ts` gains the one 401 handler. `ConsoleApp.tsx` is folded into the shell.
- **`src/App.tsx`** (the dashboard): becomes the monitoring section, rendered inside the shell;
  the monitoring stream starts only while that section is shown; `SimulationBanner` stays on
  every monitoring tab and in the shell header in simulated live mode.
- **The dashboard-to-console link** is the shared navigation itself: both are sections of one
  app.
- **Build:** one Vite input (`index.html`), `console.html` reduced to the redirect stub, the
  content security policy applied to `index.html` (Q3).
- **Types and fixtures:** `src/api/types.ts` (or `src/console/types.ts`) gains the capabilities
  shape by hand; `src/test/fixtures/capabilities-*.json` are produced by the gateway through
  `tests/test_dashboard.py`'s fixture mechanism, one per role, never written by hand.
- **New screens (P3):** Users of an organization (list, create with the temporary password shown
  once, disable, enable, reset, unlock: the existing endpoints, each write behind `guarded` for
  step-up), Applications and keys (the existing endpoints; a new key shown once and forgotten),
  and Organizations if Q5 allows. Each write control appears only after the gateway said yes, by
  the same probe rules as notes 47 and 49; a write with no dry run follows a pinned sibling,
  added to `tests/test_console_probes.py`'s `SIBLINGS` with its rows pinned equal.

## 5. Q1: a platform_admin who wants to manage an organization's settings

Today the table gives platform_admin nothing inside an organization's settings, rules, channels
or incidents, and nothing inside any application (docs/28: "none of them inside an
application"). This plan does not change that. The options, with what each costs:

| Option | What it is | Security cost | Work |
|---|---|---|---|
| **A. A separate org_admin account in that organization** (recommended) | The operator (or the platform_admin, once Q5 wires `users.create` into the console and `roles.assign` over HTTP) creates a user in organization X and grants it org_admin. The person logs in with that account to manage X. | None to the table. Each organization's settings are changed only by an account of that organization, and the audit log names that account. The cost is operational: one more credential per organization, and nothing stops the same human holding both. | Nothing for the CLI path (it exists today); P3 for the console path. |
| **B. org_admin on the platform_admin's own account** | Grant org_admin to the platform_admin account. | None to the table. | Works **only for the platform_admin's own organization** (the default organization): `_assignment` refuses an organization-scope role outside the user's own organization. Does not solve "organization X". |
| **C. "Act in organization X" mode** | A new, explicit mode: the platform_admin steps up (consume-on-use), names organization X and a reason, and gets a time-boxed window (say 15 minutes, one organization) in which a new relation `platform_admin@acting_in` is allowed for a chosen subset of organization actions; every action in it is audited with `acting_in`, a banner shows it, it ends on logout, expiry or by hand. | **Widens the platform role**, which this plan may not do on its own: a stolen platform_admin session or password reaches every organization's settings, rules and channels, not only its users. It breaks the isolation rule docs/28 states. It needs a new session field shared across instances (Postgres and Valkey, two-instance tests), new rows in `PERMISSIONS` and the matrix, a new set of mutation checks, and a choice of which actions are excluded (I would exclude provider keys, egress loosening and channel targets at least). | A plan of its own (it changes the table): a separate approval and PR series. |
| **D. Organization-consented delegation** | Like C, but an org_admin of X must grant the window first. | Smaller than C (X consents, time-boxed), still a table change and still cross-organization. | The most work: C plus a grant flow and its notifications. |

**Recommendation: A.** It keeps the one property the identity work was built on, that an
organization's data is changed only by its own accounts, and needs no table change. If you want C
or D, I will write it as a separate plan; this work then only leaves room for its banner in the
header.

## 6. Tests to add

Backend (`pytest`):

- The capabilities answer for every role and scope of 1.2 against the table of 2.3, generated, not
  copied: every item shown and every item hidden.
- The agreement test of 3.2 (3): capabilities versus the real route, every role, every action,
  every relation.
- `NAVIGATION_ACTIONS` holds no action with a step-up flag.
- The route-pin test of 3.2 (2).
- Missing equals forbidden: a missing organization and a forbidden one, byte for byte, for each
  role; an application-scope assignment to a deleted application is absent.
- `must_change_password` gets 403 `password_change_required`; no session gets 401
  `session_required`; a disabled organization gets all `false`.
- Local only: `/v1/auth/capabilities` refused with forwarding headers, a foreign Host, a
  non-loopback peer (added to `tests/test_local_only.py`'s path list).
- `Cache-Control: no-store` on every answer, refusals included.
- The two-instance tests of 3.4 (`postgres`).
- The fixtures for each role regenerated by `tests/test_dashboard.py`.

Frontend (`vitest`):

- Navigation: for each role fixture, every item shown and every item hidden, asserted from the
  fixture's own booleans so the test has no role table either.
- Direct URL to a screen the role cannot use: Home with "That page is not available to this
  account.", no request for that screen's data is made; an unknown route gives the same words.
- Deep-link login: logged out on `#/rules/app_x`, log in, the rules screen for `app_x` is drawn.
- Expired session: a 401 on a screen's call (and on a write behind `guarded`) draws login with
  the notice, keeps the hash, clears the CSRF token (the next request carries none until login),
  and closes an open step-up dialog.
- Forced password change: only the change screen; capabilities not asked until after the change.
- No accounts (404 on `/v1/auth/me`): monitoring only, no login, `SimulationBanner` shown.
- Logout: hash reset to `#/home`, CSRF token forgotten.
- The source lint: no built-in role name in `src/` outside fixtures (3.2 (4)).
- `routes.ts`: every route round-trips; old dashboard hashes map to `#/monitor/<tab>`.

End to end (Playwright, `console_server.py` extended; passwords generated by
`playwright.config.ts` as today, never written, printed or committed):

- One spec per role: platform_admin (the bootstrap admin), org_admin, viewer (organization),
  viewer (application), app_admin, no role. Each logs in at `/dashboard/`, checks the home
  screen and the exact navigation, opens each shown item, and opens one hidden item by URL.
- Deep link from logged out; expired session (the server's session ended through logout from
  a second browser context, which is the same 401); forced password change; a step-up write;
  the `console.html` stub's redirect.
- The dashboard smoke test (`smoke.e2e.ts`) in simulated live mode, unchanged in what it checks.
- **Leak scan of the new flow**: during each role spec, record every URL the page visits
  (`page.on('framenavigated')` and `location.href` after each step), every console message,
  every request URL, and a dump of `localStorage`, `sessionStorage`, IndexedDB names and
  `document.cookie` (which must not show `gw_session`, being HttpOnly) at the end; plus the
  console server's stdout and stderr and the audit rows. Scan them for each generated password,
  the session cookie value, and the CSRF token. **Planted control**: one run writes a marker
  derived from the password into `sessionStorage` and into a URL fragment through a test-only
  hook in the spec (not in the app), and the scan must report both; the run fails if the control
  is not found.

## 7. Mutation checks

Each is applied to the named file, the failing tests shown, the file restored and its sha256
compared with the sum taken before, and the tests shown passing again (the usual table in each
PR's description).

| # | Mutation | Expected to fail |
|---|---|---|
| M1 | capabilities answers `true` for one action without calling `authorize` | per-role answer test, agreement test |
| M2 | capabilities skips the `must_change_password` refusal | password-change test |
| M3 | a missing organization answers 404 (or a different body) | missing-equals-forbidden byte test |
| M4 | capabilities uses an actor cached at login instead of `actor_for` per request | role-revoked test, two-instance test |
| M5 | capabilities ignores a disabled organization | disabled-organization test |
| M6 | a navigation item's action differs from its route's declared action | route-pin test |
| M7 | `NAVIGATION_ACTIONS` gains an action no item uses, or loses one an item uses | route-pin test (both directions) |
| M8 | the application list includes an assignment's application without resolving it | deleted-application test |
| M9 | the frontend shows an item whose capability is `false` | per-role navigation test, e2e for that role |
| M10 | the 401 handler keeps the CSRF token | expired-session test |
| M11 | the 401 handler resets the hash | expired-session deep-link test |
| M12 | logout keeps the hash | logout test |
| M13 | a direct URL to a hidden item draws the screen | direct-URL test |
| M14 | the shell writes the CSRF token to `sessionStorage` | leak scan (and the vitest storage check) |
| M15 | `/v1/auth/capabilities` removed from the local-only paths | local-only test |

A mutation no test catches stops the work and is reported (memory: surviving mutations), unless
it is dead code or a duplicate check, which is fixed and listed.

## 8. Screenshots for the PRs

Taken by a Playwright capture spec against `console_server.py` on a temporary database, one per
role: the home screen and the navigation for platform_admin, org_admin, viewer (organization),
viewer (application), app_admin and no role; the login screen; the expired-session notice; the
forced password change; one hidden item opened by URL. Stored under
`docs/pr-console-integration-screenshots/`, as for C1 and C2. No password, token or cookie
appears in any of them (the leak scan's strings are also searched in the capture spec's page
text). `docs/img/` (the README's simulated pictures) is retaken with `scripts/capture_demo.py`
because the header changes; every caption still says simulated. Those pictures are not in the
protected outputs (only the report figures are).

## 9. Risks

- **The dashboard under the content security policy** (Q3) may break uPlot or a style; found by
  the e2e runs with a CSP-violation listener that fails the test.
- **Hiding is not protection.** Every screen still asks the gateway, and the monitoring API stays
  readable without login on this machine until A8 (Q2). The note and the home screen's help text
  say so; nothing in the PR text claims the dashboard is now protected by login.
- **A capabilities answer can be stale** for the life of a screen (a role revoked while it is
  open). The gateway still refuses the action; the shell asks again on every navigation to a new
  item, not on a timer.
- **The README and the quickstart** mention `/dashboard` and `/dashboard/console.html`; the stub
  keeps both working and `scripts/readme_quickstart.py` is run.
- **A large frontend diff**: folding two apps into one shell touches every console test. Split
  below to keep each PR reviewable.

## 10. Out of scope

- A8: remote access, authenticated monitoring, per-organization monitoring, TLS, limits per
  source address. The local-only guard is untouched.
- Any change to who may do what: `PERMISSIONS`, `authorize`, `relation`, `ROLE_SCOPES`, the
  grant rules of docs/28. Option C or D of Q1 would be a separate plan.
- New features beyond screens for endpoints that exist (and, if Q5 says yes, HTTP wiring of
  existing rows).
- Tagging or releasing.

## 11. PR split

Each stacked on the previous once its CI is green; merge in this order.

1. **P0, this plan** (docs only), base `main`.
2. **P1, backend**: `NAVIGATION_ACTIONS`, `/v1/auth/capabilities`, the backend tests of section 6,
   M1 to M8 and M15, the fixtures, `docs/50-console-integration.md`. Base P0's branch until it
   merges.
3. **P2, the shell**: one page, header, navigation, home, routes, deep links, expired session,
   logout, the CSP on the dashboard, the `console.html` stub, the vitest and e2e tests, the leak
   scan, M9 to M14, screenshots, `docs/img/` retaken.
4. **P3, platform and organization screens**: users of an organization, applications and keys,
   and (Q5) organizations and roles, each with probes and pinned siblings, e2e and screenshots.

## 12. Questions

**Q1. A platform_admin managing an organization's settings (section 5).**
Options: A, a separate org_admin account in that organization; B, org_admin on the same account
(own organization only); C, an audited, step-up-gated "act in organization X" mode (a table
change, separate plan); D, organization-consented delegation (separate plan).
**Recommendation: A.**

**Q2. The dashboard for an org_admin.** The request lists "the dashboard for their
organization", but monitoring is one global view, `monitoring.read` is platform_admin only, and
there is no per-organization monitoring endpoint (that is A8).
Options: (a) the monitoring section follows `monitoring.read`, so only platform_admin sees it,
and org_admins get it with A8; (b) show the global dashboard to org_admins too, which is a table
change (`monitoring.read` for `org_admin@own_organization` would still show every
organization's numbers) and is out of scope unless you ask for it; (c) build a per-organization
monitoring view now, which is A8 work.
**Recommendation: (a).**

**Q3. The content security policy on the merged page.** Options: (a) apply `CONSOLE_CSP` to the
one page, and fix anything in the dashboard it breaks (uPlot sets styles through the CSSOM, which
the policy allows, so I expect little); (b) keep two pages, the dashboard without the policy, and
share only components, accepting a second session check and a CSRF token held on a page with no
policy. **Recommendation: (a).**

**Q4. What `/` does.** Options: (a) leave it the JSON index (API clients and the quickstart read
it) and add `/dashboard/` to its list; (b) redirect browsers (`Accept: text/html`) to
`/dashboard/`. **Recommendation: (a).**

**Q5. HTTP endpoints for organizations and roles.** The table already allows
`organizations.list` (platform_admin) and `roles.assign`, `.revoke`, `.list` (platform_admin,
and org_admin in its own organization), but they are wired to the CLI only. Wiring them over
HTTP is a new network surface (no permission change). Options: (a) wire `GET
/v1/admin/orgs` (`organizations.list`) and the three roles routes in P3, with step-up as the
table says, audit as the CLI does, and the docs/28 grant checks unchanged; (b) wire only
`organizations.list`; (c) wire none, and the console's platform home links to the CLI commands.
**Recommendation: (a)**, since option A of Q1 needs a way to grant org_admin from the console;
it would get its own plan section in P3's first commit and stop for your approval like this one,
because it adds a network surface.

**Q6. Showing roles in the header.** Options: (a) the capabilities answer also returns the
session's built-in role names and scopes (its own assignments, nothing about anyone else), shown
as text only and never used to decide anything, with the source lint allowing them only in the
header; (b) the header shows user and organization only. **Recommendation: (a)**: "who am I"
reads better with the role, and it is the person's own data.

**Q7. The plan file's name.** I wrote this plan as `docs/console-integration-plan.md`, matching
the other plans, with the design note that ships with the code numbered 50
(`docs/50-console-integration.md`). Options: (a) keep that; (b) rename this file
`docs/50-console-integration-plan.md`. **Recommendation: (a).**

## 13. Answers and additions (approved 2026-11-10)

Answers:

- **Q1: A.** No permission change. The platform_admin creates organizations and their first
  org_admins; each organization's settings are managed by its own accounts.
- **Q2: (a).** Monitoring stays platform_admin only until A8. The organization home says that
  per-organization monitoring is not built yet.
- **Q3: (a).** One page under the console's content security policy.
- **Q4: (a).** `/` stays the JSON index.
- **Q5: (a), in P3,** with its own plan-first draft PR before any code: step-up, audit rows, and
  missing equals forbidden on every new route.
- **Q6: (a).** The capabilities answer carries the session's own built-in roles, shown as text.
- **Q7: (a).** This file's name stays; the design note is 50.

Additions:

1. **P3 makes a fresh install work end to end in the browser**: log in as the bootstrap admin,
   create an organization, create its first org_admin, log out, log in as that user (who changes
   the temporary password first). P3's plan says which step, if any, cannot be done over HTTP in
   P3, keeps it on the CLI, and names it.
2. **Simulated live mode (no accounts, `/v1/auth/*` answers 404)** shows a clear "this mode has
   no accounts" message where the login would be, never a broken login screen; P2 adds an e2e
   for it.
3. The tests of section 6 stay: the per-route check for every role, identical bytes for missing
   and forbidden, the mutation checks on the capabilities function restored by sha256, and the
   leak scan with planted controls.

Corrections found while building P1 (each is listed in P1's description so it can be objected
to):

- **The answer does not echo the organization asked for.** Echoing it would make the answer for
  a missing organization and for a forbidden one differ in that field, so they could not be the
  same bytes. The page knows what it asked. The answer is `roles`, `platform`, `organization`,
  `applications` and `more_applications`.
- **`organizations.list` joins `NAVIGATION_ACTIONS` in P3**, with the route that serves it
  (Q5). In P1 no route declares it, and the route-pin test requires one for every action.
- **The capabilities fixtures for the frontend move to P2**, which adds the TypeScript type
  they are checked against; in P1 nothing would read them.
- **Applications from the organization's own page** (`applications.list` allowed) are taken as
  they come from the store's page of that organization, the query the `applications.list` route
  makes. The resolver `resolve_application` is used for the applications of the session's own
  application-scope assignments, which come from the role store and are checked against the
  organization.
- **`Cache-Control: no-store` on the refusals too:** the `NoStoreMiddleware` (today only
  `/v1/admin`) also wraps `/v1/auth/capabilities`, because the `password_change_required`
  refusal is answered without the header.

## 14. P2: corrections, answered before its code (2026-11-10)

Two errors and one gap in the plan, and one step against a standing instruction, were found
before any P2 code and answered:

1. **The `console.html` stub cannot keep the hash with a meta refresh.** Checked in Chrome: a
   `<meta http-equiv="refresh">` navigates to its URL without the current fragment, so
   `console.html#/organization` (quoted in the v0.1.0 release notes and `docs/operating.md`)
   would open Home. **Answer:** the stub loads a small same-origin module that calls
   `location.replace` with `./` and the hash (allowed by `script-src 'self'`; no inline script);
   a meta refresh inside `<noscript>`, and a link, are the fallback without JavaScript, which
   loses the hash.
2. **`#/applications` was taken twice:** the old dashboard tab (mapped to
   `#/monitor/applications`) and P3's "Applications and keys" screen. **Answer:** the old hash
   keeps meaning the monitoring tab; P3's screens take `#/manage/applications`,
   `#/manage/users` and `#/manage/organizations`. P2's `routes.ts` has only the routes of the
   screens that exist (home, monitor, the console's five); P3 adds its own with its screens.
3. **`docs/img`:** `scripts/capture_demo.py` starts its own simulated-live gateway. **Answer:**
   run in P2 like the e2e suite (a short-lived server the script starts and stops itself, no keys).

Decisions made from the code, each listed in P2's description so it can be objected to:

- **The shell allows or refuses an item, not one application.** A route naming an application
  (`#/application/<id>`) is drawn when its item is allowed, and the screen asks the gateway, which
  refuses one the session cannot use ("Not permitted, or not found."). Checking the id against
  the answer's list would refuse applications past its first page (`more_applications`).
- **The application chooser** lists the applications the answer names when the organization's
  list is refused (an application's own admin or reader), instead of a typed id; the typed id
  stays for a session the answer names none for.
- **The Incidents link** opens the organization's list when `incidents.read_organization` is
  allowed, and otherwise the first named application with `incidents.read`.
- **"Open incidents" on the organization home** are `OPEN_STATES` (`incidents.py`: pending,
  firing, acknowledged): one read of each, and "100 or more" when a page is full.
- **The word "viewer"** appears in the frontend as plain English ("the person viewing the
  page"); those comments and test names are reworded so the role-name lint has no exception.
- **The page's heading** is "Gateway" for both sections; `smoke.e2e.ts` checks that heading
  instead of "Gateway monitor", and nothing else in it changes.
- **Without accounts** (simulated live mode), an empty hash or Home opens the monitoring; a console
  route, or one the shell does not know, shows "This mode has no accounts" where the login would
  be, with a link to the monitoring.

## 15. P3 onward is replaced by information architecture v2 (2026-11-10)

PRs #45 to #47 (P0 to P2) shipped as planned above. What this plan said about P3 and after is
replaced by [console-ia-v2-plan.md](console-ia-v2-plan.md), submitted for approval: the
Clients, Applications and application-page structure, costs, and the act-as mode as a separate
plan. Superseded here, subject to that plan's Q7: section 4's "New screens (P3)", section 11's
item 4, Q5 (a) (the roles routes), and addition 1 of section 13 (which holds only if that plan's
Q6 is answered C). Sections 1 to 14 stay as the record of P0 to P2.
