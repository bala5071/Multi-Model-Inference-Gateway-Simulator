# 48. Email channels, verified recipients and the vault's platform scope

PR A5 of `docs/settings-and-alerts-plan.md`, built as `docs/settings-and-alerts-a5-plan.md` (the
A5 plan) defines it, on A4 as built (note 46). Two commit groups: **group 1** (this part) makes
no outbound connection; **group 2** delivers by email (its own section below, when built).

## The operator's settings

All in each instance's environment; a value set that is not valid refuses the start, naming the
variable and never repeating its value (`gateway.alerts.email.email_settings_from`).

| Variable | Meaning |
|---|---|
| `GATEWAY_SMTP_HOST`, `GATEWAY_SMTP_PORT` | The SMTP server (a name, one label allowed, or an address). The port defaults to 587 for STARTTLS and 465 for implicit TLS. |
| `GATEWAY_SMTP_TLS` | `starttls` (default) or `implicit`. There is no plain option. |
| `GATEWAY_SMTP_USERNAME` | The login. Unset: no `AUTH`, for an operator's own relay; TLS is still required. |
| `GATEWAY_SMTP_FROM` | The sender: one address, checked as a recipient's form is. Required with the host. |
| `GATEWAY_ALERT_EMAIL_DOMAINS` | Exact domains, comma-separated: `example.com` allows `ops@example.com`, not `ops@eu.example.com`. A `*` refuses the start. |

With no host or no allowed domain, email is not set up: creating an email channel answers 422
(`type`: email is not set up on this server), and the start does not fail.

**Group 1 wires no mailer into the server**, so a server built from group 1 refuses every email
channel the same way, whatever its settings. The recipient service and its endpoints are built
and tested with a recording mailer.

## The SMTP password: the vault's platform scope

- **Set only at the CLI:** `scripts/manage.py set-smtp-password` asks twice with no echo, or
  reads the environment variable the operator names (`--from-env VAR`). It refuses without the
  SMTP settings, or when no username is set (no login, no password). It prints nothing of it,
  and audits `smtp_password_set` with its kind only (target `platform_secret`, `smtp_password`).
- **`PlatformBinding`** (kind, host, port, username): associated data
  `vault-platform-v1|smtp_password|host|port|username|key_id`. The first field names the scope,
  and the key id is last and has no `|`, so a `|` in a username cannot make two bindings alike.
  A password set for one server does not open for another host, port or username.
- **Its own key:** `HMAC-SHA256(master, "gateway vault scope platform")`. Provider keys and
  channel secrets keep the master key, so no existing row is resealed.
- **Stored apart:** `platform_secrets` (kind, key id, nonce, ciphertext, host, port, username,
  updated at), no organization column, no last four characters. Read only by the platform secret
  stores, which only the SMTP sender (group 2) and the CLI use: a test reads every module's SQL.
- **Rotation:** `rotate-vault-key` reseals the platform scope too, its subkey from the new
  master key, in the same transaction as provider keys and channel secrets, and reports any it
  cannot read.
- **Tested end to end:** no organization or provider key opens it. Every `Binding` and
  `ChannelBinding` of two organizations is tried, including crafted ones (provider id `smtp`,
  type `smtp`, origin the SMTP host and port; organization `smtp`), under the master key and
  under the platform subkey. Ciphertexts are swapped between the three tables, and the SMTP
  ciphertext is copied into a provider key row and test-connection is run. Every attempt fails,
  and the provider path sends nothing; the control, the right binding, opens it. Each
  protection is also tested alone: the associated data of every scope differs for the crafted
  values (M41), and the platform ciphertext under the master key with the right associated data
  gives `InvalidTag` (M42).

## Email channels and their recipients

- **An email channel** belongs to an organization, counts toward its 20, has a name, `enabled`
  and `default`, and no URL (`url_origin` is null) and no signing key: `rotate-key` answers 409
  `channel_has_no_key`, and a `PATCH` with a `url` answers 422. It is created with 1 to 10
  recipient addresses (`recipients` in place of `url`).
- **An address** is checked when added (`gateway.alerts.email.check_address`):
  - one `@`;
  - a local part of letters, digits and ``.!#$%&'*+/=?^_`{|}~-``: no quoted string, comment or
    display name, no dot at an end or two together, at most 64;
  - a domain that encodes as IDNA, at most 253, exactly one of the allowed domains;
  - at most 254 in all, with no control character, whitespace or backslash anywhere.

  It is stored as entered with its domain lowercased and in ASCII. One address once per channel.
- **Who sees an address:** only the organization's org_admins, through the channel's view and
  the recipient endpoints. Audit rows carry the channel and recipient ids only.

## Verified recipients

- **A recipient starts `unverified`** and is sent one confirmation; until the code is entered it
  gets nothing else (group 2 queues deliveries for verified recipients only).
- **The code:**
  - Ten characters of RFC 4648's base32 alphabet, `ABCDEFGHIJKLMNOPQRSTUVWXYZ234567` (5 bits
    each): **50 bits**. Each is drawn with `rng.randrange(32)` from the random source the server
    passes, `random.SystemRandom`; tests pass a seeded one.
  - Shown as two groups of five, and entered in any case with spaces and hyphens ignored.
  - **Stored only as an HMAC** under its own derived key (`KeyPurpose.EMAIL_CONFIRMATION`), over
    the channel, the normalised address and the code. So a code confirms only the (channel,
    address) it was sent for: the same address on another channel is verified again.
  - Valid 24 hours on the store clock, used once, void after 5 wrong entries; a re-send makes a
    new one and voids the old.
  - Never logged, audited, returned or put in an event. It exists in clear only between being
    drawn and being handed to the mailer, after the commit.
- **One answer for every refused code:** wrong, expired, used, voided, unknown, another
  channel's or another organization's recipient all answer 409 `confirmation_invalid`, byte for
  byte, after the same steps: one lookup, one HMAC, one comparison against the stored digest or
  a dummy one. A structural test records the steps of every case, with the early-return
  mutation (M47).
- **Guessing:** 5 wrong entries void a code, and each new one takes a re-send, which the
  per-address cap allows 3 times a day by default. So at most 15 guesses a day reach one
  address's codes: a chance of at most 15 / 2^50, about 1.3 x 10^-14.

## The confirmation caps

A send over any cap answers 429 `confirmation_limited` and sends nothing; a counter that cannot
answer refuses. A confirmation goes to one address, so each counts one per recipient.

| Cap | Default | Range | Variable | Held in |
|---|---|---|---|---|
| Per address, 24 hours, across every channel and organization | 3 | 1 to 18 | `GATEWAY_EMAIL_CONFIRMATIONS_PER_ADDRESS` | `confirmation_caps`: a durable row keyed by an HMAC of the normalised address (`KeyPurpose.EMAIL_ADDRESS`), one conditional upsert on the store clock |
| Per channel, an hour | 10 | 1 to 60 | `GATEWAY_EMAIL_CONFIRMATIONS_PER_CHANNEL` | the window counters (Valkey, or memory) |
| Per organization, an hour | 20 | 1 to 120 | `GATEWAY_EMAIL_CONFIRMATIONS_PER_ORGANIZATION` | the window counters |
| Per instance, all organizations, an hour | 200 | 1 to 1,200 | `GATEWAY_EMAIL_CONFIRMATIONS_PER_INSTANCE` | memory: a backstop |

- A4's rule holds: each range starts at 1, so none can be turned off; each default is at most a
  sixth of its maximum. They are part of `AlertCaps`, so they join the ceilings row: the
  `instance_ceilings` row's JSON gains their four names (the table is unchanged), and instances
  that differ are reported, not refused.
- **The order:** the hourly window counters and the instance's cap are counted first, then the
  per-address cap inside the write's transaction. A refusal by the per-address cap undoes the
  write, and the instance's count is given back. A window counter cannot be given back, so a
  send refused later may still have used a window slot: the windows err towards fewer sends.
- **The normalised address** for the per-address digest is NFC, stripped and lowercased whole,
  stricter than mail servers.

## Endpoints

Under `/v1/admin/orgs/{org}`, local only until A8, `Cache-Control: no-store`, CSRF and the Origin
check on writes. A missing organization or channel answers exactly as a forbidden one, before
anything else. A missing recipient does too on re-send and delete. On confirm the recipient is
part of the code's check, so an unknown one gets the code's one answer (409).

| Method | Path | Action | Success |
|---|---|---|---|
| `POST` | `/alert-channels/{channel}/recipients` | `alert_channels.write` | 201 |
| `POST` | `/alert-channels/{channel}/recipients/{recipient}/resend` | `alert_channels.confirm_recipient` | 200 |
| `POST` | `/alert-channels/{channel}/recipients/{recipient}/confirm` | `alert_channels.confirm_recipient` | 200 |
| `DELETE` | `/alert-channels/{channel}/recipients/{recipient}` | `alert_channels.disable` | 200 |

- **Adding** needs step-up, consume-on-use (`alert_channels.write`), marked in the transaction
  that adds it. It answers the recipient, `unverified`, and the confirmation's outcome code. On
  a webhook channel it answers 409 `channel_not_email`; past 10, 409 `recipient_limit`; an
  address the channel has, 409 `recipient_taken`.
- **Re-send and confirm** are `alert_channels.confirm_recipient` (the new permission row:
  organization level, org_admin only, no step-up; capped, and one answer for every refused
  code). Re-sending to a verified recipient answers 409 `recipient_verified`.
- **Removing** needs no step-up (it narrows where data goes), is audited, and sends no notice.
- A channel's view (`GET /alert-channels` and `/alert-channels/{channel}`) lists an email
  channel's recipients for the org_admin: id, address, state, when added and verified, and when
  the code expires; never the code or its digest. Creating an email channel answers its
  recipients and each confirmation's outcome, with no signing key.
- Audit: `alert_recipient_added`, `alert_recipient_confirmation_sent`,
  `alert_recipient_verified`, `alert_recipient_removed`, each with the channel and recipient
  ids. Refusals are `management_refused` with the reason, as for every management refusal.

## Migration 18 and Postgres schema 7

- **SQLite migration 18:**
  - **Rebuilds `alert_channels`:** `type` webhook or email, and
    `CHECK ((type = 'webhook') = (url_origin IS NOT NULL))`. Every row is copied in rowid order
    with its id. It is a table rebuild: the server refuses it on an existing database, and
    `scripts/manage.py migrate` backs up first, with `--dry-run`.
  - **The outbox:** adds `notification_deliveries.recipient_id`, and the unique index becomes
    (notification, channel, `COALESCE(recipient_id, '')`).
  - **New tables:** `alert_recipients`, `confirmation_caps` and `platform_secrets`.
  - **Reversing it** drops the new tables, gives the index back and rebuilds `alert_channels`
    as 17 made it. It is refused while an email channel or a recipient's delivery exists.
- **Postgres schema 7:** the same, with the type constraint replaced in place; its rollback is
  refused the same way. The outbox's insert takes any unique conflict (as SQLite's `INSERT OR
  IGNORE` does), since its index is now an expression.
- **The copy between databases** moves and compares the three new tables.
- **Tested on a populated database,** on SQLite and on Postgres. Two organizations, webhook
  channels, their secrets, deliveries in every state, caps rows, a summary, an incident with
  `notify_channels`, and rules naming the channels. Checked:
  - every row of every table before and after, column by column, and each table's count;
  - every channel's id, and a new channel getting a new one;
  - the foreign keys (`foreign_key_check` empty and the keys on again);
  - a failure planted after the copy leaving the database exactly as it was;
  - the reverse migration giving version 17's schema and every row back.
