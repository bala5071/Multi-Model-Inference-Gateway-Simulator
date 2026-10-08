# 10 — Overload protection

This step adds three defences for when a model gets more work than it can serve: a queue
limit, request timeouts, and an opt-in downgrade to a cheaper model while the requested one
is missing its SLO.

Code: `src/gateway/overload.py`, changes in `src/gateway/continuous.py` and
`src/gateway/app.py`. Benchmark: `benchmarks/overload.py`.
Tests: `tests/test_overload.py`, `tests/test_overload_api.py`.

All limits, SLOs and results here are illustrative or simulated.

## Summary

| Defence        | Where it lives        | What the client sees                               |
|----------------|-----------------------|----------------------------------------------------|
| Queue limit    | each worker           | 429 `queue_full` with `Retry-After`, at once       |
| Timeout        | the app               | 504 `request_timeout` after the limit              |
| SLO downgrade  | the app, per request  | 200 from a cheaper model, with `X-Downgraded-From` |

They act at different moments. The queue limit refuses work on arrival, the downgrade moves
work on arrival, and the timeout gives up on work already accepted.

The default server has all three on: `create_default_app` uses `DEFAULT_MAX_QUEUE_LEN`
(256 / 128 / 64 for economy / standard / premium) and `DEFAULT_OVERLOAD` (120 s timeout;
p99 SLOs of 5 s / 10 s / 30 s) from `app.py`. `create_app(registry, clock)` without an
`overload` argument has none of them, which is what the load generator and the performance
gate use, so their numbers are unchanged.

`GET /v1/overload` shows the current state:

```json
{
  "request_timeout_ms": 120000.0,
  "models": {
    "premium": {"degraded": false, "slo_p99_ms": 30000.0, "recent_p99_ms": null,
                "samples": 0, "waiting": 0}
  }
}
```

## Admission control

`ContinuousBatchingWorker(..., max_queue_len=N)` limits how many requests may wait for one
model. A request that would exceed the limit is refused before it is queued, with
`QueueFullError`, which the API turns into:

```
HTTP/1.1 429 Too Many Requests
Retry-After: 1

{"error": {"code": "queue_full",
           "message": "Model 'premium' is overloaded: 2 requests are already queued or in progress (limit 2). Retry in 1 seconds.",
           "model": "premium", "waiting": 2, "limit": 2, "retry_after_s": 1}}
```

- **"Waiting" means queued and not seatable.** A request that has just arrived sits in the
  queue until the scheduler's next iteration even if the batch has free slots. Free slots
  are subtracted, so a burst into an idle model fills the batch before anything counts as
  waiting: a model can hold `max_batch_size + max_queue_len` requests in total.
- **Cancelled requests are not counted.**
- **The limit is per model**, so one model's full queue does not refuse another's requests.
- **`Retry-After` is a fixed hint** (`retry_after_ms`, default 1 s), not an estimate of when
  space will free up.
- **Rejected requests are not charged, not counted in metrics, and take no simulated time.**
- A tenant's exhausted budget is also a 429, with `code: budget_exhausted`; the code tells
  them apart.
- `BaselineWorker` has no queue limit; it stays the unprotected reference.

Without a limit the queue grows without bound under overload and every request waits
longer than the one before. A bounded queue turns that into a fast, explicit refusal for
the excess and a bounded wait for everything accepted.

## Timeouts and cancellation

With `OverloadPolicy(request_timeout_ms=T)`, a request that has not finished `T` ms after it
arrived is cancelled and answered with:

```
HTTP/1.1 504 Gateway Timeout

{"error": {"code": "request_timeout",
           "message": "Model 'premium' did not finish the request within 200 ms. It was cancelled and has not been charged.",
           "model": "premium", "timeout_ms": 200.0}}
```

`run_with_timeout(clock, work, timeout_ms)` races the work against a sleep on the injected
clock, so it behaves identically on `SimClock` and `WallClock`. If both finish at the same
instant the work wins.

**Cancellation reaches the batch.** Cancelling the request cancels the future the worker is
holding for it. The worker checks for cancelled requests at the start of every iteration
and again just before each decode step, and for each one it finds it:

- removes it from the running batch, freeing its slot;
- gives back its reserved KV tokens;
- releases its lease on the prefix cache, so the prefix can be evicted again.

A cancelled request therefore stops consuming capacity within one decode step. Requests
cancelled while still queued are skipped when they reach the head of the queue. Tests:

- `test_timed_out_request_leaves_the_batch_and_frees_its_kv_tokens`: a 30-token request
  blocks a 16-token one in a 40-token KV budget; when the first times out at t=10 the
  second is admitted at the next iteration boundary, t=13, not at t=85.
- `test_request_cancelled_during_another_requests_prefill_gets_no_further_step`.
- `test_request_over_the_timeout_gets_504_and_frees_its_capacity`: through HTTP, the next
  request waits 16 ms.

Things to know:

- **Timed-out requests are not charged**, although the model did work on them. Charging for
  partial work is a pricing decision this step does not take.
- **A timed-out request counts as a latency sample** equal to the timeout, for the SLO
  monitor below. Otherwise the monitor would only see the requests that survived.
- **The work done is wasted**, and nothing is returned. The simulator has no streaming, so
  there is no partial output to deliver.
- **A client disconnecting is not handled here.** Only the server-side timeout cancels.
- **The prompt of a cancelled request is not put in the prefix cache.**

## SLO-aware downgrade

### The rule

A request is moved to a cheaper model only if **all** of these hold:

1. an `SloPolicy` is configured;
2. the request has `"allow_downgrade": true` (default `false`);
3. the model it would use is currently **degraded**;
4. the tenant's plan includes a cheaper model.

The replacement is the next cheaper model in the plan's list. If that one is degraded too,
the next one down, and so on; the cheapest model is used even if it is degraded, since there
is nowhere further to go. The request never leaves the tenant's plan, and a request that did
not opt in is never moved. Without plans, "the plan's list" is all registered models.

The response carries `X-Downgraded-From: <model originally chosen>`; `X-Model-Used` and
`model` in the body name the model that served it. It is billed at the cheaper model's
price. This applies whether the request named a model or a quality.

```sh
curl -s -i -X POST http://127.0.0.1:8000/v1/generate -H "Content-Type: application/json" \
  -d '{"tenant_id": "acme", "quality": "best", "allow_downgrade": true, "prompt": "Summarise this."}'
```

### When a model is degraded

`SloMonitor` keeps, per model, the total latency of every request that finished in the last
`window_ms` (a sliding window on the injected clock), and compares the window's
nearest-rank p99 with the model's SLO.

| Setting         | Default | Meaning                                                    |
|-----------------|--------:|------------------------------------------------------------|
| `p99_ms`        | n/a     | SLO per model id. A model with no SLO is never degraded.   |
| `window_ms`     |  60,000 | How far back "recent" reaches.                             |
| `min_samples`   |      20 | Fewer requests than this in the window prove nothing.      |
| `recover_ratio` |     0.7 | Recovered means p99 below this fraction of the SLO.        |
| `min_hold_ms`   |  30,000 | Minimum time between two changes of state.                 |

### Hysteresis

A single threshold would flap: the moment the p99 dips under the SLO traffic returns, the
p99 rises, traffic leaves again. Three things prevent that:

- **Two thresholds.** A model becomes degraded when its p99 is **above the SLO**. It
  recovers only when its p99 is **below `recover_ratio` x SLO**. Between the two, the state
  stays as it is, in both directions.
- **A hold time.** After any change, no further change for `min_hold_ms`, whatever the
  numbers say.
- **A sample floor.** With fewer than `min_samples` in the window the state does not change,
  with one exception below.

**Recovery when the window is empty.** If everyone opts in, a degraded model receives no
requests, so it produces no new measurements and could never show a low p99. An empty window
therefore counts as recovered (after the hold time): a model that finished nothing for a
whole window is idle. Traffic then returns and the model is measured again.

`test_hysteresis_keeps_the_state_between_the_two_thresholds`,
`test_no_state_change_within_the_hold_time_of_the_previous_one` and
`test_too_few_samples_do_not_end_a_degradation` cover these one at a time.
`test_downgrade_engages_under_overload_and_recovers_afterwards` runs the whole cycle through
HTTP and asserts there is exactly one change each way.

### Limits of this approach

- **It reacts late.** A request has to finish before its latency is known, so the monitor
  learns about an overload only after slow requests complete. Requests that arrived before
  that are not helped; in the benchmark below the worst of them still take 50 s. Queue
  length or queue age would signal sooner; the p99 trigger is what this step specified.
- **Sustained overload still oscillates, slowly.** If demand stays above capacity, a model
  recovers once drained, is overloaded again, and degrades again. Hysteresis lengthens the
  cycle to at least the hold time plus the time to gather evidence; it cannot remove it.
  Sending a fraction of traffic back to probe would be smoother.
- **Downgrade moves load, it does not remove it.** The cheaper model takes the extra work.
  It is usually faster and batches more, but it can be pushed over its own SLO, which is
  why the downgrade can cascade.
- **Quality drops silently unless the client reads the header.**
- **State is per process and in memory.**

## Benchmark: p99 with and without degradation

**Simulated, from the illustrative profiles. Not measurements of real models.**

```sh
uv run python -m benchmarks.overload
```

Commit `c7e75ac`. Every request asks
for the premium model and opts in. 300 requests arrive at 3 req/s, about three times what
the premium model serves on this workload, followed by 100 at 0.25 req/s. Prompts are
20-200 words; seed 7. Queue limits and timeouts are off in both runs, so the only difference
is the downgrade. Policy: premium p99 SLO 20 s, window 30 s, recover below 70%, hold 15 s,
at least 10 samples.

| Run            | Requests | p50 ms   | p99 ms    | Max ms    | By premium | By standard | Cost USD |
|----------------|---------:|---------:|----------:|----------:|-----------:|------------:|---------:|
| No downgrade   |      400 | 92,071.4 | 190,170.3 | 194,071.2 |        400 |           0 | 0.160073 |
| With downgrade |      400 |  3,433.6 |  48,214.9 |  51,611.4 |        169 |         231 | 0.095520 |

State changes in the second run:

| Time    | Model   | Change    | Window p99        |
|---------|---------|-----------|-------------------|
| 26.1 s  | premium | degraded  | 20,776.7 ms       |
| 111.6 s | premium | recovered | window was empty  |

Reading it:

- p99 falls from about 190 s to about 48 s, and the median from 92 s to 3.4 s.
- The p99 with downgrade is still well over the 20 s SLO. The requests that arrived in the
  first 26 s were already queued on premium when the monitor first had evidence, and
  downgrade does not move queued requests.
- 231 requests were served by the standard model; the standard model was never degraded and
  nothing reached economy.
- The state changed once each way. Premium recovered at 111.6 s, after it had drained its
  backlog and gone a full window without finishing anything; the calm phase then ran on
  premium.
- Cost is lower with downgrade because the standard model is cheaper per token. That is a
  saving for the tenant and less revenue for the provider.

`test_benchmark_shows_downgrade_cutting_p99_without_flapping` runs the same two scenarios
and asserts the p99 is more than halved, every moved request went from premium to standard,
and there are exactly two state changes.

## Design decisions

- **Opt-in is per request**, as the `allow_downgrade` field in the body. A tenant-wide
  default in the plans file would be a small addition.
- **The queue limit is in the worker; timeout and downgrade are in the app.** The worker
  owns its queue. The timeout and the SLO are about the request as the client sees it, and
  the downgrade needs the tenant's plan, which the worker does not know.
- **The monitor measures total latency of finished requests**, the same quantity the SLO is
  written in, on the model that actually served the request.
- **Everything uses the injected clock**: the timeout, the sliding window and the hold time.
  No test in this step waits in real time.
- **Routing order with all features on:** plan check (403), budget check (429), downgrade
  decision, queue limit (429), then run under the timeout (504).
- **Cost of the checks.** The worker looks for cancelled requests among the running batch
  twice per iteration (at most `max_batch_size` items). The queue is scanned only when a
  request arrives at a worker that has a limit.
