# 37. Budgets on Postgres, and shared mode

Scale-out plan, pull request 3b (docs/scale-out-plan.md, sections 3.1, 3.8 and 4, and "PR 3b:
budgets on Postgres, proposed before building", with its answers B1 to B4). Token budgets
move to Postgres with the reservation of section 3.1. With them in place, `GATEWAY_STATE=shared`
can start. Valkey's clock is checked against Postgres's. SQLite and the in-memory ledger are
unchanged, and so are the default path, the perf gate and the protected outputs.

## Reserve, settle, release

The request path asks `Budgets` (`gateway/ledger.py`) for three things:

- `reserve` before the request runs, or `BudgetExceededError` (429 `budget_exhausted`);
- then exactly one of `settle`, when the request billed (charge what it used), or `release`,
  when it billed nothing.

`CostLedger` (one process) implements them as before: `reserve` is the old check, `settle` the
old record, and `release` does nothing. Nothing new awaits on that path, so its answers are
byte for byte the same. `PostgresBudgets` (`gateway/postgres.py`) uses schema version 2, three
tables that exist on Postgres only:

| Table | Holds |
|---|---|
| `budget_counters` | Per application and period: `committed_tokens` and `reserved_tokens` |
| `budget_reservations` | One row per request in flight: its bound, its period, `expires_at_ms` |
| `budget_settlements` | The ids of reservations settled in the last day, so that a settle tried again is not charged twice |

| Call | Statements, in one transaction on the request pool (never the write lock) |
|---|---|
| reserve | the database's time; `UPDATE ... SET reserved += bound WHERE committed + reserved < budget RETURNING` (Q1); the reservation row. The period's first reserve seeds the counter from the period's usage events first |
| settle | the database's time; the settlement id (`ON CONFLICT DO NOTHING`: already there means already charged); the reservation deleted and taken off its counter in one statement, if it is still there; the usage event; `committed += billed` |
| release | the reservation deleted and taken off its counter in one statement |
| sweep | every reservation past `expires_at_ms` deleted and taken off; settlement ids older than a day deleted |

**The guarantee (Q1).** The admission condition and the increment are one `UPDATE`, under the
row lock Postgres takes for it. Two instances therefore never admit two requests against the
same remaining tokens: the second sees the first's reservation. The last request admitted can
still take the budget past by its own bound, as one request could before. A request that
arrives while nothing is in flight gets the same answer as before.

**The bound (Q2, B4).** `min(prompt_bytes + completion, context_tokens)`.

- `prompt_bytes` is the prompt in UTF-8 bytes; for chat it is the messages and tools as JSON.
  A byte-level tokenizer never produces more tokens than bytes, and neither does the stand-in.
- The completion is the request's `max_tokens`, else the plan's, else the tier's
  `reserve_completion_tokens`.
- `context_tokens` and `reserve_completion_tokens` are per-tier settings under `budgets:` in
  the backends file. No number ships for them; shared mode refuses to start while a tier lacks
  them.
- The bound only decides admission. The settle charges the billed tokens the request used.

**Seeding a counter.** A counter is created the first time its period is used, from the
period's usage events (`INSERT ... SELECT ... ON CONFLICT DO NOTHING`). A settle that finds no
counter seeds it with its own event already appended. If another instance seeds it at the same
moment, that instance's seed cannot see the event yet, because it is not committed. The
settle's seed then inserts nothing, and the settle adds its tokens to that counter instead. A
test holds a seed open in another session to show it.

## Which requests settle, which release

Each handler records the reservation it made. However the request ends, one `finally`
releases the reservation unless it was settled; once a stream takes the request over, the
stream's own `finally` does it. Anything that billed before is settled with the same tokens.
Anything that billed nothing is released.

| How a request ends | Budget |
|---|---|
| `/v1/generate` or chat finished, streamed or not | settled |
| timed out (504), refused by a full queue (429) | released |
| chat: a provider error before any output, refused after admission (422 `tools_not_supported`, 503 `no_local_provider`) | released |
| chat stream broken after output started (it bills nothing, as before) | released |
| client hung up mid-stream | released |
| refused by the budget itself | nothing was reserved |

When a client hangs up, Starlette cancels the stream's task, and that cancellation is
delivered again at every await in the stream's `finally`. The release is therefore shielded
(`asyncio.shield`) so that it finishes. Without the shield the reservation stayed held until it
expired; the hang-up test on Postgres caught this, and mutation B10 shows it.

## An instance that dies holding a reservation

- **How it expires.** A reservation expires at `gateway_now_ms()` at reserve plus the request
  timeout plus 60 s (`RESERVATION_MARGIN_MS`). That is 180 s with the default 120 s timeout.
  Every instance sweeps every 30 s, beside the server and never on a request. The sweep deletes
  each expired reservation, takes it off its counter, and publishes `reservation_expired` with
  the application and the tokens it held, counted in
  `gateway_events_total{type="reservation_expired"}`. So a dead instance's reservation is held
  for at most 210 s.
- **What the customer sees.** If the leaked reservation is what fills the budget, the
  application's requests get `429 budget_exhausted`, with the same code and fields as any
  budget refusal. Two things differ when the counter's committed tokens alone are below the
  budget:
  - **`Retry-After` (B3)** is the time until the earliest in-flight reservation expires, the
    latest moment room is certain to return; before, it would have said the end of the period.
  - **The message** says that tokens are held by requests in progress, instead of when the
    budget resets: "Application 'acme' has used 30 of its 100 token budget for this period,
    and 80 tokens are held by requests in progress. Retry in 5 seconds, when they have finished
    or expired." A budget that is really used up keeps "The budget resets in N seconds."
    (Changed on approval.)
- **The dead request's usage is lost** (decision 8 of the plan): its instance never settled
  it, and the expiry event records it. If the instance comes back and settles it late, the
  settle charges the usage and does not take the reservation off a second time.

Tested with three instances (`test_a_dead_instances_reservation_expires_...`): A reserves and
dies; B refuses with `Retry-After` 180, then 1, a millisecond before the expiry; the sweep
releases it at the expiry; B admits; a late settle charges once.

## A settle Postgres does not take

The answer has been sent (or is about to be), so the usage must not be lost silently. A settle
or release that fails is kept and tried again on the injected clock, starting at 100 ms and
doubling up to 5 s. While anything is kept:

- `reserve` refuses with `503 state_unavailable` (`settle_waiting`);
- `/readyz` answers not ready.

On shutdown with something still kept, each reservation id is logged and nothing else; the
sweep of another instance releases those reservations. The settlement id makes a retry safe
when the first try was written but its answer was lost.

## Shared mode

`check_state_setting` refuses `GATEWAY_STATE=shared` unless:

- `GATEWAY_POSTGRES_URL` is set: budgets are shared only on Postgres;
- `GATEWAY_COUNTERS=valkey` (B2): with counters of its own, each instance would grant its own
  rate limit and login attempts.

The message names the missing setting and never a value.

`create_shared_app` then refuses:

- simulated live mode;
- a tier without its `budgets` settings;
- a store not on this machine without verified TLS (below).

It connects to nothing when it is built. The lifespan, on the server's event loop:

1. opens both pools and applies the schema, under its advisory lock;
2. reads the stored applications and checks their plans;
3. creates the bootstrap admin;
4. checks Valkey;
5. then serves.

There is no `ProcessLock`, since several processes on one database are the point. `/readyz`
is ready only when Postgres answers, the counters are healthy and no budget write is kept. Key
expiry, sessions, step-up and the lockout use the database's clock (`StoreClock`, note 36).

**A store off this machine (B1).** `check_store_address` runs before any connection. A
Postgres or Valkey whose host is not loopback (or a Unix socket) must ask for a verified TLS
connection, or the operator must vouch for the network with
`GATEWAY_STATE_PRIVATE_NETWORK=1`:

- **Postgres:** `sslmode=verify-full`. Every host counts, including a `host` parameter or a
  list of hosts.
- **Valkey:** a `rediss://` address, without turning off the client's defaults
  (`ssl_cert_reqs=required`, `ssl_check_hostname=true`).

This applies to the Valkey of `GATEWAY_COUNTERS=valkey` on SQLite too. The error names the
setting and never the address.

## Valkey's clock against Postgres's (section 3.8)

With Postgres configured, `ValkeyWatch` measures the instance's `EpochSource` against
`SELECT gateway_now_ms()`, taking the midpoint of the round trip. Valkey's `TIME` is then not
read. The bound is `GATEWAY_STATE_MAX_SKEW_MS` (default 1000 ms).

- At start, an instance past the bound refuses to start, and the error names Postgres's clock.
- Every 30 s on the injected clock it checks again. Past the bound, every call that would use
  a Valkey window is refused (`503 state_unavailable`) and `/readyz` answers not ready, until a
  check passes again. Postgres-held windows are not affected.

The tests control both clocks: Postgres's through the test `gateway_now_ms()`, the instance's
through a `ManualEpoch`, and the 30 s on a `SimClock`.

## Postgres down, per endpoint

Note 36's table holds for shared mode; the budget calls add:

| Postgres down at | Answer |
|---|---|
| `/v1/generate`, `/v1/chat/completions` | 503 `state_unavailable` at the key check, before the budget (note 36); with the key check passing and the budget store down, 503 `state_unavailable` at the reserve, and no model sees the request |
| A settle or release | kept and tried again; not ready, and every new reserve refused with 503, until it is written |
| The sweep | logged and tried again 30 s later; reservations stay counted meanwhile (never released early) |
| Start | the server does not start |

Missing equals forbidden throughout: an answer the store cannot give is a refusal, never an
admission.

## How many calls one request makes now

Counted on the driver by `test_the_round_trips_of_a_reserve_and_a_settle`. Every statement is
one round trip, and so is the reset asyncpg sends when a connection goes back to the pool.

- **reserve:** 7 round trips: `BEGIN`, `SET LOCAL statement_timeout`, the time, the
  conditional `UPDATE`, the reservation `INSERT`, `COMMIT`, the reset.
- **settle:** 9 round trips: `BEGIN`, `SET`, the time, the settlement `INSERT`, the take-off,
  the usage `INSERT`, the commit `UPDATE`, `COMMIT`, the reset.

Counted on the Postgres stores by
`test_the_store_calls_of_a_chat_request_and_a_generate_request_in_shared_mode` and
`test_the_store_calls_of_a_chat_request_an_organization_provider_serves`, the store calls of one
request in shared mode (the first two the same for the first request of a period and after):

| Request | Store calls, one after another |
|---|---|
| Chat on the gateway's tier | 7: `lookup_key`, reserve, the two modes twice, settle (its usage row inside) |
| Chat served by an organization provider | 21: `lookup_key`; reserve; the candidates twice (the two modes, `list_for`, `get_key`); the modes; `get_key` twice (skip, lease); the modes three times (the egress scope); settle (its usage row inside) |
| `/v1/generate` | 3: `lookup_key`, reserve, settle |

Note 36's chat request made 6 on SQLite after the period's first (`lookup_key`, the modes
twice, `append`), and 7 on the first, which read the period's totals. In shared mode the
reserve and the settle replace `append` and that read, every time: one call more than note 36's
6 and 20. PR 3c's single read of the modes would take 10 of the provider path's 21 away. The
timing is below.

## Threat-model additions

- **The gateway now runs on Postgres in production** (shared mode): every durable secret it
  holds (key HMACs, password hashes, sealed provider keys) is in a database reached over the
  network. A store off this machine needs verified TLS, or the operator's statement that the
  network is private.
- **A dead instance holds budget** for at most the request timeout plus 90 s (the margin and
  one sweep interval). Its customers may be refused for that long, with a `Retry-After` that
  says so. Someone who can kill instances at will can use this to hold an application's budget,
  but no more than one request bound per request in flight.
- **Budget rows hold no request content**: application ids, periods, token counts and times.
- **The bound is generous by design** (bytes, not tokens): it can refuse concurrent requests
  near the edge sooner, never admit more.

## Timing

MEASURED on this machine (Intel Core Ultra 7 155H, 22 logical cores, 15.4 GB, Windows 11,
Python 3.12.10), at commit `d133466`, with
`GATEWAY_POSTGRES_URL=postgresql://postgres@127.0.0.1:5433/postgres GATEWAY_POSTGRES_TEST_NO_PASSWORD=1 uv run python -m benchmarks.durable_calls`,
three runs of 2000 calls per row after 20 warm-up calls. SQLite is a file in the temporary
folder. Postgres is the compose file's container under Docker Desktop, reached on 127.0.0.1,
so every round trip crosses into Docker's VM. Ranges are over the three runs.

| What | SQLite p50 ms | Postgres p50 ms | Postgres p99 ms |
|---|---|---|---|
| Budget, admit and settle (one charged request) | 0.364 to 0.401 | 8.292 to 8.427 | 11.961 to 13.214 |
| Reserve alone | - | 3.432 to 3.449 | 5.942 to 6.101 |
| Settle alone (its usage row included) | - | 4.582 to 4.598 | 7.539 to 8.010 |
| Release, with the reserve it gives back | - | 6.059 to 6.216 | 9.320 to 9.830 |
| Chat on the gateway's tier, note 36's calls (6, `append`) | 0.401 to 0.449 | 6.621 to 6.998 | 10.069 to 13.284 |
| Chat on the gateway's tier, shared budgets (7 calls) | - | 13.949 to 14.018 | 18.992 to 20.099 |

On SQLite the admit and settle is the ledger's check (in memory after the period's first
request) and the usage row: about 0.4 ms. On Postgres a charged request's budget costs about
8.3 ms at the median. That is about 7.2 ms more than the single `append` it replaces (1.080 to
1.195 ms in the same runs), and about 7.3 ms more for a chat request on the gateway's tier.

Where it goes: the reserve is 7 round trips and the settle 9 (above), about 0.5 ms each into
Docker's VM here. Half of each call is the transaction's own overhead (`BEGIN`,
`SET LOCAL statement_timeout`, `COMMIT`, the pool's reset) rather than the decisions. Not
changed in this PR: fewer round trips would mean dropping the per-transaction statement
timeout or the pool's reset, both safety settings. That is a separate decision, with a
measurement.

Store calls only, not whole requests. Nothing on the default path uses Postgres; on it the
budget calls are the ledger's, as before.
