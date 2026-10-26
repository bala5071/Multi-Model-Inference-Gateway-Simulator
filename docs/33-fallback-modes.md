# 33. Fallback modes: routing chat to organization providers

A5, pull request 3 (docs/provider-plan.md, section 5, with the request's additions and the
details settled before building). Chat requests can now be served by an organization's own
providers (note 32), when the organization and the application both allow it.

**A mode other than `local_only` sends prompts, and tool definitions and results, to a third
party the organization chose, under that third party's terms.** The gateway stores none of it
(CLAUDE.md hard rule 7); the provider may.

## Modes

| Mode | May contact | Order of candidates |
|---|---|---|
| `local_only` (the default) | local destinations only | the tier |
| `local_with_hosted_fallback` | local, then hosted | the tier, then the organization's `fallback` providers |
| `hosted_primary_with_local_fallback` | hosted, then local | the organization's `primary` providers, then the tier |

An organization and each application have a mode; both default to `local_only`, so hosted
serving needs both to opt in. An application may be as strict as its organization or stricter;
a looser one is refused (409 `mode_looser_than_organization`). **`effective_mode`** (in
`gateway.modes`) is the only function that decides which mode applies, the stricter of the two;
a test fails if any other module defines it or compares modes. When an organization becomes
stricter, its applications follow without their stored values changing.

Modes are in a table of their own (migration 12): no row means `local_only`. A table rather than
a column on `organizations` and `applications`, so neither applying nor reversing it rebuilds a
table that foreign keys refer to.

## What is local

A destination is **local** when its host and port are on the operator's list:
`GATEWAY_PROVIDER_LOCAL_ALLOW` (`host:port`, comma-separated) plus, by default, the loopback
destinations in the operator's own backends file. The simulator is local. Everything else is
**hosted**: every organization provider, and an operator tier configured at a destination not
on the list. The list is never consulted for an organization's provider (tested: under a scope
that allows hosted, an organization client's `localhost` is still refused as loopback).

## Candidates and fallback

The candidates of a request are the tier it was routed to (`X-Provider: gateway`) and the
organization's providers **serving that tier** that are enabled (both switches) and have a key,
in the mode's order. A provider known not to take tools is left out of a request with tools; if
none is left, 422 `tools_not_supported`. With no candidate the mode allows, 503
`no_local_provider`.

The router moves to the next candidate only on these:

| Trigger | `X-Fallback-Reason` |
|---|---|
| A connection error (refused, reset, DNS failure, a connect timeout, the guard refusing) | `connect_error` |
| A 5xx | `upstream_5xx` |
| No first token within the budget (`first_token_budget_ms`, 10 s for a hosted provider by default; the tier's own first-token timeout for local) | `first_token_budget` |
| The provider's circuit breaker is open (skipped, never contacted) | `circuit_open` |
| Tool-call arguments that are not a JSON object: one retry on the same candidate, then the next | `invalid_tool_json` |
| The provider's key cannot be read (skipped, never contacted) | `key_unreadable` |

**The invalid-tool-JSON retry** is never more than one: a second invalid answer moves to the next
candidate, or is the request's answer (502 `upstream_malformed`) when there is none. The attempt
that answers is the one recorded, with its provider as the source, and billed at that provider's
prices. A failed attempt is not in the ledger, but the provider may still charge the
organization for it. Both are tested
(`test_the_invalid_tool_json_retry_is_recorded_once_at_the_organizations_prices`,
`test_invalid_tool_json_is_never_retried_more_than_once`).

Not on 401/403 (answered `provider_auth_failed`), 429 (passed through), any other 4xx, a full
local queue (429 `queue_full`) or the request's own overall timeout.

**What never happens**, each with a test and a mutation:

- **Never after output has started.** The first delta passed on closes fallback for that
  request, streaming or not: a failure after it is the request's failure (in a stream, an error
  event and no `[DONE]`). So a request is never answered, or charged, twice.
- **Never another tier.** Only providers serving the routed tier are candidates; the request is
  passed on unchanged.
- **Never a model outside the plan.** The plan decides the tier before any candidate is
  considered; a provider serving a tier outside the plan changes nothing.

The last two are also checked as invariants from outside the router
(`tests/test_routing_invariants.py`): 400 seeded random cases of plans, requested tiers, modes,
provider lists (north's and another organization's, enabled or not, with a key or not) and
provider failures. Every connection that reaches the scripted networks is checked against the
case alone: an operator server only for the requested tier, inside the plan, asked for its own
model; an organization provider only when it is the organization's, serves the requested tier
inside the plan, is enabled, has a key and the mode allows hosted, asked for its own model.

## Where the answer came from

Every chat answer says `X-Provider` and `X-Provider-Class`. `X-Provider` names who answered:
`gateway` for the gateway's own tier, or the organization provider's id. `X-Provider-Class` says
where that is, `local` or `hosted`: a class, never a URL. **Only the class says local or hosted**:
the gateway's tier is `X-Provider: gateway` with `X-Provider-Class: hosted` when the operator
put it at a destination not on its local list. After a fallback, `X-Fallback-From` (the first
candidate, `gateway` or a provider id) and `X-Fallback-Reason` (why it was left). A streamed
answer has them too: they are known when the first output is sent. The usage record keeps the
same (migration 13: `provider`, `provider_class`, `fallback_from`, `fallback_reason`,
`usage_estimated`; NULL for `/v1/generate`). A hosted answer is charged at the organization's
own prices for that provider (0 when it has none), never at placeholder prices.

**`local` was the gateway tier's name until PR 20.** PR 19 (migration 13) wrote `local` in
`provider` and `fallback_from` for the gateway's own tier, as `X-Provider` said. From PR 20 (written
2026-10-26), a gateway writes `gateway` instead. There is no migration and stored rows are not
rewritten: rows written by a gateway running PR 19's code keep `local`. Read `local` and `gateway`
in those two columns as the same thing, the gateway's own tier, and take local or hosted from
`provider_class`, which is unchanged. An organization provider's id is always `prv_` and 16 hex
digits, so neither word can be one.

## Local-only, enforced twice, asked live

1. **The router** keeps hosted candidates out of a `local_only` request, and asks the mode again
   before every candidate.
2. **The egress guard** (note 32) refuses a hosted destination unless the request's scope allows
   it: the router opens a scope whose answer is asked again, through `effective_mode`, at every
   request the transport sends, every connection it opens, and again after the name is resolved.
   With no scope, every hosted destination is refused (fail closed). Test-connection opens an
   explicit probe scope: an org_admin's request to send fixed probe prompts, audited.

So a mode made stricter while a request is in flight stops its next attempt (tested both at the
router and at the guard, where the mode changes while the name is being resolved; the second
test found the guard asked only before resolving, fixed). **An answer already streaming from a
hosted provider is not cut when the mode becomes stricter: its prompt has already been sent.**
Every mode change's answer says so (`in_flight`, below). Tested over HTTP: a hosted answer is
streaming when an org_admin makes the organization `local_only` through the endpoint; that
answer ends normally, with `[DONE]`. The next request is served locally without contacting the
provider. With the router's own checks bypassed (the provider handed over as a candidate
labelled local), the guard refuses it before the provider's name is even resolved.

Every attempt at an organization provider uses a new client, so no pooled connection carries a
request past these checks. The server gives them all one TLS context, because building one loads
the certificate bundle: 145 to 148 ms on this machine, and each client used to build two
(measured below). **That context is pinned** (`gateway.egress.provider_tls_context`):
certificates verified, host names checked, httpx's own trust store (`certifi`), with the
environment ignored (`SSL_CERT_FILE` and `SSL_CERT_DIR` cannot replace it, as no proxy setting
can), and nothing per organization. The client factory takes no argument and no provider field
is a TLS setting. A test fails if any of these changes. The operator's adapters get the same
context by default.

**The test that local_only reaches nothing hosted**: hosted providers configured for the tier, the
local tier failing every way that would be a trigger in another mode (refused, 5xx, no first
token, invalid tool JSON twice), a provider's breaker just reset to half-open, streaming and
not: not one connection to a hosted address, not one hosted name resolved.

## Circuit breaker and the slow-provider warning

Per (organization, provider), in memory, on the injected clock: **open** after 5 consecutive
trigger failures, for 30 s, doubling while probes fail, up to 5 minutes; **half-open** lets one
request through (a request cancelled meanwhile frees the slot); **closed** again after 2
consecutive successes. Each transition is a `circuit_breaker` event (docs/15); the router owns
the breakers, monitoring only shows the events. A provider that misses its first-token budget 3
times in a row is flagged in `GET .../providers` and `.../providers/status` (`slow_since_ms`,
`consecutive_misses`), cleared by an answer within budget; `GET .../providers` also shows
`circuit` (the breaker's state).

## Mode endpoints

| Method and path (under `/v1/admin/orgs/{org}`) | Action | Who |
|---|---|---|
| `PUT /mode` | `mode.set_organization` | org_admin |
| `PUT /applications/{app}/mode` | `mode.set_application` | app_admin of it, org_admin |

Body `{"mode": "..."}`. Through `requires` with **consume-on-use** step-up (note 32): each change
needs its own password; a change refused before it is made (409) gives the window back. Each is
audited (`mode_changed`, with `old_mode`, `new_mode` and `step_up_consumed`) in the same
transaction as the change. A missing organization or application and a forbidden one get the same
answer, byte for byte. The application answer includes its `effective_mode`. Both answers always
include `in_flight`, the same fixed sentence whether the change is stricter or not: "A mode
change applies to the next request, and to the next attempt of a request in flight. An answer
already streaming from a hosted provider is not cut when the mode becomes stricter: its prompt
has already been sent."

## Measured

MEASURED on this machine: Intel Core Ultra 7 155H, 22 logical cores, 15.4 GB RAM, Windows 11
10.0.26200, Python 3.12.10. `uv run python -m benchmarks.chat_routing --rounds 200 --runs 3`, on
`8b70249`, nothing else running. In process: each request goes through the whole app
(`httpx.ASGITransport`: key, plan, rate limit, budget, router, adapter, ledger); the providers
are scripted behind the real egress guard, with no sockets, so this is the gateway's own work,
never a provider's latency. p50 in ms, the range over three runs of 200 requests after 20
warm-up:

| What | p50, ms |
|---|---|
| the economy tier served by the operator's server, `local_only` (PR 1's path, now through the router) | 2.497 to 2.553 |
| the same answer from an organization's provider (candidates, breaker, key decrypted for the call, a new guarded client, the guard's checks, the provider's prices) | 2.877 to 2.915 |
| a fallback: the first provider refuses the connection at once, the second answers | 4.166 to 4.254 |
| building one TLS context (`httpx.create_ssl_context()`): what each organization request paid twice before the server shared one | 145.372 to 147.841 |

So routing to an organization's provider adds about 0.4 ms per request (0.32 to 0.42 ms between
the two rows' ranges) to the gateway's own work, and a fallback after an immediate refusal about
1.3 ms more. A real provider adds its own network and TLS time, which is not measured here and is
not a property of the gateway.

## Threat model (this step)

For step 9's `docs/threat-model.md`:

- **Hosted modes send prompts to a third party** the organization chose; the gateway cannot
  control what that provider keeps. Both the organization and the application must opt in.
- **A provider key is decrypted for each hosted call**, only inside the vault's lease around
  that one call, after the mode, the plan, the breaker and the candidate checks. The leak scan
  runs full routed requests (a 5xx echoing the key with a fallback, a 401 echoing it, refused
  connections, a fallback to local, streaming): the keys appear only in the Authorization header
  of the requests to their own provider.
- **The operator's local list is trusted**: anything on it is treated as this machine's. An
  operator who lists a remote host makes it "local" for every organization.
- **Timing**: a fallback is visible to the client in its headers and in latency; the reason is a
  fixed word, never a provider's text.
- **Breakers are per process** and start closed after a restart.
- **A mode made stricter does not stop a hosted answer already streaming**: that prompt is already
  with the provider. The mode change's answer says so, so an administrator is not misled.
- **The trust store for providers is fixed in code** (`certifi`, through httpx): an environment
  variable cannot add a certificate authority that organization traffic would then trust.

## Not built

- A user interface for modes; reading a mode back other than in the answer to setting it.
- Fallback between hosted providers of different organizations (never: candidates are the
  organization's own).
- Retrying a failed hosted request on the same provider beyond the one retry for invalid tool
  JSON.
- An operator allow-list for redirects (none are followed).
