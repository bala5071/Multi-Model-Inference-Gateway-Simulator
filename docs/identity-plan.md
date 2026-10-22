# Platform plan, A2: identity and authorization

Status: approved 2026-10-22, with the answers and additions folded in below. No code has
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
| 4 | `organizations (id, name, kind, created_at)`, `kind` being `individual` or `business`; `applications.organization_id`, backfilled: every existing application goes into one organization named `default` with kind `business` |
| 5 | `users (id, username unique, password_hash, organization_id, created_at, disabled, must_change_password, bootstrap, failed_logins, first_failure_at, locked_until)` |
| 6 | `role_assignments (user_id, role, scope_type, scope_id, granted_by, granted_at)`; `app_roles (application_id, name)` for application-defined roles such as `clinic_staff` |
| 7 | `sessions (id_hash, user_id, csrf_hash, created_at, last_seen_at, expires_at, step_up_until, revoked)`; session ids and CSRF tokens are stored only as SHA-256 |
| 8 | `audit_log (seq, at, actor_user_id, action, target_type, target_id, outcome, details)`; `details` is written through an allowlist of fields |

**Organizations.** An `individual` organization has exactly one member, its `org_admin`, and
refuses to add anyone else. A `business` organization has any number of members and allows
every role. Accounts are created by an admin in both cases; there is no self-signup.
Applications belong to one organization. Nothing assumes there is only one organization: the
backfill puts the existing applications into one, which is data, not code.

## 4. Users, passwords and login

- **No self-signup.** A platform_admin, or an org_admin within their own business
  organization, creates accounts (an endpoint, and the CLI).
- **The bootstrap admin.** `GATEWAY_ADMIN_USERNAME` and `GATEWAY_ADMIN_PASSWORD` create the
  first platform_admin when there is none; with neither a platform_admin nor these variables,
  the server refuses to start. The bootstrap password is **a one-time credential**: the
  account is marked `must_change_password`, and the environment value stops working after the
  first successful login (that session can only change the password; if it is lost before
  then, `manage.py reset-password` recovers the account). An existing platform_admin is never
  overwritten from the environment.
- **`must_change_password`.** Until the password is changed, the account can only call
  `GET /v1/auth/me`, change its password and log out; everything else is 403
  `password_change_required`.
- **Password change** requires the current password and revokes every other session of that
  user. **Admin reset** through the CLI sets `must_change_password`.
- **Password policy:** at least 12 characters, at most 1024 (to bound hashing cost), not on the
  bundled blocklist of very common passwords, and no composition rules.
- **Lockout:** 5 failures within 15 minutes locks the account for 15 minutes, on the injected
  clock and epoch, kept in the database so a restart does not clear it. `manage.py unlock-user`
  unlocks one early. Login attempts are also rate-limited per username in memory. Limits per
  source address wait for A8, when there is a trustworthy client address.
- **No enumeration:** an unknown or disabled username runs a dummy verify against a fixed hash
  and gets the same answer through the same steps (a structural test, as for API keys).

## 5. Console sessions and CSRF

- `POST /v1/auth/login` sets `gw_session`: 256 random bits, `HttpOnly`, `SameSite=Strict`,
  `Path=/`, and `Secure` when `GATEWAY_COOKIE_SECURE=1` (off by default, because localhost is
  plain HTTP). Sessions expire after 30 minutes idle and 12 hours in all.
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

- **Role assignments:** `(user, role, scope_type, scope_id)`, scope types `platform`,
  `organization` and `application`.
- **Built-in roles:** `platform_admin` (global); `org_admin` (its organization, and app_admin
  and viewer of every application in it); `app_admin` (its application, and viewer of it);
  `viewer`. A user is assigned roles only within their own organization (platform_admin
  excepted).
- **Application-defined roles** (for example `clinic_staff`) are defined by an app_admin,
  carried in tokens for the application to interpret, and grant nothing inside the gateway.
- **One permission table**, `permissions.py`: each action, the roles and scope relation that
  allow it, and whether it needs step-up. Actions cover users, passwords, roles,
  organizations, applications, application API keys (moving from CLI-only to endpoints),
  application roles, the audit log and usage. It also declares `provider_keys.add`,
  `provider_keys.replace`, `provider_keys.delete` and `mode.change` (step-up; A5 adds the
  endpoints), and `monitoring.*` and `control.*`, marked "local only until A8" (section 10).
- **`authorize(actor, action, resource) -> Decision`**, deny by default. A resource carries its
  application and organization, so a request that crosses organizations is denied whatever
  the role (platform_admin excepted, by design).
- **Every endpoint declares its permission** through a FastAPI dependency. A test walks
  `app.routes` and fails on any route that neither declares one nor appears on the explicit
  public list (generate, which takes an application key; healthz; JWKS; login).
- **The test matrix is generated from the table:** every role, scope relation and action
  through `authorize`, and representative endpoints through HTTP. The role table in the docs
  is parsed by a test and must equal `permissions.py`, so every cell of the documented table
  is tested.

## 8. Step-up

`POST /v1/auth/step-up {password}` marks the session `step_up_until = now + 5 minutes` on the
injected clock. Actions flagged in the table return 403 `step_up_required` without it. In A2:
changing roles, creating or disabling users, and issuing, rotating or revoking application API
keys; provider keys and modes are flagged now and wired in A5. Step-up through a token is not
possible: tokens are for other applications, and sensitive actions need a session.

## 9. Audit log

Logins, failures, lockouts, unlocks, password changes and resets, user creation and disabling,
role changes, key changes, and (from A5) mode changes: who, when, what, and the outcome. Fields
pass through an allowlist, so a password, token, key, session id or CSRF token cannot be
written. platform_admin reads all of it; org_admin reads their organization's.

## 10. Monitoring and load control until A8

They stay without user authentication until the A8 role-based monitoring step. Until then they
are **local only**, enforced in the gateway and not just by how it is started:

- **The socket peer address only.** A request to `/v1/monitor/*`, `/metrics`,
  `/metrics/summary`, `/v1/overload`, `/v1/control/*` or the dashboard is refused (403
  `local_only`) unless its peer address is loopback. Forwarded headers are never trusted:
  uvicorn, which by default rewrites the client address from `X-Forwarded-For` when the peer
  is `127.0.0.1`, is run with proxy headers off, and a request that carries any forwarding
  header (`Forwarded`, `X-Forwarded-For`, `X-Real-IP`) is refused on these endpoints, because
  its client address may have been rewritten.
- **An opt-in for the Docker bridge network:** `GATEWAY_LOCAL_NETWORKS` (for example
  `172.16.0.0/12`) adds networks counted as local. `docker-compose.yml` sets it, because the
  host's requests reach the container from the bridge gateway, and the compose file publishes
  on `127.0.0.1` only.
- **Startup check:** a small launcher, `python -m gateway.serve`, starts uvicorn with proxy
  headers off and **refuses to bind a non-loopback address** while these endpoints are
  unauthenticated, unless `GATEWAY_ALLOW_NETWORK_BIND=1` (set in the Docker image, which must
  bind `0.0.0.0` inside the container), in which case it prints a warning naming the open
  endpoints. The README, the Makefile and the Dockerfile switch to the launcher.
- **Both are tested:** loopback allowed, a non-loopback peer refused, forwarding headers
  refused, the Docker network allowed only when opted in, the launcher refusing and warning;
  and CI's docker job still serves the dashboard through the published port.
- Load control stays off by default.

## 11. Tests

Every cell of the documented role table; cross-organization attempts on every action, always
denied; individual organizations refusing a second member; lockout, unlock and session expiry
(idle and absolute) under `SimClock`; step-up expiry; the bootstrap password failing after its
first use; password change revoking other sessions; a new session id at each login; tokens
rejected when expired, not yet valid, wrongly signed, signed by an unknown `kid`, with
`alg=none`, or HS256 using the public key as the secret; the route-declaration test; no
simulated time passing during a login; the local-only guard and the launcher; the license test;
and a scan, with a planted control, proving no password, token, session id or CSRF token
appears in logs, error bodies, audit entries, the database or test output.

## 12. Docs

This plan; a design note per step; `docs/licenses.md`; and a threat model,
`docs/threat-model.md`, stating what is **not** protected, among it: monitoring and load
control (local only, not authenticated, until A8); no TLS; no multi-factor authentication; no
limits per source address; **the lockout can be abused to lock a known username out**; no
token revocation before expiry (roles and disabling take up to 5 minutes for tokens); secrets
in the process environment; a stolen database gives argon2 hashes, key HMACs and the audit
log; timing side channels not measured; one process per database; no breached-password check
beyond the bundled blocklist.

## 13. Order

Each step is its own pull request and stops for review.

1. Dependencies, `docs/licenses.md` and the license test; the hashing module with both
   runners, the password policy and blocklist; the measured hash time.
2. The local-only guard for monitoring and control, and the launcher (section 10).
3. Organizations: migration 4 and the backfill; individual and business rules.
4. Users, the one-time bootstrap admin, login, lockout, sessions, CSRF, password change,
   `must_change_password`, the CLI (`create-user`, `reset-password`, `unlock-user`), and the
   audit log for these.
5. Roles, `permissions.py`, `authorize`, the per-route declarations, the management endpoints
   (users, roles, organizations, applications, application keys, application roles, audit)
   and the generated matrix.
6. Step-up.
7. Access tokens, JWKS and `generate-signing-key`.
8. The threat model, the leak scan across everything, and the docs.
