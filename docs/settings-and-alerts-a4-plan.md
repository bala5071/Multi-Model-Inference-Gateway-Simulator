# A4 plan: webhook channels and their delivery

The plan for PR A4 of `docs/settings-and-alerts-plan.md` (the main plan), written before any
code, for approval. A4 adds outbound connections and stored secrets, so the main plan's rules
for it apply: plan approval first; any conflict between this plan and the main plan is listed
below and decided by the reviewer, not here.

**Scope.** Part F's A4 row, the four items moved to A4 when A2 started, and nothing from A5 or
later:

- webhook channels;
- the vault's channel scope and the signing key;
- `webhook_client`, with the connect-time check and no redirects;
- the outbox, retries, the caps and the suppressed summary;
- test send and delivery visibility;
- `channel_delivery_failing`;
- the leak scans;
- moved from A2: channel id validation on rules (M15), `GET .../alert-channels`,
  `channel_delivery_failing`, and the `rule_weakened` notice (M29).

**Order.** A4 builds on the evaluator after #37 (section 11.7: pages, the time box, the
evaluator's own pool). So #37 merges first, then this plan, then A4 from `main`.

## 1. Conflicts with the main plan (blocking; for you to decide)

These are the places where the A4 request, Part F and the main plan's sections disagree, or
where the A3 schema cannot hold what a section asks. A4 is not built until each is answered.

1. **SMTP and the platform scope: A4 or A5?** The A4 request names the SMTP password, "its own
   vault scope with the three tests the plan names", "the SMTP and webhook connection rules",
   and a leak scan with the SMTP password planted. Part F puts all of email in A5: the
   operator's SMTP settings, the platform scope and `platform_secrets`, the three tests of
   section 9.1, STARTTLS, verified recipients and their caps, and the leak scan "extended to
   the code". The rest of this plan follows Part F: webhooks only.
   - Moving SMTP into A4 brings migration and schema for `platform_secrets`, the platform
     subkey, M41 to M43, `smtplib` on a thread, and the in-process SMTP test server.
   - It does not bring the recipients and their verification, which make email usable. A4
     would hold an SMTP password with nothing that sends mail.
2. **`channel_delivery_failing` has nowhere to live.** It is one built-in rule per
   organization ("on for every organization", "cannot be deleted", one series per channel). But
   rules and incidents belong to an application: `alert_rules.application_id` and
   `incidents.application_id` are `NOT NULL` in migrations 15 and 16, and every incident
   endpoint resolves through an application. The options:
   - **(a)** It is built in: code, not a rule row. Its incidents are stored with
     `application_id = ''` and a fixed rule id per organization (`rul_cdf_` and the
     organization's id). They are shown by `GET /incidents` (the organization's list) and
     reachable by no application's endpoints, so they are never acknowledged. This needs no
     table change. **Recommended.**
   - **(b)** Make `application_id` nullable. On SQLite that rebuilds `incidents` (and
     `alert_rules`, if the rule is a row), which needs approval as a table rebuild.
3. **Which leak scans.** Part F's A4 row says "both leak scans". Section 19's channel-secrets
   scan plants an SMTP password and a confirmation code, which are A5's. This plan scans A4's
   secrets (the webhook URL with a token in its path, the signing key, and the previous key
   during a rotation) and the alert contents. A5 extends the scan, as its row says. If item 1
   moves SMTP into A4, the SMTP password is planted too.
4. **Section 11.4 still says the evaluator runs "on the request pool's transactions".** #37
   replaces that with the evaluator's own pool. This plan puts delivery's claims and outcome
   writes on that pool too (decision 7). Section 11.2 says delivery runs "on every instance",
   so the pool must exist on every instance, not only on the holder's. It does: #37 opens it on
   every instance.
5. **"The rule's channels as they were before the change."** Section 8 sends the `resolved`
   notice of a rule changed while firing to the channels as they were. But A3 resolves it in
   the next cycle, after the rule row already holds the new channels. This plan keeps the
   channel ids an incident first notified on the incident itself (a new column), so its later
   notifications follow it (decision 4). Section 8 does not say this; it needs your agreement.

## 2. Channels

- **The table** (`alert_channels`, migration 17; Postgres schema 6), owned by an
  organization. Its columns:
  - `id` (`chn_` and 16 hex characters) and `organization_id`;
  - `name` (the rule-name charset), unique within the organization;
  - `type` (`webhook` only in A4, `CHECK`), `enabled`, `is_default`;
  - `url_origin`, the only part of the URL ever shown;
  - `version`, `created_at_ms`, `updated_at_ms`;
  - health: `last_delivered_at_ms`, `last_failed_at_ms`, `consecutive_failures`.
- **At most 20 channels an organization,** counted under the global write lock, as the rule
  cap of 50 is (A2). On Postgres it is held across instances by the lock.
- **The URL** is checked when stored by `check_base_url`: https only, no user info, no literal
  private address, a field error otherwise. It is sealed in the vault (section 3). The answer,
  the audit row, every log line and every event carry only `url_origin`.
- **An application's view** (`GET /applications/{app}/alert-channels`, under
  `alert_rules.read`): ids, names, types, `is_default` and `enabled`. Never a target or its
  origin.
- **A rule's `channels`:** `[]`, `["organization defaults"]`, or up to 5 channel ids of the
  rule's own organization. They are checked at write by `RuleService`, and again when a
  notification is queued: a channel deleted or moved since then is dropped from that
  delivery, said so on the delivery. A channel id of another organization gets the same 422
  `invalid_request` body as one that does not exist (M15). Default rules keep
  `["organization defaults"]`.

## 3. Secrets: the vault's channel scope and the signing key

- **`ChannelBinding`** (organization, channel id, kind, URL origin), beside today's provider
  `Binding`, which is unchanged: provider rows are not resealed. Its associated data is
  `vault-channel-v1|organization|channel|kind|origin|key_id`, under the master key (section
  9.1). The kinds are `webhook_url`, `webhook_signing_key` and `webhook_signing_key_previous`.
  The associated data's first field names the scope, so a channel ciphertext never opens as a
  provider key, nor the reverse; this is tested both ways.
- **`channel_secrets`** (channel id, kind, nonce, ciphertext, key id, `expires_at_ms` for the
  previous key): read only by the channel store. A test greps every store's SQL, so no provider
  store query reads it and no channel store query reads `provider_keys`.
- **The signing key:** 32 bytes from the injected `random_bytes`. It is returned once, with
  `Cache-Control: no-store`, in the answer that creates the channel or rotates the key, and
  never again; it is never sent to the receiver.
  - Rotation keeps the previous key for 24 hours (store clock), and signs with both.
  - `rotate-vault-key` reseals channel rows too. A channel whose secret's master is not loaded
    is `key_unreadable`: its deliveries fail with `channel_secret_unreadable`, shown like any
    failure.
- **`Secret.reveal()`** gains exactly one call site, `alerts/webhook.py`'s `build_request`,
  which takes the leased URL and keys and returns the signed request (decision 16 of the main
  plan names this site and the SMTP login; the SMTP login is A5's). The AST test's allowlist
  gains that one function (M36). Leased values go to the `Redactor`, as provider keys do.

## 4. The webhook request and its connection rules

- **The request:**
  - `POST`, `Content-Type: application/json`, and the body `json-v1` of section 10, built and
    stored at queue time (section 6);
  - `X-Gateway-Delivery`, the notification's id (receivers deduplicate on it);
  - `X-Gateway-Timestamp`, Unix seconds;
  - `X-Gateway-Signature: v1=<hex HMAC-SHA256(key, timestamp + "." + body)>`, one `v1=` per key
    during a rotation.
- **`webhook_client()` in `egress.py`,** the only way a webhook is sent. An AST test fails on
  any other `httpx` client construction in the alert modules. It is built like
  `organization_client`:
  - **at connect time, on every connection,** `GuardedBackend` resolves the name itself,
    checks every resolved address with `refusal` (and refuses if any is refused), and opens
    the socket to the checked address. This covers a test send, every delivery and every retry.
  - **`NO_LOCAL_DESTINATIONS`:** `GATEWAY_PROVIDER_LOCAL_ALLOW` never applies to webhooks.
  - **a new client per delivery,** with no pool kept between deliveries, so every delivery
    resolves and checks again.
  - **`follow_redirects=False`:** any 3xx is a permanent `http_3xx`. Its `Location` is not
    fetched, stored or logged.
  - **`trust_env=False`:** proxy settings are ignored.
  - TLS verified against the host name with the pinned context (`provider_tls_context`).
  - Timeouts: 5 s to connect, 10 s in all. At most 4 KiB of the answer read, then discarded,
    never logged.
  - It runs under `egress_scope(always_hosted)`, decision 24 of the main plan.
- **Testing without a listener.** The tests inject the resolver and the inner network
  (`mock_network`'s `FakeResolver` and `ScriptedNetwork`, as the provider tests do). The
  injected backend records every `connect_tcp` and its address. So the tests need no local
  listener, and no guard on a production path is relaxed for them.
- **Retries:** after a connection failure, a timeout, a 5xx, a 408 or a 429, at 30 s, 2 min,
  8 min, 30 min and 2 h, each with up to 10% jitter from a seeded `random.Random`. A 429's
  `Retry-After` is honoured up to an hour. Six tries in all, on the injected clock.
  - Any other 4xx, an egress refusal, a 3xx or a TLS failure is permanent.
  - The codes are `egress_refused`, `timeout`, `connect_failed`, `tls`, `http_3xx`,
    `http_4xx`, `http_5xx`, `channel_secret_unreadable` and `channel_gone`.

## 5. The outbox and delivery

- **The outbox** (`notification_deliveries`): one row per (notification, channel). Its
  columns:
  - `id` (`dlv_`), `notification_id`, `channel_id`, `organization_id`;
  - `state` (`queued`, `sending`, `delivered`, `failed`, `suppressed`; `CHECK`);
  - `attempts`, `next_attempt_at_ms`, `claimed_by`, `claim_expires_at_ms`, `last_code`;
  - `payload` (the stored `json-v1`), `created_at_ms`, `updated_at_ms`.

  It has a unique index on (notification, channel), and an index on (`state`,
  `next_attempt_at_ms`) for claims.
- **Queued in the transition's transaction** (M22). A3's `Change` gains its deliveries.
  - The evaluator resolves the rule's channels when it makes the change: the ids, or the
    organization's enabled default channels.
  - It checks the caps (section 7) in the same transaction, and writes the rows.
  - A rolled-back transition queues nothing; a transition made twice queues once (the
    compare-and-set and the unique index).
- **Held notifications** (quiet hours) get no delivery. Their quiet-hours summary does, when
  it is recorded.
- **The delivery worker, on every instance:**
  - It claims due rows. On Postgres the claim is `FOR UPDATE SKIP LOCKED` and sets
    `claimed_by` and a 60 s `claim_expires_at_ms`; on SQLite it is the one process. At most 4
    deliveries run at once per instance.
  - It records the outcome with a compare-and-set on its claim: a worker whose claim expired
    and was retaken writes nothing.
  - A claim whose instance died expires and is claimed again, so delivery is at least once,
    and `X-Gateway-Delivery` lets the receiver drop a repeat.
  - Each outcome updates the channel's health in the same transaction: `consecutive_failures`
    is set to 0 or incremented, with `last_delivered_at_ms` or `last_failed_at_ms`.
- **Off the request path.** On Postgres the worker's statements run under #37's evaluator
  context (decision 7). Each statement holds a connection only for itself, never across a
  delivery's HTTP request. On SQLite the worker gives the loop back between deliveries.

## 6. What a notification carries

- **`AlertPayload`:** a frozen dataclass with exactly the fields of section 10's `json-v1`,
  like `AuditEvent`. It is built when the delivery is queued and stored as built, and a
  delivery sends exactly what was stored.
- **The kinds:** `firing`, `firing_again`, `reminder`, `resolved`, `quiet_hours_summary`,
  `rule_weakened`, `channel_removed`, `suppressed_summary`, `test`.
- **Never in it:** prompt or response text, token ids or hashed tokens, API keys or their
  HMACs, provider keys, webhook URLs or signing keys, provider base URLs, usernames, email
  addresses, IP addresses, session ids, or the user who acknowledged. No dollars (decision 20
  of the main plan).
- **Times** are the store clock's, written in ISO 8601 UTC; `console_path` is the incident's
  admin path.

## 7. The caps and the suppressed summary

- **Per rule** 6 an hour and **per organization** 60 an hour, counted per notification (not
  per channel; decision 3). The counts are rows of `notification_caps` (scope, scope id,
  hour, sent).
  - Each count is one statement, `INSERT ... ON CONFLICT DO UPDATE SET sent = sent + 1 WHERE
    sent < cap RETURNING`, in the outbox insert's transaction, with the hour from the store
    clock (M25).
  - A notification over a cap is queued as `suppressed`, with its reason (`rule_cap` or
    `organization_cap`), and not sent.
- **The summary:** the evaluator's holder queues it in its first cycle after the hour ends.
  - There is one per organization per hour, made unique by a unique index on
    `suppression_summaries` (organization, hour) (M26).
  - It goes to the organization's default channels and does not count against the caps.
  - It says how many notifications of how many rules were not sent, and when.
- **Test sends:** 10 an hour per channel and 30 per organization, in the window counters
  (Valkey or memory), like test-connection. A counter that cannot answer refuses (missing is
  forbidden).

## 8. The four items moved from A2

- **Channel ids on rules** (M15): section 2.
- **`GET /applications/{app}/alert-channels`:** section 2.
- **`channel_delivery_failing`:** decided by conflict 2. Either way the rule is the same:
  - it is on for every organization and cannot be disabled or deleted;
  - it has one series per channel, and fires at `consecutive_failures >= 3` (warning);
  - it sends to every default channel except the failing one;
  - it is shown in the organization's incident list whatever happens to its own delivery;
  - it resolves at the channel's next delivered notification, or when the channel is deleted.
- **`rule_weakened`** (M29):
  - each rule field has a direction, like settings: a higher threshold for `>`, a longer
    window or `for`, quiet hours added or widened, a channel removed, a lower severity,
    disabled, deleted;
  - a write that makes an enabled rule fire less queues a `rule_weakened` notice in
    `RuleService`'s write transaction;
  - the notice goes to the rule's channels as they were before the write, naming the rule and
    the fields, never the user;
  - restoring defaults is judged the same way.
- **`channel_removed`** (decision 25 of the main plan): disabling or deleting a channel queues
  a notice to the organization's other default channels.

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

- `GET .../incidents/{incident}` (A3's) shows each notification's deliveries: state,
  attempts, last code and channel id.
- **New status codes:** 409 `channel_limit` and `channel_version_conflict`, 409
  `channel_name_taken`, 429 `test_send_limited`.
- **Missing is forbidden:** a channel or delivery of another organization is answered as
  missing, the same 403 bytes, on every endpoint (M13, M14).
- **Recipients and confirmations are A5's** (unless conflict 1 says otherwise).

## 10. Permission rows

New rows: `alert_channels.read`, `alert_channels.write` (consume-on-use step-up),
`alert_channels.disable` and `alert_channels.test`. All are organization-level and org_admin
only, as section 12 has them. `alert_channels.confirm_recipient` is A5's. The matrix is
regenerated and its diff shown in the PR; note 28's documented table changes with it.

## 11. Audit, metrics, events

- **Audit:** `alert_channel_created`, `alert_channel_changed`, `alert_channel_disabled`,
  `alert_channel_deleted`, `alert_channel_key_rotated`, and `alert_channel_tested` (with the
  outcome code). Each carries the channel id and the fields changed, never a URL or a key.
  Refusals are `management_refused` with their reason.
- **Metrics:** `gateway_alert_deliveries_total{channel_type, result}` per instance, with no
  organization label and no target.
- **Events:** `alert_delivery_failed` (channel id and code), with a log line.

## 12. Migration 17 and Postgres schema 6

New tables only: `alert_channels`, `channel_secrets`, `notification_deliveries`,
`notification_caps` and `suppression_summaries`. Plus one added column, `incidents.notify_channels`
(decision 4), an `ALTER TABLE ... ADD COLUMN` with a default. Nothing is rebuilt, so this is not
the table-rebuild case (unless conflict 2 is answered (b)). `migrate-to-postgres` and back copy
and compare the new tables. The rollback drops them; on SQLite the column stays, unread.

## 13. Tests

Every store test runs on memory, SQLite and Postgres; Valkey ones are marked `valkey`.

- **Channels:**
  - create, change, disable, delete, the cap of 20 (also on two Postgres instances at once);
  - names unique in an organization;
  - only the origin ever shown;
  - the application's view shows no target.
- **Vault scope:** a channel ciphertext opened as a provider key and the reverse, both refused
  (`VaultIntegrityError`); the associated data's bytes differ across scopes; rotation reseals
  channel rows; the SQL grep for `channel_secrets`.
- **Signature:** verified by an independent implementation in the test, from the key the
  create answer returned; both keys during a rotation; only the new one after 24 h.
- **Connect time** (section 17's SSRF list):
  - every private, loopback, link-local, metadata, multicast, reserved and embedded-IPv4
    address is refused at create (literal) and at connect (resolved), on a test send, the first
    delivery and each retry;
  - rebinding: public at create and private at delivery is refused, with the socket never
    opened;
  - a name resolving to one public and one private address is refused;
  - the socket is opened to the checked address;
  - a second delivery resolves again;
  - 301, 302, 307 and 308, to private and to public addresses, are each `http_3xx` with no
    second request;
  - `HTTPS_PROXY` set and ignored; http refused; a trailing dot.
- **Outbox and delivery:**
  - queued in the transition's transaction (a transition rolled back queues nothing);
  - two evaluators on two instances give one delivery;
  - a claim expired and taken again (at least once), and a stale worker's outcome refused;
  - the retry schedule on the injected clock, with seeded jitter, and `Retry-After`;
  - no retry on a permanent failure;
  - the answer never logged and at most 4 KiB read;
  - channel health and `consecutive_failures`.
- **Caps:**
  - per-rule and per-organization caps on two instances at once: exactly the cap sent, the
    rest `suppressed`;
  - one summary an hour, to default channels only, not counted;
  - test-send caps, and a counter that cannot answer refuses.
- **`channel_delivery_failing`:** fires at 3, sends to the other default channels and never to
  the failing one, is shown in the organization's incident list, resolves on the next
  delivered notification and on the channel's deletion, and cannot be deleted.
- **`rule_weakened`:** each field's direction; sent to the channels as they were; a
  strengthening sends nothing.
- **`channel_removed`** on disable and delete.
- **Permissions:**
  - every new endpoint gets the cross-organization test (a resource of `south` through
    `/orgs/north/...`: 403, the same bytes as a missing one);
  - viewers and app_admins are refused the organization's channel endpoints;
  - step-up is consumed on create, change and rotate, and the window is given back when
    nothing changed;
  - the route walk finds every route declared.
- **Postgres constraints, each reached by a test of its own and not by an earlier key:**
  - the unique (organization, name) on channels, with two different ids;
  - the unique (notification, channel) on deliveries, from two instances' transitions with
    distinct delivery ids;
  - the unique (organization, hour) on summaries, from two holders' cycles;
  - the caps' conditional update, from two instances at the cap;
  - the claim's `SKIP LOCKED`, from two workers claiming one row;
  - the `CHECK`s on state and type, by raw inserts.
- **Request path:** no new store call on any request (the counting tests).

## 14. Mutations

Each mutation is made, its tests shown failing, then restored byte for byte, with sha256
before and after. If one survives and the rule is real but untested, I stop and ask. The
ones from section 18 of the main plan that are A4's:

| # | Rule | Mutation |
|---|---|---|
| M11 | Consume-on-use on channel writes | `consume` off for `alert_channels.write` |
| M13 | Cross-organization, per resolver | the channel resolver and the delivery resolver each return another organization's (two mutations) |
| M14 | Missing equals forbidden | a missing channel answers 404 |
| M15 | A rule's channels of its own organization | skip the check (at write, and separately at queue time) |
| M16 | SSRF at create | skip `check_base_url` on create and change |
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

New ones for rules this plan adds:

| # | Rule | Mutation |
|---|---|---|
| A4-1 | At least once, a claim expires | never expire a claim |
| A4-2 | A stale worker's outcome is refused | drop the claim from the outcome's `WHERE` |
| A4-3 | Both keys during a rotation | sign with the new key only |
| A4-4 | `channel_delivery_failing` skips the failing channel | send to every default channel |
| A4-5 | Only the origin is shown | return the URL in the channel view |
| A4-6 | The cap of 20 channels | skip the count |
| A4-7 | An incident's later notifications follow its first channels | resolve the channels again at resolution |

Each new permission row gets one: `alert_channels.read`, `.write`, `.disable` and `.test`, each
granted to app_admin in its own mutation, so that row's refusal test fails. The PR also lists
the rules that have tests but no mutation.

## 15. Leak scans

- **Channel secrets:**
  - planted: a webhook URL with a token in its path, and a signing key (the generator
    injected), and the previous key during a rotation;
  - exercised: create, test, deliver (succeeding, failing, redirected), rotate, delete;
  - scanned: every stored row (SQLite and Postgres), every Valkey key and value, every log
    record, every audit row, every event, the Prometheus output, `/healthz`, every admin answer
    except the two that return a key, and every outgoing request (headers and body);
  - the control: the scan must find each planted value in a deliberately unredacted copy, and
    nowhere else.
- **Alert contents:**
  - planted: a prompt, a response, an API key, a provider key and a username, carried through
    to a firing and a resolved notification;
  - scanned: every stored payload, every delivered body and header, and the rollups;
  - the control: a payload with the prompt added on purpose, which the scan must flag.

## 16. Threat-model entries

These are the main plan's section 14 entries that A4 makes real:

- a webhook pointed at an internal address, with each of its variants;
- a redirect to an internal address;
- a proxy in the environment;
- a channel secret opened as a provider key, or the reverse;
- the webhook as an amplifier against a third party;
- forged notifications;
- a URL with a token in it.

New with this plan:

- **A stale delivery worker** (paused past its claim) records nothing: its outcome is a
  compare-and-set on its claim.
- **Delivery as a way to starve requests:** its statements run on the background pool, one
  statement at a time, never across a request; at most 4 deliveries an instance.
- **A deleted channel's queued deliveries** fail as `channel_gone`; its secrets are deleted
  with it, in the same transaction.
- **An organization whose every channel fails:** `channel_delivery_failing` stays in its
  incident list (the console banner is C2's).

## 17. Timing

Measured on this machine, with the hardware recorded, three runs, before and after:

- `benchmarks/request_store_calls.py` and `benchmarks/key_checks.py`, on SQLite and Postgres:
  A4 adds nothing to the request path, and the stop rule of section 20 applies;
- `benchmarks/alert_evaluation.py`, with the outbox inserts, at 1, 100, 1,000 and 5,000
  applications;
- one delivery's cost in process (claim, build, sign and outcome), on scripted networks.

No number is written before it is run.

## 18. Decisions not in the request

Numbered, so they can be objected to.

1. **Webhooks only;** email stays in A5 (unless conflict 1 moves it).
2. **`channel_delivery_failing` is built in,** with organization incidents stored under
   `application_id = ''` (conflict 2, option (a)).
3. **Caps count notifications, not deliveries:** a notification to three channels counts once
   against the rule and once against the organization.
4. **An incident keeps the channel ids it first notified** (`notify_channels`), so its
   `resolved`, reminders and `firing_again` go where `firing` went, even after the rule's
   channels change (conflict 5).
5. **A rule names at most 5 channels.**
6. **A channel deleted or moved after queueing** fails its pending deliveries as
   `channel_gone`; nothing is re-routed.
7. **Delivery shares the evaluator's pool on Postgres** (#37), raised from 2 to 3
   connections, so that four concurrent deliveries' outcome writes and the cycle do not wait on
   each other for long. Each statement holds a connection only for itself.
8. **`X-Gateway-Delivery` is the notification id,** as section 9 says, so a receiver behind two
   channels sees one id for one notification.
9. **The previous signing key is a row of its own** (`webhook_signing_key_previous`) with an
   expiry, deleted by the first rotation or cycle after it.
10. **`PATCH` can re-enable a channel** (`enabled: true`) under `alert_channels.write`, with
    step-up: re-enabling widens where data goes again. Disabling stays under `.disable`
    without step-up.
11. **TLS failures are permanent,** not retried: a certificate problem does not mend itself
    in two hours, and retrying would hammer the receiver.
12. **The summary is queued by the evaluator's holder,** in its first cycle after the hour
    ends, so it is made once, by the unique index, whatever instance holds the lease.

## 19. Questions

1. Conflict 1: SMTP and the platform scope in A4, or in A5 as Part F has them?
2. Conflict 2: `channel_delivery_failing` built in with `application_id = ''` (a), or a table
   rebuild making it nullable (b)?
3. Conflict 5: may an incident keep its first channels (decision 4), or should its later
   notifications go to the rule's channels at that moment?
4. The caps' numbers (6 and 60 an hour, and 10 and 30 test sends) are the main plan's: keep
   them?
5. Should a channel re-enabled by `PATCH` need step-up (decision 10)?
6. Should the background pool be 3 connections (decision 7), or should delivery get a pool of
   its own?

## 20. Not built in A4

Email and everything of A5 (unless question 1 moves part of it); operator signals and channels
(A6); the console's channel and incident screens (C2); Slack-style formats; a link to confirm
anything.
