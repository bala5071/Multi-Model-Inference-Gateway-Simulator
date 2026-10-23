# 20 — Applications and API keys

Step 2 of the platform plan (`docs/platform-plan.md`). Tenants become applications, and a
request says which application it is for with an API key instead of a `tenant_id` in its
body. This replaces the trust-based identity of note 09; routing, plans, the soft budget and
billing work as note 09 describes.

Code: `src/gateway/applications.py`, `src/gateway/apikeys.py`, `src/gateway/ratelimit.py`,
migration 2 (`src/gateway/migrations/m0002_applications.py`), changes in `routing.py`,
`monitoring.py`, `app.py` and `plans.yaml`; the CLI `scripts/manage.py`.
Tests: `tests/test_apikeys.py`, `tests/test_auth_api.py`, `tests/test_applications_api.py`
(note 09's tests, converted). Benchmark: `benchmarks/key_checks.py`.

## What changed for callers

- **Every request to a gateway with applications needs `Authorization: Bearer <key>`.**
  Without one: 401 `api_key_required`. The key is checked before the body is read.
- **A `tenant_id` in the body is a 422**, with a message pointing at the header, on every
  gateway, with or without applications.
- **Usage is `GET /v1/applications/me/usage`**, for the key's own application.
  `/v1/tenants/{id}/usage` is gone; there is no way to name another application.
- **The default server refuses to start without `GATEWAY_KEY_PEPPER`** (at least 32
  characters) and reads applications and keys from the SQLite database `GATEWAY_DATABASE`
  (default: the user's local data folder, outside the working tree; see
  [The database file](#the-database-file)).
- **Live load runs** read the key from `GATEWAY_API_KEY`:
  `GATEWAY_API_KEY=... uv run python -m benchmarks.loadgen --url http://127.0.0.1:8000`.
- `create_app(registry, clock)` without applications is unchanged: no keys, any model,
  nothing charged, and no key-checking code on its request path. The performance gate, the
  load generator and the report produce byte-identical output
  (`uv run python scripts/protected_outputs.py`).

## Applications

An application has an id, a name, and its **own copy** of the settings of the plan template
it was created from: models (cheapest first), price multiplier, monthly token budget, and two
optional limits, a `max_tokens` ceiling and a requests-per-minute limit. Changing a template
in `plans.yaml` does not change applications that already exist. The packaged templates set
neither limit, so the demo and simulated live mode behave exactly as before.

`plans.yaml` keeps the three plans as templates and lists the demo applications (Initech,
Globex, Acme) with no keys. Simulated live mode creates them in memory;
`scripts/manage.py seed-demo` creates them in a database. A plans file with a `tenants:`
section is refused with a message saying what replaced it.

Nothing assumes one organization owns every application. Organizations arrive in A3, with
their own migration; migration 2 has no organization column.

## Keys

- **Format:** `gwk_<key id>_<secret>`. The key id is 16 hex characters (64 bits) and is
  how the record is found. The secret is 32 random bytes (256 bits) from
  `secrets.token_bytes`, in unpadded base64url.
- **Storage:** only `HMAC-SHA256(pepper, key)`, as hex, in `api_keys.key_hmac`. The key is
  never stored, logged or put in an event; the pepper is never written anywhere. `repr` of
  the service and of a new key redact both.
- **Checking: the same steps for every attempt.** One record lookup (a malformed key looks
  up an id no record can have), one HMAC of the presented key, and one `hmac.compare_digest`
  of two 64-character digests (against a dummy digest when there is no record), in that
  order, whether the key is valid, has a wrong secret, an unknown id, is malformed, revoked
  or expired. `test_every_attempt_takes_the_same_steps_whatever_is_wrong_with_the_key`
  checks the sequence of steps; it is a structural test, not a timing one. **What timing
  differences remain was not measured:** a found record is read from the database while a
  missing one is not, a valid key goes on to load its application, and SQLite and Python
  may take different times on either path. With 256-bit secrets, a timing difference could
  at most tell an attacker whether a key id exists, not reveal a secret.
- **One answer for every bad key.** Unknown, malformed, wrong secret, revoked, expired and
  rotated-out keys all get the same 401, byte for byte: body
  `{"error": {"code": "invalid_api_key", "message": "The API key is not valid."}}` and the
  same headers. A test compares them.
- **Expiry** is optional and decided by the injected epoch source.
- **Rotation:** `rotate-key` issues a new key for the same application and sets the old
  one to expire after a grace period (default 24 hours), never later than an expiry it
  already had. After the grace period the old key is refused like any other bad key.
  `revoke-key` stops a key at once.
- **Issuance is through `scripts/manage.py`**, and since identity step 7 also through the
  management endpoints, by a logged-in user within that user's roles, with step-up, limits and
  an expiry on every key (docs/30). The CLI prints a new key once, alone on standard output,
  with everything else on standard error; it never logs it. Both are audited by key id.

```sh
export GATEWAY_KEY_PEPPER=...            # the same value the server has
uv run python scripts/manage.py seed-demo
uv run python scripts/manage.py create-application --id shop --name "Shop" --plan pro
uv run python scripts/manage.py create-key --application shop --label backend
uv run python scripts/manage.py list-keys --application shop     # ids and state, never keys
uv run python scripts/manage.py rotate-key --key-id <id> --grace-hours 24
uv run python scripts/manage.py revoke-key --key-id <id>
```

### Rotating the pepper

Every stored HMAC depends on the pepper, so a new pepper invalidates every key at once. In
A1 the only way to rotate it is to re-issue every key: stop the server, set the new
`GATEWAY_KEY_PEPPER`, issue a new key for each application with `create-key`, hand them
out, start the server, and revoke the old records. Clients are refused until they have
their new key. Rotation without that gap needs each record to say which pepper made it and
the server to accept a previous pepper for a while; that is not built, and the plan lists it
as deferred.

### If the pepper is lost or changed

Every key stops working at once, for every application: the stored HMACs can no longer be
reproduced. Nothing else is lost (applications, usage and key records stay), but every
client is refused until it is given a new key, issued under the new pepper. There is no way
to recover keys from the database without the pepper; that is the point of it.

**Back the pepper up** where the operator keeps other secrets (a password manager or a
secrets store), separately from the database: a database backup without the pepper cannot
check keys, and the pepper without the database grants nothing. Never commit it, put it in
the compose file or the image, or write it next to the database.

### Not built: limiting failed authentications

There is no rate limit on failed key checks per source. Guessing is not the risk: a secret
has 256 bits, so no number of attempts finds one. The risk is load: a flood of requests with
bad keys costs one lookup, one HMAC and one comparison each (about 0.02 ms on the machine in
the table below) and is otherwise unlimited. The per-application rate limit cannot help,
because a bad key has no application. Limiting per source needs a trustworthy client
address, which depends on what sits in front of the gateway (a proxy, a load balancer); it
belongs with the network-facing work of A8, before the gateway is exposed to any network.

### The database file

It holds applications, key HMACs and (from step 3) usage, so:

- **Its default location is outside the working tree:** `%LOCALAPPDATA%\inference-gateway\`
  on Windows, `$XDG_DATA_HOME/inference-gateway/` (or `~/.local/share/inference-gateway/`)
  elsewhere, as `gateway.db`. `GATEWAY_DATABASE` names another file.
- **It is created readable by its owner only where the OS supports it:** on POSIX the folder
  is created `0700` and the file `0600` (an existing file is tightened to `0600`); SQLite
  creates its `-wal` and `-shm` files with the same permissions. On Windows `chmod` cannot
  express this; the default location is in the user's own profile, which other users cannot
  read. `tests/test_database.py` checks the POSIX permissions (on Linux, in CI).
- **Git ignores it** anywhere in the tree (`gateway.db`, `gateway.db-*`, `*.sqlite3`), and
  the env file that holds the pepper (`gateway.env`), in case either is put there.

## Limits

- **`max_tokens` ceiling:** a request asking for more gets a 422 on the `max_tokens` field
  ("At most N on this application's plan."); a request asking for none gets the ceiling.
- **Requests per minute:** a sliding window of 60 s on the injected clock, per application.
  Over the limit: 429 `rate_limited`, with `Retry-After` and `retry_after_s` (when the
  oldest request in the window leaves it) and `limit_per_minute`. A refused request does not
  count. Memory is bounded by the limit.
- **Order of checks:** key, then body, then `max_tokens`, then the rate limit, then the plan
  (403), then the budget (429 `budget_exhausted`), as in note 09.
- Rate-limit refusals are not yet reported to monitoring: its rejection reasons are a fixed
  part of the snapshot schema, which step 4 changes.

## last_used_at without a write per request

A key check notes the time in memory. The times of every key used since the last write are
written together, in one transaction, by the first check after the flush interval (60 s),
and on shutdown. So a busy gateway writes `last_used_at` at most once a minute, whatever the
traffic, and a quiet one when it is next used. The time is at most a minute stale, and the
times noted since the last write are lost if the process dies.

Measured with `uv run python -m benchmarks.key_checks` (2000 checks after 20 warm-up checks;
100 flushes per batch size), run three times, on the tree of the commit that added this
note (parent `0fe88ea`). The machine is the one of note 19: Intel Core Ultra 7 155H, 22
logical cores, 15.4 GB RAM, WD PC SN810 1 TB NVMe SSD, Windows 11 (10.0.26200), Python
3.12.10, SQLite 3.49.1; the database is set up as the server's (WAL, `synchronous = FULL`) in
the system temporary folder on that SSD. Nearest-rank percentiles; ranges over the three
runs. One laptop, one disk, one day.

| What | mean ms | p50 ms | p99 ms | max ms |
|---|---|---|---|---|
| key check, batched (as the server runs it) | 0.018 to 0.019 | 0.017 to 0.019 | 0.038 to 0.046 | 0.22 to 0.26 |
| key check + write `last_used_at` per request | 0.386 to 0.412 | 0.363 to 0.376 | 0.751 to 0.854 | 1.2 to 1.6 |
| one flush of 1 key | 0.325 to 0.367 | 0.311 to 0.326 | 0.490 to 0.843 | 0.60 to 0.86 |
| one flush of 100 keys | 0.506 to 0.631 | 0.460 to 0.518 | 0.853 to 3.959 | 1.0 to 5.5 |
| one flush of 1000 keys | 1.945 to 1.971 | 1.861 to 1.881 | 2.847 to 3.487 | 3.0 to 3.7 |

Writing per request would add about a third of a millisecond to every request on this
machine, all of it blocking the event loop (one synced commit). Batched, a check costs two
indexed reads and an HMAC, about 0.02 ms, and the write is paid once a minute: about 2 ms for
a thousand keys. The benchmark gives each write a strictly increasing time; a first version
read the system clock, which on Windows can return the same value twice, so many of its
"writes" changed nothing and looked cheaper than they are.

## The Playground and the demo

In simulated live mode the server issues one throwaway key per application at startup,
with a throwaway pepper, all in memory. The dashboard's Playground posts to
`POST /v1/playground/generate` with an `application_id`; the server adds that application's
key and sends the request through the same key check as any other. Keys never leave the
process; `/v1/control` says `"playground": {"available": true}`. Elsewhere that endpoint is a
404 `playground_unavailable`, and the Playground posts to `/v1/generate` with no key, which a
gateway with applications refuses (the Playground says so).

`scripts/demo.py` builds the simulated wiring in its own process with throwaway in-memory
keys, uses them, and never prints them. Load control sends through the handler for an
application it has already resolved, with no key (load control was already a switch for
sending as anyone, and is off by default).

## Design decisions

- **The key check is an `async` FastAPI dependency.** A sync dependency would run on a
  worker thread: that would let a `SimClock`'s time jump, and the SQLite connection belongs
  to the event loop's thread.
- **Two routes, chosen at startup.** Without applications, `/v1/generate` has no dependency
  at all; with them, it has the key check. The handler body is shared.
- **401, not 404, for an unknown key**, and the same 401 for every bad key: a caller learns
  nothing about which keys or applications exist.
- **The key id is visible in the key** so a record is found by an indexed lookup, not by
  hashing against every record; the id alone is useless without the secret.
- **Usage events** were still in memory at this step; since note 21 the default server
  stores them in its database, and budget periods are calendar months in UTC.
- **Not authenticated yet:** monitoring, Prometheus metrics, load control and the dashboard
  (plan section 9). The server binds to `127.0.0.1` and must not be exposed to a network
  until A8. Keys travel in a header over plain HTTP: also a reason to keep it local.
