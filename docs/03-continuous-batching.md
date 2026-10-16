# 03 — Continuous batching

Step 3 adds `ContinuousBatchingWorker`, an iteration-level batching scheduler for one model.
It takes the same `(profile, backend, clock)` and exposes the same `submit` as
`BaselineWorker`, so either can sit behind the registry.

Code: `continuous.py`, `metrics.py`, `cost_model.decode_step_ms`.
Tests: `test_continuous.py`, `test_metrics.py`.
Comparison: `benchmarks/compare_batching.py`.

## The loop

Each pass of the scheduler loop is one decode iteration:

1. **Drop** running requests whose caller has cancelled, freeing their slots.
2. **Admit** from the head of the FIFO queue while both limits hold:
   - running requests < `max_batch_size`;
   - reserved KV tokens + this request's footprint <= `kv_capacity_tokens`.

   Each admitted request is prefilled immediately, costing `tokens_in * prefill_ms_per_token`
   on the clock. Prefills run one after another and the running batch waits for them.
3. **Decode one step** for everyone. It takes
   `decode_base_ms + decode_ms_per_extra_seq * (batch - 1)` and gives every running request
   one token.
4. **Retire** every request that now has all its tokens: record `finish_ms`, resolve its
   result, release its KV tokens. Its slot is available to the very next admission.

The loop stops when nothing is running and nothing is queued, and restarts on the next
`submit`.

## Why continuous batching beats static batching

Static batching collects a group of requests, runs that group to completion, then takes the
next group. Two things go wrong:

- **The batch empties out as it runs.** Requests want different numbers of tokens. When a
  short one finishes, its place in the batch stays empty until the longest one finishes, so
  the model spends most of the run decoding a half-empty batch. Each decode step costs
  nearly the same whether the batch is full or not, so those empty places are wasted
  capacity.
- **Arrivals wait at the door.** A request that arrives one step after a batch starts waits
  for that whole batch, however short the request is. And at the start, the scheduler has
  to choose between launching a part-filled batch or holding requests back to fill it.

Continuous batching makes the decision every decode step instead of every batch. A finished
request leaves at once and a waiting one takes its place on the next step. The batch stays
as full as the queue allows, so more tokens come out of each step, and a request waits at
most about one step (plus any prefills ahead of it) to get in rather than a whole batch. A
short request that arrives behind a long one finishes first, because it is not tied to the
long one's lifetime.

The first test in `test_continuous.py` shows this on a hand-checked timeline, on a hand-made
test profile (`EXACT` in `tests/helpers.py`: a fixture, not data): a 2-token
request arriving at t=10 finishes at t=23.5, while the 50-token request that started at t=0
finishes at t=207.5. The baseline finishes the short one at t=215. A static batcher is not
implemented in this repo, so the comparison with static batching is by argument, not by a
run.

## Results against the baseline

**Simulated on the calibrated profiles (note 01), with batches of at most 4, the most requests
measured together. Not measurements of a model server.** Produced by

```sh
uv run python -m benchmarks.compare_batching
```

(defaults: seed 7, 200 requests, load 0.5) on the tree of the commit that added this text
(parent commit `38bbb94`). Prompts are 20 to 400 words; output lengths come from the seeded
generator. Latency is finish minus enqueue. Percentiles are nearest-rank.

Burst: all 200 requests arrive at t=0. This shows maximum throughput. Its queue lasts the
whole run, so no latency is given for it: a p99 there would measure the length of the run.

| Tier     | Worker     | req/s | tok/s |
|----------|------------|------:|------:|
| economy  | baseline   |  1.01 | 142.7 |
| economy  | continuous |  1.77 | 250.8 |
| standard | baseline   |  0.45 |  63.0 |
| standard | continuous |  0.94 | 133.2 |
| premium  | baseline   |  0.15 |  21.5 |
| premium  | continuous |  0.23 |  32.1 |

Poisson arrivals at half the rate the baseline can sustain, so neither worker is saturated
and both keep up: throughput is the arrival rate, and the difference is in latency.

| Tier     | Worker     | req/s | tok/s | p50 ms    | p99 ms    | p99 TTFT ms |
|----------|------------|------:|------:|----------:|----------:|------------:|
| economy  | baseline   |  0.50 |  70.9 |   1,633.9 |   7,527.8 |     6,941.6 |
| economy  | continuous |  0.50 |  70.9 |   1,311.1 |   4,059.0 |     1,763.1 |
| standard | baseline   |  0.23 |  32.8 |   3,667.2 |  17,328.7 |    15,892.4 |
| standard | continuous |  0.23 |  32.8 |   2,695.2 |   7,553.8 |     2,313.0 |
| premium  | baseline   |  0.08 |  11.4 |  10,813.4 |  50,958.9 |    46,650.9 |
| premium  | continuous |  0.08 |  11.4 |   8,912.1 |  37,540.0 |    18,472.3 |

- **With batches held to 4, continuous batching gets a little under twice the baseline's
  throughput** in the burst, less on premium, where each extra sequence was measured to cost
  over half a base step.
- **That gain rests on `decode_ms_per_extra_seq`, unstable on every tier.** Section 1 of
  `docs/REPORT.md` shows the gain at the median and at the lowest and highest values the six
  calibration runs fitted; on premium it ranges from barely any gain to about two and a half
  times.
- **How these models batch beyond 4 requests was never measured**, so nothing here goes
  past 4. The earlier, larger gains in this note came from invented profiles with batch
  limits of 32, 16 and 8 and were removed (note 13).

## Assumptions and simplifications

- **Prefill blocks the batch.** A newly admitted request's whole prompt is prefilled in one
  go and nothing decodes meanwhile. One long prompt therefore stalls every running request.
  This is deliberate and explicit; chunked prefill in a later step will interleave it with
  decode steps. `test_prefill_of_a_new_request_delays_the_running_batch` pins the current
  behaviour.
- **Prefills are sequential**, and are not batched with each other.
- **KV reservation is the final footprint**, `tokens_in + tokens_out`, taken at admission.
  A sequence can then never outgrow the cache mid-decode. This uses the output length
  before it is generated, which a real server does not know; it would reserve by
  `max_output_tokens` or evict. No prefix sharing yet, so every request reserves its whole
  prompt.
- **Admission is strictly first come, first served.** If the head of the queue does not fit
  in the KV cache, nothing behind it is admitted, even if it would fit. This wastes some
  capacity but cannot starve a large request.
- **A request that cannot fit in an empty cache is rejected** with `RequestTooLargeError`
  rather than blocking the queue forever.
- **`start_ms` is the moment a request's own prefill begins.** Time spent waiting for other
  requests' prefills after that shows up between `start_ms` and `first_token_ms`.
- **Arrivals are noticed only between iterations**, so a request arriving mid-step waits
  for that step to end, even at an otherwise idle moment.
- **Cancellation:** a queued request is skipped; a running one is dropped at the next
  iteration boundary and its slot and KV tokens are freed.
- **Price is unchanged by batching.** Cost is still tokens times price.
- **`on_step`** is an optional callback receiving a `StepRecord` (start time, step time,
  batch size, reserved KV tokens) before each decode step. Tests use it to check the limits
  and to account for every token.

## Invariants the tests check

- Batch size is always within `1..max_batch_size` and reserved KV tokens within capacity,
  and the batch limit is actually reached under load.
- Every step's duration equals the cost model's step time for that batch size.
- Every submitted request gets exactly one result with the backend's token counts.
- The sum of batch sizes over all steps equals the sum of output tokens: no token is
  decoded twice or skipped.
- With `max_batch_size=1`, results are identical to the baseline's.
- For 20 concurrent requests, throughput is strictly higher than the baseline on every
  tier, with identical token counts and cost per request.
