# 11 — Streaming and chunked prefill

This step adds two things that belong together: streaming responses with per-token
timestamps, which make gaps between tokens visible, and chunked prefill, which removes the
largest of those gaps.

Code: `src/gateway/app.py` (streaming), `src/gateway/continuous.py` (chunked prefill,
per-token times), `src/gateway/schema.py`, `src/gateway/metrics.py`.
Benchmark: `benchmarks/chunked_prefill.py`.
Tests: `tests/test_streaming.py`, `tests/test_chunked_prefill.py`.

**The chunked-prefill results are not measured on real data** (see "Benchmark" below); the
tests pin the mechanism on hand-made test profiles (fixtures, not data).

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

It is `null` until some request has produced at least two tokens. (When this step was
built the class was `MetricsRecorder`; `MetricsStore` replaced it later, see
`docs/05-metrics.md`.)

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

(Copied from one local run of the default server: `uvicorn`, Windows 11, Python 3.12;
recorded on the working tree before it was committed; first committed in full as
`c7e75ac`. See the note on wall-clock timing at the end of this section before reading anything into those times.)

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

Hand-made test profile (`EXACT` in `tests/helpers.py`: a fixture, not data): 0.5 ms prefill
per token, 4 ms decode step. Request A is decoding; request B
with a 10-token prompt arrives at t=1. Chunks of 4 tokens.
(`test_long_prompt_is_prefilled_in_chunks_between_decode_steps`.)

| | A's tokens at (ms) | A's worst gap | B's first token at |
|---|---|---|---|
| Unchunked | 4, 13.25, 17.5 | 9.25 ms | 13.25 ms |
| Chunks of 4 | 4, 10, 16 | 6 ms | 21 ms |

Unchunked, B's 5 ms prefill lands in one of A's gaps. Chunked, it is spread over three
iterations (2 + 2 + 1 ms) with a decode step for A after each. A is smoother; B waits longer
for its first token, because A's decode steps now run between its chunks.

### Benchmark: not measured on real data

**No results are given.** Earlier drafts of this note had a table of inter-token latency and
time-to-first-token tails by chunk size; it came from invented model profiles and was removed
(note 13). It cannot be redone honestly on the calibrated profiles:

- the workload that shows the effect mixes short prompts with prompts of 6,000 to 10,000
  words, far beyond the 1,536 words the calibration measured and the 4,096-token context the
  models were run with, so it would extrapolate the fitted prefill cost well past its data;
- how a real server's prefill behaves when interleaved with decoding was never measured, and
  the simulator runs a chunk and a decode step one after the other, which a real engine does
  not (see below).

`uv run python -m benchmarks.chunked_prefill` still runs the workload, as a demonstration of
the mechanism; it says so when it starts, and its numbers are not to be quoted.
`test_chunked_prefill_cuts_the_latency_tails_on_a_mixed_workload` and
`test_medium_chunks_can_make_the_itl_p99_worse_than_no_chunking` run it on the hand-made test
profiles and pin the behaviour, not a performance figure:

- with chunking, the longest gap between tokens and each request's worst gap fall by more
  than ten times, and short prompts stop waiting behind a long prompt's whole prefill;
- the long prompts themselves start later, the more so the smaller the chunk;
- **plain ITL p99 can get worse with medium chunks.** Long prompts are rare, so unchunked
  their stalls touch fewer than 1% of gaps and the p99 never sees them; chunking turns each
  rare, huge stall into many moderate ones, which do reach the p99. The improvement is in the
  far tail: p99.9, the maximum and the per-request worst gap. A dashboard showing only ITL
  p99 would have hidden the original problem and then blamed the fix.

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
