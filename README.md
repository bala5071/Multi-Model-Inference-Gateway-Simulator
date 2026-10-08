# Cost-aware multi-model inference gateway

[![CI](https://github.com/bala5071/Multi-Model-Inference-Gateway-Simulator/actions/workflows/ci.yml/badge.svg)](https://github.com/bala5071/Multi-Model-Inference-Gateway-Simulator/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A gateway that sits in front of several language models and decides, for each request, which
model serves it, when, and what it costs whom.

## The product story

A company sells model access to business clients. The clients are not alike: a startup on a
free tier, a team on a flat monthly plan, an enterprise with a contract. Serving them costs
very different amounts depending on the model, so the plan a client pays for decides which
models it gets.

| Plan       | Models it may use            |
|------------|------------------------------|
| free       | economy                      |
| pro        | economy, standard            |
| enterprise | economy, standard, premium   |

| Tier     | Model         |
|----------|---------------|
| economy  | Llama 3.2 1B  |
| standard | Llama 3.2 3B  |
| premium  | Llama 3.1 8B  |

The gateway enforces that, and tries to serve everyone efficiently while doing it:

- **Routing by plan.** A client names a model, or just asks for `cheapest`, `balanced` or
  `best`, and gets the right model for its plan. Asking for more than the plan includes is a
  403 that says what the plan does include.
- **Metering and budgets.** Every request is charged to its tenant. A monthly token budget
  stops a tenant when it is used up.
- **Continuous batching** per model, so one model serves many requests at once.
- **Prefix reuse**, so a system prompt shared by many requests is processed once.
- **Overload protection**: queue limits, timeouts, and an opt-in downgrade to a cheaper model
  when the requested one is too slow.
- **Latency reporting** (p50/p99, time to first token, inter-token latency) and a **CI gate**
  that fails the build when p99 regresses.

**What this repository is.** Mostly a *simulator*: the scheduler, cache, routing and limits
are real code, but by default no model runs and time is simulated from a small cost model.
That makes every experiment exact and repeatable. Real models can be plugged in through
llama.cpp or Ollama, and one calibration against real models is included. The two modes
answer different questions; see [Limitations](#limitations).

## Architecture

```mermaid
flowchart TD
    client([Client]) -->|"POST /v1/generate"| app

    subgraph gateway["Gateway process"]
        app["HTTP API (FastAPI)<br/>validation, streaming, errors"]
        router["Router<br/>tenant → plan → model<br/>model name or quality"]
        ledger["Cost ledger<br/>spend, tokens, budgets"]
        overload["Overload protection<br/>timeout, SLO monitor, downgrade"]
        registry["Model registry<br/>names and aliases → worker"]
        metrics["Metrics<br/>p50/p99, TTFT, ITL"]

        app --> router --> ledger --> overload --> registry
        app -.-> metrics
    end

    registry --> sim
    registry --> real

    subgraph sim["Simulator mode (default): one worker per model"]
        worker["Continuous batching worker<br/>queue limit, admission, chunked prefill,<br/>one decode step per iteration"]
        cache["Prefix cache<br/>radix tree, LRU, ref counts"]
        cost["Cost model + model profile<br/>step times, prices"]
        clock["Clock<br/>SimClock in tests, WallClock in the server"]
        worker --- cache
        worker --- cost
        worker --- clock
    end

    subgraph real["Real-model mode: one pass-through worker per model"]
        pass["Pass-through worker<br/>forwards and timestamps only"]
        llama["llama-server or Ollama<br/>does its own batching and caching"]
        pass -->|"OpenAI-compatible stream"| llama
    end
```

Rules the code holds itself to (details in [CLAUDE.md](CLAUDE.md)):

- **Time is injected.** Every component takes a clock. Tests and benchmarks run on a
  simulated clock; nothing in the logic calls `time.sleep` or reads the wall clock, and the
  linter enforces it.
- **One backend interface.** The simulated backend is deterministic for a seed.
- **Only the API module imports the web framework** and wires components together.

## Quickstart

Needs Python 3.11+ and [uv](https://docs.astral.sh/uv/). Nothing else.

### Simulator mode (zero setup)

```sh
uv sync
make demo        # or: uv run python scripts/demo.py
```

The demo runs the gateway in-process and walks through three tenants on three plans, a
request outside a plan, a prefix-cache hit, a streamed answer and the usage ledger. It takes
a couple of seconds and needs no server.

To run it as a server:

```sh
make serve       # or: uv run uvicorn gateway.app:create_default_app --factory --port 8000
```

or with Docker:

```sh
docker compose up --build
```

Then open <http://127.0.0.1:8000/docs>.

### Real-model mode

1. Get three models running, either as three llama.cpp servers
   (`scripts/run_local_models.sh start`) or through Ollama
   (`ollama pull llama3.2:1b-instruct-q4_K_M && ollama pull llama3.2:3b && ollama pull llama3.1:8b`).
2. Start the gateway pointing at them:

   ```sh
   GATEWAY_BACKEND=llamacpp make serve                                # llama-server
   GATEWAY_BACKENDS=examples/backends.ollama.yaml make serve          # Ollama
   ```

Hardware needs, the Llama license, and what changes in this mode are in
[docs/12-real-models.md](docs/12-real-models.md). The model weights are under Meta's Llama
license, which is not an open-source license.

## Curl demo

With the server running (simulator mode shown; the three demo tenants are `initech` on free,
`globex` on pro and `acme` on enterprise):

```sh
# The best model the tenant's plan includes.
curl -s -i -X POST http://127.0.0.1:8000/v1/generate -H "Content-Type: application/json" \
  -d '{"tenant_id": "acme", "quality": "best", "prompt": "What is a radix tree?", "max_tokens": 32}'
# HTTP/1.1 200 OK
# x-model-used: premium
# x-cost-usd: 0.0000552000

# A model outside the plan.
curl -s -X POST http://127.0.0.1:8000/v1/generate -H "Content-Type: application/json" \
  -d '{"tenant_id": "initech", "model": "premium", "prompt": "hello"}'
# {"error": {"code": "model_not_in_plan", "message": "Tenant 'initech' is on the 'free' plan,
#   which does not include model 'premium'. Models on this plan: economy. Plans that include
#   'premium': enterprise.", ...}}

# Streaming, one event per token.
curl -s -N -X POST http://127.0.0.1:8000/v1/generate -H "Content-Type: application/json" \
  -d '{"tenant_id": "acme", "model": "economy", "prompt": "hello", "max_tokens": 4, "stream": true}'

# What a tenant has used, and the gateway's own view of latency and load.
curl -s http://127.0.0.1:8000/v1/tenants/acme/usage
curl -s http://127.0.0.1:8000/metrics/summary
curl -s http://127.0.0.1:8000/v1/overload
```

In simulator mode the `text` field is a placeholder; in real-model mode it is the model's
answer. The full API is in [docs/06-api.md](docs/06-api.md) and the notes after it.

## Headline results

Full tables, figures and the command behind every number are in
[docs/REPORT.md](docs/REPORT.md), regenerated by `make report`.

**The first five results are simulated**, on invented model profiles. They show how this
scheduler behaves under those assumptions, not how fast any model is.

| Question | Result (simulated) |
|---|---|
| Does continuous batching help? | The one-at-a-time baseline tops out at 1.18, 0.58 and 0.23 req/s on the three models. Continuous batching reaches 14.5, 4.5 and 1.0. At 16 times the baseline's capacity on the economy model, p99 is 5.3 s against 236 s. |
| Does the prefix cache help? | When every request shares a 400-word system prompt, median time to first token is 23.7 ms with the cache and 88.1 ms without, and the run costs about a quarter as much. |
| What does routing by plan save? | Serving a 60/30/10 free/pro/enterprise mix on the model each plan allows costs 75% less to serve than putting everyone on the premium model. |
| What happens under overload? | At three times the premium model's capacity, p99 is 190 s unprotected. Admission control: 30 s, refusing 189 of 400. Downgrade: 48 s, refusing none. Both: 26 s, refusing 45. |
| Does chunked prefill help? | With 256-token chunks the longest stall between tokens falls from 1.3 s to 77 ms. Plain ITL p99 gets worse at medium chunk sizes; the gain is in the far tail. |

![Baseline vs continuous batching](docs/report/1-batching.png)

![Overload](docs/report/4-overload.png)

**The last result is measured** on one laptop (Core Ultra 7 155H, RTX 4060 8 GB,
Ollama 0.12.6), against the three real models. Each value is the median of six runs made
on 2026-10-08, with the range over those runs in brackets; errors are against held-out
requests:

| Tier | Prefill ms/token | Decode step ms | ms per extra sequence | Per-request overhead ms | Simulator error: TTFT | Simulator error: total latency |
|---|---:|---:|---:|---:|---:|---:|
| economy (1B)  | 0.20 (0.18-0.21) | 6.7 (6.5-6.7)    | 2.7 (2.1-3.7) *    | 90 (75-114) *  | 14% | 8%  |
| standard (3B) | 0.38 (0.35-0.42) | 15.3 (14.7-22.2) * | 4.1 (1.7-4.9) *  | 88 (65-126) *  | 16% | 8%  |
| premium (8B)  | 0.86 (0.76-0.96) | 45.1 (42.8-48.3) | 24.8 (7.7-42.9) *  | 162 (42-236) * | 35% | 19% |

\* Unstable: the range is more than 25% of the median, so the value did not repeat. That is
seven of the twelve, including the cost per extra sequence on every tier. The premium tier
is also hardware-limited: Ollama had 5% of the 8B model on the CPU because it does not fit
the 8 GB GPU.

Run on the median timings instead of the invented ones, the simulated throughput gain of
continuous batching at 16 times the baseline's capacity falls from 12.3x, 7.7x and 4.5x
to 1.8x, 2.1x and 1.5x with batches held to the 4 requests that were measured together
([report, section 1](docs/REPORT.md)). Those gains rest on the unstable parameter.

![Simulator against the real premium model](benchmarks/calibration/premium.png)

What the measurement says about the simulation:

- **The invented profiles made batching look much cheaper than it was.** Each extra request
  in a batch added about 30% to the step time on these models, against the 3% to 8% the
  profiles assume. By the fitted parameters, four requests together give about twice the
  throughput of one, not close to four times. The simulated batching gains above are
  optimistic for this hardware.
- **Once calibrated, the simulator predicts total latency to within 5% to 19%, and time to
  first token badly** (31% to 54% off), because real requests carry a fixed cost of about
  90 to 140 ms that the cost model has no term for.

Details and caveats: [docs/13-calibration.md](docs/13-calibration.md).

## Design decisions and trade-offs

| Decision | Why | What it costs |
|---|---|---|
| Simulate first, on an injected clock | Every experiment is exact and repeatable, so tests can assert timings to the millisecond and the CI gate cannot flake | The numbers are only as good as the cost model, and it is simple |
| One worker, queue and cache per model | A flooded model cannot slow another; with separate workers the light model's latency is identical with and without the heavy load | No sharing of capacity between models, and no fairness between tenants on the same model |
| Backend says what, worker says when | The same simulated backend serves the baseline and the batching scheduler, so they are compared on identical work | A real model server cannot be driven step by step, so real-model mode bypasses the scheduler |
| Prefix cache as a radix tree sharing the model's KV budget with running requests | Cached prefixes are the idle part of the KV memory, not a second budget; in-use prefixes are pinned | A prefix is only reusable once its first request finishes, so a cold burst gets no sharing |
| Strict first-come-first-served admission | Predictable, and a large request cannot starve | Leaves capacity unused when the head of the queue does not fit |
| Budgets checked on arrival, charged on completion | Output length is not known in advance | A soft limit: the request that crosses the budget, and any in flight with it, are served |
| 429 for an exhausted budget and for a full queue | Both are "retry later" with a `Retry-After`; the error code tells them apart | A client must read the code, not just the status |
| Downgrade is opt-in per request, with hysteresis | Nobody is silently given a weaker model; two thresholds and a hold time stop it flapping | It reacts only after slow requests finish, so the first ones still wait |
| Streaming waits for the first token before sending headers | Every failure before any output keeps its proper status code | The client sees nothing during queueing and prefill |
| Chunked prefill gives each request its own chunk per iteration | Short prompts are not held up behind a long one, and nothing starves | The stall per iteration grows with the number of requests prefilling at once |
| Performance gate at 2% tolerance | The simulator has no noise; at 10% a doubled prefill cost would pass | Small intended changes need a re-baseline |
| Calibration kept separate from the default profiles | Earlier results and the gate's budgets stay reproducible | The default simulator knowingly runs on numbers that measurement has shown to be off |

Each step's reasoning is in its own note; start from the [docs index](docs/README.md).

## Limitations

- **Sections 1 to 5 of the results are simulations on invented profiles.** They are not
  benchmarks of Llama models or of any hardware.
- **With real models, this repo's scheduler is not what does the batching.** llama-server or
  Ollama does. The gateway still routes, meters, limits and times, but continuous batching,
  the prefix cache and chunked prefill here are not exercised. A real-mode latency says
  nothing about whether this scheduler is good, and simulated and real numbers must not be
  compared directly.
- **The cost model is simple.** Prefill is linear in tokens, a decode step is linear in batch
  size, the per-request overhead is a constant (0 unless a calibration is loaded), and each
  model has its hardware to itself. After calibration its time to first token is still
  17% to 49% off when four requests arrive together.
- **The calibration is six runs on one day on one laptop through Ollama**, with
  concurrency up to 4, and most of its parameters did not repeat between runs. The 8B
  model did not fit on the GPU and ran partly on the CPU. It has not been run under
  llama-server.
- **Only report sections 1 and 2 have a calibrated variant.** Routing, overload and chunked
  prefill results are on the invented profiles and prices only.
- **Answer quality is not modelled.** The cost comparisons show what a larger model costs,
  never what it is worth.
- **Prices and budgets are invented.**
- **No authentication.** A tenant id is taken on trust and anyone can read any tenant's usage.
- **State is in memory and per process**: ledger, metrics, cache and overload state are lost
  on restart and not shared between instances.
- **One token is one word** in the simulator; output length is known at admission.
- **Not run:** the Dockerfile, the compose file and the Makefile were written but could not
  be executed on the development machine, which has neither Docker nor make. The Makefile's
  commands were each run directly. CI has not run, because nothing has been pushed, so the
  badge above has no status yet.

## What I would do next

1. **Add a per-request overhead to the cost model** and refit. It is the largest known error.
2. **Re-run the calibration properly**: several runs, under llama-server, with the 8B model
   fully on a GPU and concurrency past 4, then make the calibrated profiles the default and
   re-baseline the gate.
3. **Redo the batching results on calibrated profiles**, since the measured cost of batching
   is several times the assumed one.
4. **Make prefixes reusable as soon as they are prefilled**, so a cold burst shares them.
5. **Fairness between tenants on the same model**; today isolation is only between models.
6. **Detect overload from queue age**, not only from finished requests, so downgrade engages
   before the first slow request completes.
7. **Hard budgets**, by reserving `max_tokens` up front.
8. **Authentication and a durable ledger**, the two things a real deployment needs first.
9. **A backend that exposes single decode steps**, so this scheduler can drive a real model
   and be compared with llama-server's own.

## Development

```sh
make check       # lint, type check, tests, performance gate (what CI runs)
make bench       # every simulated benchmark
make report      # regenerate docs/REPORT.md and its figures
make calibrate BACKENDS=examples/backends.ollama.yaml    # needs real models running
```

Without `make`, run the commands from the [Makefile](Makefile) directly. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the rules and [docs/README.md](docs/README.md) for the
documentation index.

## License

[MIT](LICENSE) for the code. The Llama model weights are not part of this repository and are
licensed separately by Meta.
