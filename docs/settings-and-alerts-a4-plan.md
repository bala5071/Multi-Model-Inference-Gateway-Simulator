# A4 plan: webhook channels and their delivery

The plan for PR A4 of `docs/settings-and-alerts-plan.md` (the main plan), written before any
code, for approval. A4 adds outbound connections and stored secrets, so plan approval comes
first, and any conflict with the main plan is decided by the reviewer.

**Status:** proposed 2026-11-05. The reviewer's decisions of 2026-11-08 are applied
throughout (section 1), and section 4.2 checks the webhook path against the reviewer's list.
**Approved 2026-11-08** (#38 merged), with the additions of section 21; built in the two groups
of section 22.

**Scope.** Part F's A4 row, the four items moved to A4 when A2 started, and nothing from A5 or
later:

- webhook channels;
- the vault's channel scope and the signing key;
- `webhook_client`, with the connect-time check and no redirects;
- the outbox, retries, the caps and the suppressed summary;
- test send and delivery visibility;
- `channel_delivery_failing`;
- the leak scans of A4's secrets and of the alert contents;
- moved from A2: channel id validation on rules (M15), `GET .../alert-channels`,
  `channel_delivery_failing`, and the `rule_weakened` notice (M29).

**Order.** #37 (the evaluator's pages, time box and own pool) is merged. This plan merges next,
then A4 is built on a branch from `main`.

## 1. Conflicts with the main plan, as decided (2026-11-08)

| # | Conflict | Decision | Where it went |
|---|---|---|---|
| 1 | SMTP and the platform scope: A4 or A5? | **SMTP stays in A5; A4 is webhooks only** | Scope; section 20 |
| 2 | `channel_delivery_failing` is per organization, but rules and incidents belong to an application (`NOT NULL` in migrations 15 and 16) | **Built in code;** its incidents are stored under an empty application id and keep the organization id. Cross-organization tests and mutations. Application-scoped endpoints never return them. Who may see them is stated. | Section 8.1; tests; mutations CDF-1 to CDF-4 |
| 3 | Section 19's channel-secrets scan plants A5's secrets | **The A4 scan covers A4's secrets and the alert contents only** | Section 15 |
| 4 | Section 11.4 put the evaluator on the request pool, and delivery needed a pool | **Delivery shares the evaluator's pool, raised to 3 connections.** No connection is held during a network call. A test with a webhook that never answers, and a mutation that holds the connection. | Section 5; tests; mutation POOL-HOLD |
| 5 | "The rule's channels as they were before the change" cannot be read once the rule row changed | **An incident's first channels are stored as ids only, and cannot be changed** | Section 5; mutations A4-7 and A4-8 |

Also decided:

- **The caps:** the plan's numbers are the defaults. The operator can set each one within a
  range (section 7).
- **Re-enabling a channel needs step-up** (section 9).

## 2. Channels

- **The table** (`alert_channels`, migration 17; Postgres schema 6), owned by an organization.
  Its columns:
  - `id` (`chn_` and 16 hex characters) and `organization_id`;
  - `name` (the rule-name charset), unique within the organization;
  - `type` (`webhook` only in A4, `CHECK`), `enabled`, `is_default`;
  - `url_origin`, the only part of the URL ever shown;
  - `version`, `created_at_ms`, `updated_at_ms`;
  - health: `last_delivered_at_ms`, `last_failed_at_ms`, `consecutive_failures`.
- **At most 20 channels an organization,** counted under the global write lock, as the rule
  cap of 50 is (A2).
- **The URL** is checked when stored (section 4.1). It is sealed in the vault (section 3). The
  answer, the audit row, every log line and every event carry only `url_origin`.
- **An application's view** (`GET /applications/{app}/alert-channels`, under
  `alert_rules.read`): ids, names, types, `is_default` and `enabled`. Never a target or its
  origin.
- **A rule's `channels`:** `[]`, `["organization defaults"]`, or up to 5 channel ids of the
  rule's own organization.
  - They are checked at write by `RuleService`, and again when a notification is queued.
  - A channel id of another organization gets the same 422 `invalid_request` body as one
    that does not exist (M15).
  - Default rules keep `["organization defaults"]`.

## 3. Secrets: the vault's channel scope and the signing key

- **`ChannelBinding`** (organization, channel id, kind, URL origin), beside today's provider
  `Binding`, which is unchanged: provider rows are not resealed.
  - Its associated data is `vault-channel-v1|organization|channel|kind|origin|key_id`, under
    the master key (section 9.1 of the main plan).
  - The kinds are `webhook_url`, `webhook_signing_key` and `webhook_signing_key_previous`.
  - The first field names the scope, so a channel ciphertext never opens as a provider key,
    nor the reverse. This is tested both ways.
- **`channel_secrets`** (channel id, kind, nonce, ciphertext, key id, and `expires_at_ms` for
  the previous key) is read only by the channel store. A test greps every store's SQL.
- **The signing key** is 32 bytes from the injected `random_bytes`.
  - It is returned once, with `Cache-Control: no-store`, in the answer that creates the
    channel or rotates the key. It is never sent to the receiver.
  - **Rotation:** the previous key is kept for 24 hours (store clock), and every delivery in
    that window is signed with both keys.
  - `rotate-vault-key` reseals channel rows too. A secret whose master is not loaded fails
    its deliveries with `channel_secret_unreadable`.
- **`Secret.reveal()`** gains exactly one call site, `alerts/webhook.py`'s `build_request`
  (M36). Leased values go to the `Redactor`, as provider keys do.

## 4. The webhook request and its connection rules

- **The request:**
  - `POST`, `Content-Type: application/json`, and the body `json-v1` of section 10, built and
    stored at queue time (section 6);
  - `X-Gateway-Delivery`, the notification's id (receivers deduplicate on it);
  - `X-Gateway-Timestamp`, Unix seconds at this attempt;
  - `X-Gateway-Signature: v1=<hex HMAC-SHA256(key, timestamp + "." + body)>`, one `v1=` per
    key during a rotation.
- **`webhook_client()` in `egress.py`** is the only way a webhook is sent. An AST test fails on
  any other `httpx` client construction in the alert modules. It runs under
  `egress_scope(always_hosted)` (decision 24 of the main plan).
- **Testing without a listener.** The tests inject the resolver and the inner network
  (`mock_network`'s `FakeResolver` and `ScriptedNetwork`, as the provider tests do). The
  injected backend records every `connect_tcp` and its address. No local listener is needed,
  and no guard on a production path is relaxed for the tests.

### 4.1 The URL, when it is stored

These checks give a quick field error; the connect-time check of 4.2 is the one relied on.
`check_webhook_url` refuses, and the tests cover each case:

- a scheme other than `https`;
- user info, a fragment, or a URL over 2,048 characters;
- any control character, space, CR, LF, tab, or backslash anywhere in it;
- a host that does not encode as IDNA;
- a host that is a literal non-public address, `localhost`, or a name ending `.localhost`,
  `.local` or `.internal`;
- a host in a numeric form other than the canonical dotted quad (`2130706433`, `0177.0.0.1`,
  `127.1`, `0x7f.1`), which resolvers read as addresses;
- a port not allowed (4.2).

A query string is allowed, since receivers often carry their token in it. It is part of the
sealed secret, like the path.

### 4.2 The reviewer's checklist (2026-11-08)

"Already" means the plan as first proposed (2026-11-05) said it; "added" means it is new
here. Each item names the test that shows it.

| Item | Covered | What the plan says | Tested by |
|---|---|---|---|
| The address checked on every connection | Already | `GuardedBackend` resolves the name itself on every connection (test send, delivery, retry), checks every resolved address with `refusal`, refuses if any is refused, and opens the socket to the checked address. `NO_LOCAL_DESTINATIONS`: the operator's local allow-list never applies. | rebinding (public at store, private at delivery: no socket opened); one public and one private address; the socket address recorded (M17, M18, M44) |
| A fresh connection per delivery | Already | A new client per delivery, no pool kept, so every delivery resolves and checks again | a second delivery resolves again (M45) |
| No redirects | Already | `follow_redirects=False`. A 3xx is a permanent `http_3xx`; its `Location` is not fetched, stored or logged. | 301, 302, 307 and 308, to private and public addresses, no second request (M19) |
| Proxy settings ignored | Already | `trust_env=False` | `HTTPS_PROXY`, `HTTP_PROXY` and `ALL_PROXY` set and ignored (M46) |
| Port rules | **Added** | Only 443 by default. The operator may allow more with `GATEWAY_ALERT_WEBHOOK_PORTS` (a list, each 1024 to 65535; anything else refuses the start). Checked when the URL is stored and again in `GuardedBackend` at connect, from the parsed URL's effective port. | 80, 22, 25, 6379, 0 and 65536 refused at store and at connect; a port the operator allowed accepted (A4-9) |
| TLS verification always on | Partly: "verified with the pinned context" | **Added:** `provider_tls_context()` with `CERT_REQUIRED`, host name checked, TLS 1.2 at least. No setting, environment variable or argument turns it off (`trust_env=False`, so `SSL_CERT_FILE` cannot either). Plain http is refused before any connection. A TLS failure is permanent. | the context's settings asserted; an AST test that the webhook client takes only that context; a scripted handshake failure gives permanent `tls` (A4-10) |
| A timeout on each stage | Partly: 5 s connect, 10 s in all | **Added:** name resolution 5 s; connect 5 s; TLS handshake 5 s; write 5 s; read 5 s per read; and the whole attempt 10 s, all on the injected clock. Each expiry gives `timeout`, retried. | a scripted network that hangs at each stage in turn (resolve, connect, handshake, write, first byte, mid-body), each ending at its own bound (A4-11) |
| A maximum payload size | **Added** | The `json-v1` body is at most 8 KiB, checked when it is built at queue time. Names are already bounded (rule 64, application 100), so a real payload is about 1 KiB. One over the limit is recorded as `failed` with `payload_too_large`, never cut short. The answer read is at most 4 KiB. | a payload at 8 KiB and one byte over; an answer of 1 MiB read only to 4 KiB (A4-12) |
| The response body never stored or logged | Already | At most 4 KiB read, then discarded. Only the status code is kept, as the delivery's code. | the leak scan plants a marker in the receiver's answer; it appears nowhere |
| Webhook URLs never returned or logged | Already, with an addition | Only `url_origin` is shown. **Added:** errors are mapped to the fixed codes and never formatted from an exception (`httpx`'s messages carry the URL). The delivery row, events and logs hold the code alone. | the leak scan of a failing, a timed-out and a redirected delivery (A4-5, A4-13) |
| Signature with timestamp, and replay protection | Partly: signature over timestamp and body | **Added:** every attempt is signed afresh with its own timestamp, and the delivery id stays the same across retries. The receiver checks the signature, refuses a timestamp more than 5 minutes old, and drops a delivery id it has seen; note 46 gives a verification recipe with these three steps. The gateway cannot enforce the receiver's side. What it guarantees is that no valid signature exists without the key, and that an old request cannot be given a new timestamp. | an independent verifier in the test; a retry carries a new timestamp and a valid signature; a changed timestamp or body fails verification (M32, A4-14) |
| Signing-key rotation with an overlap window | Already | The previous key is kept 24 hours, and both sign meanwhile | both `v1=` values during the window; one after it (A4-3) |
| Retry with backoff and a maximum of attempts | Already | 30 s, 2 min, 8 min, 30 min and 2 h, with up to 10% seeded jitter; `Retry-After` honoured up to an hour; six tries in all. Any other 4xx, an egress refusal, a 3xx or a TLS failure is permanent. | the schedule on the injected clock; no seventh try; no retry on permanent failures (M33) |
| Header and body injection | **Added** | Headers are a fixed set whose values the gateway makes (ids, digits, hex); nothing from a user goes into a header. User input reaches the request only as the URL (4.1: no control characters or whitespace) and inside the JSON body, written by `json.dumps(..., ensure_ascii=True)` from `AlertPayload`, never by string formatting. Names already refuse control and zero-width characters. | a URL with CR LF, a tab or a space refused at store; a rule or application name with `"`, `\`, `}` and Unicode round-trips as JSON with the headers unchanged (A4-15) |
| IPv6 and mapped addresses | Partly: "embedded IPv4 in IPv6" | **Added: the cases, each refused at store (literal) and at connect (resolved):** `::1`, `::`, `fe80::/10` (with a zone id such as `%eth0`), `fc00::/7`, `ff00::/8`, `::ffff:127.0.0.1`, `::ffff:169.254.169.254`, `64:ff9b::a9fe:a9fe` (NAT64 of the metadata address), `2002:7f00:1::` (6to4 of loopback), Teredo, `fd00:ec2::254`. Also an IPv6 literal in brackets, and an AAAA record private while the A record is public. `refusal` and `embedded_ipv4` in `egress.py` already handle each; A4 tests them through the webhook path. | the table of cases, through `check_webhook_url` and through `GuardedBackend` (M44) |

### 4.3 Retries and codes

The failure codes are:

- `egress_refused`, `port_refused`, `dns_failure`, `timeout` and `connect_failed`;
- `tls`, `http_3xx`, `http_4xx` and `http_5xx`;
- `payload_too_large`, `channel_secret_unreadable` and `channel_gone`.

Retried: a connection failure, a timeout, `dns_failure`, a 5xx, a 408 and a 429. Everything
else is permanent.

## 5. The outbox and delivery

- **The outbox** (`notification_deliveries`): one row per (notification, channel). Its
  columns:
  - `id` (`dlv_`), `notification_id`, `channel_id`, `organization_id`;
  - `state` (`queued`, `sending`, `delivered`, `failed`, `suppressed`; `CHECK`);
  - `attempts`, `next_attempt_at_ms`, `claimed_by`, `claim_expires_at_ms`, `last_code`;
  - `payload` (the stored `json-v1`), `created_at_ms`, `updated_at_ms`.

  It has a unique index on (notification, channel), and an index on (`state`,
  `next_attempt_at_ms`).
- **Queued in the transition's transaction** (M22). A3's `Change` gains its deliveries, and
  the caps are counted in the same transaction (section 7). A rolled-back transition queues
  nothing; a transition made twice queues once.
- **An incident's first channels** (decision 5, as decided):
  - when an incident first notifies, the channel ids it is sent to are stored on it, in
    `incidents.notify_channels`;
  - they are stored as ids only, never a name, URL or origin;
  - they are never changed afterwards: no store method writes the column except the insert
    of the first notification. The compare-and-set update leaves it out, and a test updates
    the rule's channels, then checks the column and where the later notifications went;
  - its later notifications (`resolved`, reminders, `firing_again`) go to those ids that
    still exist in the organization;
  - a reopened incident keeps them.
- **Held notifications** (quiet hours) get no delivery. Their summary does.
- **The delivery worker, on every instance:**
  - It claims due rows. On Postgres the claim is `FOR UPDATE SKIP LOCKED` with a 60 s claim;
    on SQLite it is the one process. At most 4 deliveries run at once per instance.
  - It records the outcome with a compare-and-set on its claim, so a worker whose claim was
    retaken writes nothing.
  - A dead instance's claim expires and is taken again (at least once).
  - The outcome updates the channel's health in the same transaction.
- **The pool, as decided:** on Postgres, delivery shares #37's evaluator pool, raised from 2
  connections to 3. The rule: **no connection is held during a network call.**
  - A delivery makes three short transactions: the claim; the read of its secrets and payload
    (decrypted after the connection is given back); and the outcome. The HTTP request runs
    between them, with no store connection held.
  - `alerts/delivery.py` enforces this with a guard: entering the HTTP call while the task
    holds a transaction raises.
  - **The test:** a webhook that never answers (the scripted network hangs after accepting).
    With 4 such deliveries running on one instance and the pool at 3, the evaluator's cycle
    completes, and a fifth delivery's claim and outcome go through.
  - **The mutation (POOL-HOLD):** the HTTP call made inside the claim's transaction fails
    that test.
  - On SQLite the worker gives the event loop back between deliveries.

## 6. What a notification carries

- **`AlertPayload`:** a frozen dataclass with exactly the fields of section 10's `json-v1`,
  like `AuditEvent`. It is built when the delivery is queued and stored as built, and a
  delivery sends exactly what was stored. At most 8 KiB (4.2).
- **The kinds:** `firing`, `firing_again`, `reminder`, `resolved`, `quiet_hours_summary`,
  `rule_weakened`, `channel_removed`, `suppressed_summary`, `test`.
- **Never in it:** prompt or response text, token ids or hashed tokens, API keys or their
  HMACs, provider keys, webhook URLs or their origins, signing keys, provider base URLs,
  usernames, email addresses, IP addresses, session ids, the user who acknowledged, or
  dollars.

## 7. The caps and the suppressed summary

**The numbers.** The plan's are the defaults; the operator sets each one within a range, in
the environment. A value outside its range, or not a whole number, refuses the start, naming
the variable.

| Cap | Variable | Default | Range |
|---|---|---|---|
| Per rule, an hour | `GATEWAY_ALERT_CAP_PER_RULE` | 6 | 1 to 60 |
| Per organization, an hour | `GATEWAY_ALERT_CAP_PER_ORGANIZATION` | 60 | 1 to 600 |
| Test sends per channel, an hour | `GATEWAY_ALERT_TEST_SENDS_PER_CHANNEL` | 10 | 1 to 60 |
| Test sends per organization, an hour | `GATEWAY_ALERT_TEST_SENDS_PER_ORGANIZATION` | 30 | 1 to 300 |

- **Notifications are counted, not deliveries:** one to three channels counts once.
- **The count** is one statement, `INSERT ... ON CONFLICT DO UPDATE SET sent = sent + 1 WHERE
  sent < cap RETURNING`, in the outbox insert's transaction, with the hour from the store
  clock (M25).
- **Over a cap,** the delivery is queued as `suppressed`, with `rule_cap` or
  `organization_cap`.
- **The summary** is queued by the evaluator's holder in its first cycle after the hour ends.
  There is one per organization per hour (a unique index, M26). It goes to the default
  channels and does not count against the caps.
- **Test sends** are counted in the window counters (Valkey or memory); a counter that cannot
  answer refuses.
- **Instances must agree:** each instance hashes its four values into the ceilings row of the
  main plan's decision 19, so a mismatch between instances is reported.

## 8. The four items moved from A2

- **Channel ids on rules** (M15): section 2.
- **`GET /applications/{app}/alert-channels`:** section 2.
- **`rule_weakened`** (M29):
  - each rule field has a direction, like settings: a higher threshold for `>`, a longer
    window or `for`, quiet hours added or widened, a channel removed, a lower severity,
    disabled, deleted;
  - a write that makes an enabled rule fire less queues the notice in `RuleService`'s write
    transaction;
  - the notice goes to the rule's channels as they were before the write, naming the rule and
    the fields, never the user.
- **`channel_removed`:** disabling or deleting a channel queues a notice to the organization's
  other default channels.

### 8.1 `channel_delivery_failing`, as decided

- **Built in code,** not a rule row: one per organization, always on, which cannot be changed,
  disabled or deleted. It has one series per channel and fires at `consecutive_failures >= 3`
  (warning). It sends to every default channel except the failing one, and resolves at that
  channel's next delivered notification or when the channel is deleted.
- **Its incidents** are rows of `incidents` with `application_id = ''` and the organization's
  real `organization_id`, under a fixed rule id per organization, `rul_cdf_` followed by a
  hash of the organization id. No application can own these rows, because no application id
  is empty:
  - the management path makes ids as `app_` and 16 hex characters;
  - an empty `{app}` path segment matches no route;
  - **added in A4:** every store's `add_application` refuses an empty id, on memory, SQLite
    and Postgres, with a test. There is no `CHECK` on `applications.id` today, and adding one
    on SQLite would rebuild the table, so the refusal is in the stores.
- **Who may see them:** the organization's org_admins and the organization's viewers, through
  `GET /v1/admin/orgs/{org}/incidents` (`incidents.read_organization`), and nobody else.
  - An app_admin sees them only if they also hold an organization role.
  - A platform_admin gets nothing inside an organization, as for every incident.
  - The application-scoped endpoints (`GET /applications/{app}/incidents`,
    `GET .../incidents/{incident}`, `POST .../acknowledge`) never return them: the resolvers
    match `application_id` to the path's application, which is never empty.
  - So they cannot be acknowledged: acknowledging is an application action.
  - The organization list shows them with `application: null` and the channel id as series.
- **Tests:**
  - north's built-in incident absent from south's organization list (the same bytes as an
    empty page) and from every north application's lists;
  - each application-scoped endpoint, with the incident's id, answers as missing (the same 403
    bytes as an id that does not exist);
  - an org_admin of north sees it, a viewer of north sees it, an app_admin of north without an
    organization role does not;
  - the evaluator never makes one for an organization from another's channel.
- **Mutations:**
  - **CDF-1:** the organization list without its `organization_id` filter fails the
    cross-organization test;
  - **CDF-2:** the application resolver accepting an empty application id fails the
    application-endpoint tests;
  - **CDF-3:** the built-in sending to the failing channel too fails its delivery test;
  - **CDF-4:** the series keyed by channel id across organizations (the organization left out
    of the rule id) fails the two-organization test.

## 9. Endpoints

All under `/v1/admin/orgs/{org}`, local only until A8, `Cache-Control: no-store`, with CSRF and
the Origin check on writes, and paginated as in note 30.

| Method and path | Action | Step-up | Success |
|---|---|---|---|
| `GET /applications/{app}/alert-channels` | `alert_rules.read` | | 200 |
| `GET /alert-channels` | `alert_channels.read` | | 200 |
| `POST /alert-channels` (`type`, `name`, `url`, `default`) | `alert_channels.write` | consume | 201, with the signing key once |
| `GET /alert-channels/{channel}` (with health) | `alert_channels.read` | | 200 |
| `PATCH /alert-channels/{channel}` (`expected_version`; name, URL, `default`, `enabled: true`) | `alert_channels.write` | consume | 200 |
| `POST /alert-channels/{channel}/disable` | `alert_channels.disable` | | 200 |
| `DELETE /alert-channels/{channel}` | `alert_channels.disable` | | 200 |
| `POST /alert-channels/{channel}/rotate-key` | `alert_channels.write` | consume | 200, the new key once |
| `POST /alert-channels/{channel}/test` | `alert_channels.test` | | 200, the outcome code |
| `GET /alert-channels/{channel}/deliveries` | `alert_channels.read` | | 200 |

- **Re-enabling a channel needs step-up,** as decided: it goes through `PATCH` with
  `enabled: true`, under `alert_channels.write`. `enabled: false` is refused on `PATCH` (422)
  and goes through `/disable`, so a disable never needs the password and a re-enable always
  does (mutation A4-16).
- `GET .../incidents/{incident}` (A3's) shows each notification's deliveries: state, attempts,
  last code and channel id.
- **New status codes:** 409 `channel_limit`, `channel_version_conflict` and
  `channel_name_taken`; 429 `test_send_limited`.
- **Missing is forbidden:** a channel or delivery of another organization answers as missing,
  the same 403 bytes, on every endpoint (M13, M14).

## 10. Permission rows

New rows: `alert_channels.read`, `alert_channels.write` (consume-on-use step-up),
`alert_channels.disable` and `alert_channels.test`. All are organization-level and org_admin
only. The matrix is regenerated and its diff shown in the PR; note 28's table changes with it.
`incidents.read_organization` (A3's) is unchanged and now also covers the built-in's
incidents.

## 11. Audit, metrics, events

- **Audit:** `alert_channel_created`, `alert_channel_changed`, `alert_channel_enabled`,
  `alert_channel_disabled`, `alert_channel_deleted`, `alert_channel_key_rotated`, and
  `alert_channel_tested` (with the outcome code). Each carries the channel id and the fields
  changed, never a URL, an origin or a key. Refusals are `management_refused`.
- **Metrics:** `gateway_alert_deliveries_total{channel_type, result}` per instance, with no
  organization label and no target.
- **Events:** `alert_delivery_failed` (channel id and code), with a log line.

## 12. Migration 17 and Postgres schema 6

- **New tables only:** `alert_channels`, `channel_secrets`, `notification_deliveries`,
  `notification_caps` and `suppression_summaries`.
- **One added column,** `incidents.notify_channels` (a JSON list of ids, default `'[]'`), by
  `ALTER TABLE ... ADD COLUMN`. Nothing is rebuilt.
- `migrate-to-postgres` and back copy and compare the new tables.
- The rollback drops the tables; on SQLite the column stays, unread.

## 13. Tests

Every store test runs on memory, SQLite and Postgres; Valkey ones are marked `valkey`. Beyond
the tests named in sections 4.2, 5, 8.1 and 9:

- **Channels:** create, change, disable, re-enable (with step-up), delete; the cap of 20
  (also on two Postgres instances at once); names unique; only the origin shown; the
  application's view shows no target.
- **Vault scope:** a channel ciphertext opened as a provider key and the reverse, both refused;
  the associated data's bytes differ; rotation reseals channel rows; the SQL grep.
- **Outbox and delivery:**
  - the transition's transaction;
  - two evaluators on two instances give one delivery;
  - a claim expired and taken again, and a stale worker's outcome refused;
  - the retry schedule, and channel health.
- **Caps:**
  - on two instances at once, exactly the cap is sent;
  - one summary an hour, to default channels only;
  - test-send caps;
  - each operator variable at its bounds, and one past each bound refusing the start.
- **`rule_weakened`** for each field's direction, to the channels as they were; and
  **`channel_removed`**.
- **Permissions:** every new endpoint's cross-organization test (403, the same bytes as
  missing); viewers and app_admins refused the channel endpoints; step-up consumed on create,
  change, re-enable and rotate, and given back when nothing changed; the route walk.
- **Postgres constraints,** each reached by a test of its own and not by an earlier key:
  - the unique (organization, name) on channels, with two different ids;
  - the unique (notification, channel) on deliveries, from two instances' transitions with
    distinct delivery ids;
  - the unique (organization, hour) on summaries, from two holders' cycles;
  - the caps' conditional update, from two instances at the cap;
  - `SKIP LOCKED`, from two workers claiming one row;
  - the `CHECK`s, by raw inserts.
- **Request path:** no new store call on any request.

## 14. Mutations

Each mutation is made, its tests shown failing, then restored byte for byte, with sha256 before
and after. A surviving mutation of a real, untested rule stops the work for your decision.

**From section 18 of the main plan:**

| # | Rule | Mutation |
|---|---|---|
| M11 | Consume-on-use on channel writes | `consume` off for `alert_channels.write` |
| M13 | Cross-organization, per resolver | the channel resolver and the delivery resolver each return another organization's (two mutations) |
| M14 | Missing equals forbidden | a missing channel answers 404 |
| M15 | A rule's channels of its own organization | skip the check at write, and separately at queue time |
| M16 | SSRF at store | skip `check_webhook_url` on create and change |
| M17 | SSRF at delivery | the webhook client without `GuardedBackend` |
| M18 | Connect to the checked address | connect to the name |
| M19 | No redirects | `follow_redirects=True` |
| M22 | Outbox in the transition's transaction | queue after the commit |
| M25 | Caps | the count checked in Python after a `SELECT` |
| M26 | One summary | the summary's unique index dropped |
| M29 | Rule weakened notice | skip it |
| M31 | Payload allowlist | add the webhook origin (or a username) to the payload |
| M32 | Signature | sign the body without the timestamp |
| M33 | Permanent failures not retried | retry a 400 |
| M36 | Reveal at one new site only | a second `reveal()` in the alert modules |
| M44 | Every resolved address checked | check only the first |
| M45 | A fresh check per delivery | one pooled client across deliveries |
| M46 | No proxy | `trust_env=True` |

**New:**

| # | Rule | Mutation |
|---|---|---|
| A4-1 | At least once, a claim expires | never expire a claim |
| A4-2 | A stale worker's outcome is refused | drop the claim from the outcome's `WHERE` |
| A4-3 | Both keys during a rotation | sign with the new key only |
| A4-5 | Only the origin is shown | return the URL in the channel view |
| A4-6 | The cap of 20 channels | skip the count |
| A4-7 | Later notifications go to the incident's first channels | resolve the channels again at resolution |
| A4-8 | An incident's first channels cannot be changed | the compare-and-set update writes the column |
| A4-9 | Port rules | skip the port check at connect (and, separately, at store) |
| A4-10 | TLS verification always on | build the webhook client with `verify=False` |
| A4-11 | A timeout on each stage | drop the read timeout (and, separately, the resolve timeout) |
| A4-12 | A maximum payload size | skip the 8 KiB check |
| A4-13 | No URL in an error | the error's code formatted from the exception |
| A4-14 | Each attempt signed afresh | reuse the first attempt's timestamp and signature |
| A4-15 | No injection through the URL | drop the control-character check in `check_webhook_url` |
| A4-16 | Re-enabling needs step-up | `enabled: true` under `alert_channels.disable` |
| A4-17 | Cap variables within their range | skip the range check |
| POOL-HOLD | No connection held during a network call | make the HTTP call inside the claim's transaction |
| CDF-1 to CDF-4 | The built-in's incidents (section 8.1) | as listed there |

**One per new permission row:** `alert_channels.read`, `.write`, `.disable` and `.test`, each
granted to app_admin in its own mutation, so that row's refusal test fails.

The PR also lists the rules that have tests but no mutation.

## 15. Leak scans (A4's secrets and the alert contents, as decided)

- **Channel secrets:**
  - **Planted:** a webhook URL with a token in its path and its query; a signing key (the
    generator injected); the previous key during a rotation; and a marker in the receiver's
    answer.
  - **Exercised:** create, test, deliver (succeeding, failing, timing out, redirected),
    rotate, re-enable, delete.
  - **Scanned:** every stored row (SQLite and Postgres), every Valkey key and value, every log
    record, every audit row, every event, the Prometheus output, `/healthz`, every admin answer
    except the two that return a key, and every outgoing request (headers and body).
  - **The control:** the scan must find each planted value in a deliberately unredacted copy,
    and nowhere else.
- **Alert contents:**
  - **Planted:** a prompt, a response, an API key, a provider key and a username, carried
    through to a firing and a resolved notification.
  - **Scanned:** every stored payload, every delivered body and header, and the rollups.
  - **The control:** a payload with the prompt added on purpose, which the scan must flag.

## 16. Threat-model entries

These are the main plan's section 14 entries that A4 makes real:

- a webhook pointed at an internal address, with each variant of section 4.2;
- a redirect to an internal address;
- a proxy in the environment;
- a channel secret opened as a provider key, or the reverse;
- the webhook as an amplifier against a third party;
- forged notifications;
- a URL with a token in it.

New with this plan:

- **A stale delivery worker** records nothing.
- **Delivery cannot starve requests or the evaluator:** no connection is held during a network
  call, at most 4 deliveries run an instance, and a webhook that never answers ends at its
  stage timeout.
- **A receiver used to probe internal ports:** only allowed ports (443 by default), and every
  address checked.
- **Injection** of headers or of the body through a URL or a name: section 4.2.
- **A replayed notification:** each attempt is signed with its own timestamp; the receiver
  refuses old timestamps and repeated delivery ids (the verification recipe in note 46).
- **One organization's channel health seen by another:** the built-in's incidents are
  filtered by organization and never reachable through an application (section 8.1).
- **A deleted channel's queued deliveries** fail as `channel_gone`; its secrets are deleted
  with it.

## 17. Timing

Measured on this machine, with the hardware recorded, three runs, before and after:

- `benchmarks/request_store_calls.py` and `benchmarks/key_checks.py`, on SQLite and Postgres;
  A4 adds nothing to the request path, and the stop rule of the main plan's section 20
  applies;
- `benchmarks/alert_evaluation.py` with the outbox inserts, at 1, 100, 1,000 and 5,000
  applications;
- one delivery in process (claim, build, sign, outcome), on scripted networks.

No number is written before it is run.

## 18. Decisions not in the request

Numbered, so they can be objected to.

1. **At most 5 channels a rule.**
2. **A channel deleted after queueing** fails its pending deliveries as `channel_gone`;
   nothing is re-routed.
3. **`X-Gateway-Delivery` is the notification id,** so a receiver behind two channels sees one
   id for one notification.
4. **The previous signing key is a row of its own,** with an expiry, deleted by the first
   rotation or cycle after it.
5. **`PATCH` cannot disable** (422): disabling goes through `/disable` (no step-up), so
   re-enabling (with step-up) is the only enable path.
6. **TLS failures are permanent,** not retried.
7. **The summary is queued by the evaluator's holder,** in its first cycle after the hour ends.
8. **Only port 443 by default,** with an operator list of ports from 1024 to 65535. Ports below
   1024 other than 443 are never allowed, since they are where internal services listen.
9. **A query string is allowed in a webhook URL,** sealed with the rest.
10. **A payload over 8 KiB fails, never truncated,** since a cut JSON body would not parse.
11. **The cap ranges** (1 to 60, 1 to 600, 1 to 60, 1 to 300) are this plan's proposal, as is
    each value's being one environment variable per instance, its hash compared across
    instances.
12. **The built-in's rule id** is `rul_cdf_` followed by the first 16 hex characters of an
    HMAC of the organization id under a new derived purpose
    (`KeyPurpose.BUILTIN_RULE_ID`). It is fixed per organization and reveals nothing.
13. **Each stage's timeout is 5 s, and the whole attempt 10 s,** on the injected clock.
14. **Note 46** is A4's design note, with the receiver's verification recipe.

## 19. Questions

None block A4. For your objection if any:

1. The cap ranges of decision 11.
2. The port rule of decision 8.

## 20. Not built in A4

- Email, SMTP, the platform scope, `platform_secrets`, M41 to M43, recipients and their caps:
  A5, as decided.
- Operator signals and channels: A6.
- The console's channel and incident screens: C2.
- Slack-style formats.
- A link to confirm anything.

## 21. Additions on approval (2026-11-08)

1. **The empty application id is refused on every path an application can appear:** the HTTP
   management endpoint, `scripts/manage.py create-application`, the bootstrap (the plans file's
   demo applications), and the SQLite-to-Postgres copy (and the copy back). No path renames an
   application today; if one is added it must call the same check. Migration 17 (and Postgres
   schema 6) refuse to apply while any application has an empty id, and the migration preview
   reports it as a conflict. A test per path, and a mutation of the shared check.
2. **Numeric host forms** refused when the URL is stored include the decimal and hexadecimal
   whole-number forms: `2130706433` and `0x7f000001`, beside `0177.0.0.1`, `127.1` and
   `0x7f.1`.
3. **A reference verification snippet for receivers** in note 46, with a test that runs that
   snippet, as written in the note, on a real delivery's request: it verifies it, and rejects
   a stale timestamp and a repeated delivery id.
4. **Delivery tests inject the resolver and the network** (`FakeResolver`, `ScriptedNetwork`);
   no production guard is relaxed for a test.
5. **Cap defaults are well below their maximums, and no cap can be disabled:** each range
   starts at 1 (0, a negative number, an empty value or anything not a whole number refuses
   the start), and each default is at most a sixth of its maximum (6 of 60, 60 of 600, 10 of
   60, 30 of 300).

## 22. Order of work: two groups in one PR

One draft PR, two groups of commits, one report with a section for each.

- **Group 1, nothing that opens an outbound connection:** migration 17 and Postgres schema 6
  (every table of section 12, so group 2 needs no further migration); channels, the vault's
  channel scope and the signing key; the channel endpoints that make no connection (all of
  section 9 but `/test`) and the four permission rows; the empty application id refusal
  (section 21.1); `check_webhook_url` (section 4.1, with 21.2); a rule's channel ids checked
  at write (M15); the `rule_weakened` notice and the `channel_removed` notice, queued in the
  outbox in their write's transaction (sent by group 2); step-up for creating, changing,
  re-enabling and rotating; the payload allowlist and its 8 KiB bound.
- **Group 2, delivery:** `webhook_client()` and its connection rules (section 4.2), the
  delivery worker and the pool rule (section 5), the evaluator queueing its notifications with
  the incident's first channels, retries, the caps and the suppressed summary (section 7),
  test send, `channel_delivery_failing` (section 8.1), the leak scans of section 15, and note
  46's receiver recipe with its test.

Work stops at the end of group 1 if anything blocks group 2: a conflict with this plan, a
surviving mutation of a real, untested rule, or a failed check. A conflict touching
authentication, step-up, secrets or where data can go is never resolved by the builder.
