# Platform plan, A2: identity and authorization

Status: approved 2026-10-22, with the answers and additions folded in below; amended the same
day (browser-based attacks, Docker, running uvicorn directly, `docs/licenses.md`). No code has
changed yet; this note is the plan for the `platform/identity` branch. Each step in section 13
is its own pull request, ships with tests and a design note (`docs/24-*` onwards), and runs
ruff, mypy, the full suite, the frontend checks, e2e and the protected outputs.

A2 adds people: users who log in to a console, organizations they belong to, roles that say
what they may do, and short-lived signed tokens other applications can check. It builds on A1
(applications, API keys, the SQLite stores and the migration runner) and changes none of it:
**application API keys keep working exactly as before** for applications calling
`/v1/generate`. User login and roles are a separate thing.

No third-party identity provider, and still no SQLAlchemy or Alembic: the hand-written
migration runner and the `sqlite3` stores carry on.

## 1. Dependencies

Each one is pinned to an exact version in `pyproject.toml` (and its own dependencies through
`uv.lock`), license-checked, and recorded in `docs/licenses.md` with why it is needed and what
the alternative was.

| Need | Choice | License | Alternative considered |
|---|---|---|---|
| argon2id password hashing | `argon2-cffi` | MIT | stdlib `hashlib.scrypt` (not argon2id); `passlib` (unmaintained) |
| Asymmetric token signatures | `PyJWT` with `cryptography`, Ed25519 (EdDSA) | MIT; Apache-2.0 or BSD-3-Clause | A hand-written JWS on `cryptography` alone: one dependency fewer, but token parsing is where algorithm-confusion bugs live. RSA: bigger keys, slower, no benefit here |
| Common-password blocklist (data, not code) | The 10,000 most common passwords from SecLists | MIT | NCSC's list (a different license to check); none (weak passwords allowed) |

A test reads `importlib.metadata` for every runtime dependency and fails on a license that is
not on the allowlist in `docs/licenses.md`.

`docs/licenses.md` records, for each library, the exact version pinned, its license, why it is
needed and the alternative considered (as in the table). For the blocklist it records the
source URL, the commit it was taken from, the license and the attribution, and marks it as
**public data under the rule 7 exemption** (CLAUDE.md): a public dataset with its license
attribution, not request traffic and not user data.

## 2. Slow hashing without moving simulated time

argon2id is deliberately slow (to be measured on this machine at OWASP-level parameters,
about 19 MiB and t=2, and recorded with the hardware). There are two ways it could go wrong:

- **On the event loop**, a hash would block every other request for as long as it takes.
- **On a thread**, the loop looks idle, and a `SimClock` would move time forward during it.

So the hash goes through an injected `HashRunner`:

- **`ThreadHashRunner`**, for the `WallClock` server: a dedicated pool of two threads.
  `argon2-cffi` releases the GIL while hashing, so the loop keeps serving, and the small pool
  bounds how much CPU a burst of logins can take.
- **`InlineHashRunner`**, for `SimClock` (tests, and simulated live mode if it ever needs it):
  hashes synchronously on the loop with no `await` inside. The loop stays busy, so simulated
  time cannot move. A test checks that the clock reads the same before and after a login.

`app.py` picks the runner when it wires the server, as it picks the clock. Every store call
stays synchronous on every path. The argon2 parameters are injected, so tests can use cheap
ones.

## 3. Data

New migrations only; the A1 tables are unchanged except for the one column in migration 4.

| Migration | Adds |
|---|---|
| 4 | `organizations (id, name unique, kind, created_at, disabled)`, `kind` being `individual` or `business`; a required `applications.organization_id`, backfilled: every existing application goes into one organization named `default` with kind `business`; application names unique within an organization. Done in step 3 (note 26) |
| 5 | `users (id, username unique, password_hash, organization_id, created_at, disabled, must_change_password, bootstrap, failed_logins, first_failure_at, locked_until, platform_admin)`. Step 4 |
| 6 | `sessions (id_hash, handle, user_id, csrf_hash, created_at, last_seen_at, expires_at, revoked)`; session ids and CSRF tokens are stored only as SHA-256; `handle` is a random public id for the session list. Step 4 (step 6 adds `step_up_until`) |
| 7 | `audit_log (seq, at, actor_user_id, action, target_type, target_id, outcome, details)`; `details` is written through an allowlist of fields. Step 4 |
| 8 | `role_assignments (user_id, role, scope_type, scope_id, granted_by, granted_at)`, unique per user, role and scope, `scope_id` `*` for the platform scope; `app_roles (application_id, name)` for application-defined roles such as `clinic_staff`. Step 5: moves `users.platform_admin` into `role_assignments` and drops the column through the table-rebuild path (a backup first, through `manage.py migrate`) |
| 9 | `sessions.step_up_until_ms`, an `ADD COLUMN` (no rebuild; the server applies it). The reverse rebuilds `sessions` without it (no table refers to `sessions`). Step 6 |

**Organizations.** An `individual` organization has exactly one member, its `org_admin`, and
refuses to add anyone else. A `business` organization has any number of members and allows
every role. Accounts are created by an admin in both cases; there is no self-signup.
Applications belong to one organization. Nothing assumes there is only one organization: the
backfill puts the existing applications into one, which is data, not code.

Step 3 (note 26) built organizations, the migration, the CLI and the roll-up of usage; the
individual rule is the pure function `membership_decision`, which step 4 calls when it adds a
user. A disabled organization's keys stop working, and an organization has no budget of its
own: its limit is the sum of its applications' budgets.

## 4. Users, passwords and login

Step 4 builds this section and sections 5 and 9 for logins (note 27). Until step 5 there
are no roles: whether a user is a platform_admin is the column `users.platform_admin`,
and a session reaches only login, logout, `/me`, change-password and the session list.

- **Usernames:** case-folded on input, then 3 to 64 characters of `a-z`, `0-9`, `.`, `-` and
  `_`. A user belongs to one organization; creating one calls `membership_decision`, so an
  individual organization refuses a second member.
- **The CLI** (`create-user [--platform-admin]`, `disable-user`, `enable-user`,
  `reset-password`, `unlock-user`) reads passwords from a prompt, entered twice, never from
  the command line, and never prints them.
- **No self-signup.** A platform_admin, or an org_admin within their own business
  organization, creates accounts (an endpoint, and the CLI).
- **The bootstrap admin.** `GATEWAY_ADMIN_USERNAME` and `GATEWAY_ADMIN_PASSWORD` create the
  first platform_admin when there is none; with neither a platform_admin nor these variables,
  the server refuses to start. The bootstrap password is **a one-time credential**: the
  account is marked `must_change_password`, and the environment value stops working after the
  first successful login (that session can only change the password; if it is lost before
  then, `manage.py reset-password` recovers the account). An existing platform_admin is never
  overwritten from the environment. The bootstrap admin is in the `default` organization, and
  its password must pass the policy (including not containing the username) or the server
  refuses to start. If an admin exists and either variable is still set, the server logs a
  warning naming the variables, never their values.
- **`must_change_password`.** Until the password is changed, the account can only call
  `GET /v1/auth/me`, change its password and log out; everything else is 403
  `password_change_required`.
- **Password change** requires the current password and revokes every other session of that
  user. **Admin reset** through the CLI sets `must_change_password`.
- **Password policy:** at least 12 characters, at most 1024 (to bound hashing cost), not on the
  bundled blocklist of very common passwords, and no composition rules.
- **Lockout:** 5 failures within 15 minutes locks the account for 15 minutes, on the injected
  clock and epoch, kept in the database so a restart does not clear it. `manage.py unlock-user`
  unlocks one early. Login attempts are also counted per username in memory, for known and
  unknown usernames alike (keyed by an HMAC of the name, bounded in size), so an unknown
  username "locks" exactly like a real one and lockout cannot reveal which names exist. The
  login response never says an account is locked. Limits per source address wait for A8,
  when there is a trustworthy client address.
- **Re-hashing on login:** a successful login whose stored hash used other argon2 parameters
  stores the hash made with the current ones.
- **No enumeration:** an unknown, disabled or locked username, a wrong password and a used
  bootstrap credential all get the same response, byte for byte, through the same steps: one
  lookup and one verify against the stored hash or a dummy one made at startup with the
  current parameters (a structural test, as for API keys, shown failing on an early return).
  What the writes after that step add to the time (a database update for a known username,
  memory for an unknown one) is measured on the running server and recorded in note 27; no
  mitigation is built.

## 5. Console sessions and CSRF

- `POST /v1/auth/login` sets `gw_session`: 256 random bits, `HttpOnly`, `SameSite=Strict`,
  `Path=/`, and `Secure` when `GATEWAY_COOKIE_SECURE=1` (off by default, because localhost is
  plain HTTP). Sessions expire after 30 minutes idle and 12 hours in all. `last_seen_at` is
  written at most once a minute per session. `/v1/auth` is local-only until A8 (section 10),
  so login gets the Origin, content-type and Host checks. `GET /v1/auth/sessions` lists the
  user's own sessions by handle; there is no endpoint to revoke one (a password change
  revokes all the others). Expired and revoked sessions are pruned.
- **Session hygiene:** a new session id at every login (no fixation); disabling a user or
  changing a password revokes all of that user's sessions; logout revokes the session.
- **CSRF:** a synchronizer token, random per session and stored as a hash, returned at login
  and by `GET /v1/auth/me`. It is required in `X-CSRF-Token` on every state-changing request
  that authenticates with the session cookie, and the `Origin` header is checked when present.
  `SameSite=Strict` is a second layer, not the only one. No extra secret is needed.

## 6. Access tokens

- `POST /v1/auth/token` (session and CSRF) returns a JWT signed with EdDSA (Ed25519). Claims:
  `sub` (user id), `org` (organization id), `roles` (`[{role, scope_type, scope_id}]`), `iat`,
  `nbf`, `exp`, `jti`, `iss`. Every time comes from the injected epoch source. Lifetime 5
  minutes.
- The signing key comes from `GATEWAY_TOKEN_SIGNING_KEY` (an Ed25519 private key in PEM); the
  server refuses to start without it. `manage.py generate-signing-key --out PATH` writes a new
  one to the path given, readable by its owner only, and prints only its key id, never the key.
  An optional `GATEWAY_TOKEN_SIGNING_KEY_PREVIOUS` is published for verification only, so the
  key can be rotated without breaking tokens already issued.
- `GET /.well-known/jwks.json` publishes the public key(s), with an RFC 7638 thumbprint as
  `kid`. `verify_access_token()` accepts only EdDSA with a known `kid`, and checks `exp`, `nbf`
  and `iss`.
- **Tokens carry roles for up to their lifetime.** There is no revocation before expiry: a role
  change, a disabled user or a password change takes effect on token-based calls within 5
  minutes (at once for session-based calls). This is why the lifetime is short.
- Management endpoints accept a session (with CSRF) or a Bearer access token.
  `/v1/generate` still accepts only application API keys.

## 7. Roles and `authorize`

Step 5 builds this section (note 28); the management endpoints that use it are step 7.

- **Role assignments:** `(user, role, scope_type, scope_id)`, scope types `platform`,
  `organization` and `application`, written by migration 8's table and read from the
  database on every request (no cache), so removing a role takes effect on a session's next
  request.
- **Built-in roles:** `platform_admin` (platform scope); `org_admin` (its organization, and
  app_admin and viewer of every application in it); `app_admin` (its application, and viewer
  of it); `viewer` (an organization or an application: it reads usage and the application list
  in its scope, and nothing else). A user is assigned roles only within their own
  organization (platform_admin excepted).
- **What platform_admin can do:** create, disable and enable organizations, list them, see
  aggregate usage per organization, create users in any organization, and (through the CLI
  only) grant `org_admin`. An account created by a platform_admin for an organization it is
  not a member of, or by the operator at the CLI, starts with `must_change_password`, on the CLI
  and the endpoint alike (the shared create-user function; step 5 tests the CLI, step 7 the
  endpoint). Nothing at application level: no application keys, application
  roles, provider keys or anything inside an application (the gateway stores no prompts or
  responses, so there are no transcripts to read).
- **Granting:** org_admin grants and revokes roles within its own organization, never
  `platform_admin`; app_admin cannot grant `org_admin`; `platform_admin` is granted only
  through the CLI. The last `org_admin` of an organization and the last `platform_admin`
  cannot be revoked or disabled. Roles and organizations are managed through the CLI in A2;
  there are no endpoints for them.
- **Existing users at step 5:** the role migration gives each user with
  `users.platform_admin` the `platform_admin` role, and every other existing user no role at
  all: membership of an organization alone grants nothing. The one exception is required: the
  sole member of an individual organization is given `org_admin` of it.
- **Application-defined roles** (for example `clinic_staff`) are defined by an app_admin,
  carried in tokens for the application to interpret, and grant nothing inside the gateway.
- **One permission table**, `permissions.py`: each action, the roles and scope relation that
  allow it, whether it needs step-up (enforced from step 6), and when it is wired (now, step 7,
  A5 or A8). Actions cover users, passwords, roles, organizations, applications, application
  API keys, application roles, the audit log and usage. It also declares `provider_keys.*`
  and `mode.change` (step-up; A5 adds the endpoints), and `monitoring.*` and `control.*`,
  marked "local only until A8" (section 10).
- **`authorize(actor, action, resource) -> Decision`**, deny by default. A resource carries its
  application and organization, so a request that crosses organizations is denied whatever
  the role (platform_admin excepted, by design, for its platform-level actions only).
- **Every endpoint declares its permission** through a FastAPI dependency, and that dependency
  enforces it: `requires(action, resolver)` resolves the resource from the request and calls
  `authorize` itself, so a route cannot be declared without being checked. Other routes are
  declared `local_only(action)` (enforced by the guard of section 10 until A8) or `public(...)`
  on an explicit list (generate and an application's own usage, which take an application
  key; healthz; models; the documentation; the session's own `/v1/auth` endpoints). A test
  walks `app.routes` of every app variant and fails on any route with no declaration. A
  mutation check (authorize made to allow everything) must fail the matrix and the HTTP tests.
- **No difference between missing and forbidden:** an organization or application that does
  not exist and one the actor may not touch get the same status and body, byte for byte.
- **The test matrix is generated from the table:** every role, scope relation and action
  through `authorize`, checked against invariants written apart from the table, and
  representative endpoints through HTTP. The role table in the docs is parsed by a test and
  must equal `permissions.py`, so every cell of the documented table is tested.
- **Management endpoints** (step 7: users, applications and application keys only) live under
  `/v1/admin/orgs/{org}/...`, never under `/v1/applications/...` (the prefix of the public,
  key-authenticated `/v1/applications/me/usage`), and are local-only until A8. Creating a user
  goes through the same checks as the CLI, the individual rule included. Every
  state-changing one needs the session's CSRF token and the Origin check, and step-up where
  the table says so. A new application key is returned once, with `Cache-Control: no-store`,
  and never logged. The cost of reading assignments on each request is measured, and
  `/v1/generate`, the performance gate and the protected outputs are shown unaffected.

## 8. Step-up

Step 6 builds and proves the mechanism (note 29); the management endpoints that use it are
step 7, which cannot add a flagged action without enforcement.

- **`POST /v1/auth/step-up {password}`** needs a session, the CSRF token and the Origin check,
  and **refuses without either before verifying the password** (a test shows no verify runs).
  It verifies through the `HashRunner` on the same steps whether it succeeds or fails, and on
  success sets `step_up_until_ms = now + 5 minutes` (`GATEWAY_STEP_UP_MINUTES`, 1 to 60) on the
  injected epoch. Asking again restarts the window; it never extends the session's own expiry.
- **One answer for every refusal:** 403 `step_up_failed`, one fixed body, for a wrong password,
  a locked account, the in-memory limiter, the per-session cap, and a session whose password
  must be changed first.
- **A wrong password counts like a failed login**, toward the database lockout and the
  in-memory limiter under the same username. **Per session**, at most 3 failed step-ups in 5
  minutes (configurable); after that the endpoint refuses without verifying, with the same
  body, on top of the account lockout.
- **Step-up belongs to one session**: it is never copied, inherited or shared (a new login
  starts without it), and is cleared by logout, by a password change (the current session's
  too), by revoking the session, when the account locks, when it is disabled, and when its
  role assignments change. A session survives a lockout but cannot create new step-up.
- **Enforcement is in `requires`**, after authorization: for an action flagged in the table, 403
  `step_up_required` (one fixed body) before the handler, unless the session's window is still
  open. Authorization comes first, so step-up never reveals whether a forbidden resource
  exists. `/me` includes `step_up_until_ms` (null or the end time).
- **It cannot be forgotten:** a test builds a route through `requires` for every flagged action
  and shows it refused without step-up and served with it; `local_only_route` refuses a flagged
  action, and the route-declaration test fails on any declaration of one that is not
  `requires`; an AST check of `app.py` fails on any import or call of `authorize`, and any read
  of `step_up_until_ms`, outside `make_requires`. What that check cannot catch (aliasing,
  dynamic calls) is stated in note 29: review still matters.
- **Audited:** `step_up_granted`, `step_up_failed` (with the reason) and `step_up_required`
  (with the action's name) on every refusal; the audit retention keeps the table bounded.
- **Consume-on-use** (a step-up used up by one successful call of the most dangerous actions,
  provider keys in A5) is described in note 29 and not built; a test asserts the table has no
  `consume` column yet, so adding it is a deliberate change.

Flagged in A2: changing roles, creating or disabling users, and issuing, rotating or revoking
application API keys; provider keys and modes are flagged now and wired in A5. Step-up through
a token is not possible: tokens are for other applications, and sensitive actions need a
session.

## 9. Audit log

Logins, failures, lockouts, unlocks, password changes and resets, user creation and disabling,
role changes, key changes, and (from A5) mode changes: who, when, what, and the outcome. Fields
pass through an allowlist, so a password, token, key, session id or CSRF token cannot be
written. platform_admin reads organization-, user-, role- and login-level entries; entries
about application keys and application roles are read only by the org_admin of that
organization and the app_admin of that application. org_admin reads all of their
organization's entries, including those about accounts a platform_admin created in it.

Step 4 writes the login entries. A failure for an unknown username is recorded with no
identifier at all (one row per attempt, nothing of what was typed). Rows for unknown-user
failures are capped per minute, and the number dropped is written to the log itself, so a
flood cannot grow the table without bound. Entries older than the audit retention are
pruned in batches, like usage events.

## 10. Monitoring and load control until A8

They stay without user authentication until the A8 role-based monitoring step. Until then they
are **local only**, enforced inside the gateway, not only by how it is started. The endpoints
concerned: `/v1/monitor/*`, `/metrics`, `/metrics/summary`, `/v1/overload`, `/v1/control/*`
and the dashboard. The checks are an ASGI middleware in the app itself, so they hold however
the app is served.

### The socket peer address

- A request is refused (403 `local_only`) unless its peer address is loopback: `127.0.0.0/8`,
  `::1`, or IPv4-mapped IPv6 loopback (`::ffff:127.0.0.1`).
- **Forwarded headers are never trusted.** A request to these endpoints that carries any of
  `Forwarded`, `X-Forwarded-For` or `X-Real-IP` is refused, because its client address may
  have been rewritten by a proxy.
- **Running uvicorn directly stays safe.** By default uvicorn rewrites the client address from
  `X-Forwarded-For` when the peer is `127.0.0.1`. The launcher turns that off, but the
  forwarded-header refusal does not depend on it: a test serves the app through uvicorn's own
  proxy-headers middleware, as plain `uvicorn gateway.app:create_default_app --factory` does,
  and checks that the peer check and the refusal still hold.

### Browser-based attacks on the localhost server

A page in the user's browser can make requests to `127.0.0.1`, and DNS rebinding can make a
hostile name resolve there. So, on these endpoints:

- **Host allowlist.** The `Host` header must be `localhost`, `127.0.0.1` or `[::1]` (any port),
  or a name listed in `GATEWAY_ALLOWED_HOSTS`. Anything else is refused (403
  `host_not_allowed`): a rebound name such as `attacker.example` never matches.
- **Origin check.** A state-changing request (POST, PUT, PATCH, DELETE) that carries an
  `Origin` header is refused unless the origin's host is on the same allowlist.
- **No `text/plain` bodies.** A state-changing request must send `application/json`;
  `text/plain`, `application/x-www-form-urlencoded` and `multipart/form-data`, the content
  types a cross-site page can send without a CORS preflight, are refused (415).
- Tests: a DNS-rebinding request (allowed peer, `Host: attacker.example`) refused; a
  cross-site request (`Origin: https://attacker.example`, `text/plain` body) refused; the same
  request from the dashboard's own origin accepted.

### Docker

- **Behind Docker Desktop or NAT, the peer address cannot tell a local client from a remote
  one:** every request reaches the container from the bridge gateway's address. Inside the
  container, the protection is therefore **publishing the port on `127.0.0.1` only**, not the
  peer check.
- `GATEWAY_LOCAL_NETWORKS` (for example `172.16.0.0/12`) adds networks counted as local. It is
  documented as **trusting the published binding**: setting it says "only this machine can
  reach this network", which holds only while the port is published on loopback.
  `docker-compose.yml` sets it.
- **CI asserts the compose service publishes only on loopback**, from
  `docker compose config` (every published port's `host_ip` is `127.0.0.1`) and from
  `docker compose port` on the running demo; the docker job still serves the dashboard through
  the published port.

### Startup check

- A small launcher, `python -m gateway.serve`, starts uvicorn with proxy headers off and
  **refuses to bind a non-loopback address** while these endpoints are unauthenticated, unless
  `GATEWAY_ALLOW_NETWORK_BIND=1`; then it starts and prints a warning naming the open
  endpoints. The Docker image sets the opt-in, because it must bind `0.0.0.0` inside the
  container, and is published on `127.0.0.1` only.
- In step 2, the README quickstart, the Makefile and the Dockerfile switch to
  `python -m gateway.serve`, and the readme-quickstart CI job keeps running the README's
  commands literally, and passing.

Done in step 2 (note 25). `/v1/playground` is also local-only: it sends requests as an
application with keys the server holds.

### Tests for this section

Loopback allowed for `127.0.0.1`, another `127.x` address, `::1` and `::ffff:127.0.0.1`; a
non-loopback peer refused; each of `X-Forwarded-For`, `Forwarded` and `X-Real-IP` refused on
its own; the same through uvicorn's proxy-headers middleware; the Host allowlist, including
DNS rebinding; the Origin check and the content-type rule, including a cross-site request; the
Docker networks allowed only when opted in; the launcher refusing and warning. Load control
stays off by default.

## 11. Tests

Every cell of the documented role table; cross-organization attempts on every action, always
denied; individual organizations refusing a second member; lockout, unlock and session expiry
(idle and absolute) under `SimClock`; step-up expiry; the bootstrap password failing after its
first use; password change revoking other sessions; a new session id at each login; tokens
rejected when expired, not yet valid, wrongly signed, signed by an unknown `kid`, with
`alg=none`, or HS256 using the public key as the secret; the route-declaration test; no
simulated time passing during a login; the local-only guard, the browser-attack checks and the launcher (section 10); the
compose file publishing only on loopback (CI); the license test;
and a scan, with a planted control, proving no password, token, session id or CSRF token
appears in logs, error bodies, audit entries, the database or test output.

## 12. Docs

This plan; a design note per step; `docs/licenses.md`; and a threat model,
`docs/threat-model.md`, stating what is **not** protected, among it: monitoring and load
control (local only, not authenticated, until A8); no TLS; no multi-factor authentication; no
limits per source address; **the lockout can be abused to lock a known username out**; no
token revocation before expiry (roles and disabling take up to 5 minutes for tokens); secrets
in the process environment; a stolen database gives argon2 hashes, key HMACs and the audit
log; timing side channels not measured; one process per database; **no breached-password
lookup** (checking a password against Have I Been Pwned or similar would send something
derived from it to a third party, so only the bundled blocklist is used); **the audit log is
not tamper-evident** (anyone who can write the database can change it); **the in-memory
login limiter can be flushed** by trying many unique usernames, while the database lockout
still protects real accounts; **the `__Host-` cookie prefix cannot be used** until the server
runs over HTTPS (it requires `Secure`); **a hijacked session can try the password at
step-up**, and so lock the real user out (the per-session cap of 3 failures in 5 minutes
slows that but the account lockout still applies); **step-up does not help against an
attacker who has the password**; a step-up window is a bearer state of the session: whoever
holds the cookie while it is open can use it; **a platform_admin who creates an account knows its
first password** (mitigated: an account a platform_admin creates must change its password at
first login, and the creation is in the audit log, visible to that organization's
org_admin); the measured timing difference between a failed
login for a known and an unknown username (note 27); **two hashing
threads bound login throughput**, so a flood of logins can delay legitimate ones (logins are
limited per username; limits per source address wait for A8). For the local-only endpoints it states what the checks of
section 10 do and do not cover: browser-based attacks (cross-site requests, DNS rebinding) are
handled by the Host allowlist, the Origin check and the content-type rule, but any process
running on the same machine can still read monitoring and use load control; and behind Docker
Desktop or NAT the peer address cannot tell local from remote, so there the only protection is
publishing on `127.0.0.1`.

## 13. Order

Each step is its own pull request and stops for review.

1. Dependencies, `docs/licenses.md` and the license test; the hashing module with both
   runners, the password policy and blocklist; the measured hash time.
2. The local-only guard for monitoring and control (peer address, forwarded headers, Host
   allowlist, Origin, content type), the launcher, the README, Makefile and Dockerfile on the
   launcher, and the CI check that compose publishes only on loopback (section 10).
3. Organizations: migration 4 and the backfill; individual and business rules. Done (note 26).
4. Users (migrations 5 to 7), the one-time bootstrap admin, login, lockout and the
   in-memory limiter, sessions, CSRF, password change, `must_change_password`, the CLI
   (`create-user`, `disable-user`, `enable-user`, `reset-password`, `unlock-user`), and the
   audit log for these, with retention, the unknown-user cap and session pruning. One login
   and a burst of 8 timed through the running server at the default argon2 setting (64 MiB,
   3, 1), with the hardware, in note 24; the known-versus-unknown failure timing and the
   cost of writing `last_seen_at`, in note 27. Done (note 27).
5. (Was 5a.) Done (note 28). Roles: migration 8 and its backfill, `permissions.py`, `authorize`, the
   per-route declarations (`requires` enforcing, `local_only`, `public`), the generated matrix
   and the documented table, and the CLI for roles (`grant-role`, `revoke-role`,
   `list-roles`). No new endpoint.
6. Done (note 29). Step-up, before the management endpoints so they enforce it from the start: migration 9,
   `POST /v1/auth/step-up`, enforcement in `requires`, the per-session cap, the synthetic route
   for every flagged action and the AST check, with their mutations; the audit entries.
7. (Was 5b.) The management endpoints under `/v1/admin/orgs/{org}/...`: users, applications
   and application keys, each declaring and enforcing its permission; the escalation and
   last-admin tests, missing equals forbidden, and the cost of reading assignments.
8. Access tokens, JWKS and `generate-signing-key`.
9. The threat model, the leak scan across everything, and the docs.

## 14. Waiting for A8

What A2 leaves for the A8 step, collected from the sections above:

- **Monitoring and load control with roles** in place of local-only (section 10); `/v1/auth`
  and the management endpoints under `/v1/admin` reachable from other machines.
- **TLS**, and with it the `Secure` session cookie by default and the `__Host-` prefix.
- **Limits per source address** for logins and failed key checks, once there is a client
  address the server can trust.
- **The response-time difference between known and unknown usernames on a failed login.**
  Measured on the running server (`benchmarks/login.py`, three runs of 240 failures of each
  kind, at the default argon2 setting; Intel Core Ultra 7 155H, 22 logical cores, 15.4 GB RAM,
  Windows 11, Python 3.12.10; note 27): a wrong password for a known username took 112.9 to
  117.0 ms at p50 and 115.04 to 117.87 ms on average; an unknown username 110.9 to 112.4 ms
  and 110.92 to 112.56 ms. Known minus unknown: +1.6 to +6.2 ms at p50, +8.2 to +11.4 ms at p90,
  +3.5 to +6.9 ms on average. The cause is the bookkeeping after the verify: a known
  username's failure writes its count and an audit row (two fully synced SQLite writes),
  an unknown one's at most an audit row, and none past the per-minute cap. **Proposed fix:**
  respond first, and do the bookkeeping after the response is sent (a background task that
  writes the failure count and the audit row), so the answer's timing depends only on the
  lookup and the verify. The lockout check reads the count before the verify, so a short
  delay in writing it changes only how quickly a burst of failures locks.
