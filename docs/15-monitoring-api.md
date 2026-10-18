# 15 — Monitoring API

This step adds the data layer a monitoring dashboard reads: a bounded time series per model
and per tenant, an event bus with a bounded log, and three endpoints. It adds no dashboard.

Code: `src/gateway/timeseries.py` (`TimeSeriesStore`), `src/gateway/events.py` (`EventBus`),
`src/gateway/monitoring.py` (`Monitor`, `render_prometheus`), wiring in `src/gateway/app.py`.
Tests: `tests/test_timeseries.py`, `tests/test_events.py`, `tests/test_monitoring_api.py`.

Every example value below is asserted by a test named beside it, on `SimClock` and the
exact-arithmetic test profile. They are simulated, not measurements of real models.

## Read this first

- **Monitoring watches; it decides nothing.** `app.py` tells the `Monitor` what happened to
  each request, and the monitor reads the rest. No scheduling, routing or admission code
  reads anything back from it.
- **Unavailable is not zero.** Each group of metrics has an `available` field. When it is
  `false`, every other field of the group is `null`, the matching fields of every series
  point are `null`, and `/metrics` leaves the series out.
- **With real models, most scheduler metrics are unavailable.** `PassthroughWorker` has no
  queue, batch, KV accounting or prefix cache; the model server does that out of the
  gateway's sight (note 12). A dashboard must not draw those as empty.
- **All times come from the injected clock**, in milliseconds. Under `WallClock` that is a
  monotonic reading with an arbitrary origin, not a date.

## Endpoints

| Endpoint | Returns |
|---|---|
| `GET /v1/monitor/snapshot` | One JSON snapshot. `history_s` (default 0) is how far back each series goes; `events` (default 50) is how many recent events to include. |
| `GET /v1/monitor/stream` | Server-sent events: a `snapshot` at once and then one every second, with the `event`s published in between. `history_s` applies to the first snapshot only. `count=N` ends the stream after N snapshots. |
| `GET /metrics` | The snapshot's current values in Prometheus text format 0.0.4. |

`/metrics/summary` and `/metrics/detail` (note 05) are unchanged.

A dashboard needs one connection: `GET /v1/monitor/stream?history_s=900` gives the whole
window first and then, each second, the second that just ended (now `final`) and the one
in progress. Points are keyed by `t_ms`; a later point for the same `t_ms` replaces the
earlier one.

### The stream

```
event: snapshot
data: {"at_ms": 0.0, ...}

id: 1
event: event
data: {"seq": 1, "at_ms": 500.0, "type": "queue_full", "model": "standard",
       "tenant_id": null, "data": {"waiting": 1, "limit": 1}}

event: snapshot
data: {"at_ms": 1000.0, ...}
```

(`test_stream_pushes_a_snapshot_every_second_and_the_events_in_between`; the `data` line is
one line in the real stream.)

- Events are sent just before the first snapshot taken after they happened, so they are up
  to one interval late. Snapshots in the stream have an empty `events.recent`.
- An `event` carries its sequence number as the SSE `id`. A client that reconnects with
  `Last-Event-ID`, or with the query parameter `last_event_id` (added in note 16, for a
  client that opens a new connection itself; it wins over the header), gets the retained
  events after that one. If some are already gone it
  first gets `event: events_missed` with `{"missed": n}`. A client without the header gets
  only what happens from then on.
- The interval is `MONITOR_STREAM_INTERVAL_MS` in `app.py`, slept on the injected clock.

## Snapshot schema

```json
{
  "at_ms": 17.0,
  "resolution_ms": 1000.0,
  "window_ms": 900000.0,
  "models": {"standard": {"traffic": {}, "queue": {}, "batch": {}, "kv": {}, "cache": {},
                          "latency": {}, "slo": {}, "series": []}},
  "tenants": {"available": false, "tenants": null},
  "events": {"available": true, "capacity": 1000, "retained": 0, "last_seq": 0,
             "counts": {"state_change": 0, "queue_full": 0, "timeout": 0, "downgrade": 0,
                        "budget_exhausted": 0},
             "recent": []}
}
```

`models` has every registered model, in registration order.

### Model groups: the state now

| Group | Fields besides `available` | Available when |
|---|---|---|
| `traffic` | `in_flight`, `arrived_total`, `completed_total`, `output_tokens_total`, `queue_full_total`, `timeout_total`, `downgrade_total` | always |
| `queue` | `length` | the worker has its own scheduler |
| `batch` | `size`, `max_size` | the worker has its own scheduler |
| `kv` | `used_tokens`, `capacity_tokens` | the worker has its own scheduler |
| `cache` | `size_tokens`, `lookups_total`, `hits_total`, `prompt_tokens_total`, `cached_tokens_total`, `hit_rate`, `token_hit_rate` | the worker has a prefix cache |
| `latency` | `window_ms`, and `ttft_ms`, `itl_ms`, `total_ms`, each `{p50, p99, samples}` or `null` | always |
| `slo` | `degraded`, `slo_p99_ms`, `recent_p99_ms`, `samples`, `window_ms`, `min_samples` | an SLO is configured for this model |

Which worker gives what:

| Worker | `queue`, `batch`, `kv` | `cache` |
|---|---|---|
| `ContinuousBatchingWorker` | available | available if it was built with a prefix cache |
| `PassthroughWorker` (real models) | **unavailable** | **unavailable** |
| `BaselineWorker` | unavailable | unavailable |

`BaselineWorker` does have a queue, but exposes nothing about it; it is the reference
worker and was left alone.

Definitions:

- **`in_flight`**: requests handed to the worker and not yet finished, failed or cancelled.
  Counted by the app, so it exists for every worker.
- **`arrived_total`** counts requests handed to the worker, including ones it then refused
  with `queue_full`. A request stopped earlier (unknown tenant, plan, budget) never arrives.
- **`output_tokens_total`** counts tokens as they are produced, so it includes the tokens of
  requests that later timed out, and can exceed the completed requests' total in
  `/metrics/detail`. With a real backend one streamed piece counts as one token; if the
  server then reports more tokens than pieces, the difference is added at completion.
- **`queue.length`** is the worker's `waiting`: requests that cannot be seated in a free
  batch slot right now (note 10). It is the number the queue limit applies to.
- **`batch.size`** is requests seated in the batch, decoding or being prefilled.
  `StepRecord.batch_size` (note 03) counts only the decoding ones.
- **`kv.used_tokens`** is tokens reserved by running requests plus tokens in the prefix
  cache, the same as `StepRecord.kv_tokens`.
- **`cache.hit_rate`** is lookups that matched at least one token over lookups;
  **`token_hit_rate`** is cached prompt tokens over looked-up prompt tokens. Both are since
  the cache was created, and `null` before the first lookup: no lookups is not a 0% hit rate.
- **`latency`** is the `MetricsStore` sliding window (note 05): the same numbers as
  `/metrics/detail`, measured from enqueue, nearest-rank.
- **`slo`** is `SloMonitor.health` (note 10). A model with no SLO has no degraded flag;
  that is `available: false`, not `degraded: false`. **Its window is not `latency`'s.**
  `recent_p99_ms` is the p99 of the requests finished in the SLO monitor's own window,
  `slo.window_ms` (60 s by default), and is the number `degraded` is decided on, once the
  window holds at least `min_samples` requests and outside the hysteresis band of note 10.
  `latency.total_ms.p99` is over the metrics window (five minutes). The two can disagree
  for minutes after a burst, so a client must not set `latency` against the SLO and read
  the result as the model's health. `window_ms` and `min_samples` were added after the
  dashboard did exactly that (note 16).

After one request of 10 prompt and 3 output tokens on the test profile
(`test_snapshot_of_one_request_has_exact_values`):

```json
{
  "traffic": {"available": true, "in_flight": 0, "arrived_total": 1, "completed_total": 1,
              "output_tokens_total": 3, "queue_full_total": 0, "timeout_total": 0,
              "downgrade_total": 0},
  "queue": {"available": true, "length": 0},
  "batch": {"available": true, "size": 0, "max_size": 4},
  "kv": {"available": true, "used_tokens": 10, "capacity_tokens": 1024},
  "cache": {"available": true, "size_tokens": 10, "lookups_total": 1, "hits_total": 0,
            "prompt_tokens_total": 10, "cached_tokens_total": 0, "hit_rate": 0.0,
            "token_hit_rate": 0.0},
  "latency": {"available": true, "window_ms": 300000.0,
              "ttft_ms": {"p50": 9.0, "p99": 9.0, "samples": 1},
              "itl_ms": {"p50": 4.0, "p99": 4.0, "samples": 2},
              "total_ms": {"p50": 17.0, "p99": 17.0, "samples": 1}},
  "slo": {"available": false, "degraded": null, "slo_p99_ms": null, "recent_p99_ms": null,
          "samples": null, "window_ms": null, "min_samples": null}
}
```

The same model behind a `PassthroughWorker`
(`test_passthrough_models_report_scheduler_metrics_unavailable_not_zero`):

```json
{
  "queue": {"available": false, "length": null},
  "batch": {"available": false, "size": null, "max_size": null},
  "kv": {"available": false, "used_tokens": null, "capacity_tokens": null},
  "cache": {"available": false, "size_tokens": null, "lookups_total": null, "hits_total": null,
            "prompt_tokens_total": null, "cached_tokens_total": null, "hit_rate": null,
            "token_hit_rate": null}
}
```

### Model series: one point per second

`series` is a list of points, oldest first. A point covers `[t_ms, t_ms + resolution_ms)`.

| Field | Meaning |
|---|---|
| `t_ms` | Start of the second. |
| `final` | `false` while the second is still running; the point can still change. |
| `arrived`, `completed`, `queue_full`, `timeouts`, `downgrades` | Counts in the second. `queue_full` is the model's 429s; `downgrades` is requests moved away from this model. |
| `req_per_s` | `arrived` divided by the resolution in seconds. |
| `output_tokens`, `tokens_per_s` | Tokens produced in the second, and the same as a rate. |
| `in_flight`, `in_flight_max` | Last value in the second, and the largest. |
| `queue_len`, `batch_size`, `kv_used_tokens`, each with `_max` | Last sample in the second, and the largest. `null` if the group is unavailable or the second has no sample. |
| `cache_lookups`, `cache_hit_rate`, `cache_token_hit_rate` | Lookups observed in the second and their hit rates. The rates are `null` when there were no lookups. |
| `ttft_p50_ms`, `ttft_p99_ms`, `itl_p50_ms`, `itl_p99_ms`, `total_p50_ms`, `total_p99_ms` | Nearest-rank over the requests that **finished in this second**. `null` if none did. |
| `latency_truncated` | `true` if the second had more samples than its cap; see below. |
| `degraded` | The SLO state at the end of the second. `null` for a model with no SLO. |

Things to know:

- **A second in which nothing was recorded has no point.** Readers see a gap. For the
  counts a gap means zero; for the gauges it means nobody looked. A snapshot looks at every
  model, so while a stream is connected every second has a point.
- **The gauges are sampled, not integrated.** The worker's queue, batch and KV are read
  when a request starts or ends, once per instant at which the model produces a token (so
  once per decode step), and on every snapshot. `_max` is the largest of those samples; a
  peak between two samples is not seen.
- **A refused request is in flight for an instant.** `in_flight_max` counts it. In
  `test_a_burst_shows_queue_batch_kv_and_the_rejected_request` six requests arrive at once,
  one is refused, and `in_flight_max` is 6.
- **Cache lookups are booked to the second in which they were observed**, which can be one
  sample later than they happened.
- **Per-second percentiles are small samples.** A p99 over the three requests that finished
  in a second is the slowest of the three. For a stable p99 use the `latency` group, which
  covers the metrics window. Percentiles of different seconds cannot be averaged.
- **ITL gaps are booked to the second the request finished in**, as in the metrics store.

### Tenants

`tenants.available` is `false` when the gateway has no plans. Otherwise `tenants.tenants`
has every tenant in the plans file, used or not.

| Field | Meaning |
|---|---|
| `plan`, `budget_tokens` | From the plans file. |
| `period_start_ms`, `period_end_ms` | The current 30-day budget period (note 09). |
| `used_tokens`, `remaining_tokens`, `spend_usd`, `requests` | From the cost ledger, **for the current period**. `spend_usd` is what was billed, after the plan's multiplier. |
| `rejections_total` | `{"budget_exhausted": n, "queue_full": n}`, **since the gateway started**. |
| `timeout_total`, `downgrade_total` | Since the gateway started. |
| `series` | Points of `t_ms`, `final`, `requests`, `billed_tokens`, `spend_usd`, `rejections`, `timeouts`, `downgrades`, for the seconds in which the tenant did something. |

A request with an unknown tenant id is refused before anything is recorded, so it creates no
series (`test_tenants_show_spend_tokens_against_budget_and_rejections`).

### Events

| Type | When | `data` |
|---|---|---|
| `state_change` | A model became degraded or recovered | `degraded`, `p99_ms` |
| `queue_full` | A request was refused with 429 `queue_full` | `waiting`, `limit` |
| `timeout` | A request was cancelled with 504 | `timeout_ms` |
| `downgrade` | A request was moved to a cheaper model | `to_model` (`model` is the one it left) |
| `budget_exhausted` | A request was refused with 429 `budget_exhausted` | `used_tokens`, `budget_tokens` |

Every event has `seq`, `at_ms`, `type`, `model` and `tenant_id` (`null` without plans).
`seq` counts from 1 with no gaps. `events.counts` has every type, counted since the start.

## Bounded memory

| What | Bound | Default |
|---|---|---|
| Buckets per model and per tenant | `ceil(window_ms / resolution_ms)` | 900 (15 min at 1 s) |
| Raw latency samples per model | one open bucket: `2 * max_bucket_samples + max_bucket_itl_samples` | 2 x 1,024 + 8,192 |
| Event log | `max_events` | 1,000 |
| Number of series | registered models + tenants in the plans file | |

- **Buckets are created on first use and dropped by age.** A bucket older than the window is
  removed on the next write or read of that series.
- **Only the open bucket holds raw samples.** When the clock moves to a later second they
  are reduced to six percentiles and freed. A second with more finished requests or gaps
  than the caps keeps the first ones and sets `latency_truncated`.
- **The store does not limit the number of series itself.** `Monitor` only records
  registered models and configured tenants; unknown names are refused by the router first.
- **A flood of one event type pushes the others out of the log.** The counters still count
  them. A separate log per type would be the fix.
- `create_app(..., series=TimeSeriesStore(clock, window_ms=...), events=EventBus(clock,
  max_events=...))` sets other limits.

Tests: `test_memory_stays_bounded_over_many_windows_of_traffic` (300,000 requests over five
windows leave 60 buckets per series), `test_memory_stays_bounded_after_many_events`,
`test_memory_stays_bounded_through_the_api`.

## Prometheus

`GET /metrics` renders the same snapshot. Counters end in `_total`; times are in
milliseconds and the names say so (`gateway_ttft_ms`), against the Prometheus habit of
seconds, because every other number in the gateway is in ms.

```
gateway_metric_group_available{model="premium",group="queue"} 0
gateway_requests_completed_total{model="premium"} 1
gateway_ttft_ms{model="premium",quantile="0.99"} 50.0
```

- **An unavailable metric has no series.** `gateway_queue_length` is simply absent for a
  pass-through model, and `gateway_metric_group_available` says why. A scrape must never
  show a real model with a queue of 0.
- The latency quantiles are gauges over the metrics window, not a Prometheus summary: there
  is no lifetime `_sum` to go with them.
- Tenant usage (`gateway_tenant_used_tokens` and the like) is exported as gauges, since it
  starts again from zero each budget period.
- The full output for one request is asserted in
  `test_metrics_endpoint_is_exact_prometheus_text_for_one_request`.

## Design decisions

- **No background task.** Nothing ticks once a second. Buckets are filled by what happens
  and by whoever reads. A ticker on `SimClock` would keep simulated time running for ever
  and would add wake-ups to every test and benchmark. The cost is the gaps described above.
- **Per-second percentiles over that second's requests, not a sliding p99 sampled each
  second.** They are exact, need no raw history, and do not depend on when a bucket is
  read. The sliding view already exists in the metrics store and is in the `latency` group.
- **The stream polls the log with a cursor** instead of holding a queue per client. A slow
  client costs no memory and learns how many events it missed. `EventBus.subscribe` exists
  for in-process listeners; nothing in the gateway uses it yet.
- **Tokens are counted as they are produced**, by passing a token callback for every
  request, streamed or not. The worker calls it at times it had already fixed, so simulated
  timings do not move: the full suite and the performance gate pass unchanged.
- **`Monitor` reads workers through a protocol, `SchedulerView`.** A worker either has
  `waiting`, `batch_size`, `kv_used_tokens`, `prefix_cache` and `profile`, or its scheduler
  groups are unavailable. `PassthroughWorker.waiting` is always 0 by construction, which is
  exactly the zero this step must not report.

### Changes outside the new modules

The step was not to touch scheduling logic. Three additive changes were needed to observe
it; none alters a decision or a time:

- `ContinuousBatchingWorker` gained two read-only properties, `batch_size` and
  `kv_used_tokens`.
- `SloMonitor` gained an optional `on_transition` callback, called after a state change.
- `app.py` reports to the monitor and passes the token callback for every request.

### One way reading affects the SLO monitor

`SloMonitor` evaluates lazily: a model's state can only change when something asks about
it. A snapshot asks, exactly as `GET /v1/overload` already did. So with a dashboard
connected, a degraded model that has gone idle is marked recovered within a second of its
window emptying, instead of when its next request arrives, and the hold time then counts
from that earlier moment. `test_degradation_downgrade_and_recovery_show_in_flags_counters_and_events`
shows the recovery being stamped at the time of the snapshot.

### Cost

- Per output token: one counter update, and once per decode step a read of the worker's
  queue and batch, which scans both.
- Per snapshot: `MetricsStore.summary` for every model, which sorts the retained samples
  (up to 10,000 requests and 50,000 gaps per model at the defaults), and one ledger lookup
  per configured tenant. Each connected stream does that once a second. It was not measured.

## Not in this step

A dashboard, authentication (anyone can read every tenant's spend, as with
`/v1/tenants/{id}/usage`), persistence, events for backend errors or client disconnects,
per-tenant latency, histograms, a queue view for `BaselineWorker`, and anything from inside
a real model server.
