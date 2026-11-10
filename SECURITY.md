# Security

## Reporting a vulnerability

Please report it privately through GitHub's **private vulnerability reporting**: the
repository's *Security* tab, then *Report a vulnerability*. Do not open a public issue with the
details. If that button is not there, open an issue that says only that you have a security
report and asks for a private channel.

Say what you found, how to reproduce it, the commit you tested, and what an attacker gains.
This is a one-person project: expect an acknowledgement within a week, and a fix or a written
decision after that. Fixes go into the next release and its [release notes](docs/release-notes.md).

**Supported versions:** the latest release and `main`. There is no backporting.

## Scope

In scope: the code in this repository, which is the `gateway` package, `scripts/manage.py`, the
dashboard and the admin console in `frontend/`, the Dockerfile and compose files, and the CI
workflow, run as the documentation says.

Out of scope:

- **The known limits below.** They are documented gaps, not undisclosed vulnerabilities; a way to
  get past one of them *further than stated* is in scope.
- **A deployment that exposes the server to a network.** The documentation says not to, until A8.
- Third-party services the operator or an organization chooses (hosted model providers, webhook
  receivers, SMTP servers), the Llama model weights, Docker Desktop, and the dependencies
  themselves (report those upstream; a way this repository uses one unsafely is in scope).

## The threat model

**What is protected.** The secrets the operator holds in the environment: the API-key pepper,
the vault's master key and the bootstrap admin's password. Secrets derived from or sealed under
them: API keys (stored only as an HMAC), password hashes (argon2id), sessions (stored only as
digests), organizations' provider keys, webhook URLs and signing keys, and the SMTP password
(sealed with AES-256-GCM, each bound to its scope). Also each organization's data, kept apart
from every other's, and the rule that **no prompt, response or token id is stored or logged**.

**Who is trusted.**

- **The operator** holds the environment and the CLI and is fully trusted.
- **Other processes on the same machine are trusted until A8.** Monitoring, `/metrics`, load
  control, the Playground, the dashboard, console logins and the admin endpoints answer only
  loopback peers (and networks listed in `GATEWAY_LOCAL_NETWORKS`).

**Who is not trusted.**

- Clients with an API key: each sees only its own application.
- Console users: each acts within its roles, through one permission table that denies by
  default, with step-up for sensitive actions.
- Browsers on other sites: refused by the Origin, Host and content-type checks and CSRF tokens.
- Organizations: each is kept apart from every other, and a missing resource is answered
  exactly as a forbidden one.
- Hosted providers, webhook receivers and anything their names resolve to: reached only through
  the egress guard, which allows https only, public addresses checked at connect, no redirects,
  no proxy, and TLS 1.2 or higher.

**How each rule is held.**

- Every rule about who may do what, and every refusal, has a test and a mutation check: the
  rule broken on purpose, the failing tests shown, the code restored byte for byte.
- Every refused login, API key, step-up and confirmation code gets one answer, after the same
  steps, with a structural test that they are the same.
- Every path that handles a secret has a leak scan with a planted control that must be found.

The notes behind each part: [20](docs/20-applications-api-keys.md) (keys),
[25](docs/25-local-only.md) (local only), [27](docs/27-users-and-sessions.md) to
[30](docs/30-management-endpoints.md) (identity), [32](docs/32-provider-settings-and-vault.md)
(vault and egress), [46](docs/46-alert-channels.md) and [48](docs/48-email-channels.md) (channels).

## Known limits

- **Not for a network.** Plain HTTP on `127.0.0.1`. Passwords, session cookies and API keys
  cross the loopback interface in clear text, and the session cookie is `Secure` only with
  `GATEWAY_COOKIE_SECURE=1` behind HTTPS ([note 25](docs/25-local-only.md),
  [note 27](docs/27-users-and-sessions.md)).
- **Any local process can use the local-only endpoints** (each path and everything under it).
  In the compose demo, Docker's bridge network counts as local:

  | Endpoint | What it gives whoever can reach it |
  |---|---|
  | `/v1/monitor` | The snapshot and live stream: every application's usage and spend |
  | `/metrics` | Prometheus text, and the summary and detail views |
  | `/v1/overload` | Each model's overload state |
  | `/v1/control` | Load control: start and stop load, change the speed (off by default) |
  | `/v1/playground` | Sends requests as an application, with keys the server holds |
  | `/dashboard` | The dashboard and the admin console's page |
  | `/v1/auth` | Console login, logout, sessions and step-up; a password is needed |
  | `/v1/admin` | Users, applications, keys, providers, modes, settings, alert rules, channels and incidents; a session, a role and, where flagged, step-up are needed |
- **No limits per source address** on failed logins or failed key checks: the server cannot yet
  trust a client address (A8). Logins are limited per username, with a lockout.
- **A failed login for a username that exists takes about 3 to 7 ms longer on average** than for
  one that does not, on the machine measured. A patient local process could tell them apart
  ([note 27](docs/27-users-and-sessions.md)).
- **The audit log is not tamper-evident:** anyone who can write the database can change it.
- **Rotating the pepper means issuing every API key again**; the vault's master key can be
  rotated (`scripts/manage.py rotate-vault-key`).
- **A fallback mode other than `local_only` sends prompts, and tool definitions and results, to
  a third party the organization chose**, under that party's terms ([note 33](docs/33-fallback-modes.md)).
- **What has not been tested:** no hosted provider live, no real SMTP server, no real webhook
  receiver, and nothing beyond one Windows laptop and CI's Linux runners. There has been no
  external review or penetration test.
