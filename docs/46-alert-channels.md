# 46. Alert channels: webhooks, their secrets, the outbox

PR A4 of `docs/settings-and-alerts-plan.md`, as `docs/settings-and-alerts-a4-plan.md` (approved
2026-11-08) defines it. A4 is built in two groups (that plan's section 22). This note covers
**group 1**: channels, their secrets, their endpoints, and the notices that changes queue. Group
1 opens no outbound connection. Group 2 (delivery) adds its own sections.

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
