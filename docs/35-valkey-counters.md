# 35. Shared counters in Valkey

Scale-out plan, pull request 2 (docs/scale-out-plan.md, section 8, and "PR 2: Valkey counters,
settled before building"). The counters of note 34's protocols, shared by every instance
through one Valkey. Off by default: without `GATEWAY_COUNTERS=valkey` every counter is this
process's own, exactly as before, and nothing connects to Valkey.

## What is shared, and what is not

| Counter | Shared in Valkey | Atomic rule |
|---|---|---|
| Request rate limit (per application, a minute) | yes | the window script: trim, count, add, in one step |
| Login limiter (per username's HMAC, 15 minutes) | yes | one `ZCOUNT` to check; a transaction to record |
| Step-up session cap (per session, 5 minutes) | yes | as the login limiter |
| User-creation cap (per session, an hour) | yes | the window script (`FailureWindow.admit`, new: check and count in one step) |
| Test-connection counts (per provider and per organization, an hour) | yes | the window script over both windows: counted in both, or neither |
| Circuit breakers (per organization and provider) | yes | the breaker script; one probe at a time across instances |
| Audit cap on unknown-user rows (a minute) | yes | the take script; a finished minute's dropped count is taken once (`GETDEL`) |
| Slow-provider warning | no: each instance's own (plan, decision 11) | |
| Budgets | no: the Postgres PR (section 3.1; answered 2026-10-29) | |

Every key name is an HMAC of what it counts, under a key derived from the pepper for that
purpose alone (`KeyPurpose.COUNTER_KEYS`): no username, session digest, application,
organization or provider id appears in Valkey, and every value is a count or a time
(tested, with a planted key the same scan finds). Windows are timed on the instance's
`EpochSource`, never its `Clock` (per process) and never Valkey's `TIME`.

## Missing is forbidden

Every call has a timeout on the injected clock (200 ms by default). A call that fails, times
out, or is made while the instance's clock is past the skew bound refuses what it would have
decided:

| Counter | When Valkey cannot answer |
|---|---|
| Rate limit | the request is refused: 503 `state_unavailable`; no model sees it |
| Login limiter | `blocked` says yes: the login is refused with the same bytes as every refusal, after the same steps (lookup, verify). A failure that cannot be recorded is not; its attempt was refused anyway |
| Step-up session cap | `blocked` says yes: refused (403 `step_up_failed`, as every refusal) |
| User-creation cap | `admit` says no: refused (429 `user_creation_limited`) |
| Test-connection counts | 503 `state_unavailable`; no test is run |
| Circuit breakers | `admit` says no: the provider is skipped, so a request never goes further than its mode allows because of it; `state` reports `open`; reports are dropped |
| Audit cap | the unknown-user row is not written (writing it would let a flood through) and is counted in the instance; that count is written as its own `audit_rows_dropped` entry when the minute is over |
| Startup | refused: unreachable, `maxmemory-policy` other than `noeviction`, `appendonly` other than `yes`, or the skew past the bound |

A 503 never says which store or why; the log does, without the address or what was sent. The
URL may carry a password: it is in no error, log record or repr (tested, with a planted control).

## Restarts and the clock

- **Restart detection** (plan, section 3.9): the run id of `INFO server` at startup and every 30
  s. A new one means the counters started again from empty: each instance that sees it
  publishes a `state_store_restarted` event (counted in `gateway_events_total`), and the first
  instance to `SET NX` a marker for that run id writes one `state_store_restarted` audit row.
- **The skew bound** (section 3.8, with the answer of 2026-10-29): the instance's epoch against
  Valkey's `TIME`, from the midpoint of one round trip, at startup and every 30 s.
  `GATEWAY_STATE_MAX_SKEW_MS` (default 1000). Past it the server does not start; a running
  instance that drifts past it refuses every shared count until a check passes again. `TIME` is
  read for this and nothing else; the Postgres PR moves the check to `gateway_now_ms()`.

## Settings

`GATEWAY_COUNTERS` (`memory`, the default, or `valkey`), `GATEWAY_VALKEY_URL` (a secret: it may
carry a password), `GATEWAY_STATE_MAX_SKEW_MS`. The database is still one process per SQLite
file (note 21): until Postgres, shared counters do not make two instances share keys, sessions
or usage.

## Tests

`docker-compose.valkey.yml` runs the pinned Valkey on 127.0.0.1:6380; tests marked `valkey` run
against it when `GATEWAY_TEST_VALKEY_URL` is set, and CI's `valkey` job runs them. Without it,
the tests of failing closed (against an address where nothing listens), of the watch (a
scripted client), of the settings and of the URL's password still run.

Two-instance tests, each pair of instances in one process with its own stores, sharing one
Valkey: 30 requests at once against a limit of 10 a minute, 10 served; 30 user-creation
attempts at once against a cap of 10, 10 taken; the login limiter blocking on both after 5
failures split between them; a half-open breaker letting exactly one of 20 concurrent requests
probe; 10 unknown-user rows against a cap of 3, three written and one `audit_rows_dropped`
entry of 7 from six concurrent flushes; the test-connection windows counting in both or
neither. The budget edge (Q1) is tested with the budgets, in the Postgres PR.

## Threat-model additions

- **A Valkey restart resets every shared counter** (accepted, Q4): one fresh window for the
  rate limit, the login limiter, the step-up and user-creation caps, the test-connection
  counts and the audit cap, and every breaker closed. It is never unseen: an event on each
  instance and one audit row per restart. The account lockout, step-up consume-on-use,
  budgets, sessions and keys are not in Valkey.
- **Anyone who can write to Valkey can lift those counters**, within the same windows. The
  compose file publishes it on 127.0.0.1 only and it has no password: never expose it.
- **Clock skew** shifts a shared window by at most the bound between any instance and Valkey;
  past the bound, refused.
- **A dead instance's half-open probe** frees its slot after 120 s (plan, decision 10).

## Timing

Measured, not simulated: `GATEWAY_VALKEY_URL=redis://127.0.0.1:6380/0 uv run python -m
benchmarks.counter_calls` at `5b088cf`, three runs of 2,000 calls each after 20 warm-up calls,
against `docker-compose.valkey.yml` (`valkey/valkey:9.0.7@sha256:7e1deda0...`) on Docker Desktop
29.8.2. Intel Core Ultra 7 155H, 22 logical cores, 15.4 GB, Windows 11 (10.0.26200), Python
3.12.10. Valkey runs in Docker Desktop's Linux VM, so every call also crosses its port
forwarding: a Valkey on the same host or a nearby one may answer faster, and these numbers say
nothing about one across a network. Ranges are over the three runs; each call includes its
timeout on the injected clock.

| One call | mean ms | p50 ms | p99 ms |
|---|---|---|---|
| rate limit (the window script), in memory | 0.000 to 0.001 | 0.000 to 0.001 | 0.001 to 0.002 |
| rate limit, Valkey | 0.751 to 0.800 | 0.678 to 0.707 | 1.637 to 2.493 |
| login limiter check (`ZCOUNT`), in memory | 0.000 | 0.000 | 0.001 |
| login limiter check, Valkey | 0.639 to 0.745 | 0.606 to 0.652 | 1.043 to 2.307 |
| breaker admit (the breaker script), in memory | 0.000 | 0.000 | 0.001 |
| breaker admit, Valkey | 0.719 to 0.736 | 0.677 to 0.684 | 1.309 to 1.698 |

So each shared counter call adds about 0.6 to 0.8 ms (mean) here. A generate request with an
application key makes one (the rate limit); a login one or two; a chat request one or two per
organization provider it tries. The default (`memory`) path is unchanged: the perf gate is
+0.0% on every model and the protected outputs match.
