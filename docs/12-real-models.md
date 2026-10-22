# 12 — Real models through llama.cpp

This step lets the gateway serve real models. `LlamaCppBackend` calls a local llama.cpp
`llama-server` over its OpenAI-compatible streaming API, one server per tier, and the
gateway measures time to first token and total latency from the stream as it arrives.

**Updated by platform step 2 (`docs/20-applications-api-keys.md`).** Requests carry an application's API key (`Authorization: Bearer <key>`) instead of a `tenant_id` in the body, which is now a 422. The curl examples below predate that; the README's Curl demo is current. The server also needs `GATEWAY_KEY_PEPPER` set and a key from
`scripts/manage.py`, as in the README's Quickstart.

Code: `src/gateway/backends/llamacpp.py`, `src/gateway/passthrough.py`,
`src/gateway/backend_config.py`, `src/gateway/backends.yaml`,
`scripts/run_local_models.sh`, `examples/backends.ollama.yaml`.
Tests: `tests/test_llamacpp_backend.py`, `tests/test_real_backend_api.py`,
`tests/test_real_models.py`, with the mock server in `tests/mock_llama.py`.

## Read this first: who is doing the batching

**In this mode the gateway's own scheduler is not in the path.**

| | Simulated backend (default) | llama.cpp backend |
|---|---|---|
| Who batches requests | `ContinuousBatchingWorker` in this repo | llama-server, across its `--parallel` slots |
| Who decides when tokens appear | the cost model and the clock | the real model |
| Prefix / KV cache | `PrefixCache` in this repo | llama-server's own per-slot prompt cache |
| Chunked prefill, KV accounting, FIFO admission | this repo | not used; llama-server has its own logic |
| Worker in the gateway | `ContinuousBatchingWorker` | `PassthroughWorker` |

`PassthroughWorker` does no queueing, batching or caching. It forwards each request to the
model server at once, concurrently with any others, and stamps the clock when the request
arrives, when each piece of output arrives, and at the end.

What is still the gateway's in this mode: model names and aliases, tenants, plans and
budgets, the cost ledger, quality routing, SLO-aware downgrade, request timeouts, an
in-flight limit per tier, metrics (TTFT, ITL, total), and streaming to the client.

### What this means for comparisons

- **Steps 3, 4 and 11 are not being tested here.** Continuous batching, the radix-tree
  prefix cache and chunked prefill in this repo are simulations of a scheduler. Running
  real models through llama-server exercises llama.cpp's scheduler, not ours. A latency
  number from this mode says nothing about whether our batching design is good.
- **Simulated and real numbers are not comparable.** The simulator's model profiles are
  calibrated against these servers (note 13), but a simulated figure is this repo's
  scheduler with those step costs, and a real-mode figure is the model server's own
  scheduling. Do not put a figure from `docs/03` to `docs/08` beside a figure measured here
  and read the difference as meaning anything.
- **You cannot A/B our scheduler against llama-server's** with this setup. That would need
  a backend that exposes prefill and single decode steps to our scheduler, which the HTTP
  API does not.
- **What real mode is good for:** checking that routing, metering and limits work with a
  real model; getting real TTFT, ITL and total latency for a workload on your hardware; and
  collecting the measurements needed to calibrate the simulator's profiles later.
- **The performance gate stays on the simulator.** It needs determinism (see `docs/08`).

### What the numbers in a real-mode response are

- **`timing.ttft_ms`, `timing.total_ms`, token `t_ms`**: measured by the gateway with
  `WallClock`, from the moment it received the request to the arrival of the first and last
  piece. They include the HTTP hop to the model server and any time the request waited
  inside it.
- **`timing.queue_ms` is always 0.** The gateway does not queue. llama-server queues
  requests when all its slots are busy, but that wait is not reported separately, so it
  shows up inside TTFT.
- **`usage`**: the model server's own counts when it reports them. If it does not,
  completion tokens are counted as one per streamed piece and prompt tokens are estimated at
  one per word.
- **`usage.prompt_tokens` can be larger than the prompt looks.** A server may wrap the
  prompt in the model's chat template; in the check described below, Ollama reported 72
  prompt tokens for "Say hi.".
- **`usage.cached_tokens` is always 0.** llama-server's prompt cache is not read.
- **ITL** is the gap between streamed pieces. A piece is usually one token, not always.
- **`cost`** is the real token counts times the *placeholder* prices of note 01
  (operator-supplied, not data), times the plan's multiplier. The token counts are real; the
  prices are not.
- **`text`** is the model's real output.

## Setup with llama-server

### Hardware

Download sizes are exact (checked against the files on Hugging Face on 2026-10-07):

| Tier     | Model                    | File (Q4_K_M)                              | Size    |
|----------|--------------------------|--------------------------------------------|--------:|
| economy  | Llama 3.2 1B Instruct    | `Llama-3.2-1B-Instruct-Q4_K_M.gguf`        | 0.81 GB |
| standard | Llama 3.2 3B Instruct    | `Llama-3.2-3B-Instruct-Q4_K_M.gguf`        | 2.02 GB |
| premium  | Llama 3.1 8B Instruct    | `Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf`   | 4.92 GB |

That is 7.75 GB of disk. The rest of this section is **rule of thumb, not measured here**:

- **Memory:** each model needs about its file size plus room for context, so plan for
  roughly 10 to 12 GB of free RAM (or VRAM, if offloaded) to run all three at once. 16 GB of
  system RAM is a practical minimum; more slots or a larger context need more.
- **CPU only works**, slowly: a few tokens per second for the 8B model on a typical laptop,
  much faster for the 1B. A GPU with 8 GB or more helps a great deal (`GPU_LAYERS=99`).
- **To use less**, start fewer tiers (`TIERS="economy standard"`), though the default
  config expects all three; or lower `PARALLEL` and `CTX_PER_SLOT`.

### Steps

1. Install llama.cpp so that `llama-server` is on your PATH (a release build from the
   llama.cpp project, or a package manager). It is MIT-licensed.
2. Download the models and start the three servers:

   ```sh
   scripts/run_local_models.sh start     # downloads on first use, then starts
   scripts/run_local_models.sh status
   ```

   Ports are 8081 (economy), 8082 (standard), 8083 (premium), matching
   `src/gateway/backends.yaml`. Logs go to `.run/llama/`, models to `models/`; both are in
   `.gitignore`. Settings are environment variables listed at the top of the script. On
   Windows run it from Git Bash or WSL.
3. Start the gateway with the real backend:

   ```sh
   GATEWAY_BACKEND=llamacpp uv run uvicorn gateway.app:create_default_app --factory --port 8000
   ```

4. Send a request:

   ```sh
   curl -s -X POST http://127.0.0.1:8000/v1/generate -H "Content-Type: application/json" \
     -d '{"tenant_id": "acme", "quality": "best", "prompt": "What is a radix tree?", "max_tokens": 64}'
   ```

5. Stop the servers with `scripts/run_local_models.sh stop`.

Always send `max_tokens`. Without it a model generates until it decides to stop, and on a
CPU that can pass the gateway's 120 s request timeout (504).

### Ollama as a fallback

If you would rather not build or install llama.cpp, Ollama speaks the same API. One Ollama
process serves all three models, so every tier gets the same URL and a different model name:

```sh
ollama pull llama3.2:1b-instruct-q4_K_M && ollama pull llama3.2:3b && ollama pull llama3.1:8b
GATEWAY_BACKENDS=examples/backends.ollama.yaml \
  uv run uvicorn gateway.app:create_default_app --factory --port 8000
```

Differences that matter: Ollama loads and unloads models on demand, so the first request to
a tier can take many seconds and switching tiers can evict another model unless it is
configured to keep several loaded; it applies the model's prompt template itself; and its
concurrency is governed by its own settings, not by `--parallel`.

## Configuration

`src/gateway/backends.yaml` is the packaged default. `GATEWAY_BACKENDS=/path/file.yaml`
replaces it; `GATEWAY_BACKEND=llamacpp` or `simulated` overrides only the choice.

```yaml
backend: simulated          # or llamacpp

llamacpp:
  endpoint: completions     # POST /v1/completions; "chat" uses /v1/chat/completions
  max_in_flight: 32         # open requests per tier; more get 429 queue_full
  servers:
    economy:  {url: "http://127.0.0.1:8081"}
    standard: {url: "http://127.0.0.1:8082"}
    premium:  {url: "http://127.0.0.1:8083", model: "optional-name"}
```

`model` is sent as the request's `model` field: llama-server ignores it, Ollama needs it.
With `backend: llamacpp` every tier must have a server, or the gateway refuses to start.
The plans file, tenants and the HTTP API are the same for both backends.

With `endpoint: completions` the prompt is sent as raw text; llama-server does not apply the
chat template, so instruct models answer more naturally with `endpoint: chat`, which sends
the prompt as one user message.

## The interface

Both backends implement the same `Backend`:

```python
async def generate(self, request, *, on_token=None) -> Generation
```

- `SimulatedBackend` returns at once with token counts and never calls `on_token`. The
  worker in front of it decides the timing.
- `LlamaCppBackend` takes as long as the model takes, calls `on_token(index, text)` as each
  piece arrives, and returns the text and the server's token counts.

`Generation` gained `text` and `prompt_token_count` (for backends that report a count but
not token ids). `RequestResult` gained `text`, and the token callback now carries each
token's text, so streaming shows the real words.

**The backend and the worker go in pairs.** Simulated backend with
`ContinuousBatchingWorker`; real backend with `PassthroughWorker`. The config picks the pair.
Putting the real backend behind our batching worker would be wrong twice over: the worker
would call the model during admission, blocking its own loop, and then add simulated time on
top of real time.

## When the model server is down

| Situation | Status | `code` |
|---|---|---|
| Cannot connect to the tier's server | 502 | `backend_unavailable` |
| Server answers with an HTTP error (for example 503 while loading) | 502 | `backend_error` |
| Connection lost mid-response, malformed stream, or empty completion | 502 | `backend_error` |
| The same, after a streamed response has started | 200, then an `error` event | as above |

```json
{"error": {"code": "backend_unavailable",
           "message": "Model backend 'premium' at http://127.0.0.1:8083: could not connect. Is the model server running? See docs/12-real-models.md.",
           "backend": "premium", "url": "http://127.0.0.1:8083"}}
```

(The body asserted in `test_server_down_is_a_502_with_a_clear_message`.) One tier being down
does not affect the others. Failed requests are not charged and not counted in the metrics.
The gateway does not check the servers at startup; a server that is down is discovered by
the first request to it.

A gateway timeout or a client disconnect closes the connection to the model server, which is
how llama-server knows to stop generating and free the slot.

## Tests

- **Mock server.** `tests/mock_llama.py` is an httpx transport that streams OpenAI-style
  chunks paced on the test's clock. With `SimClock`, token timestamps are exact, so the
  tests assert TTFT and latency to the millisecond. It can refuse connections, return HTTP
  errors and break mid-stream.
- **Real models.** `tests/test_real_models.py` is marked `real_models` and skipped unless
  `GATEWAY_REAL_MODELS=1`. CI does not set it, so CI never needs a model.

  ```sh
  scripts/run_local_models.sh start
  GATEWAY_REAL_MODELS=1 uv run pytest -m real_models -v
  ```

  These check that each tier returns text with plausible usage and timing. They assert
  nothing about speed.

## What has and has not been run

- **Run:** all mock-server tests, as part of the normal suite.
- **Run:** the four `real_models` tests, once, against **Ollama 0.12.6 with one small model
  that was already on this machine (`deepseek-coder`, about 0.8 GB) standing in for all
  three tiers**, on Windows 11. They passed. This checked the real protocol end to end:
  streaming, usage parsing and timing against a live server. It found one bug, since fixed
  and covered by a test: Ollama attaches an all-zero `usage` to every chunk and the real
  one only to the last.
- **Not run:** llama-server itself. It is not installed on the machine this was written on.
  The backend follows the OpenAI-compatible streaming format that llama-server documents,
  and the same code worked against Ollama, but the pairing with llama-server is untested.
- **Run later, through Ollama:** the three Llama models (`llama3.2:1b-instruct-q4_K_M`,
  `llama3.2:3b`, `llama3.1:8b`) were downloaded with `ollama pull` and the four
  `real_models` tests passed against them. They were then measured by the calibration
  tool; see `docs/13-calibration.md`. They have still not been run under llama-server.
- **`scripts/run_local_models.sh`:** syntax-checked with `bash -n`; `status`, `stop`, the
  usage message and the "llama-server not found" path were run. The three download URLs
  were confirmed to exist, with the sizes in the table. The download and start paths were
  not run.

## The Llama license

The tools here are open source: llama.cpp and Ollama are MIT-licensed. **The model weights
are not.** Llama 3.1 and Llama 3.2 are released by Meta under the Llama 3.1 and Llama 3.2
Community License Agreements. These are free of charge and allow commercial use, but they
are Meta's own licenses, not open-source licenses in the OSI sense. Among other things they
come with an acceptable use policy, and they set conditions on redistribution, such as
passing on the license and attribution. The GGUF files used here are third-party
conversions of Meta's weights and remain under the same terms.

Running `scripts/run_local_models.sh download` or `ollama pull` fetches those weights, and
using them means accepting Meta's terms. Read the license for each model before you rely on
it; this note is a pointer, not legal advice. If the project's "free and open-source tools
only" rule is meant to cover model weights too, these models do not strictly meet it, and
that is a decision for you. This repository does not contain or redistribute any weights.
