# Release notes

What an operator upgrading the gateway should know: changes in behaviour that are not new
features. Newest first. Each entry names its PR and its note.

## Unreleased

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
