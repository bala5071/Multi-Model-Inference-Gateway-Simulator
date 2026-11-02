# 41. Breaker reports off the request path, and a cap on hosted attempts

PR 2b of `docs/scale-out-plan.md`, as decided on 2026-11-02. Two follow-ups of the shared
counters (note 35): the circuit breakers' reports leave the request path, and one request's
calls to organization providers are capped.

## The reports, through a queue

`admit` stays on the path: it decides whether a provider is tried, and it still fails closed
(Valkey unavailable: refused). The reports (`success`, `failure`, `release`) of an attempt a
closed breaker admitted go into a bounded queue of the instance's (`BreakerReports`, 1,024 at
most, a number chosen, not measured), sent in the order they were made by one sender; the request no longer waits for them.

**A half-open probe's report is sent at once, on the path, never queued**, after any report of
the same breaker still in the queue, so the breaker sees every report in the order it was made.
`admit_attempt` says whether an admission is a probe (in Valkey the breaker script already
returns the state). A probe's slot is therefore never held by a report waiting in, or dropped
from, the queue.

**What a dropped report does.** A report is dropped when the queue is full, when the shutdown
flush has no time left for it, or when Valkey cannot take it. It never admits anything. It can
make a breaker open late (a `failure` lost) or early (a `success` lost, so the run of failures
is not reset), refusing a provider for at most its open time, after which it probes. Every drop
is counted, so such a refusal is visible:

- in the monitoring snapshot, `breakers.reports_dropped` by reason (`queue_full`,
  `flush_timeout`, `unavailable`; docs/15; the dashboard's fixtures and types regenerated);
- in Prometheus, `gateway_breaker_reports_dropped_total{reason=...}`.

Nothing decides anything from the counts. The server's lifespan flushes the queue at shutdown,
before the counters' client closes, for at most 2 s of the injected clock (chosen, not
measured); what is left is
dropped and counted, a report cut off while being written included.

## A probe that never reports, in memory too, and the hole that made one

The in-memory breakers now free a half-open probe's slot after 120 s without a report
(`PROBE_MS`), as the Valkey ones always have, whatever keeps the report from coming.

One way it did not come existed before any queue: a probe admitted and then cancelled (the
client hangs up), or whose mode re-read just before the hosted attempt (note 38) failed, sent
no report. In Valkey the slot freed itself after 120 s; in memory it stayed taken until the
process restarted, and every request skipped that provider meanwhile. That hole is closed:
when the re-read is cancelled or fails, the request gives back what it was admitted for, so the
slot is free at once, and the request still fails as before (closed; 503 `state_unavailable`
when the store cannot answer). It gives back only its own admission: another request's probe
is never released (`test_another_requests_probe_is_never_released`).

## The cap on hosted attempts

`GATEWAY_CHAT_MAX_HOSTED_ATTEMPTS`, the operator's: 3 by default, a whole number from 1 to 5;
anything else refuses the server's start, naming the variable. Each call to an organization
provider within one request is an attempt, the retry after invalid tool JSON included; a
provider skipped without a call (an open breaker, an unreadable key) is not. At the cap, the
remaining organization providers are skipped without a call or a question to their breakers,
with the fallback reason `attempt_cap`, recorded like the others (the route trace keeps every
reason; `X-Fallback-Reason` is still the first). The gateway's own tier is still tried where
the mode allows it, so the cap never sends a prompt anywhere the mode would not. It bounds the
time one request spends on providers and how many third parties one prompt reaches. The
per-application setting under it (S14) is PR S3 of the settings plan.

## Counter calls on the path

The worst case, counted by `test_the_most_counter_calls_one_chat_request_makes_*`, with the cap
C (3): one rate-limit call, then an admission per attempt, so **1 + C** with every breaker
closed (4; the reports queued); and **3C** when every attempt is a half-open probe (9): its
admission, its report at once, and the refused admission of its retry, except the last
attempt's retry, which the cap stops before its breaker is asked. Before this PR: 1 + 4N for N
providers, with no cap (13 for three).

## Store calls and time

Unchanged store calls: a chat request makes 5 (`lookup_key`, `reserve`, the two modes,
`settle`) on the gateway's tier and 13 with one organization provider, on SQLite and on
Postgres with the shared budgets, before and after (`benchmarks.request_store_calls`).

MEASURED on this machine (Intel Core Ultra 7 155H, 22 logical cores, 15.4 GB, Windows 11
10.0.26200, Python 3.12.10; SQLite in the temp folder; Postgres 18.6 in Docker Desktop on the
same machine): `python -m benchmarks.request_store_calls`, three runs of 300 requests each, p50
of one request in ms, the range over the runs, before (`9c2c272`) and after (`43b48fd`):

| Case | Before | After |
|---|---|---|
| Gateway tier, SQLite | 4.527 to 4.847 | 4.206 to 4.811 |
| Gateway tier, Postgres | 18.372 to 19.111 | 17.712 to 18.303 |
| One organization provider, SQLite | 5.505 to 6.177 | 4.701 to 5.467 |
| One organization provider, Postgres | 28.890 to 33.670 | 28.070 to 28.935 |

No case's range moved up. That run keeps the counters in memory, where a breaker call costs
little.

**With the counters in Valkey** (`--counters valkey`, the Valkey of
`docker-compose.valkey.yml` on the same machine), the same benchmark run on the base and this
branch in turn, three times each, p50 in ms, before (`9c2c272`) and after (`c8756ae`):

| Case | Before | After |
|---|---|---|
| Gateway tier, SQLite | 5.317 to 6.087 | 5.249 to 9.321 |
| Gateway tier, Postgres | 19.091 to 19.470 | 17.696 to 25.223 |
| One organization provider, SQLite | 7.537 to 8.382 | 7.076 to 7.913 |
| One organization provider, Postgres | 30.668 to 33.597 | 29.225 to 32.982 |

**The saving is small.** A request the gateway's tier answers makes no breaker call, so nothing
moves there; the one high run (9.3 and 25.2 ms) is the machine's, on a path this PR does not
change. A request one organization provider answers sends one report fewer on the path (its
`success`), and its ranges move down by about 0.3 to 0.6 ms, the size of their own spread. The
queue matters in the worst case (13 counter calls became at most 4 with closed breakers), not in
the common one.
