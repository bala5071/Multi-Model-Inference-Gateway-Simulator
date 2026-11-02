# 43. The alert recorder, its rollups, and the signals

PR A1 of `docs/settings-and-alerts-plan.md` (sections 6 and 11.1). The numbers alert rules will
be judged on: what happened to each application's requests, per minute, and the signals
computed from them with their minimums. No rule, no evaluator, nothing that reads them but
tests; no outbound connection and no new secret.

## The recorder

`AlertRecorder` (`gateway/alertrecorder.py`) is told how each keyed request of `/v1/generate`
and `/v1/chat/completions` ended, and counts, per application and per minute of calendar time
(the instance's `EpochSource`, so minutes line up across instances):

- requests, by kind (generate, chat);
- outcomes by status class (`2xx`, `4xx`, `5xx`, and `504` alone for the timeout rate) and by
  error code, from a fixed list (any other code is `other`, so the fields stay a small fixed set
  whatever a client sends);
- rejections by reason (`rate_limited`, `budget_exhausted`, `queue_full`);
- fallbacks, by the first reason (as `X-Fallback-Reason` reports it), when the answer came from
  a later candidate; hosted attempts (the retry included); answers from a hosted provider;
  downgrades;
- two latency histograms, time to first token and total, for answered requests.

**It never awaits and does no I/O**: a call adds integers to a dictionary (a structural test
checks the module has no coroutine, no `await` and no I/O import). One call costs about 1.1 to
1.9 microseconds (below). `AlertOutcomeMiddleware` in `app.py` calls it once per request, when the
last of the answer is sent, so a stream is recorded at its end; the handlers note the
application, the latencies, the downgrade and the chat route on a per-request note
(a context variable). A request with no application (no key, an invalid key) is not recorded; a
stream whose client went away before its end is not either; nor are the requests load control
runs inside the server (it calls the handler without HTTP).

**The histogram** has fixed edges at `2^(k/4)` ms for `k = 0..71` (1 ms to about 220 s, each
edge about 19% above the last) and an overflow bucket. A value goes to the first edge at or
above it. A percentile is the upper edge of the bucket its nearest rank falls in (`inf` in the
overflow bucket). Bucket counts from several instances add up exactly; percentiles would not. A
latency threshold will be snapped to the nearest edge (`snap`), so "p99 above T" compares
exactly against counts (decision 11).

## The rollups

`AlertRollups` (`gateway/alertrollups.py`), a counter protocol with two implementations:

- **in memory** (`InMemoryAlertRollups`): with in-memory counters, the recorder writes each
  request's counts straight into it, so nothing waits and no task runs (simulated live mode's
  determinism is untouched). It keeps 120 minutes behind the newest per application.
- **Valkey** (`ValkeyAlertRollups`, with `GATEWAY_COUNTERS=valkey`): one hash per (application,
  minute), its name an HMAC under a new derived key (`KeyPurpose.ALERT_ROLLUPS`, note 35's
  rule), its fields the recorder's fixed names, its values counts, kept two hours. The recorder
  keeps deltas, and `RollupFlusher` sends them every 10 s from the lifespan, off the request
  path, and once more at shutdown.

**Sent once.** Each flush is a batch with an id; one Lua script sets the id (`SET NX`) and adds
the counts, so a batch sent again after a call that timed out once Valkey had applied it adds
nothing. New deltas go in a new batch.

**Kept, then dropped, and said so.** A batch Valkey does not take is kept and sent with the
next flush; kept 10 minutes, it is dropped, counted (`RollupFlusher.dropped`, and a warning
with the count), and its minutes are marked incomplete once Valkey answers again. A restart of
Valkey (a new run id, which already publishes `state_store_restarted`) marks every minute up to
it incomplete. A window with an incomplete minute is too few to judge. An instance that dies
loses up to 10 s of deltas without saying so; a `requests < N` rule can read low by that much.
With SQLite and Valkey, two deployments sharing one Valkey would add up an application id they
both have, as the rate limit would (note 35): the operator must not share it.

## The signals

`gateway/signals.py`, pure functions. Every signal is computed only from one application's own
rollups (`evaluate` refuses another application's or a window of the wrong length), or from a
durable value of its own (`budget_used`, `circuit_open`, `key_expires_in`,
`reservation_expired`, read by the evaluator in A3). Model-wide numbers never reach them: the
recorder keys every count by application.

| Signal | Windows (min) | Too few to judge below |
|---|---|---|
| `latency_total_p50`, `_p95`, `_p99`, `latency_ttft_*` | 5, 15, 60 | 10, 100, 500 requests in the histogram (`5 / (1 - p)`) |
| `error_rate`, `timeout_rate` | 5, 15, 60 | 20 requests |
| `state_unavailable_rate` | 5, 15 | 20 requests |
| `rejections` (a reason) | 5, 15, 60 | never (a count) |
| `rejection_rate` (a reason) | 5, 15, 60 | 20 requests |
| `fallback_rate` (a reason, or any) | 15, 60 | 20 chat requests |
| `hosted_share` | 15, 60 | 20 chat requests |
| `downgrade_rate` | 15, 60 | 20 generate requests |
| `requests` | 5, 15, 60 | never (a count) |
| `budget_used` | the period | an effective budget of 0 |
| `circuit_open` | now | the breaker store did not answer |

Below its minimum a signal is "too few to judge" (`SignalValue(None, "too_few")`), never 0;
over an incomplete window, `incomplete`.

## Not built here

The rules (A2), the evaluator and its states (A3), channels (A4). The metric
`gateway_alert_rollups_dropped_total` waits for the PR that reads the rollups: the Prometheus
families are built from the monitoring snapshot, whose schema the dashboard mirrors, and
nothing would read it before then. The drop count is on the flusher and in the log.

## Time

MEASURED on this machine (Intel Core Ultra 7 155H, 22 logical cores, 15.4 GB, Windows 11
10.0.26200, Python 3.12.10), `uv run python -m benchmarks.alert_recorder`, 10,000 calls a row
over 100 applications, three runs, on branch `settings-alerts/s3-a1-a2` after S3:

| Call | p50 ms (3 runs) | p99 ms |
|---|---|---|
| answered, flushed (deltas) | 0.0013 to 0.0014 | 0.0018 to 0.0021 |
| answered, in memory | 0.0018 to 0.0019 | 0.0025 to 0.0039 |
| chat, fell back | 0.0016 | 0.0024 to 0.0040 |
| refused | 0.0011 to 0.0012 | 0.0012 to 0.0022 |

**On the request path**, with the recorder and its middleware in place (in-memory counters,
so the recorder writes straight into the in-memory rollups), `GATEWAY_POSTGRES_URL=... uv run
python -m benchmarks.request_store_calls`, three runs, against the same benchmark after S3
(note 42):

| Request | Store calls after S3 | after A1 | p50 ms after S3 (3 runs) | after A1 |
|---|---|---|---|---|
| gateway tier, SQLite | 5 | 5 | 3.510 to 4.093 | 3.567 to 4.025 |
| gateway tier, Postgres | 5 | 5 | 16.928 to 17.855 | 16.867 to 17.656 |
| org provider, SQLite | 12 | 12 | 4.271 to 4.812 | 4.109 to 4.819 |
| org provider, Postgres | 12 | 12 | 26.839 to 28.669 | 26.557 to 27.720 |

Postgres round trips per request unchanged (22 and 36). `uv run python -m
benchmarks.key_checks`, the key check: p50 0.022 to 0.023 ms after S3, 0.023 to 0.029 ms after
A1 (the recorder is not on that path). The recorder adds no store call, and its cost does not
show in the request's time at this spread.
