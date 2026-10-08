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

**Simulated, from the illustrative profiles of step 1. Not measurements of real models.**

```sh
uv run python -m benchmarks.metrics_example
```

Run on the tree this note was first committed with (parent commit `7ff9c0c`). It runs the
load generator's default workload (seed 7, 300 requests at 8 req/s, weights 70/20/10)
in-process on simulated time and reads `/metrics/detail` when the last request finishes, at
t = 42.1 s. Nothing has expired or been dropped: the run is shorter than the window and
smaller than the rings. Times in ms.

| Model | Metric | Samples | Mean | p50 | p90 | p99 | Max |
|----------|----------|-------:|--------:|--------:|---------:|---------:|---------:|
| economy  | queue    |    197 |     3.7 |     3.3 |      6.4 |     16.9 |     18.3 |
| economy  | TTFT     |    197 |    19.1 |    15.0 |     34.6 |     71.8 |     74.9 |
| economy  | total    |    197 | 1,004.7 | 1,014.2 |  1,736.5 |  1,917.8 |  1,998.2 |
| economy  | ITL      | 26,311 |     7.4 |     7.0 |      7.8 |     16.6 |     71.8 |
| standard | queue    |     68 |     6.4 |     6.2 |     12.0 |     16.9 |     16.9 |
| standard | TTFT     |     68 |    45.9 |    38.3 |     65.0 |    135.4 |    135.4 |
| standard | total    |     68 | 2,156.9 | 2,254.8 |  3,549.4 |  3,993.8 |  3,993.8 |
| standard | ITL      |  9,668 |    14.8 |    13.8 |     15.6 |     40.0 |    132.6 |
| premium  | queue    |     35 | 2,373.4 | 2,893.6 |  4,936.0 |  6,266.1 |  6,266.1 |
| premium  | TTFT     |     35 | 2,517.6 | 3,029.3 |  5,021.9 |  6,448.6 |  6,448.6 |
| premium  | total    |     35 | 9,522.5 | 9,585.4 | 13,458.2 | 17,402.3 | 17,402.3 |
| premium  | ITL      |  5,132 |    47.8 |    47.5 |     47.5 |    135.1 |    501.7 |

| Model | Requests | Prompt tokens | Cached tokens | Output tokens | Cost USD |
|----------|----:|-------:|-------:|-------:|---------:|
| economy  | 197 | 50,442 | 19,800 | 26,508 | 0.008366 |
| standard |  68 | 18,536 |  7,200 |  9,736 | 0.009242 |
| premium  |  35 |  7,656 |  2,600 |  5,167 | 0.015390 |

Reading it:

- **Ranks.** Premium has 35 samples, so p50 is rank `ceil(0.50 * 35) = 18`, p90 is rank
  `ceil(0.90 * 35) = 32` and p99 is rank `ceil(0.99 * 35) = 35`, the largest: its p99 and
  max are the same number, 17,402.3. Economy has 197, so p99 is rank
  `ceil(0.99 * 197) = 196`, the second largest, and differs from the max (1,917.8 against
  1,998.2). The premium p99 is one request; treat it as such.
- **Queue wait is inside the total.** Premium's mean total is 9,522.5 ms, of which
  2,373.4 ms is waiting; the mean service time is the difference, 7,149.1 ms. Economy waits
  3.7 ms of 1,004.7.
- **The mean hides a stall that the tail shows.** Premium's inter-token gap has a mean of
  47.8 ms and a median of 47.5 ms, which looks perfectly steady. Its p99 is 135.1 ms and
  its worst gap is 501.7 ms: a running request stalls while a newly admitted prompt is
  prefilled (chunked prefill, note 11, is off in this wiring).
- **The gap count checks out.** A request with `k` output tokens has `k - 1` gaps: economy
  has 26,508 output tokens over 197 requests and 26,508 - 197 = 26,311 gaps.
- The p50 and p99 columns for TTFT and total equal the load generator's own table in
  `docs/07-load.md`, which computes them from the HTTP responses instead of from the store.

## Not in this step

Histograms or sketches, per-tenant metrics, a Prometheus endpoint, counting failed or
rejected requests (the ledger and `/v1/overload` cover some of that), persistence.
