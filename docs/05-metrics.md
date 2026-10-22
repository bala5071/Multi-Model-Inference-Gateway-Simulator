# 05 — Metrics store

This step adds `MetricsStore`, which keeps per-model request metrics in bounded memory. It
replaces `MetricsRecorder`, which kept every result for the life of the process.

**Built out of order.** Step 5 was skipped when the API was written and built after step 13.
Notes 03, 06 and 11 mention `MetricsRecorder`; that is what existed when they were written.

Code: `src/gateway/metrics.py`, the `/metrics/detail` endpoint and the `metrics` argument of
`create_app` in `src/gateway/app.py`. Example: `benchmarks/metrics_example.py`.
Tests: `tests/test_metrics.py`.

## What is recorded

One call, `store.record(model, result)`, when a request finishes successfully. Per model:

| Metric | Definition (clock times in ms) | Kept as |
|---|---|---|
| Queue wait | `start_ms - enqueue_ms` | distribution |
| TTFT | `first_token_ms - enqueue_ms` | distribution |
| Total latency | `finish_ms - enqueue_ms` | distribution |
| ITL | gaps between consecutive tokens, pooled over requests | distribution |
| Tokens | prompt, cached and output tokens | lifetime total and window total |
| Cost | `cost_usd` | lifetime total and window total |
| Requests | count | lifetime total and window total |

**TTFT and total latency both start at enqueue, so both include the queue wait.** They are
what the client waited, not what the model spent. To see service time alone, subtract the
means (means add up; percentiles do not).

A distribution is `samples`, `mean`, `p50`, `p90`, `p99` and `max`.

## Bounded memory

Each model has two rings: one entry per request, one entry per inter-token gap. A sample is
kept only while both of these hold:

- it is younger than `window_ms` on the injected clock (default 300,000 ms, five minutes);
- it is among the newest `max_samples` requests (default 10,000) or `max_itl_samples` gaps
  (default 50,000).

So a model never holds more than 10,000 request samples and 50,000 gaps, whatever the
traffic. The lifetime totals are five numbers per model and never reset.

- A sample is stamped when it is recorded, which is when its request finished. A sample
  recorded exactly `window_ms` ago is out.
- Expiry happens on `record` and on `summary`. An idle model keeps its last samples in
  memory until the next call, but no summary ever reports them.
- **When the ring fills before the window does**, the distributions cover less than the
  window. The summary says so: `window.truncated` (requests) and `window.itl_truncated`
  (gaps) are true while a sample dropped by the ring would still be inside the window, and
  `window.span_ms` is the age of the oldest request still held.
- The number of models is not bounded by the store. The app only records models in the
  registry.

Why rings of raw samples and not a histogram: percentiles stay exact and need no choice of
bucket edges, and the limits above are small enough to sort on each read. A histogram would
be the next step if a summary had to cover millions of samples.

## Percentile method

**Nearest-rank**, the same `percentile` function the benchmarks and the performance gate
use. For `n` samples sorted ascending, the q-th percentile is the sample at rank
`ceil(q / 100 * n)`, counting from 1. It is always a value that was observed; nothing is
interpolated.

## Empty stores and small samples

| Samples | What a summary gives |
|---|---|
| 0 (never recorded, or all expired) | Distributions are `None` (`null` in JSON), totals are 0. Never an error. |
| 0 gaps (no request had two tokens) | `itl_ms` is `None`; the other distributions are present. |
| 1 | `mean`, every percentile and `max` are that one sample. |
| 2 | `p50` is the lower sample; `p90`, `p99` and `max` are the upper one. |
| under 100 | `p99` is the largest sample, the same as `max`. |
| exactly 100 | `p99` is the second largest: one outlier in a hundred shows in `max`, not in `p99`. |

`samples` is reported with every distribution so that a reader can tell a p99 over 35
requests from one over 10,000. The store does not hide or flag small samples itself.

## Concurrency

`record` and `summary` are plain functions with no `await` in them. On an asyncio event
loop each runs to completion before another task can run, so updates from concurrent
requests cannot interleave and a summary never sees half an update. A summary is a frozen
snapshot. The store is **not** thread-safe; every endpoint in `app.py` is `async` and runs
on the loop, so nothing calls it from another thread.

## The API

`GET /metrics/summary` keeps its fields and their meaning, with one change: its `p50` and
`p99` now cover the retained window instead of every request since the process started.
The totals beside them are still lifetime totals. For a run shorter than the window and
smaller than the ring the response is identical to before.

`GET /metrics/detail` is new and gives everything:

```json
{
  "percentile_method": "nearest-rank",
  "models": {
    "slow": {
      "lifetime": {"requests": 3, "prompt_tokens": 30, "cached_tokens": 0,
                   "completion_tokens": 9, "cost_usd": 0.024},
      "window": {"window_ms": 1000.0, "span_ms": 34.0, "requests": 3, "prompt_tokens": 30,
                 "cached_tokens": 0, "completion_tokens": 9, "cost_usd": 0.024,
                 "truncated": false, "itl_truncated": false},
      "queue_ms": {"samples": 3, "mean": 17.0, "p50": 17.0, "p90": 34.0, "p99": 34.0, "max": 34.0},
      "ttft_ms": {"samples": 3, "mean": 26.0, "p50": 26.0, "p90": 43.0, "p99": 43.0, "max": 43.0},
      "total_ms": {"samples": 3, "mean": 34.0, "p50": 34.0, "p90": 51.0, "p99": 51.0, "max": 51.0},
      "itl_ms": {"samples": 6, "mean": 4.0, "p50": 4.0, "p90": 4.0, "p99": 4.0, "max": 4.0}
    }
  }
}
```

(The body asserted by `test_metrics_detail_reports_the_window_and_summary_keeps_its_shape`,
on the exact-arithmetic test profile; reformatted for reading.)

`create_app(registry, clock, metrics=MetricsStore(clock, window_ms=...))` sets a different
window or ring size. The default server uses the defaults.

## Worked example

**Simulated on the calibrated profiles (note 01), with batches of at most 4. Not measurements
of a model server.**

```sh
uv run python -m benchmarks.metrics_example
```

Run on the tree of the commit that added this text (parent commit `38bbb94`). It runs the load
generator's default workload (seed 7, 1000 requests at 0.8 req/s, weights 70/20/10; no model
saturated, see note 07) in-process on simulated time and reads `/metrics/detail` when the last
request finishes, at t = 1,237.6 s. The run is longer than the five-minute window, so the
percentiles below cover only the requests that finished in its last 300 s; the rings were not
filled. Times in ms.

| Model | Metric | Samples | Mean | p50 | p90 | p99 | Max |
|----------|----------|-------:|---------:|---------:|---------:|---------:|---------:|
| economy  | queue    |    165 |     98.4 |     90.1 |     96.5 |    539.2 |    689.5 |
| economy  | TTFT     |    165 |    132.9 |    116.1 |    157.2 |    569.6 |    814.0 |
| economy  | total    |    165 |  1,345.0 |  1,273.0 |  2,305.7 |  3,098.7 |  3,099.0 |
| economy  | ITL      | 22,098 |      9.0 |      9.4 |     12.1 |     14.7 |    137.3 |
| standard | queue    |     60 |     92.0 |     88.4 |    102.2 |    109.2 |    109.2 |
| standard | TTFT     |     60 |    157.9 |    134.5 |    206.9 |    407.3 |    407.3 |
| standard | total    |     60 |  2,451.0 |  2,402.6 |  4,022.5 |  5,316.9 |  5,316.9 |
| standard | ITL      |  7,764 |     17.7 |     15.3 |     23.6 |     27.7 |    305.9 |
| premium  | queue    |     23 |    172.9 |    161.9 |    199.1 |    225.0 |    225.0 |
| premium  | TTFT     |     23 |    368.9 |    350.2 |    441.0 |    621.8 |    621.8 |
| premium  | total    |     23 | 10,614.4 | 11,231.7 | 16,066.3 | 22,677.0 | 22,677.0 |
| premium  | ITL      |  3,641 |     64.7 |     69.9 |     94.7 |    119.4 |    268.0 |

| Model | Requests (lifetime) | In window | Window span s | Prompt tokens | Cached tokens | Output tokens |
|----------|----:|----:|------:|--------:|-------:|-------:|
| economy  | 693 | 165 | 294.2 | 171,067 | 71,200 | 94,582 |
| standard | 204 |  60 | 299.8 |  52,180 | 23,400 | 28,524 |
| premium  | 103 |  23 | 219.7 |  24,694 |  9,600 | 14,408 |

The store also keeps a lifetime cost per model; it is not shown, because it is at placeholder
prices (note 01), operator-supplied, not data.

Reading it:

- **The window is not the run.** The lifetime counters cover all 1000 requests; the
  percentiles cover the 248 that finished in the last five minutes. They differ from the load
  generator's table in `docs/07-load.md`, which is computed over the whole run from the HTTP
  responses: premium's p99 total is 22,677.0 here and 25,175.3 there.
- **Ranks.** Premium has 23 samples in the window, so p99 is rank `ceil(0.99 * 23) = 23`, the
  largest: its p99 and max are the same number, 22,677.0. Economy has 165, so p99 is rank
  `ceil(0.99 * 165) = 164`, the second largest, and differs from the max (3,098.7 against
  3,099.0). The premium p99 is one request; treat it as such.
- **Queue wait is inside the total, and here it is mostly the per-request overhead.**
  Premium's mean total is 10,614.4 ms, of which 172.9 ms is waiting; its fixed overhead alone
  is 161.9 ms (unstable, note 01). Economy waits 98.4 ms of 1,345.0, against an overhead of
  90.1 ms.
- **The mean hides a stall that the tail shows.** Premium's inter-token gap has a mean of
  64.7 ms; its p99 is 119.4 ms, a step with the batch full, and its worst gap is 268.0 ms: a
  running request stalls while a newly admitted prompt is prefilled (chunked prefill, note
  11, is off in this wiring).

## Not in this step

Histograms or sketches, per-tenant metrics, a Prometheus endpoint, counting failed or
rejected requests (the ledger and `/v1/overload` cover some of that), persistence.
