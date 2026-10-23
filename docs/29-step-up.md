# 29 — Step-up

Step 6 of the identity plan (`docs/identity-plan.md`, section 8). A session can prove, with a
fresh password, that its user is present, and actions the permission table flags need that
proof. There are no management endpoints until step 7, so this step builds the mechanism,
proves it on synthetic routes for every flagged action, and arranges that step 7 cannot add a
flagged action without enforcement.

Code: migration 9, `AccountService.step_up`, `check_step_up` and `step_up_until` in
`src/gateway/auth.py`, the route and `requires` in `src/gateway/app.py`. Tests:
`tests/test_step_up.py`, and additions to `tests/test_permissions.py` and
`tests/test_secret_leaks.py`.

## The endpoint

`POST /v1/auth/step-up {"password": ...}`, local-only like the rest of `/v1/auth`.

- **Before any password is verified**, the local-only guard checks the Origin (another site's
  is refused) and the route's dependency checks the session and its CSRF token. A step-up
  without a valid CSRF token or from another site is refused, and a test shows no verify runs.
- Then **the same steps whether it succeeds or fails**: one argon2 verify of the session
  user's password through the injected `HashRunner` (inline on a `SimClock`, so simulated time
  does not move; on the 2-thread pool in the server), then the decision.
- **Every refusal is one answer**: 403, `Cache-Control: no-store`, `{"error": {"code":
  "step_up_failed", "message": "Step-up was refused: the password was not accepted."}}`, for a
  wrong password, a locked account, the in-memory login limiter, the per-session cap, and a
  session whose password must be changed first. A test compares the raw bytes of all five.
- **On success** the session's `step_up_until_ms` is set to now plus 5 minutes
  (`GATEWAY_STEP_UP_MINUTES`, 1 to 60) on the injected epoch, and the answer is
  `{"step_up_until_ms": ...}`. Asking again restarts the window from now. It never changes the
  session's own idle or absolute expiry. `/v1/auth/me` shows `step_up_until_ms`, or null.

## Counting failures

- **A wrong password counts exactly like a failed login**: toward the account lockout in the
  database (5 within 15 minutes lock it for 15) and the in-memory login limiter, under the same
  username. Five wrong step-ups across two sessions lock the account (tested).
- **A per-session cap**: a session may fail 3 step-ups in 5 minutes
  (`GATEWAY_STEP_UP_SESSION_FAILURES`, default 3); after that the endpoint refuses at once, even
  the right password, with the same body, until the oldest failure is 5 minutes old. It is per
  session (another session of the same user is not capped) and in memory.
- **The cap's refusals do not hash, so they are faster** than the refusals that verify, which
  each take one argon2 verify (about 110 ms at the default setting, measured in note 24; the cap's
  refusal itself was not timed). That is
  acceptable: the caller is already authenticated (it has the session and its CSRF token), so
  the timing tells it only that its own session is capped, which it knows from its own
  failures. Nothing about the account or another user is revealed.
- `test_every_step_up_takes_the_same_steps` records one verify, at the current parameters, for
  a success and every refusal that verifies; changed to refuse a locked account before the
  verify, it failed.

## Belonging to one session

The window is a column of one session. It is **never copied, inherited or shared**: a new login
starts without it (tested). It is closed:

- by **logout** (the session is revoked and its window cleared);
- by a **password change**: the other sessions are revoked, and this session's window is
  cleared too;
- when the session is **revoked** any other way (a reset, disabling): a revoked session is
  refused before anything else, and its window is cleared with it;
- when the account **locks**: every window of the user's sessions closes. The sessions
  themselves survive the lockout but cannot open a new window while it lasts;
- when the user is **disabled** (its sessions are revoked as well);
- when the user's **role assignments change** (granted or revoked): every window closes, so the
  new roles are used with a fresh password.

## Enforcement, in `requires`

On every request through `requires(action, resolver)`: the session (401), the CSRF token on a
change, a password that must be changed first, the actor's roles read afresh, the resource,
`authorize` (403 `forbidden`, the same for missing and forbidden), and **only then**, for an
action whose `step_up` flag is set, the session's window: if it is not open, 403
`{"error": {"code": "step_up_required", "message": "This action needs a recent password: POST
/v1/auth/step-up."}}`, one fixed body, before the handler runs. Authorization comes first, so a
user with no role gets `forbidden` with or without a window: step-up never tells a forbidden
resource from a missing one (tested). The window was read with the session, so the check adds
no query. The window is half-open: at 4 minutes 59.999 seconds the action passes, at exactly 5
minutes it is refused (tested under `SimClock`).

The 18 flagged actions are the table's `step_up` column (docs/28): organizations created,
disabled or enabled; users created, disabled, enabled or their password reset; roles assigned
or revoked; application keys issued, rotated or revoked; application roles defined or deleted;
provider keys added, replaced or deleted, and the mode changed (A5).

## It cannot be forgotten

- **Every flagged action, through a real route**: `test_every_flagged_action_needs_step_up_and_
  passes_with_it` reads the table and, for each of the 18, adds a route through `requires` with
  a user the table allows, and shows it answers `step_up_required` without a window, reaches
  the handler with one, and is refused again once the window has passed.
- **No declaration without `requires`**: `local_only_route` raises for a flagged action, and the
  route-declaration test of docs/28 fails on any route declaring a flagged action other than
  through `requires`. `public_route` takes no action at all.
- **No authorization outside `requires`**: an AST check of `app.py` fails on any import of
  `authorize` (also under another name) outside `make_requires`, any use of the name
  `authorize` or of an attribute `.authorize` outside it, and any read of `step_up_until_ms`
  anywhere in `app.py` (routes ask the service: `step_up_until`, `check_step_up`). It is shown
  finding five planted bypasses.
- **What the AST check cannot catch**: aliasing it does not see (`import gateway.permissions as
  p` followed by `getattr(p, "auth" + "orize")`), dynamic calls (`globals()`, `eval`), the
  check being imported in another module and called from there, or a route that declares one
  action and performs another. Review still matters; the checks make the honest path the easy
  one, not every mistake impossible.

**The mutations**, each restored to the exact bytes afterwards (sha256 unchanged), all 49 tests
of `tests/test_step_up.py` and `tests/test_permissions.py` passing again:

| Broken on purpose | Tests that failed |
|---|---|
| `requires` ignores the step-up flag | 6: every flagged action, the 5-minute end, one session's window, logout and password change, lockout and role changes, the audit entry |
| One action (`users.create`) not enforced | 6, the same: the per-action test names `users.create` as the action that passed without a window |
| `authorize` imported into `app.py` outside `make_requires` | 1: the AST check |
| A locked account refused before the verify | 1: the same-steps test |
| No per-session cap | 2: the identical refusals, and the cap test |

## Audit

`step_up_granted` (the window's end), `step_up_failed` (the reason: `wrong_password`, `locked`,
`rate_limited`, `session_cap` or `must_change_password`; and the failure count for a wrong
password) and `step_up_required` (the action's name, which must be in the permission table), on
every refusal. Each names the session by its handle and the user by id; no password, session id
or token. `step_up_required` can be repeated by an authenticated session; every row names that
session's user, and the audit retention keeps the table bounded.

## Consume-on-use (described, not built)

For the most dangerous actions (adding or replacing a provider key, in A5), a window that lasts
5 minutes is more than one change needs. **Consume-on-use** would add a `consume` column to the
table. For an action with `consume=True`, `requires` would become a dependency with a cleanup:
after the handler, if the response is a success (2xx), it closes the session's window (sets
`step_up_until_ms` to NULL), so the next such change needs the password again; a failed call
leaves the window open. Read and write would stay in one place (`requires`), the audit entry for
the action would say the window was consumed, and the matrix and documented table would gain the
column. It is not built now because nothing would use it until A5, and an unused mechanism would
be tested only by synthetic routes. `test_the_table_has_no_consume_column_yet` asserts the
table's fields, so adding the column is a deliberate change.

## Migration 9

`ALTER TABLE sessions ADD COLUMN step_up_until_ms REAL`: no rebuild, so the server applies it
itself (no `manage.py migrate` step needed). The reverse rebuilds `sessions` without the column,
keeping every session and both indexes; no table refers to `sessions`, so foreign keys need no
special handling. Tested: apply, again (nothing), reverse, the rows unchanged.

## Threat model additions

For `docs/threat-model.md` (plan section 12, written in step 9):

- **A hijacked session can try the password** at step-up, and so lock the real user out: each
  wrong attempt counts toward the account lockout. The per-session cap slows it to 3 attempts
  in 5 minutes per session, but a thief with several stolen sessions, or who also tries logins,
  still reaches the lockout.
- **Step-up does not help against an attacker who has the password.** It defends against a
  stolen session (a cookie) used without the password.
- **The window is a bearer state of the session**: whoever holds the cookie while it is open can
  perform flagged actions for up to 5 minutes.
- **The per-session cap is in memory**: a restart forgets it (the database lockout does not).
