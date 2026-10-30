# 38. Store round trips on the request path

PR 3c of `docs/scale-out-plan.md`. Two things: each mode is read once per chat request, and
where a Postgres transaction's round trips go, measured, with a proposal for cutting them that
is not built.

## Each mode read once per request

The chat handler reads the organization's and the application's mode once, through
`ChatRouter.mode` (so `effective_mode` stays the only place the mode is decided), right after
the budget's reserve, where it first asked for candidates before. `ChatRouter.candidates` and
`ChatRouter.run` take that mode and read no store for it. The egress scope the router opens
answers "may it go hosted?" from the same value, at every request, connection and resolved name.

**What this changes, on request.** Until PR 3c a mode made stricter mid-request stopped that
request's next attempt, at the router and at the egress guard (note 33). Now a request finishes
under the mode it started with, its fallback attempts included, and a mode change applies from
the next request. How long a stricter mode can take to reach every request: one request's
lifetime, at most its tier's total timeout. What stays: the guard fails closed without a scope,
refuses every hosted destination under a `local_only` request, and an answer already streaming
is not cut. The mode endpoints' `in_flight` sentence says the new rule; their codes and fields
are unchanged.

**Tested** (`tests/test_chat_routing.py`):

- a mode changed mid-request, the organization's or the application's, stricter or looser, is
  not seen by that request and is seen by the next (four cases);
- the guard answers from the request's mode: the organization made `local_only` while the
  provider's name is resolved does not stop that connection, and the next request is local;
- each mode read once per request, counted on the store, on the request that read them most
  (the primary refused, then the tier, streaming and not) and on one the tier answers at once;
- the store calls of a chat request, on SQLite and on Postgres
  (`tests/test_budgets_postgres.py`), and the benchmark's round trips by kind.

## Store calls

| Chat request, shared mode | Before | After |
|---|---|---|
| Gateway tier | 7: `lookup_key`, reserve, the two modes twice, settle | 5: `lookup_key`, reserve, the two modes, settle |
| One organization provider | 21: `lookup_key`, reserve, the candidates twice (the modes, `list_for`, `get_key`), the modes, `get_key` twice, the modes three times (the egress scope), settle | 11: `lookup_key`, reserve, the two modes, `list_for` and `get_key` twice (the handler's and the router's candidates), `get_key` twice (skip, lease), settle |

On SQLite, without the shared budgets: 6 to 4 and 20 to 10 (the usage row's `append` in place
of the reserve and the settle).

## Where a request's round trips go

On Postgres each store call outside a transaction is one statement and one reset: asyncpg's
pool resets every connection given back to it (`SELECT pg_advisory_unlock_all(); CLOSE ALL;
UNLISTEN *; RESET ALL;`, one round trip). The reserve and the settle are each one transaction:
BEGIN, the statement timeout (`SET LOCAL statement_timeout`), the statements, COMMIT, and the
reset.

Round trips per chat request on Postgres, counted by `benchmarks/request_store_calls.py` (and
pinned by a test). Time is the median over 300 requests of each kind's total per request,
the range over three runs (below).

| Kind | Gateway tier, before | After | ms per request, before | After | Provider, before | After | ms per request, before | After |
|---|---|---|---|---|---|---|---|---|
| BEGIN | 2 | 2 | 1.612 to 1.683 | 1.489 to 1.602 | 2 | 2 | 1.581 to 1.802 | 1.464 to 1.968 |
| `SET LOCAL statement_timeout` | 2 | 2 | 1.321 to 1.390 | 1.212 to 1.358 | 2 | 2 | 1.317 to 1.461 | 1.210 to 1.711 |
| Statements | 13 | 11 | 9.645 to 10.338 | 7.633 to 8.466 | 27 | 17 | 19.265 to 21.891 | 11.620 to 15.803 |
| COMMIT | 2 | 2 | 1.294 to 1.378 | 1.178 to 1.322 | 2 | 2 | 1.258 to 1.418 | 1.179 to 1.659 |
| Pool reset | 7 | 5 | 4.756 to 5.052 | 3.144 to 3.455 | 21 | 11 | 13.880 to 15.549 | 6.704 to 9.391 |
| **All** | **26** | **22** | | | **54** | **34** | | |
| Client work outside round trips | | | 1.391 to 1.486 | 0.993 to 1.070 | | | 3.381 to 3.821 | 1.752 to 2.285 |

After this PR, 11 of the gateway tier's 22 round trips are not statements: the two
transactions' BEGIN, timeout and COMMIT (6) and the resets (5). On the provider path 17 of 34 are
not statements. Every round trip costs about the same, a statement no more than a BEGIN: what
costs is going to the database and back (here, Postgres in Docker Desktop on the same machine; on
a network the share would be larger).

## Measured

MEASURED on this machine: Intel Core Ultra 7 155H, 22 logical cores, 15.4 GB RAM, Windows 11
10.0.26200, Python 3.12.10; Postgres 18.6 in Docker Desktop 29.8.2 (`docker-compose.postgres.yml`)
on the same machine. Nothing else was running.

```sh
GATEWAY_POSTGRES_URL=postgresql://postgres@127.0.0.1:5433/postgres GATEWAY_POSTGRES_TEST_NO_PASSWORD=1 uv run python -m benchmarks.request_store_calls --rounds 300 --runs 3
```

Before: at `a8dd42a` (the benchmark, before the change). After: at `1cd32c7` (the change). Three
runs each of 300 requests per case after 20 warm-up; p50 in ms, the range over the three runs.
"Store time" is the time spent inside the store calls of one request, which run one after
another on its path: what the stores add to it.

| Chat request | Store calls | Request p50 | Store time p50 |
|---|---|---|---|
| Gateway tier, SQLite, before | 6 | 6.260 to 7.115 | 1.155 to 1.285 |
| Gateway tier, SQLite, after | 4 | 5.994 to 7.583 | 0.946 to 1.131 |
| Gateway tier, Postgres, before | 7 | 26.645 to 27.935 | 20.483 to 21.874 |
| Gateway tier, Postgres, after | 5 | 21.391 to 23.411 | 16.072 to 17.788 |
| One organization provider, SQLite, before | 20 | 7.793 to 7.948 | 1.523 to 1.590 |
| One organization provider, SQLite, after | 10 | 6.445 to 7.943 | 1.197 to 1.320 |
| One organization provider, Postgres, before | 21 | 48.136 to 53.732 | 40.888 to 46.044 |
| One organization provider, Postgres, after | 11 | 30.487 to 40.629 | 24.294 to 33.200 |

(The SQLite rows' counts are the benchmark's: its ledger's `reserve` and `settle` stand for the
budget check and the usage row; `tests/test_chat_routing.py` counts the store's own calls, 4 and
10.) The first of the three "after" runs of the provider path on Postgres was slower than the
other two (40.629 against 30.487 and 30.893); the range keeps it. On SQLite the change is within
the spread between runs. These requests come one at a time: under load a pool connection and the
database are shared, which this does not measure.

## Cutting the per-transaction overhead: proposed, not built

Measured above: on Postgres, the overhead of a transaction (BEGIN, the statement timeout,
COMMIT) and the pool's reset after every store call are 11 of the gateway tier's 22 round trips
and 17 of the provider path's 34. Ways to cut them, each with what it would change. None is
built: the request was to build only what keeps behaviour identical **and** is covered by the
existing tests, and none is both.

| | What | Round trips per chat request (gateway tier, provider) | Why not built |
|---|---|---|---|
| 1 | **No reset on the request pool.** `create_pool(reset=...)` with a reset that sends nothing; asyncpg still rolls back an open transaction and drops listeners itself. The request path sets no session state: every `SET` is `SET LOCAL`, the advisory locks are transaction locks, nothing listens or holds a cursor. | -5, -11 | Identical only while that stays true, and no test checks it. It needs a test that after each request-pool call the session is as new (settings, locks, listeners), and the management pool keeps its reset. |
| 2 | **BEGIN and the timeout in one round trip.** `BEGIN; SET LOCAL statement_timeout = ...` sent together, COMMIT or ROLLBACK by hand, in place of asyncpg's `transaction()`. | -2, -2 | No test covers the statement timeout of a request transaction today, so a mistake there would pass. Setting the timeout on the request pool's connections instead (`server_settings`) would also bound the statements outside a transaction: a behaviour change. |
| 3 | **Each budget call one statement.** The reserve (the time, the admission, the reservation) and the settle (the time, settle-once, take-off, the usage row, the commit, the seed) each as one SQL function, run outside an explicit transaction: the reserve's 7 round trips and the settle's 9 become 2 each. | -12, -12 | Rewrites the statements the atomic rules rest on (Q1, settle once, the seed race): mutation checks B1 to B29 of PR 3b would all have to be run again on the new statements. The largest cut; worth its own PR. |
| 4 | **The two modes in one statement.** A store method returning both, one `SELECT` over the `modes` table. | -2, -2 | A new store method on three stores, with tests of its own. |
| 5 | **The candidates and the key read once.** The handler's candidates passed to the router, and the key read once for skip and lease. | 0, -8 | Changes when a provider disabled, or a key replaced, mid-request is seen (as this PR did for modes); not asked. |

With 1, 3 and 4 (3 makes 2 unnecessary), the gateway tier's request would make 4 round trips
instead of 22: the lookup, the two modes in one statement, the reserve and the settle; the
provider path 10 instead of 34. How much time that saves is not stated: it was not built, so it
was not measured.

## Not built

Everything in the proposal above. The candidates are still worked out twice and a provider's key
read three times on the provider path (proposal 5).
