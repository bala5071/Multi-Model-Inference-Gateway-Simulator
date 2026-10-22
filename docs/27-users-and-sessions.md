# 27 — Users, login, lockout, sessions, CSRF and the audit of logins

Step 4 of the identity plan (`docs/identity-plan.md`, sections 4, 5 and 9). People can now log
in to a console. There are no roles yet (step 5): a session reaches only login, logout, `/me`,
change-password and the session list, and nothing else in the gateway looks at it.

Code: `src/gateway/users.py` (usernames, users, sessions, their store), `src/gateway/auth.py`
(the account service: creating users, the bootstrap admin, login, lockout, the limiter,
sessions, CSRF, password change, retention), `src/gateway/audit.py`, migrations 5 to 7, the
routes in `src/gateway/app.py`, and the CLI in `scripts/manage.py`. Tests:
`tests/test_accounts.py`, `tests/test_manage_users.py`, `tests/test_secret_leaks.py`.
Benchmarks: `benchmarks/login.py`, `benchmarks/session_checks.py`.

## What this server still is

**Plain HTTP, bound to 127.0.0.1**, and `/v1/auth` is local-only like monitoring (note 25):
logins come only from this machine, never through a proxy, never for a foreign `Host`, never
from another site's page. A password and a session cookie cross the loopback interface in
clear text: anything on this machine that can read that traffic can read them. That is
acceptable for a console on the same machine and for nothing else; TLS and logins from
elsewhere wait for A8.

**What the cookie flags do and do not protect.** The session cookie is
`gw_session=<id>; HttpOnly; Path=/; SameSite=Strict`, plus `; Secure` when
`GATEWAY_COOKIE_SECURE=1` (a test checks the exact string).

- `HttpOnly`: a script in a page cannot read the cookie, so a cross-site scripting bug in a
  page on this origin cannot steal it. It does not stop that script making requests with it,
  nor a process on the machine reading it from the browser's profile or from the traffic.
- `SameSite=Strict`: the browser does not send the cookie on a request started by another
  site, which defeats most cross-site request forgery on its own. It is not relied on alone:
  "site" is not "origin", and a request from another port or subdomain of the same site is
  same-site. That is why there is also a CSRF token, and an Origin check.
- `Secure`: the browser sends the cookie only over HTTPS. Over the plain HTTP this server
  speaks it would not send it at all, so it is off by default; it is for running behind TLS.
- **The `__Host-` prefix cannot be used yet**: it requires `Secure`, so it waits for HTTPS.
- No `Expires` or `Max-Age`: the browser drops the cookie when it closes, and the server
  enforces the idle and absolute expiry itself.

## Users

- **Usernames** are case-folded on input, then must be 3 to 64 of `a-z 0-9 . - _`. Only ASCII
  is accepted, checked before folding, so the Kelvin sign (which folds to `k`) is refused.
- **Creating a user** (CLI only in this step, and the bootstrap): the organization must exist
  and be enabled; `membership_decision` must allow another member, so **an individual
  organization refuses a second member**; the username must be free; the password must pass
  the policy of note 24 with the username. Only the argon2id hash is stored.
- **The CLI** (`create-user [--organization ID] [--platform-admin]`, `disable-user`,
  `enable-user`, `reset-password`, `unlock-user`) reads a password from a prompt, twice, never
  from the command line (a test shows `--password` is refused), and never prints it.
  `reset-password` sets a temporary password that must be changed and revokes every session;
  `disable-user` revokes every session.
- **platform_admin** is the column `users.platform_admin` until step 5 moves it into role
  assignments (plan, section 7: existing users then get no role, except that the sole member
  of an individual organization becomes its org_admin).

## The bootstrap admin

At startup the default server needs a platform_admin.

- If one exists, nothing is created or changed. If `GATEWAY_ADMIN_USERNAME` or
  `GATEWAY_ADMIN_PASSWORD` is still set, a warning names the variables that are set, never
  their values (tested with values that cannot appear by accident).
- If none exists and both are set, the admin is created in `default`, with
  `must_change_password`. Its password must pass the policy, including not containing the
  username, or **the server refuses to start**, naming the rule and never the password.
- If none exists and either is missing, the server refuses to start, naming the variables and
  `manage.py create-user --platform-admin`.
- **The environment password logs in once.** The first successful login marks it used; any
  later login with it is refused (the same answer as every refusal), even before it is
  changed. The session from that login can change it; changing to the same password is
  refused, since that would bring the used password back. If the session is lost,
  `reset-password` recovers the account.
- While `must_change_password` is set, a session reaches `/me`, change-password and logout;
  the session list answers 403 `password_change_required`.

## Login: one answer, the same steps

Every refused login gets **the same response, byte for byte**: 401, `Cache-Control: no-store`,
and `{"error": {"code": "invalid_credentials", "message": "The username or password is not
correct, or the account cannot log in."}}`. It never says the account is locked. A test
compares the raw status, headers and body for an unknown username, a malformed one, a wrong
password, a disabled account, a locked account given the right password, the in-memory limiter
alone, and a used bootstrap password.

**The same steps:** one user lookup (a malformed username looks up a name no user can have),
then one argon2 verify, against the stored hash or a dummy hash made at startup with the
current parameters, through the injected `HashRunner`. Only then is the outcome decided.
`test_every_login_takes_the_same_steps_whatever_is_wrong` records the steps for every refusal
and a success: always `[lookup, verify($argon2id$v=19$m=…,t=…,p=…)]`, with the same
parameters. Changed to refuse an unknown username before the verify, it failed
(`[('lookup', None)]`), while the byte comparison still passed, which is why both exist; with
the change undone it passed and the file was byte-for-byte as before.

**What is not the same: the writes after the decision.** A wrong password for a known username
writes its failure count to `users` and an audit row: two synchronous SQLite writes. An
unknown username's failure is counted in memory and writes at most one audit row, and none
past the per-minute cap. On the running server that difference is visible (below). No
mitigation is built; the threat model records it.

A successful login whose stored hash was made with other argon2 parameters stores the hash
made with the current ones (tested: an older hash verifies, is upgraded, and needs no second
upgrade; the audit entry says `password_rehashed`).

## Lockout and the limiter

- **Lockout, in the database:** 5 wrong passwords within 15 minutes of the first lock the
  account for 15 minutes, on the injected epoch; a restart does not clear it (tested with a
  new service on the same database). While locked even the right password is refused, and the
  attempts do not extend the lock. A wrong current password at change-password counts too.
  `unlock-user` ends a lock early. A success clears the count.
- **The limiter, in memory:** the same rule for every username typed, known or not, keyed by
  an HMAC of the text with the pepper, at most 10,000 names (least recently failed forgotten
  first). So an unknown username "locks" exactly as a real one would, and lockout cannot
  reveal which names exist. It is per process and forgotten on restart.

## Sessions and CSRF

- **A new session at every login**: 256 random bits (43 characters), stored only as SHA-256.
  A session cookie sent with the login is ignored (tested: a planted value never becomes a
  session, and two logins give two ids).
- **Expiry:** 30 minutes idle, 12 hours in all, on the injected epoch (tested under
  `SimClock`). `last_seen_at` is written **at most once a minute** per session, so the idle
  limit ends between 29 and 30 minutes after the last request (tested: twelve requests over
  two minutes write twice).
- **CSRF:** the token is `HMAC-SHA256(session id, "gateway csrf token")`, base64url. Only its
  SHA-256 is stored, so `/me` can give the same token again without storing it, and nothing
  without the session id can compute it (a page cannot read the `HttpOnly` cookie). It is
  required in `X-CSRF-Token` on logout and change-password and compared in constant time;
  missing, wrong or another session's token is 403 `csrf_failed`. The Origin check of note
  25 applies to every state-changing request on `/v1/auth`, login included (tested:
  `https://evil.example` and `null` refused, `http://localhost:8000` accepted; a form body
  415; a rebound `Host` 403).
- **Password change** needs the current password and a different new one that passes the
  policy, and revokes every other session of the user. **Disabling** a user revokes all of
  them; enabling does not bring them back. **Logout** revokes the session and clears the
  cookie.
- **The session list** (`GET /v1/auth/sessions`) shows the user's own live sessions by a
  random `handle`, with their times and which is current; never an id or a digest. There is
  no endpoint to revoke one.

## The audit log

Logins, failures, lockouts, logouts, user creation, disabling and enabling, unlocks, password
changes, failed password changes and resets, and session revocations: who (a user id), when,
what, to what (a user id or a session handle), the outcome.

- **An allowlist**: `details` may hold only `reason` (a fixed set), `sessions_revoked`,
  `failures`, `dropped` (counts), `locked_until_ms`, `minute_start_ms` (numbers),
  `platform_admin`, `password_rehashed` (booleans) and `organization_id` (in id form). Any
  other key, type or value raises before anything is written; ids must be user ids or
  session handles. So a password, session id, CSRF token or hash cannot be stored.
- **An unknown username is never recorded**: its failure is a row with no identifier at all,
  so a password typed into the username box is not kept.
- **A cap**: at most 60 unknown-user rows per minute of epoch time (configurable in code); the
  rest are counted, and the count is written as an `audit_rows_dropped` entry once the minute
  is over, so a flood cannot grow the table without bound.
- **Retention**: entries older than `GATEWAY_AUDIT_RETENTION_DAYS` (default 400) are pruned
  in batches of 1,000 by a task beside the server, which also deletes revoked, expired and
  idle sessions. Nothing on the request path prunes.

## No secret leaks: the scan

`tests/test_secret_leaks.py` runs logins over HTTP (successful, wrong, unknown with a password
typed as the username, oversized, locked out), a CSRF failure, a refused and a successful
password change, the bootstrap admin, logout, and the CLI with its prompt answered. It then
searches every log record (all loggers at DEBUG), every response's status, headers (except
`Set-Cookie`, the cookie's own channel) and body, every audit row, the users and sessions
tables outside their digest columns, the captured standard output and error, and the reprs of
the objects a log line might carry, for every password, session id, CSRF token, password hash
and session digest involved: 17 secrets. It finds none. The CSRF token appears only in
the login and `/me` bodies, which is how it is delivered, and the test checks it is there.
**The control:** a session id then logged at DEBUG and an admin password printed are both
found, exactly where they were put; otherwise the scan proves nothing.

## Measured

**Logins on the running server**: `uv run python -m benchmarks.login`, three runs, on the tree
of the commit that adds this note. It starts `python -m gateway.serve` on a throwaway database
at the default argon2 setting (64 MiB, 3 iterations, 1 lane; 2 hashing threads) and logs in
over HTTP from the same machine. Machine: Intel Core Ultra 7 155H, 22 logical cores, 15.4 GB
RAM, Windows 11 (10.0.26200), Python 3.12.10. Ranges over the three runs; nearest-rank
percentiles; one laptop, one day. One login and the burst of 8 are in note 24.

| Refused login (240 of each per run, interleaved) | p50 | p90 | mean |
|---|---|---|---|
| Known username, wrong password | 112.9 to 117.0 ms | 124.3 to 125.6 ms | 115.04 to 117.87 ms |
| Unknown username | 110.9 to 112.4 ms | 114.2 to 116.1 ms | 110.92 to 112.56 ms |
| Known minus unknown | +1.6 to +6.2 ms | +8.2 to +11.4 ms | +3.5 to +6.9 ms |

**A failed login for a username that exists takes about 3 to 7 ms longer on average, and 8 to
11 ms at the 90th percentile**, against a 110 ms hash. The cause is the writes after the
verify: a known username's failure writes its count to `users` and an audit row (two
synchronous, fully synced SQLite writes; one such write, of a session's `last_seen_at`, is
measured below at about 0.35 ms on average, and more at the tail); an unknown username's failure writes nothing to `users`, and its audit row stops
at the cap of 60 a minute (the runs wrote 350 to 391 audit rows for 510 logins). Someone
timing many failed logins from this machine can tell which usernames exist. Not mitigated
(threat model).

**Session checks**: `uv run python -m benchmarks.session_checks`, three runs, same tree and
machine, 2,000 checks after 20 warm-up checks, SQLite WAL with synchronous FULL:

| | mean | p50 | p99 |
|---|---|---|---|
| Session check, no write | 0.016 to 0.018 ms | 0.016 to 0.017 ms | 0.028 to 0.029 ms |
| Session check that writes `last_seen_at` | 0.394 to 0.403 ms | 0.369 to 0.370 ms | 0.739 to 0.766 ms |
| The write alone | 0.339 to 0.352 ms | 0.313 to 0.317 ms | 0.667 to 0.671 ms |

Writing on every request would make each check over twenty times dearer; at most once a
minute, nearly every check is the first row.

## Threat model additions

For `docs/threat-model.md` (plan section 12, written in step 8):

- **The audit log is not tamper-evident**: anyone who can write the database can change or
  delete entries.
- **No limits per source address**: the server cannot yet trust a client address (A8).
- **The in-memory limiter can be flushed** by failing logins for more than 10,000 unique
  usernames; the database lockout still protects real accounts.
- **Lockout can be abused** to keep a known username locked out.
- **The known-versus-unknown timing difference** above.
- **The `__Host-` cookie prefix** waits for HTTPS.
- **Two hashing threads bound login throughput** (note 24): two hashes of about 110 ms at a
  time, so about 18 logins a second here (computed from the measurement, not measured).
- **The bootstrap password sits in the environment** until it is removed; the server warns
  while it is set.

## Not done here

Roles, `authorize` and management endpoints (step 5), step-up (step 6), access tokens (step
7). No endpoint creates users. No session can be revoked by handle.
