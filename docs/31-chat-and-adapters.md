# 31. The chat endpoint and the provider adapters

A5, pull request 1 (docs/provider-plan.md, sections 1, 2, 8 and 9). The gateway now speaks one
chat format, with tools, to three kinds of provider, and serves it at
`POST /v1/chat/completions` in the OpenAI-compatible shape. In this step every tier is still the
gateway's own: the simulator, or the operator's model servers. Organization providers, keys and
fallback come in pull requests 2 and 3.

Code: `src/gateway/chat.py` (the format and the errors), `src/gateway/secret.py`,
`src/gateway/adapters/` (`http.py`, `ollama.py`, `openai_compatible.py`, `anthropic.py`),
`src/gateway/chatservice.py`, the route in `src/gateway/app.py`, `chat_api` in
`src/gateway/backend_config.py`. Tests: `tests/test_adapters.py`, `tests/test_chat_api.py`,
`tests/test_secret.py`, `tests/test_live_providers.py`, with `tests/mock_providers.py`.

## One format

Messages (`system`, `user`, `assistant` with its tool calls, `tool` naming the call it
answers), text only; tools (name, description, a JSON Schema object); tool calls (id, name, the
arguments as JSON text of an object); `tool_choice` (`auto`, `none`, `required`, or one tool);
streaming as deltas (text as it comes, each tool call whole once its arguments parse); usage.
Every failure is a `ProviderError` with a fixed code and message; a provider's own error text is
never passed on, because providers echo parts of a bad key in it.

`Backend` gained `chat()` and `capabilities()`, with defaults, so `SimulatedBackend` and
`LlamaCppBackend` are unchanged. The credential is an argument of each `chat()` call, never held
by an adapter (tested).

## The adapters

Each was written against its provider's current documentation (2026-10-22) and is tested only
against scripted mock providers (`tests/mock_providers.py`: an httpx transport paced on the
test's clock, recording every request and whether each stream was closed).

- **Ollama** (`/api/chat`, newline-delimited JSON). Tool calls come without an id, their
  arguments as an object, each whole in one chunk. The adapter gives each call its own id
  (`call_` and 24 random hex characters); on the next turn each tool result is matched to its
  call by that id, which gives the call's position and name, and the results go to Ollama in the
  calls' order, each with its `tool_name`. Tested with the same tool called twice in one turn and
  the results sent back in the other order; ids do not repeat across 20 turns; a result that
  answers no call is a 422 before anything is sent. Ollama cannot be told to call a tool, so
  `tool_choice` `required` or a named tool is refused (`tools_not_supported`). Operator-configured
  only.
- **OpenAI-compatible** (`{base_url}/chat/completions`, server-sent events). Tool-call fragments
  are put together by index and emitted when the answer finishes. Usage is taken from the last
  usage that reports any tokens (some servers attach zeros to every chunk).
- **Anthropic Messages** (`/v1/messages`, `anthropic-version: 2023-06-01`, named events).
  System messages go to `system`, tool results to `tool_result` blocks, same-role messages are
  merged, `max_tokens` is always sent, temperature is capped at 1. Errors in the stream
  (`event: error`) are mapped like HTTP errors.

**Anthropic thinking blocks are never dropped silently.** `thinking` is sent only when the
configuration sets it (sending `{"type": "disabled"}` to a model that cannot turn thinking off is
itself refused, so the gateway does not guess). An answer with thinking blocks and a tool call
means the model needs those blocks sent back with the tool result, which the OpenAI format cannot
carry: the request is answered 422 `tools_not_supported` (`thinking_blocks_required`) and the
adapter records `supports_tools: false` with that reason. Thinking blocks in an answer without a
tool call are counted (`thinking_blocks_omitted` in the usage, `X-Thinking-Blocks-Omitted`).

**Capabilities** are `supports_tools`, `supports_streaming` and `context_window`, each with its
source (`declared`, `probed`, `configured`, `built_in`, `unknown`); unknown is `null`, never
false or zero. The simulator's tiers have no tools (`built_in`). Probing comes with
test-connection in pull request 2.

### Errors

| Upstream | Answer |
|---|---|
| 401, 403 | 502 `provider_auth_failed` |
| 429 | 429 `upstream_rate_limited`; `Retry-After` passed through only as whole seconds from 1 to 3600 |
| 5xx, Anthropic's 529, a connection refused | 502 `upstream_unavailable` |
| connect, first-token or total timeout | 502 `upstream_timeout` (which, in the message) |
| a stream that is not JSON, ends early, or breaks | 502 `upstream_malformed`, or `upstream_unavailable` for a dropped connection |
| 3xx | 502 `upstream_rejected` (`redirect`): redirects are never followed |
| other 4xx | 502 `upstream_rejected`, with the provider's error type only if it is on a fixed list |

Every case is tested for all three adapters, and in each the mock's body echoes the key: it is
in no error, message, `repr` or traceback.

**The HTTP client** follows no redirect and takes no proxy from the environment. **Timeouts**:
connect 5 s (httpx); first token 30 s and total 120 s, on the injected clock
(`gateway.chatservice`); chosen settings, not measurements. When either passes, or the client
goes away, the adapter's task is cancelled and the upstream stream closed (tested at the
adapter, and through the endpoint with a client that hangs up after the fourth chunk).

## `POST /v1/chat/completions`

Authenticated with an application API key, with the same plan, rate limit, budget and
`max_tokens` rules as `/v1/generate`. Accepted: `model` (a tier or alias) or `quality`,
`messages` (`developer` is taken as `system`), `tools`, `tool_choice`, `max_tokens`,
`temperature` (0 to 2), `stream`, `stream_options.include_usage`. **Any other field is a 422
`unsupported_field` naming it** (`n`, `logprobs`, `response_format`, `functions`,
`max_completion_tokens`, `seed`, `user`, ...), as is an image or audio part. Every refusal uses
the OpenAI error shape (`{"error": {"message", "type", "param", "code"}}`).

Headers: `X-Model-Used` (the tier), `X-Provider` (`local` in this step), `X-Usage-Estimated`
when the provider gave no usage (the body's usage then says `usage_estimated: true`; the
estimate is one token per four characters), `X-Thinking-Blocks-Omitted`. **No `X-Cost-USD`**:
chat is priced only with prices an organization enters (pull request 2), never with the
profiles' placeholder prices, so the ledger records the tokens at no cost. `X-Fallback-From`
comes with pull request 3.

A failure before any output is an ordinary error answer, streamed or not. After output has
started the status cannot change: the stream ends with an error event in the OpenAI shape and no
`[DONE]` (tested).

**What serves a tier.** The simulator: the conversation is flattened into one prompt and served
by the same scheduler as `/v1/generate`, answering `[simulated output, N tokens]`, no tools.
With `backend: llamacpp`, each tier's chat goes to the operator's server through the adapter
`llamacpp.chat_api` names (`openai_compatible`, the default, which llama-server and Ollama both
speak at `/v1/chat/completions`; or `ollama`, its own `/api/chat`). `/v1/generate` keeps using
`LlamaCppBackend` exactly as before.

## The OpenAI wire format, without the SDK

The OpenAI Python SDK was not added: its `httpx2` and `httpcore2` dependencies could not be shown
to be canonical (docs/provider-plan.md, section 8). Instead `tests/test_chat_api.py` sends
OpenAI's own documented example requests ("Default", "Streaming", "Functions", from the
published `openai-openapi` description at a pinned commit; docs/licenses.md) to the gateway, with
only the model replaced by a tier, and checks every answer, chunk, tool call, usage and error
against the `required` field lists of the same description.

## Live tests

`tests/test_live_providers.py`. Hosted ones (`live_provider`) are skipped unless
`GATEWAY_LIVE_PROVIDERS=1` and that provider's variables are set by whoever runs them; **they
can cost money**; at most four requests per provider, `max_tokens` 16. The local Ollama one
(`real_models`) was run on this machine: `GATEWAY_REAL_MODELS=1 uv run pytest -m real_models
tests/test_live_providers.py`, a plain answer and a full tool round trip through the Ollama
adapter with `llama3.2:1b-instruct-q4_K_M`, passed.

## Measured

MEASURED on this machine (Intel Core Ultra 7 155H, 22 logical cores, 15.4 GB RAM, Windows 11
10.0.26200, Python 3.12.10): `uv run python -m benchmarks.chat_overhead --rounds 500 --runs 3`,
on the code of this step's commit. Two processes on this machine: a mock OpenAI-compatible
provider that answers at once with a short streamed answer, and the real gateway
(`gateway.serve`, `backend: llamacpp`, `chat_api: openai_compatible`, every tier pointed at the
mock). Interleaved round by round from a client on the same machine; p50 of each run in ms, as
the range over the three runs (500 rounds each, after 20 warm-up rounds):

| Request | p50, ms |
|---|---|
| the mock directly (the streaming request the adapter sends, read to the end) | 1.70 to 1.74 |
| `POST /v1/chat/completions` through the gateway, not streamed | 5.78 to 6.10 |
| difference | 4.08 to 4.38 |

The difference contains the gateway's own call to the mock, a request that on its own takes the
first row's time, plus the key check, routing, the adapter, metrics and the ledger's usage write.
It is not a provider's latency: the provider here is a mock.

## Threat model (this step)

For step 9's `docs/threat-model.md`:

- **Prompts and tool results go to the operator's model servers** as before; nothing new leaves
  the machine in this step unless the operator points `backends.yaml` at a remote host.
- **A provider's errors are discarded**, not relayed; the cost is less detail for whoever debugs
  a failure (the provider's own logs have it).
- **The usage estimate can be wrong** in either direction when a provider reports none; it is
  marked, and it is what the budget is charged.
- **A credential is plaintext in memory** for the length of one request, in the `Secret` and in
  the outgoing request's headers; Python cannot erase it. Only `credential_headers` reveals it
  (an AST test fails on any other call).
- **Hostile tool calls**: the gateway checks only that a call's arguments are a JSON object; the
  application runs its tools and must treat the arguments as untrusted.
- **The chat endpoint is not local only**, like `/v1/generate`: it needs an application key.
