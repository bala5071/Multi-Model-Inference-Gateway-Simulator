# 07 — Load generator and fairness

Step 7 adds a load generator that drives the HTTP API, and uses it to show that one model's
overload does not slow another model down.

Code: `benchmarks/loadgen.py`, `benchmarks/fairness.py`. Tests: `tests/test_loadgen.py`.

## Running it

In-process on simulated time (the default; deterministic, finishes in about a second):

```sh
uv run python -m benchmarks.loadgen --seed 7 --requests 300 --rate 8
uv run python -m benchmarks.loadgen --json          # the report as JSON
```

Against a server that is already running, on real time:

```sh
uv run uvicorn gateway.app:create_default_app --factory --port 8000
uv run python -m benchmarks.loadgen --url http://127.0.0.1:8000 --tenant acme --requests 40 --rate 10
```

The default server requires a tenant since the routing step (`docs/09-routing.md`);
`--tenant` sends that id with every request. `acme` is on the plan that allows all three
models.

| Option                     | Config field             | Default                              |
|----------------------------|--------------------------|--------------------------------------|
| `--seed`                   | `seed`                   | 7                                    |
| `--requests`               | `total_requests`         | 300                                  |
| `--rate`                   | `arrival_rate_per_s`     | 8.0, over all models together        |
| `--weights`                | `model_weights`          | `economy=70,standard=20,premium=10`  |
| `--prompt-words MIN-MAX`   | `prompt_lengths`         | 60% 20-80, 30% 80-300, 10% 300-800   |
| `--shared-prefix-fraction` | `shared_prefix_fraction` | 0.5                                  |
| `--shared-prefix-words`    | `shared_prefix_words`    | 200                                  |
| `--max-tokens`             | `max_tokens`             | none (the backend decides)           |
| `--url`                    | n/a                      | none: run in-process                 |

## What it does

`build_workload(config)` turns a `LoadConfig` into a list of `PlannedRequest`s using one
`random.Random(seed)`. For each request, in this order, it draws:

1. the gap since the previous arrival, exponential with mean `1000 / rate` ms (a Poisson
   process);
2. the model, with odds given by the weights;
3. a prompt-length bucket by weight, then a word count uniform within it;
4. whether the prompt starts with the shared system prompt.

A prompt is its own words, unique to the request, optionally preceded by the system prompt.
The length distribution describes the request's own part; the system prompt is extra.

`run_workload` starts one task per request. Each sleeps on the clock until its arrival time,
POSTs to `/v1/generate`, and records the response. Requests overlap freely; nothing limits
concurrency. The same code runs in both modes: in-process it uses `httpx`'s ASGI transport
and the app's own `SimClock`; live it uses a real connection and `WallClock`.

### Report

Per model and overall:

| Field                                   | Meaning                                                      |
|-----------------------------------------|--------------------------------------------------------------|
| `ttft_p50_ms`, `ttft_p99_ms`            | Time to first token, as reported by the server.              |
| `total_p50_ms`, `total_p99_ms`          | Enqueue to finish, as reported by the server.                |
| `throughput_rps`                        | Successful requests / (last completion - first arrival).     |
| `completion_tokens_per_s`               | Output tokens over the same span.                            |
| `cache_hit_rate`                        | Share of successful requests with at least one cached token. |
| `cache_token_hit_rate`                  | Cached prompt tokens / prompt tokens.                        |
| `cost_usd`                              | Sum of the responses' cost.                                  |
| `requests`, `errors`                    | Sent, and non-200 responses.                                 |

Percentiles are nearest-rank. Latencies come from the server's `timing` block, so they
exclude network and HTTP overhead; in-process there is none anyway. Errors are counted and
excluded from latencies. Cache figures are computed from the responses, so they also work
against a live server.

## Determinism

In-process, **the same config gives byte-identical output**. `to_json()` is the stable form
(sorted keys, fixed indent). Two tests check it: the JSON bytes of two runs are equal, and
the per-request outcomes are equal. Run in two separate processes, the JSON also hashed the
same (`python -m benchmarks.loadgen --json | sha256sum`, twice).

It holds because nothing in the path depends on real time, hash randomisation or scheduling
luck: the workload is a pure function of the config, `SimClock` only advances when every
task is blocked and breaks ties in a fixed order, token ids and output lengths are hashes of
the text, and request ids are a counter.

Live runs are not deterministic, and repeat runs with the same seed send the same prompts,
so a server that has seen them reports more cache hits the second time.

## Example results

**Simulated, from the illustrative profiles of step 1. Not measurements of real models.**
Working tree for this step (not yet committed; parent commit `ba18512`).

`uv run python -m benchmarks.loadgen` (seed 7, 300 requests, 8 req/s, weights 70/20/10, half
the requests sharing a 200-word system prompt):

| Model    | Reqs | TTFT p50 ms | TTFT p99 ms | Total p50 ms | Total p99 ms | req/s | tok/s | Hit rate | Token hit rate | Cost USD |
|----------|-----:|------------:|------------:|-------------:|-------------:|------:|------:|---------:|---------------:|---------:|
| economy  |  197 |        15.0 |        71.8 |      1,014.2 |      1,917.8 |  5.38 | 723.6 |     0.50 |           0.39 | 0.008366 |
| standard |   68 |        38.3 |       135.4 |      2,254.8 |      3,993.8 |  1.81 | 259.2 |     0.53 |           0.39 | 0.009242 |
| premium  |   35 |     3,029.3 |     6,448.6 |      9,585.4 |     17,402.3 |  0.84 | 123.4 |     0.37 |           0.34 | 0.015390 |
| all      |  300 |        21.6 |     5,021.9 |      1,229.6 |     13,458.2 |  7.13 | 984.3 |     0.49 |           0.39 | 0.032998 |

At this rate the economy and standard models keep up and the premium model, at 0.8 req/s,
is near its limit, so its requests queue. The request-level hit rate is close to the 0.5
shared fraction; the premium model's is lower because more of its requests are admitted
while the first carriers of the system prompt are still in flight, and a prefix only becomes
reusable when its request finishes (see step 4).

A live run was also done once, to check that the mode works: 40 requests at 10 req/s with
`--max-tokens 16` against `uvicorn` on the same machine (Windows 11, Python 3.12) completed
with 0 errors. Its latencies are wall-clock waits of the simulator, inflated by the OS timer
(see `docs/06-api.md`), so they are not reported here as results.

## Fairness: a saturated model must not starve a light one

### The test

`test_saturated_model_does_not_starve_a_lightly_loaded_one` sends 1000 requests at 200
req/s, 98% to the economy model and 2% to the premium model (prompts of 20-80 words, 16
output tokens, no shared prefix). That is far more than the economy model can serve, while
the premium model sees about 4 req/s. It asserts:

- the economy model is saturated: its p99 is more than 30 times its slowest possible
  unloaded request (100 ms);
- the premium model's p99 is within the bound of **twice its slowest possible unloaded
  request**, 2 x (80 x 0.6 + 16 x 30) = 1056 ms;
- stronger than a bound: with every economy request removed from the workload, each premium
  request's TTFT and total latency are **exactly the same**. The heavy load has no effect on
  the light model at all.

`test_a_single_shared_queue_breaks_that_isolation` runs the same workload with both model
names routed to one worker, and asserts the premium requests' p99 now exceeds the bound.

`uv run python -m benchmarks.fairness` prints the three scenarios (simulated):

| Scenario         | Model   | Reqs | Total p50 ms | Total p99 ms |
|------------------|---------|-----:|-------------:|-------------:|
| separate workers | economy |  979 |      1,794.3 |      3,494.6 |
| separate workers | premium |   21 |        715.5 |        943.2 |
| premium alone    | premium |   21 |        715.5 |        943.2 |
| one shared queue | economy |  979 |      1,887.4 |      3,663.9 |
| one shared queue | premium |   21 |      1,460.6 |      3,045.4 |

In the shared-queue scenario the single worker uses the economy profile, which is the
faster one, and the premium requests are still more than three times slower at p99 than on
their own slower model.

### Why per-model workers give isolation

Each model has its own worker, and a worker owns everything a request waits on:

- **its own queue**, so a premium request only ever has premium requests ahead of it;
- **its own batch slots** (`max_batch_size`), so economy requests cannot occupy them;
- **its own KV capacity and prefix cache**, so economy prompts cannot evict premium
  prefixes or use up the room premium requests need;
- **its own scheduler loop**, which makes its decisions from that model's state only.

Nothing a request waits for is shared between models, so there is no path by which economy
load can delay a premium request. That is why the result is exact equality and not merely
"close".

### What would break it

- **A single shared queue**, as tested above. With first-come-first-served, a light model's
  request sits behind the heavy model's whole backlog. Its wait is set by someone else's
  arrival rate.
- **A shared batch or shared batch slots.** If one scheduler filled one batch from all
  models, the heavy model would take the slots.
- **A shared KV budget or prefix cache.** The heavy model's prompts would evict the light
  model's prefixes, and its reservations would make light requests wait for room.
- **A shared concurrency limit or admission control in front of the workers**, such as one
  global semaphore or rate limiter, for the same reason as a shared queue.
- **Real shared hardware.** This is the limit of the simulation. Here each model's step
  time depends only on its own batch. If the three models ran on one GPU, they would compete
  for compute and memory bandwidth, and a busy economy model would slow premium decode
  steps even with separate queues. The isolation shown here is isolation of *scheduling*,
  and holds physically only if each model has its own device or a reserved share of one.
- **The event loop, in the live server.** All workers share one asyncio loop. Under
  `SimClock` that costs nothing. Under `WallClock` a flood of requests for one model takes
  real CPU time for HTTP parsing and JSON, which can delay another model's timers. The
  simulated tests cannot see this.

Per-tenant fairness *within* a model is a different question and is not addressed: one
tenant can still fill a model's queue ahead of another tenant on the same tier.

## Design decisions

- **The generator drives HTTP, not the registry.** The in-process run exercises validation,
  routing and serialisation, and the live mode is then the same code with a different
  client and clock.
- **`benchmarks` is a package** run with `python -m`. Tests import it; `pythonpath = ["."]`
  in the pytest config makes that work.
- **One RNG, fixed draw order.** Changing the order of draws, or adding one, changes every
  workload for every seed. Treat it as part of the format.
- **Open loop.** Arrivals follow the schedule regardless of how the server is doing, so
  overload shows up as growing latency, not as a slower client.
- **No concurrency cap, timeout or retry** in the generator.
- **Unknown models are not checked up front.** A weight for a name the server does not know
  shows up as errors in the report.
