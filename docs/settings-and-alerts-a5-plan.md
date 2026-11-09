# A5 plan: email channels, verified recipients and the vault's platform scope

The plan for PR A5 of `docs/settings-and-alerts-plan.md` (the main plan), written before any
code, for approval. A5 adds a stored secret (the SMTP password), a new outbound surface (the
operator's SMTP server) and a migration that rebuilds a table, so plan approval comes first,
and any conflict with the main plan or with A4 as built is decided by the reviewer.

**Status:** proposed 2026-11-09; the reviewer's decisions of 2026-11-09 on the six questions
are applied (section 16). One question remains open before the build (section 16, Q7: name
resolution and the hard deadline). A4 (#39) is merged; this plan builds on it as built
(`docs/46-alert-channels.md`).

**Scope.** Part F's A5 row, and nothing from A6 or later:

- email through the operator's SMTP server: the operator settings, STARTTLS or implicit TLS
  (never plain), the domain allowlist;
- the vault's **platform scope** and `platform_secrets` for the SMTP password, with the test that
  no organization or provider key opens it;
- email channels, their recipients, and **verified recipients** with the confirmation code and
  its caps;
- delivery by email through A4's outbox and worker, test send to verified recipients;
- the in-process SMTP test server, written in the test suite;
- the leak scans extended to the SMTP password, the confirmation code and recipient addresses.

**Not in A5:** operator signals and the operator's alert channel (A6), a confirmation link (A8),
the console's channel screens (C2), HTML mail.

## 1. Conflicts with the main plan and with A4 as built

Each was put to the reviewer; the column on the right is what was decided (section 16).

| # | Conflict | Decided |
|---|---|---|
| 1 | **`alert_channels.type` is `CHECK (type IN ('webhook'))`** (migration 17). SQLite changes a `CHECK` only by rebuilding the table, and `url_origin` is `NOT NULL`, which an email channel does not have. The main plan did not foresee a rebuild here. | **Migration 18 rebuilds `alert_channels`** with `type IN ('webhook', 'email')` and `url_origin` nullable (`CHECK ((type = 'webhook') = (url_origin IS NOT NULL))`). It is a table rebuild, so it follows note 26's path: the server refuses it on a database with data, and `scripts/manage.py migrate` backs up first, with `--dry-run`. Postgres schema 7 changes the constraint in place. Tested on a populated database (section 9). Decided: the rebuild, not a separate `email_channels` table (Q1). |
| 2 | **The main plan renames `Binding` to `ProviderBinding`.** A4 kept `Binding` and added `ChannelBinding` beside it. | **Keep `Binding`**, add `PlatformBinding`. No caller changes; the scopes are told apart by their associated data and their key, which is what the tests check. |
| 3 | **Timeouts.** A4 bounds each webhook stage on the injected clock. The main plan puts SMTP on `smtplib` in a thread, whose timeouts are socket timeouts in real time. | **`smtplib` on a thread, with a hard 10 s deadline for the whole attempt:** a subclass sets the socket's timeout to what is left of the attempt's budget before every blocking socket call (section 7). A hung attempt's thread keeps its delivery slot until it finishes (Q4). Name resolution cannot take a timeout: open question Q7. |
| 4 | **The main plan's platform scope also holds A6's operator webhook secrets.** | A5 builds the scope with one kind, `smtp_password`. A6 adds its kinds. |
| 5 | **Several recipients in one delivery.** A4's outbox has one row per (notification, channel); an email channel has up to 10 recipients. The main plan does not say whether they see each other. | **One message per recipient,** each its own delivery row (notification, channel, recipient), with `To:` that recipient alone, so no recipient sees another's address and one recipient's failure is its own. **The caps count per recipient** (section 7). (Q2) |

## 2. The operator's settings

All in the environment of each instance; a value out of range refuses the start, naming the
variable.

| Variable | Meaning |
|---|---|
| `GATEWAY_SMTP_HOST`, `GATEWAY_SMTP_PORT` | The SMTP server. Unset: email channels cannot be created (422), and none exist to deliver. |
| `GATEWAY_SMTP_TLS` | `starttls` (default) or `implicit`. There is no plain option. |
| `GATEWAY_SMTP_USERNAME` | The login. Unset: no `AUTH`, for an operator's own relay; TLS is still required (Q5). Set: the password must be in the vault, or every delivery fails `smtp_credential_unreadable`. |
| `GATEWAY_SMTP_FROM` | The envelope sender and the `From` header: one address, checked as a recipient is. |
| `GATEWAY_ALERT_EMAIL_DOMAINS` | The recipient domains allowed, comma-separated, each an exact domain: `example.com` allows `ops@example.com` and not `ops@eu.example.com`; a `*` anywhere refuses the start (Q6). Empty: email channels cannot be created. |

- **The SMTP password** is never an environment variable the server reads. The operator sets it
  with `scripts/manage.py set-smtp-password`: typed at a prompt with no echo, or read from an
  environment variable the operator names (`--from-env VAR`) for automation. It is sealed in the
  platform scope (section 3) and stored in `platform_secrets`. The command prints nothing of it
  and audits `smtp_password_set`.
- **The SMTP server is the operator's,** so A4's public-address rule does not apply to it (it may
  be on a private network). Its TLS does: the certificate is verified with the pinned context
  (`provider_tls_context()`, TLS 1.2 or higher) against `GATEWAY_SMTP_HOST`.
- **STARTTLS is required when chosen:** a server that does not offer it fails the delivery with
  `tls` before any credential or message is sent. `smtplib`'s `starttls` is called with the
  pinned context, and `ehlo` is sent again after it.
- **SMTP without a login** (no `GATEWAY_SMTP_USERNAME`) is for an operator's own relay, one that
  accepts mail from the gateway's address without `AUTH`. TLS is required all the same, with the
  certificate verified: there is no setting that sends mail in the clear, logged in or not. The
  operator's note says so, and that such a relay should accept mail only from the gateway's
  hosts.

## 3. The vault's platform scope

- **`PlatformBinding`** (`kind`, host, port, username): its associated data is
  `vault-platform-v1|smtp_password|host|port|username|key_id`. The first field names the scope,
  so no binding of another scope gives the same bytes, whatever its other fields hold; a test
  checks the bytes against crafted bindings (below).
- **Its own key:** `HMAC-SHA256(master, "gateway vault scope platform")`, used for nothing else.
  The provider and channel scopes keep the master key, so no existing row is resealed. The
  label differs from the key-id label, and a test checks the two derived values differ.
- **Stored apart:** `platform_secrets` (`kind` primary key, `key_id`, `nonce`, `ciphertext`,
  `host`, `port`, `username`, `updated_at_ms`), with no organization column. It is read only by
  `SmtpSender` and the CLI: a test reads every store's SQL, as A4's does for `channel_secrets`.
- **No last four characters,** as for channel secrets.
- **Rotation:** `rotate-vault-key` reseals the platform scope too, its subkey derived from the
  new master, in the same transaction as provider keys and channel secrets. A password whose
  master is not loaded fails email deliveries with `smtp_credential_unreadable`.
- **`reveal()`** gains its second alert call site, the SMTP login (main plan decision 16).
  Leased values go to the `Redactor`.
- **The test that no organization or provider key opens it** (main plan section 9.1, as
  written there):
  - seal an SMTP password;
  - try to open its ciphertext with every `Binding` and `ChannelBinding` of two test
    organizations, including one whose provider id, type and origin are `smtp`, the SMTP host
    and its port, and one whose organization id is `smtp`, under the master key and under the
    platform subkey;
  - copy a provider key's and a channel secret's ciphertext into the `platform_secrets` row and
    open them as the SMTP password;
  - copy the SMTP ciphertext into a provider key row and run test-connection;
  - every attempt fails with `VaultIntegrityError` (or `key_unreadable` for the provider row),
    and the provider path sends nothing; the control, the right `PlatformBinding`, opens it.
- **Each protection tested alone:** associated data apart (the bytes of every scope's
  associated data differ, for the crafted values) and key apart (the platform ciphertext opened
  with `AESGCM(master)` and the right associated data, bypassing `Vault`, gives `InvalidTag`).

## 4. Email channels and their recipients

- **A channel of type `email`** belongs to an organization, counts toward its 20, has a name,
  `enabled` and `default` as a webhook does, and no URL and no signing key. It is created with
  1 to 10 recipient addresses.
- **An address** is checked when added:
  - one `@`; a local part of letters, digits and ``.!#$%&'*+/=?^_`{|}~-`` (no quoted strings, no
    comments, no display name, no dot at an end or two together), at most 64;
  - a domain that encodes as IDNA, at most 253, **exactly one of the allowed domains**;
  - at most 254 in all; no control character, whitespace or backslash anywhere.
  - It is stored as entered, with its domain lowercased and in ASCII.
- **Recipients** (`alert_recipients`): id (`rcp_` and 16 hex), channel, organization, address,
  state (`unverified`, `verified`), the code's digest, its expiry, its wrong entries, and when it
  was added and verified. One address once per channel.
- **Who sees an address:** the organization's org_admins, who entered it, through the channel's
  view. An application's readers see the channel's id, name, type, `default` and `enabled`, as
  for a webhook, never a recipient. Audit rows, events and logs carry recipient ids only.

## 5. Verified recipients

As the main plan's section 9 sets them out:

- **An added address is `unverified`** and is sent one confirmation: plain text, naming the
  organization (its name in quotes and its id) that asked to send it alerts, saying nothing else
  will be sent unless the code is entered, and a 10-character code.
- **Until the code is entered,** the address gets no notification, no test send and no summary.
- **The code's alphabet and entropy:**
  - 10 characters from RFC 4648's base32 alphabet, `ABCDEFGHIJKLMNOPQRSTUVWXYZ234567` (32
    symbols, 5 bits each): **50 bits**, 2^50 (about 1.1 x 10^15) codes. The alphabet has no
    `0`, `1`, `8` or `9`, so `O`/`0` and `I`/`1` cannot be confused.
  - Each character drawn uniformly with `rng.randrange(32)` from the injected random source; the
    server injects `random.SystemRandom` (the operating system's generator), tests a seeded
    `random.Random`. A test checks the server's wiring passes `SystemRandom`, and the mutation
    A5-18 replaces it with a seeded one.
  - Shown as two groups of five (`ABCDE-FGH23`); entered in any case, with spaces and hyphens
    ignored; anything else after that (a wrong length, a character outside the alphabet) is a
    wrong entry, answered as every refusal is.
  - Guessing: at most 15 guesses a day reach one address (section 6), so the chance of a guess
    landing in a day is at most 15 / 2^50, about 1.3 x 10^-14.
- **The code:**
  - stored only as an HMAC under a new derived key (`KeyPurpose.EMAIL_CONFIRMATION`), compared
    in constant time;
  - valid 24 hours on the store clock, single use, void after 5 wrong entries (re-send to get a
    new one);
  - bound to (channel, address): the same address on another channel is verified again;
  - never logged, audited, returned or put in an event.
- **One answer for every refusal:** wrong, expired, used, voided, unknown and another
  organization's codes and recipients all answer 409 `confirmation_invalid`, byte for byte, after
  the same steps (one lookup, one HMAC, one compare against the stored or a dummy digest). The
  structural test of note 27's refusals is applied here, with the early-return mutation (M47).
- **No link:** the console is local only until A8 (main plan decision 26).

## 6. The confirmation caps

A send over any cap answers 429 `confirmation_limited` and sends nothing. A counter that cannot
answer refuses. A confirmation goes to one address, so each cap counts one per recipient.

| Cap | Default | Range | Held in |
|---|---|---|---|
| Per address, 24 hours, across every channel and organization | 3 | 1 to 18 | `confirmation_caps`: a durable row keyed by an HMAC of the normalised address (`KeyPurpose.EMAIL_ADDRESS`), one conditional upsert `... WHERE sent < cap` on the store clock, so a Valkey restart releases nothing |
| Per channel, an hour | 10 | 1 to 60 | the window counters (Valkey or memory), like test sends |
| Per organization, an hour | 20 | 1 to 120 | the window counters |
| Per instance, all organizations, an hour | 200 | 1 to 1,200 | memory: a backstop against a bug |

- **Each is an environment variable** (`GATEWAY_EMAIL_CONFIRMATIONS_PER_ADDRESS`,
  `..._PER_CHANNEL`, `..._PER_ORGANIZATION`, `..._PER_INSTANCE`), as A4's caps are, and A4's rule
  holds (Q3):
  - **none can be turned off:** each range starts at 1, and 0, a negative, an empty value or a
    word refuses the start, naming the variable; so does a value past the maximum;
  - **conservative defaults:** each default is at most a sixth of its maximum, and the defaults
    are the main plan's numbers;
  - the four values join A4's ceilings row (`instance_ceilings`), so instances that differ are
    reported, as A4's caps are.
- **The normalised address** for the per-address digest is NFC, stripped and lowercased whole
  (stricter than mail servers: two spellings that may be one mailbox count together).
- **Guessing is bounded by the code, not by a new counter:** 5 wrong entries void a code, and
  each new code needs a re-send, which the per-address cap allows 3 times a day. So at most 15
  guesses a day reach any one address's codes, against 50 bits.

## 7. Delivery by email

- **Through A4's outbox and worker,** one row per (notification, channel, **recipient**) for an
  email channel (Q2): the claim, the read, the attempt with no store connection held, the
  compare-and-set outcome, the channel's health. The worker sends by the channel's type.
  - `notification_deliveries` gains `recipient_id` (`NULL` for a webhook), and its unique index
    becomes (notification, channel, `COALESCE(recipient_id, '')`), since SQLite and Postgres
    both treat `NULL`s as distinct in a unique index.
  - **Rows are made at queue time for the verified recipients only,** in the change's
    transaction (A4's `then`). A recipient removed or no longer verified by the time its row is
    claimed is not sent to: the row ends `cancelled`, code `recipient_not_verified`, not counted
    toward the channel's health. An email channel with no verified recipient at queue time gets
    one row with no recipient, which fails `no_verified_recipient` and counts toward its health,
    so the built-in `channel_delivery_failing` says that alerts reach no one.
- **The caps count per recipient:** A4's per-rule and per-organization notification caps count
  each recipient's message as one, so a notification to a channel of 10 verified recipients
  takes 10. A4's test-send caps count per recipient too. Suppression and the hourly summary work
  per row, as in A4.
- **The attempt** (`SmtpSender`, on a thread of its own pool of 2), one message to one
  recipient:
  - connect; STARTTLS (or implicit TLS) with the pinned context;
  - `AUTH` with the password leased from the platform scope, if a username is set;
  - `MAIL FROM` the operator's address, one `RCPT TO` the recipient, `To:` that recipient.
- **The hard deadline (Q4):** the whole attempt within 10 s.
  - `DeadlineSMTP`, a subclass of `smtplib.SMTP` (and of `SMTP_SSL` for implicit TLS), wraps its
    socket so that **before every blocking socket call** (connect, each `send` and `recv`, the TLS
    handshake) it sets the socket's timeout to what is left of the budget, read from the injected
    clock, and raises `timeout` if nothing is left. Setting it once per SMTP command would not be
    enough: one reply is read with several `recv` calls, and a server sending a byte at a time
    could take a full timeout for each.
  - CPython bounds one blocking call, the TLS handshake included, by the timeout in force when
    it starts, so the attempt ends within the budget plus the time of the last call's own
    non-blocking work.
  - **A hung attempt's thread keeps its delivery slot until it finishes:** the worker's limit of
    4 at once is released when the thread returns, not when the attempt is declared `timeout`.
    With the deadline, that is at most the budget.
  - **Name resolution** (`getaddrinfo`) has no timeout in Python and cannot be bounded this way
    (Q7, open).
  - Tested without real time: a scripted clock that moves on at each socket call, and a test
    server that sends one byte per read, show the attempt ends `timeout` once the budget is
    spent, whatever the server does.
- **The message:** plain text, UTF-8, built with the standard library's `EmailMessage` under the
  SMTP policy (which refuses a CR or LF in a header); no HTML, no images, no tracking, no link.
  Subject `[gateway] FIRING warning: <rule> on <application>` (and the other kinds' words),
  encoded by the library when a name is not ASCII. `Message-ID` derived from the notification
  id, so a retried message is the same message. The body is made from the payload's fields
  (section 10 of the main plan's allowlist) by a fixed template, never from a URL, a key or a
  user.
- **Codes:** `delivered`; `tls` (no STARTTLS offered, a handshake that fails), `timeout`,
  `connect_failed`, `smtp_4xx` (retried on A4's schedule), `smtp_5xx` (permanent),
  `smtp_credential_unreadable`, `no_verified_recipient`, `recipient_not_verified`
  (cancelled, above).
- **Test send** (A4's endpoint): one `test` message to each verified recipient, each its own row,
  each counted by A4's test-send caps; `no_verified_recipient` if none.
- **The built-in `channel_delivery_failing`** applies to email channels as to webhooks.
- **Metrics and events:** `gateway_alert_deliveries_total{channel_type="email", result}` and
  `alert_delivery_failed`, as A4's.

## 8. Endpoints

Under `/v1/admin/orgs/{org}`, local only until A8, `Cache-Control: no-store`, CSRF and the Origin
check on writes, missing equal to forbidden. A4's endpoints take email channels (create with
`recipients` in place of `url`; no `rotate-key`, 409 for an email channel). New:

| Method and path | Action | Step-up | Success |
|---|---|---|---|
| `POST /alert-channels/{channel}/recipients` (an address) | `alert_channels.write` | consume | 201, `unverified`, the confirmation sent (capped) |
| `POST /alert-channels/{channel}/recipients/{recipient}/resend` | `alert_channels.confirm_recipient` | | 200 (429 `confirmation_limited`) |
| `POST /alert-channels/{channel}/recipients/{recipient}/confirm` (the code) | `alert_channels.confirm_recipient` | | 200; any refusal 409 `confirmation_invalid` |
| `DELETE /alert-channels/{channel}/recipients/{recipient}` | `alert_channels.disable` | | 200 |

- **New permission row:** `alert_channels.confirm_recipient`, organization level, org_admin
  only, no step-up (capped; one answer for every refusal). The matrix is regenerated and its
  diff shown; note 28's table changes with it.
- **Removing a recipient** needs no step-up (it narrows where data goes, main plan decision 25)
  and is audited. It sends no notice: unlike disabling a channel, it removes no destination the
  organization's other channels need to hear about.
- **Audit:** `alert_recipient_added`, `alert_recipient_confirmation_sent`,
  `alert_recipient_verified`, `alert_recipient_removed` (channel and recipient ids only, never the
  address or the code), `smtp_password_set` (CLI). Refusals are `management_refused`.

## 9. Migration 18 and Postgres schema 7

- **SQLite migration 18:** rebuilds `alert_channels` (conflict 1; `rebuilds_tables`, with its
  preview, and the foreign keys checked around it); adds `notification_deliveries.recipient_id`
  and replaces its unique index (section 7); adds the four confirmation caps to
  `instance_ceilings` (section 6); creates `alert_recipients`, `confirmation_caps` and
  `platform_secrets`. Reversing it drops the new tables, the column and the index, and rebuilds
  `alert_channels` back, refused while an email channel or a recipient's delivery exists.
- **Postgres schema 7:** the same tables, column and index; the type constraint replaced in
  place; the rollback refused as on SQLite.
- **Tested on a populated database (Q1),** on SQLite and its Postgres equivalent: before the
  migration, two organizations with webhook channels, their secrets, deliveries in every state,
  caps rows, summaries, incidents with `notify_channels`, and rules naming the channels.
  - **Rows:** every row of every table read before and after, compared column by column.
  - **Ids:** every channel keeps its id, and the next channel created gets a new one.
  - **Foreign keys:** `PRAGMA foreign_key_check` empty after the rebuild, and the foreign keys
    on again (`PRAGMA foreign_keys` is 1); on Postgres, every constraint present and validated.
  - **Counts:** each table's row count equal before and after.
  - **Rollback:** a failure planted after the copy (a raising step) leaves the database exactly
    as it was, compared the same way; and the reverse migration, run on the populated database,
    gives back the version 17 schema and every row.
  - The server refuses the rebuild on this database at start, and `scripts/manage.py migrate`
    backs it up first; `--dry-run` writes nothing.
- **The copy between databases** (`migrate-to-postgres` and back) copies and compares the new
  tables.

## 10. Tests

Every store test on memory, SQLite and Postgres; Valkey ones marked `valkey`.

- **The test SMTP server** (in the suite, on asyncio streams, no dependency): EHLO, STARTTLS with
  a test certificate the test trusts through an injected context, AUTH PLAIN, MAIL, RCPT, DATA;
  it records every envelope and message, and can be scripted to answer 4xx or 5xx at each step,
  to offer no STARTTLS, or to hang.
- **TLS:** STARTTLS required, a server without it refused before AUTH, plain never possible
  (no setting), the certificate verified against the host (a wrong-name certificate fails `tls`).
- **Recipients:** each address rule; a domain outside the allowlist refused at add; 1 to 10 a
  channel; one address once per channel.
- **Verified only:** an unverified address gets only its confirmation (the test server's mailbox
  checked after a firing, a test send and a summary); the right code verifies; wrong, expired,
  used, voided and another organization's codes answer one way, byte for byte, after the same
  steps; five wrong entries void it; the code's alphabet, length and the `SystemRandom` wiring
  (section 5).
- **Caps:** the per-address cap across two organizations and two instances, and after a Valkey
  restart; per channel, per organization and per instance; a counter that cannot answer refuses;
  each variable at its bounds and one past each refusing the start.
- **Vault:** section 3's end-to-end test, the associated-data-apart and key-apart tests, the
  derived labels apart, rotation resealing all three scopes, the SQL grep for
  `platform_secrets`.
- **Delivery:** a firing, a resolved and a summary delivered to the verified recipients only;
  4xx retried on the schedule, 5xx not; the hard deadline against a hanging server and a
  byte-at-a-time server (section 7), and the slot held until the thread returns; no store
  connection held while the thread sends (A4's guard); the subject and headers with a rule or
  application name holding quotes, a backslash, a brace and non-ASCII letters; the
  `Message-ID` the same across retries; one message per recipient, each naming only its own
  recipient; the caps counting per recipient; a recipient removed after queueing cancelled;
  SMTP without a login still refused without TLS.
- **Permissions:** every new endpoint's cross-organization test (the same 403 bytes as missing);
  viewers and app_admins refused; step-up consumed on adding a recipient; the route walk.
- **Migration:** section 9's populated-database tests (rows, ids, foreign keys, counts,
  rollback) on SQLite and Postgres; the server refuses it on a database with data; the CLI backs
  up first; `--dry-run`; the copy both ways.
- **Postgres constraints,** each reached by a test of its own: one address once per channel
  (two ids), the per-address cap's conditional update from two instances at the cap, the
  channel type and `url_origin` check by raw inserts.

## 11. Mutations

Each made, its tests shown failing, restored byte for byte with sha256 before and after. A
surviving mutation of a real, untested rule stops the work for your decision.

**From the main plan's section 18:**

| # | Rule | Mutation |
|---|---|---|
| M41 | Vault scopes: associated data apart | the platform prefix dropped, fields laid out like a provider binding |
| M42 | Vault scopes: key apart | the platform subkey replaced by the master key |
| M43 | No organization or provider key opens the SMTP password | M41 and M42 together |
| M47 | One answer for every confirmation refusal | return early for an unknown recipient, before the HMAC |
| M48 | Only verified recipients | skip the verified filter on notifications (and, separately, on test sends, and on summaries) |
| M49 | The per-address cap, durable and atomic | the cap in a Valkey window instead (and, separately, checked in Python after a `SELECT`) |
| M50 | The code never stored or logged in clear | store the code instead of its HMAC; log it on send (two mutations) |

**New:**

| # | Rule | Mutation |
|---|---|---|
| A5-1 | STARTTLS required | skip `starttls` when the server does not offer it |
| A5-2 | The SMTP certificate verified | an unverified context for the SMTP connection |
| A5-3 | The domain allowlist | skip it at add (and, separately, at send) |
| A5-4 | A code valid 24 hours | no expiry check |
| A5-5 | A code used once | leave the digest after a confirmation |
| A5-6 | Void after 5 wrong entries | no count |
| A5-7 | A confirmation bound to (channel, address) | look the code up by recipient id alone |
| A5-8 | The confirmation caps | skip each of the per-channel, per-organization and per-instance counts (three) |
| A5-9 | One message per recipient, naming only that recipient | send one message with every verified recipient in the envelope and `To` |
| A5-10 | Headers refuse injection | build the subject by string formatting into raw headers |
| A5-11 | `platform_secrets` read only by `SmtpSender` and the CLI | a provider store query reading it (the grep's planted control) |
| A5-12 | Rotation reseals the platform scope | skip it in `rotate-vault-key` |
| A5-13 | 5xx not retried | retry a 5xx |
| A5-14 | The cap variables in range | skip the range check |
| A5-15 | The rebuild refused on a database with data at server start | skip the refusal |
| A5-16 | An email channel's address checks (no display name, no CR or LF) | drop the character check |
| A5-17 | No store connection held while the thread sends | send inside the claim's transaction |
| A5-18 | The code from the operating system's generator | the server wired with a seeded `random.Random` |
| A5-19 | The hard deadline | set the socket timeout once per SMTP command instead of before every socket call (the byte-at-a-time test fails) |
| A5-20 | The slot held until the thread returns | release the slot when the attempt is declared `timeout` |
| A5-21 | The caps count per recipient | count one per notification and channel |
| A5-22 | Exact domains | match the allowlist as a suffix |
| A5-23 | TLS required without a login | skip STARTTLS when no username is set |
| A5-24 | The rebuild keeps every row | drop one column from the copy's `INSERT ... SELECT` (the populated-database test fails) |

**One per new permission row:** `alert_channels.confirm_recipient` granted to app_admin, so its
refusal test fails.

The PR lists the rules that have tests but no mutation.

## 12. Leak scans

- **Channel and platform secrets** (the main plan's section 19, extended):
  - **Planted:** the SMTP password (set through the CLI), a confirmation code (the generator
    injected, so the planted value is the one sent), a recipient address, and A4's planted URL
    and keys.
  - **Exercised:** set the password, create an email channel, add a recipient, re-send, confirm
    (wrong, then right), test, deliver (delivered, 4xx retried, 5xx), rotate the vault key, remove
    the recipient, delete.
  - **Scanned:** every stored row (SQLite and Postgres), every Valkey key and value, every log
    record at DEBUG (with the `smtplib` debug level forced on, which prints the conversation:
    it must stay off, or its output be dropped as A4's HTTP libraries' are), every audit row,
    every event, the Prometheus output, `/healthz`, every admin answer, and every mail the test
    server received except the confirmation, which must carry the code.
  - **The address** may appear only in its recipient row, the org_admin's channel view, and the
    envelope and headers of mail to it: nowhere else.
  - **The control:** each planted value found in its unredacted copy.
- **Alert contents:** A4's scan extended to every email body and header.

## 13. Threat model

The main plan's section 14 entries A5 makes real:

- **The gateway used to send mail to anyone:** allowed domains only, 10 a channel, verified
  recipients only.
- **The confirmation email as the spam vector:** a fixed text that says who asked and that
  nothing follows without the code, and the four caps (the per-address one durable, across
  organizations).
- **Guessing a code:** 50 bits, 24 hours, void after 5 wrong entries, one answer for every
  refusal after the same steps.
- **A code leaked** through logs, audit or answers: stored only as an HMAC, in the leak scan.
- **The SMTP password opened by an organization's path:** its own scope, its own key, its own
  table, with the end-to-end test.

New with this plan:

- **A downgrade to plain SMTP** by a server or a network in between that hides STARTTLS: the
  delivery fails before the password or the message is sent.
- **Header injection** through a rule or application name in the subject: the library's SMTP
  policy refuses CR and LF, and names already refuse control characters.
- **Recipients learning each other's addresses:** one message per recipient, naming only it.
- **The SMTP conversation in a log** (`smtplib`'s debug output prints `AUTH`): the debug level is
  never set, and the scan forces it on to show nothing reaches a log.
- **A slow SMTP server tying up the worker:** the hard 10 s deadline for every socket call of
  the attempt, a byte-at-a-time server included, and at most 4 deliveries at once. A slow
  resolver is Q7.
- **An open relay made of the operator's relay:** SMTP without a login is documented for the
  operator's own relay, which should take mail only from the gateway's hosts; the gateway sends
  only to verified recipients in exact allowed domains either way.
- **A table rebuild losing rows:** the CLI's backup, the dry run, and the test that every row
  survives.

## 14. Timing

Measured on this machine, three runs, before (main at A4) and after:

- `benchmarks/request_store_calls.py` and `benchmarks/key_checks.py`: A5 adds nothing to the
  request path, and the stop rule of the main plan's section 20 applies;
- one email delivery in process, to the test SMTP server (claim, read, lease, STARTTLS, AUTH,
  send, outcome), on SQLite and Postgres;
- one confirm call (the lookup, the HMAC, the compare), right and wrong.

No number is written before it is run.

## 15. Decisions not in the request

Numbered, so they can be objected to.

1. **Keep `Binding`; add `PlatformBinding`** (conflict 2).
2. **The populated-database tests' content** (section 9): which tables are seeded, and the
   planted failure for the rollback. Migration 18 rebuilding the table was decided (Q1).
3. **The deadline is enforced before every socket call,** not once per SMTP command, since one
   reply takes several reads (section 7). `smtplib` on a thread was decided (Q4).
4. **The platform scope holds `smtp_password` only** in A5 (conflict 4).
5. **A recipient no longer verified when its row is claimed is cancelled,** not sent to and not
   counted toward health; **a channel with no verified recipient gets one failing row,** counted,
   so the built-in fires. One message per recipient was decided (Q2).
6. **The confirmation caps join A4's ceilings row** so instances that differ are reported.
   Environment variables with ranges was decided (Q3).
7. **A4's test-send caps count per recipient too,** as the notification caps do.
8. **The normalised address for the cap is lowercased whole,** stricter than mail servers.
9. **The SMTP password is set only through the CLI,** never read by the server from the
   environment.
10. **An email channel has no `rotate-key`** (409): it has no signing key.
11. **The thread pool for SMTP is 2,** as the password hasher's.
12. **Unset `GATEWAY_SMTP_HOST` or allowlist means no email channel can be created**, and the
    start does not fail: a gateway without email is valid.
13. **The code is shown as two groups of five, entered in any case with spaces and hyphens
    ignored** (section 5).
14. **`notification_deliveries`' unique index uses `COALESCE(recipient_id, '')`,** since both
    databases treat `NULL`s as distinct.

## 16. Questions

**Decided by the reviewer, 2026-11-09:**

1. **Migration 18 rebuilds `alert_channels`** on note 26's path, tested on a populated database:
   rows, ids, foreign keys, counts, rollback, and the Postgres equivalent (section 9).
2. **One message per recipient;** the caps count per recipient (section 7).
3. **The confirmation caps are environment variables** with ranges and conservative defaults;
   none can be turned off (section 6).
4. **`smtplib` on a thread,** with a subclass setting the socket timeout from what is left of the
   attempt's budget, to keep a whole attempt within 10 s; a hung attempt's thread keeps its
   delivery slot until it finishes. If a hard deadline is not feasible, the work stops for the
   reviewer (Q7).
5. **SMTP without a login is allowed** when no username is set; TLS is still required;
   documented as for an operator's own relay (section 2).
6. **Exact domains only** (section 2).

**Open, for the reviewer before the build:**

7. **Name resolution and the hard deadline.** Every socket call can be held to the budget (section
   7), but the host name's lookup (`getaddrinfo`, which `smtplib` calls through
   `socket.create_connection`) takes no timeout in Python: a resolver that hangs holds the thread
   for as long as the system resolver's own timeouts allow (often 5 to 30 s, set by the
   operating system, not by the gateway). So the deadline is hard for everything after the lookup,
   and not for the lookup itself. The choices:
   - (a) **Resolve on the event loop**, bounded on the injected clock by the attempt's budget,
     then hand the thread the address to connect to (the certificate still checked against
     `GATEWAY_SMTP_HOST`). The attempt then ends within 10 s, always. If the lookup hangs, the
     lookup's own thread, in the loop's default pool, runs on until the resolver gives up. It
     holds no delivery slot, socket or credential (the password is leased only after the
     lookup). This departs from "the thread keeps its slot" for the lookup alone.
   - (b) **Resolve in the SMTP thread**, keeping the slot through it. The 10 s deadline is then
     hard only after the lookup, and the lookup is bounded only by the operating system.
   - (c) **Resolve once at start and at each lease attempt** (every 10 s), in the background,
     and connect to the last good address. No lookup in the attempt at all, but a change of
     address is seen up to 10 s late, and a name with several addresses uses one.

   The plan proposes (a): the attempt and its slot are within the deadline, and what runs past
   it holds nothing.

## 17. Not built in A5

- Operator signals, the operator's alert channel, `smtp_credential_unreadable` as an operator
  signal: A6.
- A confirmation link: A8.
- The console's channel and recipient screens: C2.
- HTML mail, attachments, a per-channel subject template.
- DKIM signing: the operator's SMTP server's.
- A subdomain form of the allowlist (decided: exact domains only).
