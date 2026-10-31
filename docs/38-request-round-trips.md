# 38. Store round trips on the request path

PR 3c of `docs/scale-out-plan.md`. Two things: each mode is read once per chat request, and
again just before each hosted attempt; and where a Postgres transaction's round trips go,
measured, with a proposal for cutting them that is not built.

## The modes: once per request, and again before each hosted attempt

The chat handler reads the organization's and the application's mode once, through
`ChatRouter.mode` (so `effective_mode` stays the only place the mode is decided), right after
the budget's reserve, where it first asked for candidates before. `ChatRouter.candidates` takes
that mode and reads no store for it, and the egress scope the router opens answers "may it go
hosted?" from the same value, at every request, connection and resolved name.

**One exception, added on approval of #28: before every hosted attempt** (a primary, a fallback,
the retry after invalid tool JSON), the router reads both modes again, just before the attempt
leaves the machine (after the breaker and key check, before the key is leased and the guarded
client opened). If the current mode is stricter than the request's, that attempt is not made: the
candidate is skipped and a breaker slot it claimed is given back. The rule, as the mode
endpoints' `in_flight` sentence now says:

- **a stricter mode** stops every hosted attempt a request in flight has not yet made;
- **a looser mode** never opens one for it: it applies from the next request;
- **an answer already streaming** from a hosted provider is not cut: its prompt has been sent.

So a request the gateway's tier answers reads the modes once; one with a hosted attempt reads
them once more per hosted attempt. The egress guard still fails closed without a scope and
refuses every hosted destination under a `local_only` request. The window left for a stricter
mode: between that read and the first byte sent, the key is read for the lease, the provider's
name resolved and the connection opened; a change in that window applies to the next attempt.

**Tested** (`tests/test_chat_routing.py`):

- the organization's or the application's mode made stricter mid-request stops the request's
  hosted fallback, and the next request's; made looser, the request does not fall back and the
  next one does (four cases);
- made stricter while the first primary is tried (hosted primary to hosted fallback, organization
  and application), the next primary is not tried and the gateway's tier answers;
- an answer already streaming is not cut, and the next request does not reach the provider;
- the guard answers from the request's mode: a change while the provider's name is resolved,
  after the router's read, does not stop that connection, and the next request is local;
- counted on the store: a request the tier answers reads each mode once (`local_only` and
  `local_with_hosted_fallback`); with one hosted attempt, twice;
- the store calls of a chat request, on SQLite and on Postgres
  (`tests/test_budgets_postgres.py`), and the benchmark's round trips by kind.

## Store calls

| Chat request, shared mode | Before | After |
|---|---|---|
| Gateway tier | 7: `lookup_key`, reserve, the two modes twice, settle | 5: `lookup_key`, reserve, the two modes, settle |
| One organization provider | 21: `lookup_key`, reserve, the candidates twice (the modes, `list_for`, `get_key`), the modes, `get_key` twice, the modes three times (the egress scope), settle | 13: `lookup_key`, reserve, the two modes, `list_for` and `get_key` twice (the handler's and the router's candidates), `get_key` (skip), the two modes again (before the hosted attempt), `get_key` (lease), settle |

On SQLite, without the shared budgets: 6 to 4 and 20 to 12 (the usage row's `append` in place
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
| BEGIN | 2 | 2 | 1.612 to 1.683 | 1.681 to 2.209 | 2 | 2 | 1.581 to 1.802 | 1.735 to 1.957 |
| `SET LOCAL statement_timeout` | 2 | 2 | 1.321 to 1.390 | 1.326 to 1.857 | 2 | 2 | 1.317 to 1.461 | 1.416 to 1.695 |
| Statements | 13 | 11 | 9.645 to 10.338 | 8.587 to 11.669 | 27 | 19 | 19.265 to 21.891 | 14.827 to 17.732 |
| COMMIT | 2 | 2 | 1.294 to 1.378 | 1.325 to 1.858 | 2 | 2 | 1.258 to 1.418 | 1.335 to 1.631 |
| Pool reset | 7 | 5 | 4.756 to 5.052 | 3.465 to 4.776 | 21 | 13 | 13.880 to 15.549 | 9.385 to 11.255 |
| **All** | **26** | **22** | | | **54** | **38** | | |
| Client work outside round trips | | | 1.391 to 1.486 | 1.165 to 1.404 | | | 3.381 to 3.821 | 2.442 to 2.560 |

After this PR, 11 of the gateway tier's 22 round trips are not statements: the two
transactions' BEGIN, timeout and COMMIT (6) and the resets (5). On the provider path 19 of 38 are
not statements; the re-read before the hosted attempt is 4 of the 38 (two statements, two
resets). Every round trip costs about the same, a statement no more than a BEGIN: what
costs is going to the database and back (here, Postgres in Docker Desktop on the same machine; on
a network the share would be larger).

## Measured

MEASURED on this machine: Intel Core Ultra 7 155H, 22 logical cores, 15.4 GB RAM, Windows 11
10.0.26200, Python 3.12.10; Postgres 18.6 in Docker Desktop 29.8.2 (`docker-compose.postgres.yml`)
on the same machine. Nothing else was running.

```sh
GATEWAY_POSTGRES_URL=postgresql://postgres@127.0.0.1:5433/postgres GATEWAY_POSTGRES_TEST_NO_PASSWORD=1 uv run python -m benchmarks.request_store_calls --rounds 300 --runs 3
```

Before: at `a8dd42a` (the benchmark, before the change). After: at `66956b3` (the modes read
once, and again before each hosted attempt). Three runs each of 300 requests per case after 20
warm-up; p50 in ms, the range over the three runs.
"Store time" is the time spent inside the store calls of one request, which run one after
another on its path: what the stores add to it.

| Chat request | Store calls | Request p50 | Store time p50 |
|---|---|---|---|
| Gateway tier, SQLite, before | 7 | 6.260 to 7.115 | 1.155 to 1.285 |
| Gateway tier, SQLite, after | 5 | 3.598 to 6.924 | 0.622 to 1.193 |
| Gateway tier, Postgres, before | 7 | 26.645 to 27.935 | 20.483 to 21.874 |
| Gateway tier, Postgres, after | 5 | 24.049 to 30.681 | 18.022 to 24.067 |
| One organization provider, SQLite, before | 21 | 7.793 to 7.948 | 1.523 to 1.590 |
| One organization provider, SQLite, after | 13 | 6.967 to 8.133 | 1.262 to 1.386 |
| One organization provider, Postgres, before | 21 | 48.136 to 53.732 | 40.888 to 46.044 |
| One organization provider, Postgres, after | 13 | 38.898 to 44.608 | 31.370 to 37.497 |

(The SQLite rows' counts are the benchmark's: its ledger's `reserve` and `settle` stand for the
budget check and the usage row; `tests/test_chat_routing.py` counts the store's own calls, 6 to 4
and 20 to 12.)

The gateway tier's request is the same at `1cd32c7` (the modes read once, before the re-read was
added) and at `66956b3`: 5 calls and 22 round trips. Measured at `1cd32c7` it took 21.391 to
23.411 ms on Postgres; at `66956b3`, in a later session just after Docker Desktop restarted,
24.049 to 30.681 (the first run slowest). That difference is the spread between sessions, not the
change, and it is wider than the change: the later session's range covers the "before" range
(26.645 to 27.935). So the gateway tier's saving is stated as the two calls and four round trips it
no longer makes, not as a time. The provider path at `1cd32c7` (11 calls, 34 round trips) took
30.487 to 40.629 ms; the re-read adds two calls and four round trips to it. On SQLite the change is within
the spread between runs. These requests come one at a time: under load a pool connection and the
database are shared, which this does not measure.

## Cutting the per-transaction overhead: proposed, not built

Measured above: on Postgres, the overhead of a transaction (BEGIN, the statement timeout,
COMMIT) and the pool's reset after every store call are 11 of the gateway tier's 22 round trips
and 19 of the provider path's 38. Ways to cut them, each with what it would change. None is
built: the request was to build only what keeps behaviour identical **and** is covered by the
existing tests, and none is both.

| | What | Round trips per chat request (gateway tier, provider) | Why not built |
|---|---|---|---|
| 1 | **No reset on the request pool.** `create_pool(reset=...)` with a reset that sends nothing; asyncpg still rolls back an open transaction and drops listeners itself. The request path sets no session state: every `SET` is `SET LOCAL`, the advisory locks are transaction locks, nothing listens or holds a cursor. | -5, -13 | Identical only while that stays true, and no test checks it. It needs a test that after each request-pool call the session is as new (settings, locks, listeners), and the management pool keeps its reset. |
| 2 | **BEGIN and the timeout in one round trip.** `BEGIN; SET LOCAL statement_timeout = ...` sent together, COMMIT or ROLLBACK by hand, in place of asyncpg's `transaction()`. | -2, -2 | No test covers the statement timeout of a request transaction today, so a mistake there would pass. Setting the timeout on the request pool's connections instead (`server_settings`) would also bound the statements outside a transaction: a behaviour change. |
| 3 | **Each budget call one statement.** The reserve (the time, the admission, the reservation) and the settle (the time, settle-once, take-off, the usage row, the commit, the seed) each as one SQL function, run outside an explicit transaction: the reserve's 7 round trips and the settle's 9 become 2 each. | -12, -12 | Rewrites the statements the atomic rules rest on (Q1, settle once, the seed race): mutation checks B1 to B29 of PR 3b would all have to be run again on the new statements. The largest cut; worth its own PR. |
| 4 | **The two modes in one statement.** A store method returning both, one `SELECT` over the `modes` table, at the handler's read and at each re-read. | -2, -4 | A new store method on three stores, with tests of its own. |
| 5 | **The candidates and the key read once.** The handler's candidates passed to the router, and the key read once for skip and lease. | 0, -8 | Changes when a provider disabled, or a key replaced, mid-request is seen (as this PR did for modes); not asked. |

With 1, 3 and 4 (3 makes 2 unnecessary), the gateway tier's request would make 4 round trips
instead of 22: the lookup, the two modes in one statement, the reserve and the settle; the
provider path 11 instead of 38 (the lookup, the modes twice, the four candidate reads, the two
key reads, the reserve and the settle, with no resets). How much time that saves is not stated:
it was not built, so it was not measured.

## Not built

Everything in the proposal above. The candidates are still worked out twice and a provider's key
read three times on the provider path (proposal 5).
