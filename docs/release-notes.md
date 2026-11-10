# Release notes

What an operator upgrading the gateway should know: changes in behaviour that are not new
features. Newest first. Each entry names its PR and its note.

## Unreleased

### PR A5 (email channels and verified recipients)

- **An existing SQLite database needs `scripts/manage.py migrate` before the server starts.**
  Migration 18 rebuilds `alert_channels` (SQLite changes a check only by rebuilding a table), so
  the server refuses to run it on an existing database and names the command. The CLI backs the
  database up first, beside it; `--dry-run` shows what it would do. Every channel keeps its id.
  A new database is migrated by the server as before (note 48).
- **Postgres is brought to schema 7 at start, as before;** its rollback is refused while an email
  channel or a recipient's delivery exists (note 48).
- **Four more cap variables,** `GATEWAY_EMAIL_CONFIRMATIONS_PER_*`, with defaults; a value out of
  range refuses the start, as A4's do. They join the ceilings row, so instances with different
  values report it (note 48).
- **Email is sent only when the operator sets it up:** with no `GATEWAY_SMTP_HOST` or no
  `GATEWAY_ALERT_EMAIL_DOMAINS`, email channels cannot be created and nothing changes. With them,
  mail goes over STARTTLS or implicit TLS only, to verified recipients only (note 48).
- **The monitoring snapshot's `alert_deliveries` gains `email` and `smtp_lookups_abandoned`,**
  `/metrics` gains `gateway_alert_smtp_lookups_abandoned` and the email results of
  `gateway_alert_deliveries_total`, and there is one more event type, `alert_smtp_resolver_busy`
  (note 15).

### PR #39 (A4: alert channels and their webhook delivery)

- **Provider calls are now held to TLS 1.2 or higher.** The pinned TLS context
  (`gateway.egress.provider_tls_context`, note 33) used to take its lowest protocol version from
  the platform's OpenSSL defaults: TLS 1.2 on some systems, lower on others (CI's Linux runner
  allowed any). It now sets TLS 1.2 itself, for organization providers, the operator's own
  adapters and webhooks alike. A provider that offers only TLS 1.0 or 1.1 now fails its
  handshake, as a provider with a certificate that does not verify does. No setting turns
  this off (note 46).
- **The HTTP libraries no longer log webhook attempts.** `httpx`'s INFO request line and
  `httpcore`'s DEBUG trace are dropped for webhook attempts only, since they carry the webhook
  URL (a secret) and the receiver's headers. Other requests, provider calls among them, log as
  before (note 46).
- **The alert evaluator's Postgres pool is 3 connections** (it was 2), shared with the webhook
  delivery worker (note 46).
