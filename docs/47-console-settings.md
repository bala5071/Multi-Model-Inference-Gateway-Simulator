# 47. The admin console: settings and organization settings

PR C1 of `docs/settings-and-alerts-plan.md`: the console's **Settings** (per application) and
**Organization settings** screens of the plan's section 16, on the settings endpoints already
merged (notes 40 and 42). Frontend only: no backend change. The alert screens (rules, channels,
incidents, the evaluator banner) are C2.

## Where it lives

A second page of `frontend/`: `console.html`, its code in `src/console/`, built by the same
`npm run build` into `src/gateway/dashboard/` and served by the existing `/dashboard` mount at
**`/dashboard/console.html`**. So it is local only until A8, like the dashboard and every admin
endpoint (note 25), and needs no route of its own. The dashboard is unchanged; the two pages
share the theme hook and the stylesheet's tokens, so both themes apply as they are.

## What it holds, and what it does not

- **It logs in, sends the CSRF token and asks for step-up; it holds no rules of its own.** Who
  may change a setting, what is valid, what is capped and whether a change loosens where prompts
  go are the gateway's answers, shown as they come. The pure transforms (`src/console/derive.ts`)
  only reshape answers for display, each with tests.
- **Who may write is asked of the gateway.** The console has no copy of the permission table.
  On opening an application it dry-runs an empty change (`POST .../settings/validate` with
  `set: {}`), which writes nothing and is not audited: 200 means this account may write, 403
  `forbidden` means it may not, and anything else (a 401, a CSRF refusal, a 503) is taken as "may
  not". Write controls (the edit form, Apply, Revert) are drawn only on a 200: they fail
  closed.
- **The session** is the gateway's HttpOnly, SameSite=Strict cookie (note 27), which no script
  can read. **The CSRF token** comes from the login answer, or from `/v1/auth/me` after a
  reload. It is held in a closure in memory and sent on every change, never on a read, and is
  never written to `localStorage`, `sessionStorage`, IndexedDB, a cookie or the URL.
- **Passwords** (login, step-up, change of password) live in their form field until sent; the
  field is emptied when the request leaves, and the step-up dialog is gone once the gateway has
  accepted it. Nothing keeps a password.
- **No inline script.** The built `console.html` carries a content security policy, added at
  build time (`vite.config.ts`, `CONSOLE_CSP`): `default-src 'none'`, scripts, styles and
  requests from the gateway only, no `unsafe-inline`, `base-uri` and `form-action` `'none'`. It
  is not added in development, where Vite injects inline styles of its own. The only browser
  storage used is the dashboard's theme choice.

## The screens

- **Login**, and **change your password** when the account must (an account the operator or an
  admin creates must choose its own at its first login, note 27).
- **Application settings:** the application chosen from the organization's list, or, for an
  account that may not list them (an application's own admin or viewer), typed by its id.
  - Every setting with its value in force, its source, what capped it, what the application
    stored, and every cap above it.
  - **Edit** (when allowed): each setting kept, set to a JSON value, or unset. Every change is
    sent to the dry run and its answer shown (valid or the refusal, the fields that would change,
    the values that would be in force) before **Save** is offered. When the dry run says the
    change loosens where prompts go, Save asks for the password first. If a write answers
    `step_up_required` anyway (the window ran out, a revert or a preset that loosens), the
    console asks then and tries once more. A version conflict reads the settings again.
  - **Presets** with their exact values, and Apply when allowed. **History** with the fields
    each version changed, who, how and when, and "Revert to the previous version" when allowed.
- **Organization settings:** the organization's caps beside the platform's, the applications
  each cap holds down now, and the history. It shows; it changes nothing. An application's
  admin, who may not read it, is told "Not permitted, or not found."

**"The applications each cap holds down"** are those whose value in force the gateway says came
from the organization (`source: organization`: the cap is the value, nothing lower was set) or
was lowered by it (`capped_by: organization`). Note 39 defines both. The first reading, only
`capped_by`, missed applications that had set nothing, and the e2e test found it.

A refusal is shown in the gateway's words, except `forbidden`, shown as "Not permitted, or not
found." for a missing and a forbidden resource alike (note 28).

## Tests

- **Unit** (vitest): the transforms; the API client (the CSRF header on every change and only
  there, `same-origin`, `no-store`, the error shapes); the dry-run probe and its answers; the
  console against a stand-in gateway: login, a reload through `/v1/auth/me`, a viewer and every
  doubtful probe answer with no write control, step-up asked before saving and after a
  `step_up_required`, cancelling it, and neither the password nor the CSRF token in storage or
  the page.
- **E2e** (`frontend/e2e/console.e2e.ts`, one per screen and flow), against the plain server on
  a database made for the run (`frontend/e2e/console_server.py`), seeded through the operator's
  CLI. `playwright.config.ts` generates the pepper, the vault key and every password at the
  start of the run from the operating system's generator and passes them in the environment
  only. They are never written to a file, printed or committed, and the console project records
  no trace, since a trace would hold the login and step-up bodies. The tests cover:
  - the content security policy and no inline script;
  - a wrong password refused;
  - each account's first login asking for a new password;
  - the application screen end to end (in force and why, a checked edit, a change refused by the
    organization's cap, step-up on loosening, history, revert, a preset);
  - the organization screen;
  - **a viewer sees no write control on either screen**;
  - an application's own admin;
  - **the leak scan**. After a login and a step-up, the password and the CSRF token are in none
    of `localStorage`, `sessionStorage`, IndexedDB, `document.cookie`, any input, the page, the
    URL or the console. The control: the same scan finds the password in the login request's
    body and the token in the login answer.
- **Screenshots** of the application screen (as an admin and as a viewer) and the
  organization screen are taken by those tests, into the run's output folder, only after the
  test finds none of the run's generated secrets in the page's text or fields. The control: a
  password typed into the login form is found.
- **Moved to C2,** with their screens: the test that a webhook URL is never rendered (only its
  origin), and the evaluator's stale and unknown states on every alert screen.

## Not built

- Editing the organization's caps: the plan's section 16 lists the caps, the applications they
  limit and the history for that screen, not an edit form. The CLI (`settings-set
  --organization`) and the endpoint do it.
- The alert screens and the evaluator banner (C2), and a link to the console from the dashboard.
- Remote access: the console is local only until A8.
