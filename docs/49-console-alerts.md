# 49. The admin console: alert rules, channels, incidents

PR C2 of `docs/settings-and-alerts-plan.md`: the console's **Alert rules**, **Channels** (email
channels included) and **Incidents** screens of the plan's section 16, and the evaluator banner,
on the endpoints already merged (notes 44 to 46 and 48). Frontend only: no backend code change.
Tests were added on the gateway's side to pin what the console relies on
(`tests/test_console_probes.py`). The console's rules from note 47 hold unchanged.

## Who may write, asked of the gateway

As in C1, a write control is drawn only when the gateway has said this account may make the
write, and any doubt shows none.

- **Where a write has a dry run, the console asks with it.** It sends a body the route's own
  model refuses (a JSON array), which the gateway checks only after `requires` has checked who may
  write. That answers 422 `invalid_request` to someone allowed and 403 `forbidden` to anyone else
  (the same bytes as a missing resource), and writes and audits nothing. Two writes are asked this
  way: an alert rule's (`POST .../alert-rules`, `alert_rules.write`) and a recipient's code
  (`POST .../recipients/rcp_probe/confirm`, `alert_channels.confirm_recipient`). The confirm
  route resolves only the channel, and a refused body counts no wrong entry: seven probes leave
  the code valid.
- **Where a write has none, its control follows an answer of the same role row.**
  - `incidents.acknowledge` takes no body: Acknowledge follows the rule probe on the incident's
    application.
  - `alert_channels.disable` and `.test` take no body.
  - `alert_channels.write` has no dry run either. Without an open step-up window its probe is
    refused `step_up_required`, and every step-up refusal is an audit row. A test shows the row.
  - So every channel control (create, add a recipient, Disable, Delete, Remove a recipient, Test
    send) follows the channel list's own answer (`alert_channels.read`), which writes and audits
    nothing.

  `tests/test_console_probes.py` pins each pair's rows equal in `PERMISSIONS` (the same level,
  the same roles at the same relations). A change to the matrix that parts them fails CI rather
  than showing a control the gateway would refuse. The gateway still checks every write.

## The screens

- **Alert rules** (per application, chosen or typed as on the settings screen).
  - Each rule shows its condition, severity, channels by name, its state and its last value.
    The state is its open incident's, else "not firing" or "disabled". The last value is the last
    complete minute's from the gateway's own preview of the rule (`validate`), or "too few to
    judge", or the open incident's value for a signal the preview does not cover.
  - Defaults are marked, and changed defaults marked as changed (the gateway's
    `changed_from_default`).
  - When allowed: the editor (each field JSON, every change sent to `validate` and its answer
    shown before Save), Copy (a new rule from this one), Enable or Disable, Delete, and Restore
    default rules. A version conflict reads the rules again.
- **The preview.** Section 16's example reads "would have fired at 10:14 and resolved at 10:31".
  The preview gives, per minute, the value and whether the condition held. Firing also depends
  on `for`, cooldown and the incident's state, which the evaluator decides and the console must
  not. So the editor says when the condition **held** ("from 10:14 UTC to 10:32 UTC, 18
  minutes"), how many minutes had too few requests to judge, and that firing depends on the
  rest.
- **Channels** (the organization's).
  - **The list** with health: last delivered, last failed, failures in a row. A webhook's target
    is its origin, the only part the gateway sends; an email channel's is its recipients counted
    by state.
  - **Create.** The URL is in its field until the gateway takes it, then emptied. A webhook's
    signing key is shown once, with a copy button and a warning, held in component state only
    and forgotten at "I have stored it" or on leaving the screen. An email channel's notice gives
    each recipient's confirmation outcome as the gateway answered it.
  - **A channel's view:** test send and its outcome, Disable, and Delete (asked twice).
  - **An email channel's recipients:**
    - each address with its state, added and verified times, and how long its code is valid;
    - the code entry and re-send, on the confirm probe; the code is in its field until sent,
      then emptied, and kept nowhere;
    - Remove, and Add a recipient (step-up);
    - its deliveries.
  - Addresses are only in the channel endpoints' answers, which only the organization's admin
    may read (`alert_channels.read`). The incident and application channel views carry channel
    ids and names only.
- **Incidents:** the organization's, filtered by application and state as the gateway filters
  them. An application's own admin, who may not read the organization's, names its application.
  Each incident opens to its timeline (opened, pending, fired, acknowledged and by whom, no data,
  resolved and why) and its notifications with each delivery's channel, state, attempts and last
  code, and Acknowledge on a firing incident when allowed. An incident of the built-in
  `channel_delivery_failing` has no application (note 46): listed, not opened, since the detail
  route is per application.
- **The evaluator banner,** on every alert page, never on the settings screens.
  - It reads `/healthz` when the page opens and every 30 s while it stays open.
  - It shows while `alert_evaluator` is `stale` or `unknown`, and also when `/healthz` cannot be
    read or gives a word the console does not know ("unreadable": shown, never hidden).
  - `ok` and `disabled` show nothing.
  - `/healthz` carries the word only (note 45), so the banner names the state without the
    plan's "since 10:14".

Step-up is asked wherever the gateway answers `step_up_required`: creating a channel, adding a
recipient. The console retries once after the password is accepted, as in C1.

## Tests

- **Gateway side** (`tests/test_console_probes.py`):
  - each probe's answers for org_admin, app_admin, viewer, no role and another organization;
  - nothing written and nothing audited (every alert table and the audit log counted);
  - the code still valid after seven probes;
  - the channel-write probe's audit row;
  - the four sibling pairs pinned equal.
- **Unit** (vitest):
  - the transforms (`src/console/alerts.ts`) and the probe (`probeRefusedBody`: allowed only on
    422 `invalid_request`, forbidden only on 403 `forbidden`, otherwise unknown, an accepted
    array included);
  - against a stand-in gateway: the banner on each alert page for `stale`, `unknown` and an
    unreadable `/healthz`, none when `ok`;
  - no rule control on any doubtful probe answer;
  - Acknowledge only on a firing incident and the rule probe's yes;
  - nothing of channels when the list is refused;
  - a planted webhook URL sent once and in no field, the page or the browser's storage, its key
    shown once, copied, then gone;
  - a planted code sent once and kept nowhere;
  - no code entry when the confirm probe is refused.
- **E2e** (`frontend/e2e/console_alerts.e2e.ts`, project `console-alerts`, after C1's project on
  the same server). The seed's API key of `app-one` expires in 3 days, so the default rule "Key
  expires within 14 days" fires on the evaluator's first cycle, a real incident. Email goes to the
  suite's test SMTP server over STARTTLS, and the code is read from its mailbox by the test, never
  through the browser (decision of 2026-11-09, the plan's Part F). The tests:
  - the banner on each alert page (the gateway's `ok`, then `stale`, `unknown`, an unknown word
    and no answer, as `/healthz` would answer them);
  - the rules screen end to end (state, last value, defaults, a checked edit with its preview,
    refusals, copy, enable, delete, restore);
  - channels (a webhook's origin and its key once; an email recipient verified with the mailed
    code after a wrong one; a second added, sent its code again and removed; test send to the
    verified recipient only; disable, delete);
  - incidents (the list, the timeline and notifications, Acknowledge, the state filter);
  - a viewer (no write control anywhere, no channel, no recipient address in the page or the
    browser's console);
  - an application's own admin;
  - **the leak scan:** a planted webhook URL, the signing key, the code (both its shown and bare
    forms) and a recipient's address are in none of `localStorage`, `sessionStorage`,
    IndexedDB, `document.cookie`, any field, the URL or the console. The URL, key and code are in
    the page neither. The controls: the URL and the code are found in the requests sent, the key
    in the gateway's answer, the address in the admin's own page.
- **Screenshots** of each new screen as an admin and as a viewer
  (`docs/pr-c2-screenshots/`), taken by those tests only after none of the run's generated
  secrets, signing keys or codes is found in the page's text or fields. No screenshot is taken
  while a signing key is shown.
- **Mutations:** the plan names none for the console. C2 makes one for each rule it has about who
  may do what and where a secret may go, listed in the PR.

## Not built

- Changing a channel (its name, URL, default flag), re-enabling a disabled one, and rotating a
  signing key: section 16 lists none of them for the console; the endpoints do them.
- The banner's time ("since 10:14"): `/healthz` has none, and the monitoring snapshot that has
  the age is not read by the console.
- An incident of the built-in rule opened in detail: no route serves it.
- Remote access: the console is local only until A8.
