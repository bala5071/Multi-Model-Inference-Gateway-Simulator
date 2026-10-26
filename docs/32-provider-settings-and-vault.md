# 32. Provider settings, the key vault, consume-on-use and the egress guard

A5, pull request 2 (docs/provider-plan.md, sections 3 and 4). An organization can now configure
its own model providers and give the gateway their keys, kept encrypted. In this step the only
thing that contacts an organization's provider is test-connection; chat goes on being served by
the gateway's own tiers (the default mode, `local_only`, comes in pull request 3).

## The endpoints

Under `/v1/admin/orgs/{org}`, local only like all of `/v1/admin`, each through
`requires(action, resolver)`; a test compares this table with the routes and the permission
table. `consume`: step-up that one successful call uses up.

| Method | Path under `/v1/admin/orgs/{org}` | Action | Step-up | Success |
|---|---|---|---|---|
| `GET` | `/providers` | `providers.list` | | 200 |
| `GET` | `/providers/status` | `providers.status` | | 200 |
| `POST` | `/providers` | `providers.create` | yes | 201 |
| `PATCH` | `/providers/{provider_id}` | `providers.update` | yes | 200 |
| `POST` | `/providers/{provider_id}/disable` | `providers.disable` | yes | 200 |
| `POST` | `/providers/{provider_id}/enable` | `providers.enable` | yes | 200 |
| `DELETE` | `/providers/{provider_id}` | `providers.delete` | consume | 200 |
| `PUT` | `/providers/{provider_id}/key` | `provider_keys.set` | consume | 200 |
| `DELETE` | `/providers/{provider_id}/key` | `provider_keys.delete` | consume | 200 |
| `POST` | `/providers/{provider_id}/test` | `providers.test` | | 200 |

Code: `src/gateway/providers.py`, `src/gateway/vault.py`, `src/gateway/egress.py`,
`src/gateway/redaction.py`, migrations 10 and 11, consume-on-use in `requires`
(`src/gateway/app.py`), `rotate-vault-key` and `vault-drop-unreadable` in `scripts/manage.py`.
Tests: `tests/test_providers_api.py`, `tests/test_vault.py`, `tests/test_egress.py`,
`tests/test_secret_leaks.py`, with `tests/mock_network.py`.

## Who may do what

Providers belong to the organization. The permission table changed accordingly (note 28, the
generated matrix): `providers.list|status|create|update|disable|enable|delete|test` and
`provider_keys.set|delete` at organization level; `provider_keys.read` is gone (no endpoint
returns a key); `mode.change` became `mode.set_organization` and `mode.set_application` (built
in step 3).

- **org_admin** configures its organization's providers and is the only one who sets or deletes
  a key.
- **platform_admin** sees only that each provider exists and whether it is enabled
  (`/providers/status`), and can turn the platform's switch off and on. It never adds, replaces
  or reads a key, and sees no type, URL, model, price, key state or `last4`.
- **app_admin, viewer, application-defined roles and users with no role** get `forbidden` on
  every endpoint (tested for each).
- **Two switches.** A provider is enabled when the organization's switch and the platform's are
  both on. Which one `disable`/`enable` moves is decided from the role and relation `authorize`
  allowed the request with, so an org_admin cannot re-enable what the platform turned off.
- **Missing equals forbidden**: another organization's provider through this organization's
  path, an unknown id and nonsense all get the same 403, byte for byte, with or without a step-up
  window (tested on every endpoint), and nothing is contacted.

## What an organization may configure

Type `openai_compatible` or `anthropic`; **never `ollama`**, which is the operator's own
(`backends.yaml`). The base URL must be `https`, have a host, no user, query or fragment, and not
be plainly internal (`localhost`, `.local`, `.internal`, a literal private or loopback address);
that is a quick 422 at entry. The check that matters is at connect time (the egress guard,
below). Prices per million input and output tokens, a first-token budget, the tier it can serve
and its role are optional settings. Changing the **type or URL deletes the key** in the same
transaction (it is bound to them and could not be used anyway; the answer says
`key_deleted: true`); changing the **model resets the probed capabilities** to unknown.

## The vault

- **AES-256-GCM** (`cryptography`, already pinned), a fresh 96-bit nonce per encryption.
- **The master key** is `GATEWAY_VAULT_KEY`, 32 random bytes in base64url. The default server
  refuses to start without it, or with a value that is not 32 bytes (the message names the
  variable, never the value). It is named by an id (the first 16 hex characters of an HMAC of a
  fixed label under it) that identifies it without revealing it.
- **Bound to where it may be used.** The associated data is the organization, the provider, its
  type, the origin of its base URL, and the master key's id. A ciphertext copied to another
  row, organization or URL does not decrypt (tested for each).
- **Stored**: the master key's id, the nonce, the ciphertext, and the last four characters only
  for keys of 20 characters or more. Never the key. A key has its own table, so deleting it
  removes the row.
- **No trial decryption.** A row opens only with the master key its id names. If that key is not
  loaded, the row is `key_unreadable`: shown as such in `GET /providers`, logged as a warning once
  per provider at startup and once at its first use (the provider id and the missing master key
  id, nothing else), never tried with another key, and never used. Routing will skip such a
  provider without bringing in one the mode forbids (step 3).

**Rotating the master key.** Set the new key as `GATEWAY_VAULT_KEY` and the old one as
`GATEWAY_VAULT_KEY_PREVIOUS`; the server opens rows under either. Run
`uv run python scripts/manage.py rotate-vault-key`: it backs the database up beside itself, then
re-seals every key under the new master key in one transaction, and reports how many (and any it
could not read). Remove `GATEWAY_VAULT_KEY_PREVIOUS` and restart. Tested.
The backup it makes holds the keys sealed under the old master key: delete it with the old key,
or keep the two apart.

**If the master key is lost, every stored provider key is lost.** That is the design: nothing
else can decrypt them. The server still starts (one lost key must not stop the gateway); the keys
show as `key_unreadable`, each organization's admin enters them again, and
`manage.py vault-drop-unreadable` deletes the dead rows. Back the master key up apart from the
database, as the pepper.

**A backup holds every sealed provider key** (`manage.py backup`, the copy `rotate-vault-key`
makes, any copy of the database file). Sealed is only as safe as the master key is far away:
never store a backup with `GATEWAY_VAULT_KEY` (or the env file that holds it), nor with the
pepper. A backup restored and started under another master key does not crash the server: it
starts, warns once per provider, and shows each key `key_unreadable` until it is entered again;
started with the master key it was sealed under, the same rows open (tested, through
`backup_database`, `restore_database` and `create_default_app`).

## Where a key is decrypted, and how plaintext is kept out of everything

- **One place decrypts**: `Vault.lease()`, inside test-connection (the only path to an
  organization's provider in this step), after authorization, the rate limit and the readability
  check, immediately around the probes. Nothing caches plaintext. A test fails if anything in
  `src/` or `scripts/` other than the provider service, the wiring and the CLI imports `Vault`.
- **One place reveals it**: `Secret.reveal()` is still called only by the adapters'
  `credential_headers` (note 31). Sealing takes the key as it arrived in the request body, so it
  never has to be wrapped and unwrapped.
- **The redaction layer** (`gateway.redaction`): while a key is leased, and while a submitted
  key is being sealed, its exact value is scrubbed from every log record; so are credential-shaped
  patterns (`Bearer ...`, `x-api-key: ...`, `sk-...`, `sk-ant-...`, the gateway's `gwk_...`), and
  exception text. It is a log-record factory installed by the server for its lifetime, so it
  covers every logger (uvicorn's included) before any handler formats a record.
- **A record that fails to format.** When a handler cannot format a record, logging's own error
  handling (`Handler.handleError`) prints the message and `Arguments: ` with each argument's
  `repr` to stderr. Arguments are scrubbed when the record is made, and if a secret would still
  show, in the formatted message or in the arguments' `repr` (an object whose `str` is safe but
  whose `repr` is not), the record keeps only the scrubbed message and no arguments. Tested
  through a handler whose format always fails: without the layer the planted key reaches stderr
  (the control); with it, neither a string argument, a watched value, such an object, nor a
  message that cannot be formatted shows it.
- **Provider bodies are never passed on** (note 31): a 401 that echoes the key is answered
  `provider_auth_failed` with a fixed message.
- **The leak scan**: a key set through the endpoint, used by test-connection against a provider
  that answers, one that answers 401 echoing the key, and one whose stream breaks, appears in no
  response, log record, audit row, table (other than sealed), `repr` or output; only in the
  `Authorization` header of the five requests that carried it. The planted control (the key
  logged once) is found, and with the redaction layer installed the same line is scrubbed.
- **The limit**: Python cannot erase a string. During a test-connection the plaintext is in
  memory, in the `Secret` and the outgoing request's headers; nothing keeps it afterwards.

## Consume-on-use

Adding, replacing or deleting a key, and deleting a provider, are **consume-on-use**: one
successful change uses up the step-up window, so each change needs the password again.

- `requires` **claims** the window before the handler, reading and closing it in one write
  transaction, so two changes sent together with one window give **exactly one success** and one
  `step_up_required` (tested with both in flight). An expired window cannot be claimed (tested on
  the same, still live, session).
- The handler marks a token in the transaction that makes its change. If it fails **before**
  changing anything (a key of the wrong length, no key to delete), the window is given back; if
  it fails **after** (the write done, the answer not), the window stays used (both tested).
- Note 29 had described clearing the window after a successful handler; claiming first closes the
  race that would leave. The audit entry of each says `step_up_consumed: true`.

## The egress guard

Every connection to an organization's provider goes through one `httpcore` network backend whose
`connect_tcp` is the only place a socket is opened (`gateway.egress`):

- `https` only (refused before connecting otherwise), no redirects (a 3xx is answered
  `upstream_rejected` and not followed), no proxy from the environment;
- the host is resolved and **every** address checked; one that is not public refuses the
  connection: loopback, private, link-local, carrier-grade NAT, unspecified, multicast,
  reserved, documentation, the cloud metadata addresses, and IPv6 forms that embed IPv4
  (mapped, NAT64, 6to4, Teredo), judged on what they embed;
- the socket connects to **the address that was checked**, not the name again, so DNS rebinding
  cannot change it; TLS still verifies the certificate for the name in the URL;
- no allow-list on this path: the operator's list is for the operator's own servers and is not
  consulted.

Tests: each range by literal address, by name and embedded; one bad address among good ones;
DNS rebinding (public at the check, private afterwards: the connection goes to the public one,
with one resolution); a redirect to the metadata address not followed; `http` refused; a proxy
variable ignored; the base-URL checks at entry; test-connection to a name that now resolves
inside is refused with no connection made. Tests use a scripted network in place of sockets.
A name that does not resolve is answered `upstream_unavailable` (detail `dns_failure`) rather
than escaping as a socket error.

## Test-connection

**It spends tokens on the organization's account with that provider.** It runs only when an
org_admin asks (`POST .../test`), never when a provider or key is saved, at most 5 times per
provider per hour and 20 times per organization per hour, all its providers together (one
provider per address would get round the first; both `429 test_rate_limited`; chosen values,
`TESTS_PER_HOUR` and `TESTS_PER_ORGANIZATION_HOUR`). It sends, through the guard: a short answer
("Reply with the word OK.", `max_tokens` 8, streamed), and a tool round trip (a `ping` tool,
then its result and one more turn). It records `supports_streaming`, `supports_tools` and
`context_window`, each with its source (`probed`, or `unknown` with a reason), **when** it was
probed and **with which model**; a model change resets them. It answers only with the
capabilities or a mapped error code: never the provider's text or error body. Audited with the
outcome code.

**Not a network scanner.** Every failure to reach the host gets one answer, the same status and
the same bytes: `502 provider_unreachable`, "The provider could not be reached." That covers an
address the guard refuses, a name that does not resolve, a refused or failed connection, a lost
connection and no answer in time. Which one it was goes to the audit entry only
(`outcome_code` and `outcome_detail`, fixed words such as `private`, `dns_failure`, `connect`).
A host that answered over TLS gets its mapped code and fixed message, with no detail derived
from its body (and none in the audit). Tested: the four cases give identical status, headers
and bytes, distinct audit entries, and neither the key nor the host's words in any of them.

## Audit

`provider_created`, `provider_updated` (with `key_deleted`), `provider_disabled` and
`provider_enabled` (with which switch), `provider_deleted`, `provider_key_set`,
`provider_key_deleted` (both with `step_up_consumed`), `provider_tested` (with the outcome code):
the provider id, its type, the organization and the actor. Never the key, `last4` or the URL.
The detail fields take only fixed values (the audit allowlist of note 27). Reading the audit log
over HTTP is not built; visibility is as planned (org_admin and app_admin; platform_admin the
switches only).

## Measured

MEASURED on this machine: Intel Core Ultra 7 155H, 22 logical cores, 15.4 GB RAM, Windows 11
10.0.26200, Python 3.12.10. `uv run python -m benchmarks.provider_requests --rounds 200
--runs 3`, on this step's code commit. p50 in ms, the range over three runs of 200 rounds after
20 warm-up rounds:

| What | p50, ms |
|---|---|
| opening one sealed key (AES-256-GCM), in process | 0.010 to 0.011 |
| the guard's resolve-and-check for one connection (system resolver, `localhost`, refused) | 0.311 to 0.332 |
| `GET .../providers` on the running server | 1.774 to 1.845 |
| `GET .../providers/status` on the running server | 1.756 to 1.836 |
| `PUT .../providers/{id}/key` (claim the window, seal, store, audit) on the running server | 2.925 to 3.012 |

The step-up before each key change uses argon2 and is not timed here (notes 24, 29). **The
guard's figure used no stub**: `GuardedBackend(system_resolver)`, the real `getaddrinfo`, asked
for `localhost`, which the system answers on this machine without a DNS server (Windows resolves
it itself). It is the guard's own cost plus a local lookup. A lookup that goes to a DNS server
was **not measured**; it takes what the network takes and adds to the first connection.

## Threat model (this step)

For step 9's `docs/threat-model.md`:

- **The master key is in the process environment**, like the pepper: whoever can read the
  environment and the database can decrypt every provider key.
- **A plaintext key is in memory during a test-connection**; Python cannot erase it.
- **A lost master key loses every provider key** (by design); they must be entered again.
- **An org_admin can point a provider at any public HTTPS host**, and so send its test prompts
  (and, from step 3, its organization's prompts) anywhere; that is its authority, and it is
  audited.
- **SSRF residue**: the guard trusts the resolver at the moment it asks; it checks every address
  and connects to the checked one, but a public address that forwards inward is beyond it.
- **Test-connection spends the organization's money** (rate-limited, audited).
- **Test-connection as a probe of the network**: inside addresses are refused before any
  connection, and every failure to reach a host has one answer. What is left: a public host
  that answers over TLS is told apart from one that does not (that is the purpose of the test),
  and timing differs (a refused address answers at once, a timeout after the connect timeout).
  Both are bounded by 5 tests per provider and 20 per organization an hour, each audited.
- **A backup holds the sealed keys**: with the master key beside it, it holds the keys.
- **`last4` is shown to org_admins** (only for keys of 20 or more characters).
- **The redaction layer scrubs patterns and watched values**: a key in a shape it does not know,
  logged outside a lease, would not be caught; keys are never logged on purpose, and the leak
  scans check it.

## Not built

- Routing chat to an organization's providers, modes, fallback, the circuit breaker (step 3).
- An operator allow-list for redirects (none are followed).
- The guard on the operator's own adapters (step 3, with the locality rule).
- Reading the audit log over HTTP.
