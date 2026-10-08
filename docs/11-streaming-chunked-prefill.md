# 11 — Streaming and chunked prefill

This step adds two things that belong together: streaming responses with per-token
timestamps, which make gaps between tokens visible, and chunked prefill, which removes the
largest of those gaps.

Code: `src/gateway/app.py` (streaming), `src/gateway/continuous.py` (chunked prefill,
per-token times), `src/gateway/schema.py`, `src/gateway/metrics.py`.
Benchmark: `benchmarks/chunked_prefill.py`.
Tests: `tests/test_streaming.py`, `tests/test_chunked_prefill.py`.

All benchmark figures are simulated from the illustrative profiles.

## Per-token times

Both workers now record when each output token is produced. `RequestResult.token_times_ms`
has one entry per token, and `inter_token_ms` gives the gaps between consecutive tokens.
`Worker.submit(request, on_token=...)` also calls `on_token(index, time_ms)` at each token,
which is what streaming is built on.

**Inter-token latency (ITL)** is the gap between one token and the next for the same
request. It is what a reader of a streamed answer experiences as smoothness: a low average
with an occasional long gap reads as a stutter.

`/metrics/summary` now reports it per model, over all requests whether streamed or not:

```json
"itl_ms": {"p50": 4.5, "p99": 4.5}
```

It is `null` until some request has produced at least two tokens. (The class is
`MetricsRecorder`; there is no `MetricsStore` in this repo.)

## Streaming

Send `"stream": true` to `/v1/generate` to get server-sent events instead of one JSON body.

```sh
curl -s -N -X POST http://127.0.0.1:8000/v1/generate -H "Content-Type: application/json" \
  -d '{"tenant_id": "acme", "model": "economy", "prompt": "Explain what a radix tree is in one sentence.", "max_tokens": 4, "stream": true}'
```

```
HTTP/1.1 200 OK
x-model-used: economy
cache-control: no-cache
content-type: text/event-stream; charset=utf-8

event: token
data: {"index": 0, "token": "tok0", "t_ms": 19.12999999523163}

event: token
data: {"index": 1, "token": "tok1", "t_ms": 46.18300002813339}

event: token
data: {"index": 2, "token": "tok2", "t_ms": 46.4553000330925}

event: token
data: {"index": 3, "token": "tok3", "t_ms": 46.62699997425079}

event: done
data: {"id": "req-1", "text": "[simulated output, 4 tokens]", "model": "economy", "usage": {"prompt_tokens": 9, "cached_tokens": 0, "completion_tokens": 4, "total_tokens": 13}, "timing": {"queue_ms": 0.25029999017715454, "ttft_ms": 19.039200007915497, "total_ms": 46.53619998693466}, "cost": {"usd": 1.3600000000000001e-06}}
```

(Copied from one local run of the default server: `uvicorn`, Windows 11, Python 3.12, working
tree for this step, not yet committed; parent commit `ba18512`. See the note on wall-clock
timing at the end of this section before reading anything into those times.)

| Event   | When                                  | Data                                              |
|---------|---------------------------------------|---------------------------------------------------|
| `token` | After each decode step for the request | `index`, `token`, `t_ms`                          |
| `done`  | Once, at the end, on success          | Exactly the body a non-streaming request returns  |
| `error` | Once, at the end, on failure          | `code`, `message` and the usual extra fields      |

- **`t_ms`** is milliseconds since the gateway received the request, so the first token's
  `t_ms` is the time to first token.
- **`token`** is a placeholder, `tok0`, `tok1`, ...: the simulator produces no text.
- **Events are emitted as decode steps happen.** A token is put on the response the moment
  the worker's step produces it. Concurrent streams on one model share decode steps, so
  their tokens carry the same timestamps.
- **Headers.** `X-Model-Used` and `X-Downgraded-From` are sent at the start. `X-Cost-USD` is
  **not** sent for streams, because the cost is not known until the end; it is in the `done`
  event's `cost`.
- Routing, plans, budgets, downgrade, metrics and the ledger work exactly as for a
  non-streaming request. The request is charged when it finishes.

### Errors

- **Before the first token**, nothing has been sent, so the gateway answers with the same
  status code and JSON error body as a non-streaming request: 401, 403, 404, 422, 429
  (`budget_exhausted` or `queue_full`), or 504 if the timeout fires first. To make this
  possible, **the response headers are not sent until the first token is ready**; a client
  sees nothing during queueing and prefill.
- **After the first token**, the status is already 200. A timeout ends the stream with an
  `error` event carrying the `request_timeout` body. There is no `done` event, and the
  request is not charged or counted in the metrics.
- **If the client disconnects**, the gateway cancels the request. It leaves the batch at the
  next step boundary and gives back its KV tokens, as a timed-out request does
  (`test_client_disconnecting_mid_stream_cancels_the_request`).

### Wall-clock timing on Windows

The times in the example above are not what the cost model asks for (6 ms per step). On
Windows, asyncio's timers have a resolution of about 16 ms, so a 6 ms sleep sometimes
returns almost at once and sometimes takes far longer; three of the four tokens above are
0.2 ms apart. This is a property of the operating system's timer and the event loop, not of
the gateway, and the live server is a demo of a simulator in any case. Everything measured
in this repo is measured on `SimClock`, where timing is exact.

While checking this, `WallClock.now_ms` was switched from `time.monotonic` to
`time.perf_counter`. On Windows `monotonic` itself only ticks every ~16 ms, which gave
consecutive tokens identical timestamps.

## Chunked prefill

### The problem

Without chunking, a new request's whole prompt is prefilled the moment it is admitted and
nothing else happens meanwhile. For a short prompt that is a small delay. For a very long
prompt, every request already streaming on that model freezes for the whole prefill. On the
standard profile a 10,000-token prompt is a 1.5 s freeze.

### What changes

`ContinuousBatchingWorker(..., prefill_chunk_tokens=N)`. With it:

1. Admission takes no time. The request gets its batch slot and KV reservation and is marked
   as having `tokens_in - cached` prompt tokens still to prefill.
2. Each iteration, **every request that is still prefilling advances by up to N tokens**.
   The chunks are processed one after another; the iteration is delayed by their total.
3. Then one decode step runs for the requests that have finished prefilling. A request
   whose last chunk was just processed joins that step and gets its first token from it.
4. If every running request is still prefilling, there is no decode step; the next
   iteration starts straight away.

So a long prompt delays each decode step by at most one chunk, not by its whole length.

Unchanged: the FIFO admission rule, batch and KV limits (a prefilling request holds a slot
and its reservation), the prefix cache (cached tokens are not prefilled), what a request is
billed, and its token counts. A request cancelled while prefilling is dropped at the end of
the chunk in progress.

`prefill_chunk_tokens=None`, the default, keeps the previous behaviour exactly, so the
performance gate, the load generator and every earlier number are unchanged. The default
server uses 256 (`DEFAULT_PREFILL_CHUNK_TOKENS`).

### A worked example

Test profile: 0.5 ms prefill per token, 4 ms decode step. Request A is decoding; request B
with a 10-token prompt arrives at t=1. Chunks of 4 tokens.
(`test_long_prompt_is_prefilled_in_chunks_between_decode_steps`.)

| | A's tokens at (ms) | A's worst gap | B's first token at |
|---|---|---|---|
| Unchunked | 4, 13.25, 17.5 | 9.25 ms | 13.25 ms |
| Chunks of 4 | 4, 10, 16 | 6 ms | 21 ms |

Unchunked, B's 5 ms prefill lands in one of A's gaps. Chunked, it is spread over three
iterations (2 + 2 + 1 ms) with a decode step for A after each. A is smoother; B waits longer
for its first token, because A's decode steps now run between its chunks.

### Benchmark

**Simulated, from the illustrative profiles. Not measurements of real models.**

```sh
uv run python -m benchmarks.chunked_prefill
```

Working tree for this step (not yet committed; parent commit `ba18512`). One model, the
standard tier. 400 short prompts of 20-80 words arrive as a Poisson stream at 4 req/s; 8
long prompts of 6,000-10,000 words are spread evenly through the run. Up to 64 output
tokens each; no prefix cache; seed 7. All times in ms.

| Chunk tokens | ITL p50 | ITL p99 | ITL p99.9 | ITL max | Worst-gap p99 | Short TTFT p50 | Short TTFT p99 | Long TTFT p50 | Total p99 |
|-------------:|--------:|--------:|----------:|--------:|--------------:|---------------:|---------------:|--------------:|----------:|
| off          |    15.0 |    29.4 |   1,063.0 | 1,303.0 |       1,303.0 |           30.1 |        1,027.0 |       1,075.1 |   2,500.7 |
| 4096         |    15.0 |    29.7 |     630.0 |   643.5 |         643.5 |           30.4 |          999.4 |       1,080.4 |   2,503.4 |
| 1024         |    15.0 |   168.6 |     180.8 |   188.4 |         186.8 |           30.7 |          308.9 |       1,173.7 |   2,488.4 |
| 256          |    15.0 |    57.6 |      74.1 |    77.4 |          77.4 |           31.3 |          121.6 |       1,507.2 |   2,487.6 |
| 64           |    15.6 |    33.6 |      38.6 |    44.8 |          44.8 |           34.0 |           83.6 |       2,725.0 |   3,648.8 |
| 16           |    16.2 |    23.4 |      24.6 |    25.8 |          25.8 |           71.4 |          129.1 |       5,566.7 |   6,401.7 |

"Worst-gap p99" is the p99, over requests, of each request's single longest gap: how bad the
worst stutter is for the unluckiest 1% of streams. "Short" and "long" split TTFT by prompt
kind. The run takes 92.4 s of simulated time in every row; throughput does not change.

What it shows:

- **The worst stall falls from 1.3 s to 77 ms at 256 tokens** and to 26 ms at 16. So does
  the per-request worst gap. Unchunked, about one stream in a hundred freezes for over a
  second.
- **Short prompts' TTFT p99 falls from 1,027 ms to 122 ms at 256** and 84 ms at 64. They no
  longer queue behind a long prompt's whole prefill.
- **Plain ITL p99 does not behave the way one might expect.** Unchunked it is a healthy
  29.4 ms, because long prompts are rare: their stalls touch fewer than 1% of all gaps, so
  the 99th percentile never sees them. Chunking turns each rare, huge stall into many
  moderate ones, and at 1024 tokens those are frequent enough to reach the p99, which gets
  *worse* (168.6 ms). Only at 16 tokens is ITL p99 better than unchunked. The improvement is
  in the far tail: p99.9, the maximum, and the per-request view. A dashboard showing only
  ITL p99 would have hidden the original problem and then blamed the fix.
- **The long prompts pay.** Their own TTFT rises from 1.1 s to 1.5 s at 256 and 5.6 s at 16,
  because decode steps for everyone else now run between their chunks.
- **Too small is bad for everyone.** At 16 tokens the short prompts' TTFT p50 more than
  doubles (30 to 71 ms) and total p99 goes from 2.5 s to 6.4 s. Many requests are
  mid-prefill at once and each iteration only moves each of them 16 tokens.
- **Around 64 to 256 tokens is the sensible range on this workload**: nearly all of the tail
  benefit for a modest delay to the long prompts.

`test_chunked_prefill_cuts_the_latency_tails_on_a_mixed_workload` runs this workload and
asserts the improvements and the cost. `test_medium_chunks_can_make_the_itl_p99_worse_than_
no_chunking` pins the ITL p99 observation.

### Correctness tests

- Token counts and cost are identical with and without chunking, for chunk sizes 1, 4, 7
  and 64 on a mixed workload.
- Every request has one timestamp per output token, strictly increasing, consistent with
  `first_token_ms` and `finish_ms`.
- No request gets a token before all of its prompt has been prefilled.
- The decode steps account for every output token exactly once.
- No iteration prefills more than one chunk per running request.
- No gap exceeds one iteration's worth of chunks plus a full-batch decode step.
- A request alone takes exactly the same time whatever the chunk size.
- A prompt that fits in one chunk behaves as without chunking.

## Where this differs from real engines

- **Chunk budget.** Here each prefilling request gets its own N tokens per iteration, so the
  delay per iteration grows with the number of requests prefilling at once (bounded by the
  batch size). Engines such as vLLM use a single token budget per step shared by all
  requests, served first come first served, which bounds the step more tightly but lets a
  long prompt hold up short ones behind it unless extra rules are added. I chose the
  per-request rule because it is starvation-free and lets short prompts through without
  special cases. This description of vLLM is from memory; check its documentation.
- **Prefill and decode are sequential here.** A real engine runs a chunk and the decode
  tokens in one batched forward pass, so the cost is not a simple sum.
- **Prefill cost is linear in tokens.** In reality later chunks attend to more context and
  cost more.
- **Chunked and unchunked modes are separate code paths**, kept so that earlier results stay
  reproducible. They differ in one more way than the chunking itself: unchunked, a request
  arriving during another's prefill can be admitted before the next decode step; chunked,
  admission happens once per iteration.
- **`start_ms`** is the admission time in chunked mode. Prefill then takes several
  iterations, so `ttft - queue` includes other requests' decode steps.

## Design decisions

- **Per-token times are always recorded**, not only for streams, so ITL covers all traffic
  and tests can assert on it without going through HTTP.
- **`on_token` is an argument of `submit`**, not a field of the request, so the request
  stays a plain value. `Worker`, `ModelRegistry.submit`, and both workers take it.
- **Streaming waits for the first token before responding**, trading a delayed header for
  correct status codes on every failure that happens before any output.
- **The end of a stream is signalled inside the gateway by a sentinel** queued when the
  request's task finishes. The worker emits a request's last token before it resolves the
  result, so the sentinel always follows the last token.
- **`StepRecord` gained `prefill_tokens`**, and its `batch_size` now counts only requests
  that decode in that step.
