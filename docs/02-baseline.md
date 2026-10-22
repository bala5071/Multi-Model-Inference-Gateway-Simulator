# 02 — Baseline worker, registry, backend, clock

Step 2 adds the pieces needed to serve a request end to end in simulated time, in the
simplest way possible. The result is the baseline every later step is compared against.

Code: `clock.py`, `schema.py`, `backends/`, `registry.py`, `baseline.py`.
Tests: `test_clock.py`, `test_backend.py`, `test_registry.py`, `test_baseline.py`.

## What the baseline does

`BaselineWorker` serves one model. It keeps a FIFO queue and serves one request at a time,
start to finish:

1. take the oldest request; record `start_ms`;
2. ask the backend for the token counts;
3. sleep `prefill_ms` on the clock;
4. sleep one `decode_step_ms` per output token, at batch size 1; record `first_token_ms`
   after the first step and `finish_ms` after the last.

No batching, no prefix reuse, no priorities, no admission control. Each model has its own
worker, so models do not block each other, but requests for the same model always do.

Each `RequestResult` records `enqueue_ms`, `start_ms`, `first_token_ms`, `finish_ms`,
`tokens_in`, `tokens_out` and `cost_usd`, with `queue_wait_ms`, `time_to_first_token_ms` and
`latency_ms` derived from them.

## Throughput limits

For a request with `n_in` prompt tokens and `n_out` output tokens on profile `p`:

```
service_ms      = n_in * p.prefill_ms_per_token + n_out * p.decode_base_ms
max requests/s  = 1000 / service_ms
max tokens/s    = 1000 / p.decode_base_ms        (upper bound, reached only with no prefill)
```

Consequences:

- **Throughput is capped at one sequence's decode rate.** Capacity for more sequences in
  the same decode step goes unused.
- **Head-of-line blocking.** The k-th of N requests that arrive together waits for the
  k-1 service times ahead of it. A short request behind a long one waits for all of it.
- **Time to first token includes the whole queue.** It is queue wait + prefill + one step.
- **The queue is unbounded.** If requests arrive faster than `1000 / service_ms` per
  second, the queue and the wait grow without limit. Nothing is rejected or shed.
- **Every prompt token is prefilled every time.** Two requests sharing a long prefix pay
  for it twice, in time and in money.

### Numbers

These figures are **simulated on the calibrated profiles** (note 01: the median of six
calibration runs on one laptop with Ollama, the premium model partly on the CPU). They are
this scheduler with measured step costs, not measurements of a model server. Produced by

```sh
uv run python -m benchmarks.baseline_limits
```

on the tree of the commit that added this text (parent commit `38bbb94`).
Workload: every request has 500 tokens in and 136 tokens out; 10 requests arrive together.
The waits include each request's per-request overhead (90, 88 and 162 ms), which is
**unstable** on every tier.

| Tier     | Service ms | Max req/s | Decode tok/s | Mean wait ms (burst of 10) | Last wait ms |
|----------|-----------:|----------:|-------------:|---------------------------:|-------------:|
| economy  |    1,010.7 |     0.989 |        149.0 |                    4,638.0 |      9,186.0 |
| standard |    2,268.2 |     0.441 |         65.4 |                   10,295.3 |     20,502.2 |
| premium  |    6,570.0 |     0.152 |         22.2 |                   29,726.7 |     59,291.5 |

For scale, the same cost model says a decode step with the batch full, at 4 requests, the
most that were measured together, would yield:

| Tier     | Baseline tok/s | Full-batch tok/s | Ratio |
|----------|---------------:|-----------------:|------:|
| economy  |          149.0 |            271.4 | 1.82x |
| standard |           65.4 |            144.4 | 2.21x |
| premium  |           22.2 |             33.5 | 1.51x |

The full-batch column is a ceiling implied by the profile formulas, not a run. It rests on
`decode_ms_per_extra_seq`, which is **unstable** on every tier: across the six calibration
runs it moved by 60% to 142% of its median (note 13).

## What later steps must beat

On the same seed and the same arrival pattern, compared against `BaselineWorker`:

- continuous batching must give higher throughput and lower queue wait (p50 and p99);
- prefix reuse must lower prefill time and cost for requests that share a prefix;
- neither may change `tokens_in`, `tokens_out` for a request.

Re-run `benchmarks/baseline_limits.py` if the profiles change; the tables above go stale.
`tests/test_entry_points.py` checks two of its rows against this note.

## Design decisions

- **Backend says what, worker says when.** `Backend.generate(request)` returns a
  `Generation` (token counts) and takes no simulated time. The worker turns that into time
  with the cost model and the clock. A batcher can then reuse the same backend and only
  change the scheduling. `SimulatedBackend` therefore needs a seed but no clock.
- **Tokenizer stand-in.** `tokens_in` is the number of whitespace-separated words in the
  prompt. `tokens_out` comes from `OutputLengthGenerator`, capped by `max_output_tokens`.
  No text is generated.
- **One backend for all tiers.** Output length depends on seed and prompt, not on the
  model, so the same workload costs the same tokens on every tier and tiers can be compared
  directly. Real models would differ.
- **First token arrives after the first decode step**, not at the end of prefill.
- **Registry names are tiers; model names are aliases.** `economy`, `standard`, `premium`
  are the registered names and `llama-3.2-1b` etc. resolve to them. Lookup is exact: no case
  folding or trimming. `UnknownModelError` is a `LookupError` with `.model` and
  `.valid_names` (names, then aliases).
- **Failures and cancellation.** A backend error is raised to that request's caller and the
  queue carries on. A request cancelled while queued is skipped. A request cancelled while
  being served still occupies the worker until it would have finished.
- **Times are milliseconds everywhere**: `Clock.now_ms()`, `Clock.sleep_ms()`.

## SimClock

`SimClock` needs no driver. `sleep_ms` puts the sleeper in a heap; a background task waits
until no task on the event loop is runnable, jumps time to the earliest wake-up and resumes
that sleeper. Ties resume in the order they went to sleep. Concurrent sleeps overlap rather
than add up, so several workers can share one clock.

To know that nothing is runnable it reads the event loop's private `_ready` queue; asyncio
has no public equivalent. The alternative, yielding a fixed number of times and hoping, can
advance time too early and silently give wrong latencies. If the loop has no `_ready`
(a non-standard loop such as uvloop), `sleep_ms` raises instead of guessing. A test relays
through 200 tasks to check that time waits for them.

The timing tests compare with `==`, not `approx`: the worker and the test add the same
durations in the same order.
