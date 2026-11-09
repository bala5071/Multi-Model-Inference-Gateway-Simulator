# 46. Alert channels: webhooks, their secrets, the outbox

PR A4 of `docs/settings-and-alerts-plan.md`, as `docs/settings-and-alerts-a4-plan.md` (approved
2026-11-08) defines it. A4 is built in two groups (that plan's section 22). **Group 1**:
channels, their secrets, their endpoints, and the notices that changes queue; it opens no
outbound connection. **Group 2**: delivery, from "Delivery" on.

## Channels

A channel belongs to an **organization** (`alert_channels`, SQLite migration 17, Postgres schema
6): a name in the rule-name charset, unique within the organization; a type (`webhook`, the only
one in A4); `enabled`; `default`; the origin of its URL; a version; and its health
(`last_delivered_at_ms`, `last_failed_at_ms`, `consecutive_failures`). An organization has at
most **20**, counted under the global write lock.

- **The URL is a secret** (receivers carry tokens in its path or query). It is sealed in the
  vault, and only its origin (`https://hooks.example.com`) is ever shown, audited, logged or put
  in an event. A URL given in a change always counts as a change, even the same one: comparing
  would mean decrypting the stored one.
- **The URL when stored** (`check_webhook_url`) must be https, with no user info or fragment, at
  most 2,048 characters, with no control character, whitespace or backslash. Its host must be
  IDNA, and must not be `localhost`, `*.localhost`, `*.local` or `*.internal`, a literal address
  that is not public (the IPv6, mapped, NAT64, 6to4 and Teredo forms are judged on the IPv4
  address they carry), or a host whose last label is a number other than a canonical dotted
  quad (`2130706433`, `0x7f000001`, `0177.0.0.1`, `127.1`, `0x7f.1`: resolvers read them as
  addresses). Its port must be 443, or one the operator lists in `GATEWAY_ALERT_WEBHOOK_PORTS`
  (each 1024 to 65535; anything else refuses the start). This check gives a quick field error.
  The connect-time check of every delivery (group 2) is the one relied on.

## Secrets: the vault's channel scope

`ChannelBinding` (organization, channel, kind, URL origin) is the provider `Binding`'s sibling.
Its associated data is `vault-channel-v1|organization|channel|kind|origin|key_id`. The first
field names the scope, so a channel ciphertext never opens as a provider key, nor the reverse
(tested both ways). No provider row is resealed.

- **Kinds:** `webhook_url`, `webhook_signing_key`, `webhook_signing_key_previous`, in
  `channel_secrets`. Only the channel stores read that table (a test reads every module's SQL).
  A channel secret keeps no "last four characters": a URL's are the end of its token.
- **The signing key** is 32 bytes from the injected `random_bytes`. It is shown once, as 64 hex
  digits, in the answer that creates the channel or rotates the key, and never again. The HMAC
  key is those 32 bytes.
- **Rotation** keeps the previous key for 24 hours on the store clock (the database's, in shared
  mode), replacing any older previous key.
- **A URL whose origin changes** keeps its signing keys, rebound to the new origin in the same
  transaction.
- **`rotate-vault-key`** reseals channel secrets with provider keys, in one transaction. A
  channel whose secrets no loaded key can read is named, and its deliveries fail as
  `channel_secret_unreadable`.

## Endpoints

Under `/v1/admin/orgs/{org}`, each through `requires`, local only until A8, `Cache-Control:
no-store`. A channel of another organization, or an id not in a channel id's form, answers as
missing: 403, the same bytes as forbidden.

| Method | Path | Action | Success |
|---|---|---|---|
| `GET` | `/applications/{app}/alert-channels` | `alert_rules.read` | 200 |
| `GET` | `/alert-channels` | `alert_channels.read` | 200 |
| `POST` | `/alert-channels` | `alert_channels.write` | 201 |
| `GET` | `/alert-channels/{channel}` | `alert_channels.read` | 200 |
| `PATCH` | `/alert-channels/{channel}` | `alert_channels.write` | 200 |
| `POST` | `/alert-channels/{channel}/disable` | `alert_channels.disable` | 200 |
| `DELETE` | `/alert-channels/{channel}` | `alert_channels.disable` | 200 |
| `POST` | `/alert-channels/{channel}/rotate-key` | `alert_channels.write` | 200 |
| `POST` | `/alert-channels/{channel}/test` | `alert_channels.test` | 200 |
| `GET` | `/alert-channels/{channel}/deliveries` | `alert_channels.read` | 200 |

- **Who:** each `alert_channels.*` action is org_admin only. An application's readers
  (`alert_rules.read`) see the channels' ids, names, types, `default` and `enabled`, never a
  target or its origin.
- **Step-up** (consume-on-use) is needed for `alert_channels.write`: creating, changing (name,
  URL, `default`), re-enabling (`PATCH` with `enabled: true`) and rotating the key. A change that
  changes nothing gives the window back, and so does any refused change. `PATCH` with `enabled:
  false` is refused (422): disabling has its own route, so it never needs the password, and
  re-enabling always does.
- **Codes:** 409 `channel_limit`, `channel_version_conflict`, `channel_name_taken`; 422
  `invalid_request` naming the field. A refusal never repeats the URL.
- **Audit:** `alert_channel_created`, `_changed` (the names of the fields changed), `_enabled`,
  `_disabled`, `_deleted` and `_key_rotated`, each with the channel id and its type. None
  carries a URL, an origin or a key.

## A rule's channels

`[]`, `["organization defaults"]`, or 1 to 5 different channel ids. The ids are checked at write
to be channels of the rule's own organization. An id of another organization's channel gets the
same 422 as an id that does not exist, byte for byte (M15). The dry run (`/validate`) checks the
same way.

## Notices, queued in the outbox

`notification_deliveries` has one row per (notification, channel), unique on the pair, written
in the transaction of the change it belongs to. Each row holds the notification's `json-v1`
body as built (`AlertPayload`: only its own fields, each checked; `json.dumps` with ASCII only;
at most 8 KiB, and one over is queued as failed `payload_too_large`, never cut short). Group 1
queues two notices. Group 2 sends them and counts them against the caps.

- **`rule_weakened`** (M29): a rule write that makes an enabled rule fire or notify less. That
  is a stricter condition (a higher threshold for `>`, a lower one for `<`, a strict comparison
  where it was not, another signal); a longer window, `for` or cooldown; a reminder removed or
  made rarer; quiet hours added or widened (any minute of the week quiet now that was not); a
  lower severity; a channel removed; disabling; or deleting. `restore-defaults` is judged the
  same way. The notice goes to the rule's channels as they were before the write, names the rule
  and the fields, and never names the user.
- **`channel_removed`:** disabling or deleting an enabled channel tells the organization's other
  enabled default channels. A deleted channel's queued deliveries fail as `channel_gone`.

Where a notice goes: `["organization defaults"]` means the organization's enabled default
channels, and ids mean those that exist in the organization now and are enabled. Both are
checked again when the notice is queued.

## The empty application id

The built-in `channel_delivery_failing` (group 2) keeps its incidents under the empty
application id. So no application may have one: `check_application_id` refuses it in every
store's `add_application`, in the management service (the CLI's `--id`), in the plans file's
demo applications, and in the copy to Postgres and back. Migration 17 and Postgres schema 6
refuse to apply while one exists (the preview says so). No path renames an application.

## Delivery

**The request.** `POST` with the stored `json-v1` body, exactly as queued, and four headers
whose values the gateway makes: `Content-Type: application/json`; `X-Gateway-Delivery`, the
notification's id (the same across retries, and across channels: receivers deduplicate on it);
`X-Gateway-Timestamp`, Unix seconds at this attempt; and `X-Gateway-Signature`,
`v1=<hex HMAC-SHA256(key, timestamp + "." + body)>`, one `v1=` per signing key (two during a
rotation). The HMAC key is the 32 bytes the signing key's 64 hex digits spell. Every attempt is
signed afresh. Nothing a user typed reaches a header; the body is `json.dumps` of the payload's
fields. `build_request` is the alert modules' one `reveal()` (M36), inside the vault's leases.

**The connection.** `webhook_client()` in `egress.py` is the only way a webhook is sent (an AST
test), and a new one is made for every attempt, so every delivery resolves and checks again
(M45). It goes through `GuardedBackend`, with no local destination ever:

- the name resolved through the injected resolver on every connection, and every address it
  gives checked (one that is not public refuses: loopback, private, link-local, metadata, CGNAT,
  multicast, reserved, and the IPv6 forms judged on the IPv4 they carry);
- the socket opened to the checked address, never the name again (TLS still checks the name);
- the port the URL's effective port, 443 or one `GATEWAY_ALERT_WEBHOOK_PORTS` allows, checked
  again at connect;
- TLS with `provider_tls_context()` (certificates required, names checked, TLS 1.2 at least;
  `trust_env=False`, so no variable can replace it). A failed handshake is a permanent `tls`;
- no redirects (`follow_redirects=False`): a 3xx is a permanent `http_3xx`, its `Location`
  neither fetched, stored nor logged;
- no proxy from the environment (`trust_env=False`);
- each stage bounded on the injected clock: name resolution, connect, the handshake, each write
  and each read 5 s; the whole attempt 10 s. Each expiry is `timeout`, retried;
- at most 4 KiB of the answer read, then discarded: only its status class is kept.

**Nothing of an attempt is logged by the HTTP libraries.** `httpx` logs every request's full URL
at INFO, and `httpcore`'s DEBUG trace logs the answer's headers. Group 2's leak scan found both.
What the gateway does about it, exactly:

- **The filter (`QuietAttempt`) is process-wide in where it sits:** it is attached once to the
  six loggers of `httpx` and `httpcore`, the first time a webhook is sent, and stays attached for
  the life of the process. It is never removed.
- **It is switched on per attempt, not per process:** by a context variable set when `send`
  starts and reset in its `finally`, on every path (delivered, a timeout, a connection error, a
  redirect, any failure). It drops a record only while that variable is set in the context the
  record is made in: the attempt's own task and the tasks it starts.
- **So a concurrent provider call keeps its lines.** Another task, a chat request's provider
  call among them, logs through the same loggers as before, even while an attempt is in
  flight. Tested: a provider-style `httpx` line logged by another task during an attempt is
  kept, and the same task's `httpx` line after an attempt is kept (the reset). A task started
  from inside an attempt would inherit the quiet state; none is.
- **What an operator loses:** `httpx`'s and `httpcore`'s own lines for webhook attempts. The
  gateway's line for a failed attempt (`alert delivery failed: channel <id>, <code>`) and the
  `alert_delivery_failed` event take their place, with no URL in them.

**Codes.** `delivered`; `egress_refused`, `port_refused`, `dns_failure`, `timeout`,
`connect_failed`, `tls`, `http_3xx`, `http_4xx`, `http_5xx`; `payload_too_large`,
`channel_secret_unreadable`, `channel_gone`, `channel_disabled`; and for suppressed rows
`rule_cap` and `organization_cap`. Each is chosen by the kind of failure, never formatted from
an exception (A4-13).

**Retries.** A connection failure, a timeout, `dns_failure`, a 5xx, a 408 and a 429 are retried;
everything else is permanent. Six tries in all: 30 s, 2 min, 8 min, 30 min and 2 h after each
failure, each with up to 10% jitter from a seeded random source, or the receiver's
`Retry-After` (whole seconds, at most an hour) when it gives one.

## The outbox and the worker

Every instance runs a delivery worker, at most 4 deliveries at once. It **claims** due rows:
queued and due, or sending with a claim past its 60 s (a dead instance's: taken again, so a
delivery is made at least once). On Postgres the claim is `FOR UPDATE SKIP LOCKED`, so two
workers never claim one row and neither waits for the other. Each claim is a fresh
`claimed_by`. The worker **records** each outcome with a compare-and-set on its claim (`WHERE
claimed_by = ?`), so a worker whose claim was taken again writes nothing (A4-2). The channel's
health changes in the outcome's transaction: delivered resets `consecutive_failures`; a failed
attempt adds one, retried or not. Neither changes the channel's version.

**No store connection is held during a network call** (decided 2026-11-08). A delivery makes
three short transactions: the claim; the read of its channel and sealed secrets (decrypted
after the connection is given back); and the outcome. The HTTP request runs between them, and a
guard raises if the task holds a transaction when it starts one. On Postgres the worker shares
the evaluator's pool, raised from 2 connections to 3. Tested with four webhooks that never
answer: the evaluator completes a cycle, a fifth delivery is claimed and recorded by a second
worker on the same pool, and chat and generate requests are answered (on Postgres, and on
SQLite, whose one connection must never wait across an await).

**A channel deleted after queueing** fails its deliveries as `channel_gone`; one disabled fails
them at send as `channel_disabled`; one whose secrets no loaded master key can read fails them as
`channel_secret_unreadable`. Nothing is re-routed.

## What the evaluator queues

Each incident change's recorded notifications are queued **in the change's own transaction**
(`IncidentStore.apply`'s `then`, M22): a change rolled back queues nothing, and one made twice
queues once. Held notifications (quiet hours) get no delivery; their summary does.

**An incident's first channels** (`incidents.notify_channels`, migration 17's column, now NULL
until set): the channel ids its first delivered notification went to, ids only. They are set
once, by the statement that records that notification (`WHERE notify_channels IS NULL`); the
compare-and-set update leaves the column out (A4-8). Its later notifications (resolved,
reminders, firing again) go to those of them still in the organization and enabled, never to
the rule's channels as they are now (A4-7). A reopened incident keeps them.

## The caps and the suppressed summary

| Cap | Variable | Default | Range |
|---|---|---|---|
| Per rule, an hour | `GATEWAY_ALERT_CAP_PER_RULE` | 6 | 1 to 60 |
| Per organization, an hour | `GATEWAY_ALERT_CAP_PER_ORGANIZATION` | 60 | 1 to 600 |
| Test sends per channel, an hour | `GATEWAY_ALERT_TEST_SENDS_PER_CHANNEL` | 10 | 1 to 60 |
| Test sends per organization, an hour | `GATEWAY_ALERT_TEST_SENDS_PER_ORGANIZATION` | 30 | 1 to 300 |

A value that is not a whole number in its range (0, a negative number and an empty value
among them) refuses the start, naming the variable; no cap can be turned off, and each default
is at most a sixth of its maximum. A notification counts once whatever its number of channels,
against its rule's hour and then its organization's (one refused by its organization has
counted against its rule), each by one conditional upsert in the outbox's transaction (M25),
the hour on the store clock. Over a cap, its deliveries are queued `suppressed` with
`rule_cap` or `organization_cap`.

**The summary:** in its first cycle after an hour ends, the evaluator's holder queues one
`suppressed_summary` an organization (a unique index on organization and hour, M26), with the
number of notifications and rules suppressed and the hour, to the default channels. It is not
counted.

- **Only the hour just ended is summarised.** Each evaluator tries once an hour, for the last
  complete hour. If no instance holds the lease through the whole of the following hour (every
  instance down, or none able to reach the store), that hour's summary is never sent. The
  suppressed deliveries stay in the outbox (`GET .../alert-channels/{channel}/deliveries` shows
  them, state `suppressed`, with `rule_cap` or `organization_cap`), so nothing is lost from the
  record, only the notice of it.
- **A summary goes only to default channels.** An organization with no enabled default channel
  gets none: its suppressed notifications are visible only in the deliveries list.

**Instances agree, or say so** (the main plan's decision 19, for the four caps only; decided
2026-11-08). On every lease attempt, every instance writes its four values and their sha256 to
`instance_ceilings` (migration 17, Postgres schema 6), on the store clock, and reads the other
**live** instances' rows: those written in the last 60 s. A dead instance's row stops counting
then, and is deleted after an hour. A difference is reported, never refused: an
`alert_caps_differ` event naming the caps that differ, and a warning log line, once for each new
difference. The row holds the four numbers and their digest; the event, the caps' names.

## Test send

`POST /alert-channels/{channel}/test` (`alert_channels.test`, org_admin only, no step-up) sends
one `test` notification now, through the same attempt, and answers its code (`{"outcome":
"delivered", "delivered": true}`), never anything the receiver said. It is counted per channel
and per organization an hour, in Valkey in shared mode (keys of their own) or in memory: over
either, 429 `test_send_limited` and nothing is sent; a counter that cannot answer refuses (503).
A disabled channel is sent nothing (409 `channel_disabled`). It is audited as
`alert_channel_tested` with its code, and changes neither the outbox nor the channel's health.

## `channel_delivery_failing`, the built-in

Built in code, not a rule row: one per organization, always on, which cannot be changed,
disabled or deleted. Its series are the organization's channels. A channel with 3 consecutive
failed attempts fires one (warning) to every default channel **but the failing one** (CDF-3),
and it resolves when that channel next delivers (`channel_delivered`) or is deleted
(`channel_deleted`). Its incidents are rows of `incidents` with `application_id = ''` (which no
application has) and the organization's own id, under the organization's rule id: `rul_cdf_`
and the first 16 hex characters of an HMAC of the organization id under its own key derived
from the pepper (`KeyPurpose.BUILTIN_RULE_ID`).

Who sees them: the organization's org_admins and viewers, through `GET .../incidents`, where
they show with `application_id: null` and the channel id as series. Nobody else: an app_admin
without an organization role does not, another organization's list never holds them, and no
application endpoint answers them (the same 403 bytes as an incident that does not exist), so
they cannot be acknowledged.

## Monitoring

`gateway_alert_deliveries_total{channel_type, result}` counts this instance's attempts by
result, with no organization, channel or target (and the snapshot's `alert_deliveries`, note
15). Each failed attempt publishes `alert_delivery_failed` (the channel id and the code) with a
log line. Monitoring decides nothing.

## Verifying a delivery (for receivers)

A receiver should check three things: the signature, the timestamp's age, and that it has not
handled the delivery id before. This is a reference in Python's standard library; a test runs it,
exactly as written here, on a real delivery. Keep the delivery ids you have handled for at least
as long as you accept a timestamp (5 minutes here).

<!-- receiver-recipe -->
```python
import hashlib
import hmac
import time

MAX_AGE_S = 300
seen: set[str] = set()  # delivery ids already handled; keep each at least MAX_AGE_S


def verify(key_hex: str, headers: dict[str, str], body: bytes, now: float | None = None) -> bool:
    """True for a fresh, signed delivery not handled before; then it is marked handled."""
    timestamp = headers.get("X-Gateway-Timestamp", "")
    delivery = headers.get("X-Gateway-Delivery", "")
    if not timestamp.isdigit() or not delivery:
        return False
    if abs((time.time() if now is None else now) - int(timestamp)) > MAX_AGE_S:
        return False  # too old (or too far ahead): it may be a replay
    expected = hmac.new(
        bytes.fromhex(key_hex), timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    given = [
        part[len("v1=") :]
        for part in headers.get("X-Gateway-Signature", "").split(",")
        if part.startswith("v1=")
    ]
    if not any(hmac.compare_digest(expected, g) for g in given):
        return False  # not signed with your key
    if delivery in seen:
        return False  # handled already: a repeat or a retry that was delivered
    seen.add(delivery)
    return True
```

During a rotation each delivery carries two `v1=` values, so a receiver that still has the old
key keeps verifying for 24 hours.

## Timing (group 2)

MEASURED on an Intel Core Ultra 7 155H (22 logical cores, 15.4 GB), Windows 11, Python 3.12.10,
a SQLite file and the Postgres of `docker-compose.postgres.yml`, at PR #39's group 2 commit
against group 1's (`6e007a9`), three runs each, alternated:

- **The request path** (`benchmarks/request_store_calls.py`, `benchmarks/key_checks.py`): the
  same store calls (5 for the gateway tier, 12 for an organization provider) and Postgres round
  trips (22 and 36) before and after; the p50 ranges overlap (gateway tier on Postgres 14.210
  to 14.828 ms before, 14.120 to 14.674 after). Group 2 adds nothing to a request.
- **One delivery in process** (`benchmarks/alert_delivery.py`, scripted network, no receiver):
  p50 2.984 to 3.227 ms on SQLite, 12.109 to 12.407 ms on Postgres (nine passes each).
- **An evaluation cycle with the outbox** (`benchmarks/alert_evaluation.py`): the first cycle,
  whose firings now queue their deliveries in their transactions (250 at 5,000 applications),
  takes 4,084 to 4,174 ms on Postgres (3,141 to 3,203 before) and 1,245 to 1,272 ms on SQLite
  (1,138 to 1,163); steady cycles, with nothing to queue, are unchanged.

## What group 2 decided beyond the plan

- **The ceilings row** holds the four cap values and their digest, compared on each lease
  attempt; only live instances count (60 s), a dead one's row is deleted after an hour.
- **Every failed attempt counts toward `consecutive_failures`**, retried or not. So a
  receiver that fails three tries of one delivery (the first, and the retries at 30 s and
  2 min) fires the built-in `channel_delivery_failing` within about 2.5 minutes, while that
  delivery still has three retries left; three different deliveries failing once each do the
  same. It resolves when the channel next delivers anything. A receiver that is down briefly
  can therefore raise and resolve the built-in during one delivery's retries.
- **A disabled channel's queued deliveries fail at send as `channel_disabled`**, and a test send
  to a disabled channel is refused: a disabled channel gets nothing.
- **`Retry-After` replaces the backoff** when the receiver gives one (at most an hour).
- **A test send changes neither the outbox nor the channel's health.**
- **The HTTP libraries' log records of an attempt are dropped** (above).
- **The built-in's payload** is kind `channel_delivery_failing` when it fires and `resolved`
  when it resolves, with `application` and `rule` null and the channel id in `details`.
