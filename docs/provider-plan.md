# Provider plan, A5: provider adapters, the chat endpoint, the key vault and fallback modes

Status: **approved 2026-10-22**; PR 1 built (`provider/step-1`), PR 2 being built (`provider/step-2`), with the answers and changes folded in below (it handles
secrets and opens a new network surface: the gateway starts calling hosts an organization
chooses). Branch `provider/step-1`, from `main` at `b5787ec` (identity step 7 merged). Identity
steps 8 and 9 come after A5; each A5 pull request's note has its own threat-model section, and
step 9 consolidates them.

A5 lets the gateway talk to real model providers in one chat format: the operator's local
Ollama, any OpenAI-compatible endpoint, and Anthropic's Messages API. Organizations bring their
own provider keys, kept encrypted; each organization chooses whether its prompts may leave the
machine, and when. It builds on A1 (applications, keys, usage), A2 steps 1 to 7 (organizations,
users, roles, `requires` with step-up, `/v1/admin` local only) and the existing `Backend`
interface. It changes none of `/v1/generate`: every existing test, the performance gate and the
protected outputs stay exactly as they are.

**No real provider key is ever needed.** Every test runs against mock servers in the test
process. Live tests exist, are skipped unless explicitly enabled, and are described in
section 9 with what they would need.

## 1. The pull requests

Your default split, unchanged; the egress guard (SSRF) lands in PR 2, where the first
organization-supplied URL appears. Each PR starts from the previous branch once that branch's CI is
green, and must be merged in order: 1, then 2, then 3.

| PR | Branch | What | Approval |
|---|---|---|---|
| 1 | `provider/step-1` | The chat format, the three adapters, capability flags, error mapping, timeouts, cancellation, usage, and `POST /v1/chat/completions` serving the gateway's own tiers (simulated, or the operator's Ollama). Mock servers; the OpenAI SDK test | this plan |
| 2 | `provider/step-2` | `ProviderConfig` per organization, the vault and its master key, the egress guard (SSRF) for organization-supplied URLs, the provider endpoints under `/v1/admin`, consume-on-use step-up, test-connection and probing, the permission table changes, audit, the redaction layer | this plan |
| 3 | `provider/step-3` | Fallback modes per organization and application, the triggers, the circuit breaker, the slow-provider warning, local-only enforcement in one place, usage records with the fallback reason | this plan |

PR 1 serves only the gateway's own tiers, so it has no organization-supplied URL and no key:
nothing in it can reach a host the operator did not configure. The hosted adapters exist and are
tested against mocks in PR 1, but nothing routes to them until PR 2. Each PR is a draft with the
PR template, CI green, and the definition of done; each ships its note (`docs/31-*` to
`docs/33-*`).

## 2. The chat format (PR 1)

One internal format, `gateway/chat.py`, frozen dataclasses:

- `ChatMessage(role, content, tool_calls, tool_call_id)`: roles `system`, `user`, `assistant`,
  `tool`. Content is text only (image and audio parts are refused with 422 in A5).
- `ToolSpec(name, description, parameters)`: `parameters` is a JSON Schema object, checked to be
  a JSON object with `"type": "object"`; names `^[a-zA-Z0-9_-]{1,64}$` (the intersection of what
  OpenAI and Anthropic accept).
- `ToolCall(id, name, arguments)`: `arguments` is the JSON text, validated as a JSON object
  before it is emitted (section 6, invalid tool-call JSON).
- `ChatRequest(messages, tools, tool_choice, max_tokens, temperature, stream)`;
  `tool_choice` is `auto`, `none`, `required` or one named tool.
- `ChatDelta` (text, or a complete tool call) for streaming; `ChatResult(message,
  finish_reason, usage, usage_estimated)`; `Usage(input_tokens, output_tokens)`.

**The backend interface.** `Backend` gains one method with a default, so `SimulatedBackend` and
`LlamaCppBackend` are untouched:

```python
async def chat(self, request: ChatRequest, *, credential: Secret | None,
               on_delta: ChatDeltaCallback | None) -> ChatResult: raise ChatNotSupported
def capabilities(self) -> Capabilities  # supports_tools, supports_streaming, context_window
```

The credential is an argument of each call, never held by the adapter, never read from the
environment or any global (a test constructs each adapter and asserts it has no attribute that
holds a `Secret`). Each adapter also implements `generate` by wrapping `chat` (one user
message), so it could stand behind a `PassthroughWorker`, but `/v1/generate` keeps using exactly
the backends it uses today.

### The adapters

All three use one `httpx.AsyncClient` per adapter with `trust_env=False` (no proxy from the
environment), `follow_redirects=False`, and the egress transport of section 5 from PR 2 on.

| | Ollama | OpenAI-compatible | Anthropic Messages |
|---|---|---|---|
| Endpoint | `POST {base}/api/chat` | `POST {base}/chat/completions` | `POST {base}/v1/messages` |
| Stream format | NDJSON, `done: true` last | SSE `data:`, `[DONE]` | SSE named events |
| Auth | none (local); headers from config if any | `Authorization: Bearer <key>` plus configured headers | `x-api-key: <key>`, `anthropic-version: 2023-06-01` |
| Tools out | `tools: [{type: function, function: {name, description, parameters}}]` | same | `tools: [{name, description, input_schema}]` |
| Tool calls in | `message.tool_calls[].function{index, name, arguments}`, arguments a JSON **object**, **no id**, sent whole in one chunk when streaming | `delta.tool_calls[]{index, id, function{name, arguments}}`, arguments a JSON **string** in fragments | `content_block_start` (`tool_use`, `id`, `name`), then `input_json_delta.partial_json` fragments |
| Tool results back | `{role: tool, tool_name, content}` | `{role: tool, tool_call_id, content}` | user message with `tool_result` blocks (`tool_use_id`) |
| Usage | `prompt_eval_count`, `eval_count` on the final chunk | `usage` (with `stream_options.include_usage`) | `message_start.usage.input_tokens`, `message_delta.usage.output_tokens` |
| Errors mid-stream | broken line or `error` field | malformed chunk | `event: error` (for example `overloaded_error`) |

Checked against the current documentation on 2026-10-22: Ollama's `/api/chat` and tool-calling
pages, and Anthropic's Messages, streaming and errors pages. Two details that shape the design:

- **Ollama gives tool calls no id.** The adapter assigns each call an id unique to it
  (`call_` and 24 random hex characters, never a counter that could repeat across turns). On
  the next turn each `tool` message is matched to its call by `tool_call_id` within the
  assistant message earlier in the same request, which gives both the call's **position** and
  its **name**; the tool results are sent to Ollama in the calls' order, each with its
  `tool_name`. So the same tool called twice in one turn gets two ids, and each result goes back
  in the place of the call it answers (tested). A `tool` message whose id matches no call is a
  422. Nothing is stored between requests (hard rule 7).
- **Anthropic system prompts are top-level** (`system`), consecutive same-role turns are merged,
  `max_tokens` is required (the gateway sends the application's limit when the request has none),
  and `tool_choice: required` and a named tool map to `{"type": "any"}` and `{"type": "tool"}`,
  which some current models refuse with 400.
- **Anthropic thinking blocks** (approved answer): the adapter asks for thinking off where the
  model allows it (the provider's `thinking` setting, default `{"type": "disabled"}`). Blocks
  are **never dropped silently**: if an answer carries thinking blocks together with a tool
  call, that model needs its thinking blocks sent back with the tool result, which the
  OpenAI-compatible shape cannot carry; the request is answered with 422 `tools_not_supported`
  (reason `thinking_blocks_required`) and the model is recorded `supports_tools: false` with
  that reason. Thinking blocks in an answer without a tool call are not part of the OpenAI
  shape; their count is reported in `X-Thinking-Blocks-Omitted` and in the usage
  (`thinking_blocks_omitted`), so nothing disappears unannounced. No extension field.

### Capability flags

`Capabilities(supports_tools, supports_streaming, context_window)` per configured model, each
with where it came from: `declared` (the provider says so: Ollama's `/api/show` returns
`capabilities` and `model_info.<arch>.context_length`), `probed` (test-connection observed it,
PR 2), `configured` (the operator or organization entered it), or `unknown`. An unknown value is
`null`, never `false` or `0` (the rule monitoring already follows). A request with `tools` to a
model whose `supports_tools` is `false` is refused with 422 `tools_not_supported`; with `null`
it is sent, and a failure is mapped like any other.

### Error mapping

One function maps every upstream outcome; the upstream body is **never** passed on (providers
echo part of a bad key in their 401 messages):

| Upstream | Gateway answer |
|---|---|
| 401, 403 | 502 `provider_auth_failed`, a fixed message |
| 429 | 429 `upstream_rate_limited`, `Retry-After` passed through when it is a whole number of seconds from 1 to 3600, else omitted |
| 5xx (Anthropic's 529 included), connect error | 502 `upstream_unavailable` |
| connect, first-token or total timeout | 502 `upstream_timeout` (which one, in the body) |
| malformed stream, invalid JSON, unknown event order | 502 `upstream_malformed` |
| 400, 404, 413, other 4xx | 502 `upstream_rejected`, with the provider's error `type` only if it is on a fixed list |
| any of these after output has been streamed | an SSE `error` event with the same code, then the stream ends |

**Timeouts**: connect (httpx, 5 s default), first token and total (the injected clock, as
`run_with_timeout` does; defaults 30 s and 120 s, per provider). These defaults, and every other
timeout, budget, cap and limit in this plan, are chosen settings, not measurements. **Cancellation**: when the
client goes away, the response generator is cancelled, which cancels the upstream task and closes
its stream (tested: the mock server records the disconnect). **Usage** comes from the response;
when absent, it is estimated (input from the text length, output from the deltas) and marked
`usage_estimated: true` in the body's usage and in `X-Usage-Estimated: true`.

### `POST /v1/chat/completions`

Authenticated with an application API key, as `/v1/generate` is; the same rate limit, budget and
plan checks. The OpenAI-compatible shape:

- Accepted: `model` (a tier name or alias, as `/v1/generate`; or `quality`), `messages`,
  `tools`, `tool_choice`, `max_tokens`, `temperature` (0 to 2), `stream`,
  `stream_options.include_usage`. **Anything else is a 422** naming the field
  (`unsupported_field`), for example `n`, `logprobs`, `response_format`, `functions`,
  `max_completion_tokens`.
- Non-streaming: `{id, object: "chat.completion", created, model, choices: [{index: 0,
  message, finish_reason}], usage}`. Streaming: `chat.completion.chunk` events with text and
  tool-call deltas, a final chunk with `finish_reason`, usage when asked, `data: [DONE]`.
- Headers: `X-Model-Used` (the tier, as for `/v1/generate`), `X-Provider` (`local`, or the
  provider id from PR 2), `X-Cost-USD` only when the organization entered prices for the
  provider used (absent otherwise: the profiles' placeholder prices are never used for chat,
  hard rule 5), `X-Fallback-From` and `X-Fallback-Reason` when a fallback happened (PR 3).
- Errors use the OpenAI error shape (`{"error": {"message", "type", "code"}}`), so the SDK
  raises its typed exceptions.

**What serves a tier in PR 1**: the gateway's own registry. With simulated models, chat answers
`[simulated output, N tokens]` through the same worker and scheduler as `/v1/generate`, with
the messages flattened to a prompt, and `supports_tools` is `false`; with
`GATEWAY_BACKEND=llamacpp` pointing at Ollama, the Ollama adapter serves each tier through a
`PassthroughWorker`. Usage is charged to the application's budget as for generate.

## 3. Provider settings and the vault (PR 2)

### Data (migrations 10 and 11, both new tables, no rebuild)

| Table | Columns |
|---|---|
| `provider_configs` (10) | `id` (`prv_` + 16 hex), `organization_id`, `type` (`openai_compatible`, `anthropic`: an organization cannot create `ollama`, see section 4), `base_url`, `model`, `serves_tier` (the plan tier it can serve: `economy`, `standard`, `premium`), `role` (`primary`, `fallback`), `enabled`, `disabled_by_platform`, `price_input_per_mtok`, `price_output_per_mtok` (both optional, entered by the organization), `capabilities` (JSON with the source of each flag), `probed_at_ms`, `first_token_budget_ms`, `created_at_ms`. Unique per organization: (`type`, `base_url`, `model`) |
| `provider_keys` (11) | `provider_id` (primary key, references `provider_configs`), `key_id` (which master key), `nonce`, `ciphertext`, `last4`, `created_at_ms`, `updated_at_ms` |

A key is in its own table so **delete removes the row** (ciphertext, nonce and last 4 gone), not
a column set to null. `serves_tier` is how a provider meets the plan rule (section 7).

### The vault (`gateway/vault.py`)

- **Cipher**: AES-256-GCM through `cryptography` (already pinned, 50.0.2, no new runtime
  dependency). A fresh 96-bit random nonce per encryption.
- **Associated data** binds each ciphertext to where it may be used:
  `vault-v1|<organization_id>|<provider_id>|<type>|<base_url origin>|<key_id>`. A ciphertext
  copied to another organization's row, or left behind when the provider's type or base URL is
  changed, **fails to decrypt**. So changing a provider's URL cannot send the old key to the new
  host: it makes the key unusable, and the API deletes it in the same transaction and says so.
- **Master key**: `GATEWAY_VAULT_KEY`, 32 random bytes, base64url. The default server refuses to
  start without it, or with a value that does not decode to 32 bytes (the message names the
  variable, never the value). Its `key_id` is the first 16 hex characters of
  `HMAC-SHA256(master, "gateway vault key id")`: it identifies the key without revealing it.
  Simulated live mode uses a throwaway in-memory master key, as it does a throwaway pepper.
- **Stored**: `key_id`, `nonce`, `ciphertext`, and `last4`: the last 4 characters, only when the
  key has at least 20 characters (otherwise nothing), so a short key never shows a large share
  of itself.
- **The API never returns a key**, only `last4` and `updated_at_ms`, to org_admin. Replace
  overwrites the row; delete removes it.
- **Rotation** (`docs/32`, `manage.py rotate-vault-key`): set the new key as `GATEWAY_VAULT_KEY`
  and the old one as `GATEWAY_VAULT_KEY_PREVIOUS`; the server decrypts with whichever `key_id`
  a row names. The command re-encrypts every row under the new key in one transaction (a backup
  first, as `migrate` does) and reports how many; then the previous variable is removed and the
  server restarted. Tested: rotate, decrypt everything, old key no longer needed.
- **If the master key is lost**, every stored provider key is unrecoverable. That is the design:
  nothing else can decrypt them. The server still starts (one organization's lost key must not
  stop everyone's gateway). A row whose `key_id` matches no available master key is
  **`key_unreadable`**: shown as such in `GET /providers` and `/providers/status`, and logged
  as a warning once per provider at startup and at first use (the provider id and the missing
  `key_id`, nothing else). It is **never tried with another master key**: the vault decrypts a
  row only with the key its `key_id` names, so there is no trial decryption. A provider with an
  unreadable key is **not a candidate**: routing skips it (reason `key_unreadable`, visible in
  the fallback headers if another candidate answers), and skipping it never brings in a
  candidate the effective mode forbids (the candidates are computed from the mode first; an
  unreadable key only removes one). Tested, including with `local_only` and a hosted provider
  whose key is unreadable. The organization's admin re-enters the key.
  `manage.py vault-drop-unreadable` deletes such rows. The backup of the master key is the
  operator's responsibility, separate from the database backup (as for the pepper).

### Where a key is decrypted, and how plaintext is kept out of everything

- **One place decrypts**: `Vault.lease(provider)`, an async context manager called by the
  provider router (PR 2, `gateway/providers.py`) immediately around the adapter call, after
  authorization, routing, the plan check, the mode check, the circuit breaker and the egress
  check of the destination. It yields a `Secret`; on exit the `Secret`'s reference is dropped and
  the lease removed from the redaction layer. Nothing caches plaintext: every request decrypts
  again (AES-GCM; its cost is measured in PR 2, not assumed).
- **One place reveals it**: `Secret.reveal()` is called only by the adapters' header builder
  (`gateway/adapters/http.py`), to put it into that one request's headers. An AST test fails on
  any other call of `.reveal(` in `src/`, and on any import of `Vault` outside the router and the
  admin endpoints.
- **`Secret`** is a frozen class whose `__repr__`, `__str__` and `__format__` return
  `Secret(<redacted>)`, which refuses pickling and comparison by value, and which
  `json.dumps` cannot serialize. So an f-string, a log argument, an exception message, a
  traceback or a test assertion shows `<redacted>`.
- **Upstream bodies are never passed on** (section 2), because providers echo key fragments.
- **httpx and httpcore** log requests at DEBUG without headers; the redaction layer covers them
  anyway.
- **The redaction layer** (`gateway/redaction.py`) is a `logging.Filter` installed on the root
  logger by the server factories. It scrubs (1) the exact values of keys currently leased and of
  keys just submitted to the admin endpoint, and (2) patterns: `Bearer \S+`, `x-api-key: \S+`,
  `sk-[A-Za-z0-9_-]{16,}`, `sk-ant-[A-Za-z0-9_-]{16,}`, and the gateway's own `gwk_<id>_<secret>`.
  It also applies to exception text formatted by the logging module.
- **Honest limit**: Python cannot erase a string from memory. The plaintext lives in the
  `Secret`, the decrypted bytes and the outgoing request's header bytes for the duration of one
  request; nothing keeps a reference after it. A core dump or memory read of the process during
  a request would see it (threat model).
- **The leak scan** (as for API keys and temporary passwords): a key planted through the admin
  endpoint, used for requests against a mock that answers 200, 401 (echoing the key, as real
  providers do) and a broken stream, then searched for in every response, log record, exception
  text, traceback, `repr` of every object involved, audit row, database row other than the
  ciphertext, and captured test output. A planted control (the key logged once on purpose) must
  be found, and the redaction layer is then shown to have scrubbed it from the log handler's
  output.

## 4. Provider endpoints and consume-on-use (PR 2)

Under `/v1/admin/orgs/{org}/providers` (already local only: the `/v1/admin` prefix), each
through `requires()`:

| Method and path | Action | Who | Step-up |
|---|---|---|---|
| `GET /providers` | `providers.list` | org_admin | |
| `GET /providers/status` | `providers.status` | org_admin, platform_admin (only that each provider exists and whether it is enabled; never type, URL, model, prices, key state or last 4) | |
| `POST /providers` | `providers.create` | org_admin | yes |
| `PATCH /providers/{id}` | `providers.update` | org_admin (URL, model, tier, role, prices, budget; a URL or type change deletes the key) | yes |
| `POST /providers/{id}/disable`, `/enable` | `providers.disable` / `providers.enable` | org_admin, platform_admin | yes |
| `DELETE /providers/{id}` | `providers.delete` (and its key) | org_admin | **consume** |
| `PUT /providers/{id}/key` | `provider_keys.set` (add or replace) | org_admin | **consume** |
| `DELETE /providers/{id}/key` | `provider_keys.delete` | org_admin | **consume** |
| `POST /providers/{id}/test` | `providers.test` | org_admin; rate-limited (section 4, test-connection) | |

app_admin, viewer and application-defined roles get nothing here (a test walks every role and
relation through the matrix). platform_admin never adds, replaces or reads a key, and sees no
URL, model, price or `last4`. Missing equals forbidden, as for step 7. A key is accepted only in
the body of `PUT .../key` (`{"key": "..."}`, 8 to 512 printable ASCII characters), answered with
`{"last4", "updated_at_ms"}` and `Cache-Control: no-store` like every admin answer.

### PR 2 as built: details settled while building

Five details the plan left open, settled here so they can be objected to:

1. **What platform_admin sees** is only that a provider exists and whether it is enabled
   (`GET /providers/status`), as restated with the approval; not its type or key state.
2. **Two switches.** A provider has the organization's own `enabled` switch and a separate
   platform switch. `providers.disable` and `providers.enable` act on the switch of whoever
   holds the action (org_admin the organization's, platform_admin the platform's), so an
   org_admin cannot re-enable a provider the platform disabled. A provider is enabled when both
   are on. Which switch is decided from the role and relation `authorize` allowed it with, in
   `requires`, not by the handler.
3. **No redirects at all** for organization providers in this step: the operator allow-list for
   redirects (`GATEWAY_PROVIDER_REDIRECT_HOSTS`) is not built, which is stricter; it can be
   added if a provider is found that needs it.
4. **Where the egress guard applies in this step.** In PR 2 the only path that contacts an
   organization's provider is test-connection (routing chat to organization providers depends
   on the modes of PR 3; the default, `local_only`, never contacts one). The guard is on that
   path. The operator's own adapters keep their configuration as in PR 1 and gain the guard,
   with the operator allow-list, when PR 3 adds the locality rule.
5. **The redaction layer** is installed process-wide by the server factories as a log-record
   factory, so it applies to every logger (uvicorn's included, whose loggers do not propagate)
   before any handler formats a record.

### Permission table changes

The table declared `provider_keys.add|replace|delete|read` and `mode.change` at **application**
level (org_admin and app_admin). This plan makes providers belong to the organization, as you
asked, so those rows are **replaced**: `providers.list|status|create|update|disable|enable|
delete|test`, `provider_keys.set|delete` at organization level, and `mode.set_organization`
(organization level, org_admin, step-up) and `mode.set_application` (application level,
org_admin of its organization and app_admin of the application, step-up). `provider_keys.read`
is removed: no endpoint returns a key. `permissions.py`, note 28's documented table, the
generated matrix and their tests change **together, in one commit**, and PR 2's description
shows the generated matrix's diff.

### Consume-on-use in `requires()`

Note 29 described it; this builds it, with one change for a race note 29 did not cover.

- The table gains a **`consume`** column (the test that pins the table's fields changes on
  purpose). `consume=True` implies `step_up=True`.
- For a consume action, `requires` does not just read the window: it **claims** it with one
  statement, `UPDATE sessions SET step_up_until_ms = NULL WHERE id_hash = ? AND
  step_up_until_ms > ?`, and proceeds only if one row changed. Two requests sent together with
  one window cannot both pass: the second finds it already claimed and gets `step_up_required`.
  (Clearing it after the handler, as note 29 put it, would let two parallel changes share one
  window.)
- `requires` becomes a dependency with `yield` for these actions: if the handler raises (every
  failure path of these handlers raises), the window is **restored** to its claimed value, so a
  failed change does not cost the password. A test pins that FastAPI (0.142.2, the locked
  version) runs this exit before the answer is sent; if an upgrade changes that, the test fails.
- The audit entry of a consume action records `step_up_consumed: true`.
- **The window is given back only when the handler failed before changing anything.** The
  handler receives a token from `requires` and marks it changed in the same transaction that
  writes the key (`token.changed()` just before commit). On an exception the window is restored
  only if the token is unmarked; a failure after the write (the audit row, the answer) keeps the
  window consumed.
- Tests: two key changes sent **simultaneously** with one window produce **exactly one
  success** and one `step_up_required` (both requests in flight together, the order not
  fixed); a second change after the first is refused; a handler that fails before writing gives
  the window back; one that fails after writing does not; a non-consume flagged action leaves
  the window open; the synthetic route for every consume action. Mutations: no claim, claim
  without the time condition, restore always, restore never.

### Test-connection and probing

**It runs only when an org_admin asks for it**, never when a provider or key is saved. The docs
and the endpoint's description say plainly that **it spends tokens on the organization's account
with that provider**. Changing a provider's model resets its probed capabilities to `unknown`
until it is tested again (re-probe on model change); each capability records the time it was
probed and **the model it was probed with**, and a probe of another model is never shown as
this one's.

`POST /providers/{id}/test` decrypts the key (if any) through the lease and sends, through the
egress guard: (1) one non-streaming request, "Reply with the word OK.", `max_tokens` 8; (2) the
same streaming; (3) one tool round trip: a `ping` tool with an empty-object schema and
`tool_choice` auto, then, if a call comes back, a tool result and one more turn with `max_tokens`
8. For Ollama it also reads `/api/show` (declared capabilities and context length). It stores
each capability with its source (`probed`, `declared`) and `probed_at_ms`, and returns only
`{"ok": true, "capabilities": {...}}` or a mapped error code. It never returns the provider's
text or error body. **Rate-limited**: 5 tests per provider per hour, in memory, on the injected
clock (429 `test_rate_limited`). Audited.

**Probed here, for the answer to question 4** (`curl` to `http://127.0.0.1:11434/api/chat` and
`/api/show` on this machine, Ollama 0.12.6, 2026-10-22, one non-streaming request each at
temperature 0 with one `get_weather` tool): `llama3.2:1b-instruct-q4_K_M`, `llama3.2:3b` and
`llama3.1:8b` (the three tiers) each returned one correct `get_weather` call with
`{"city": "Paris"}`. Streaming, `llama3.2:1b-instruct-q4_K_M` sent the call whole in the first
of two chunks, usage on the `done` chunk. `ollama show` declares `tools` for those three and for
`llama3.2:1b`, `qwen2.5` and `qwen2:7b`; not for `llama3`, `llama2`, `codellama` (all three),
`deepseek-coder` or `deepseek-r1` (which declares `thinking`). Declared context length 131072
for the llama3.x models; Ollama's `num_ctx` (what it actually allocates) is not in the model's
parameters and is recorded separately as configured or unknown. One probe each is a capability
check, not a quality measurement.

### SSRF: the egress guard (`gateway/egress.py`)

Every adapter's client uses one `httpcore` network backend whose `connect_tcp(host, port)` is
the single point where a connection is opened. For each connection, redirects included:

1. **Who may point the gateway where.** Provider type `ollama` is **operator-configured
   only** (`backends.yaml`, or `GATEWAY_BACKENDS`), never created by an organization. The
   operator's allow-list `GATEWAY_PROVIDER_LOCAL_ALLOW` (for example `localhost:11434`) applies
   only to the operator's own configuration: it is the only way to reach a loopback or internal
   address and the only way to use plain `http`. **Anything an organization supplies** must be
   `https`, resolve only to public addresses and pass every check below, with no allow-list
   exception: an org_admin cannot point the gateway at loopback, a private network, link-local,
   metadata or the gateway itself, whatever the operator's list says. Tested for every range,
   with mutations (the organization path consulting the operator's list; `http` accepted;
   `ollama` accepted from an organization).
2. The host is resolved through an injected resolver (the system resolver in the server, a fake
   in tests), and **every** returned address is checked; one bad address refuses the request.
3. Refused unless allow-listed: loopback (`127.0.0.0/8`, `::1`), private (`10/8`, `172.16/12`,
   `192.168/16`, `fc00::/7`), link-local (`169.254/16`, `fe80::/10`), carrier-grade NAT
   (`100.64/10`), `0.0.0.0/8`, multicast, reserved and documentation ranges, cloud metadata
   (`169.254.169.254`, `fd00:ec2::254`, `100.100.100.200`), and IPv6 forms that embed IPv4
   (`::ffff:0:0/96`, NAT64 `64:ff9b::/96`, 6to4 `2002::/16`), judged on the embedded address.
4. The socket connects to **the address that was checked**, not to the name again, so DNS
   rebinding between the check and the connect cannot change it. TLS still verifies the
   certificate for the original host name.
5. No redirects by default (`follow_redirects=False`; a 3xx is `upstream_rejected`). An
   operator can allow them per host in `GATEWAY_PROVIDER_REDIRECT_HOSTS`; each hop passes
   through steps 1 to 4 again.
6. No proxy from the environment (`trust_env=False`), so `HTTPS_PROXY` cannot route around it.

The same backend enforces the mode's locality rule in PR 3: one place opens connections, one
place refuses them. Base URLs are also checked when entered (scheme, no userinfo, no fragment,
a host), which gives a quick 422, but the connect-time check is the one that matters.

Tests: each blocked range by literal IP, by name and by IPv6 embedding; DNS rebinding (the fake
resolver answers a public address for the check and a private one afterwards: the connect still
goes to the checked one); a redirect to `169.254.169.254` refused; `http` refused unless
allow-listed; a proxy variable ignored. Mutations: drop one range, check only the first address,
reconnect by name, follow redirects.

### Audit

`provider_created|updated|disabled|enabled|deleted`, `provider_key_set|deleted` and
`provider_tested` (with the outcome code), naming the provider id, its type and the actor;
never the key, never `last4`, never the URL's path or query. Visibility, as decided in identity
plan section 9: org_admin reads them, and so does app_admin (a provider serves every
application of its organization); platform_admin reads only `provider_disabled` and
`provider_enabled`, and sees existence and state through `GET /providers/status`.

## 5. Fallback modes (PR 3)

### Modes, and the one place they are computed

`organizations.mode` and `applications.mode` (migration 12, `ADD COLUMN`, no rebuild):

| Mode | May contact | Order |
|---|---|---|
| `local_only` (default) | local destinations only | local |
| `local_with_hosted_fallback` | local, then hosted | local, then the organization's `fallback` providers for the tier |
| `hosted_primary_with_local_fallback` | hosted, then local | the `primary` providers for the tier, then local |

Strictness: `local_only` > `local_with_hosted_fallback` > `hosted_primary_with_local_fallback`
(the last sends every prompt to a third party). An application may set a mode **at least as
strict** as its organization's; setting a looser one is a 409 `mode_looser_than_organization`.
**`effective_mode(org, app)`** returns the stricter of the two and is the only function that
decides it: if an organization later becomes stricter, its applications follow without their
stored value changing. "Local" means the gateway's own tiers and destinations on the operator's
`GATEWAY_PROVIDER_LOCAL_ALLOW`; everything else is hosted.

**Local-only is enforced in one place**: the egress backend of section 3 refuses any connection
to a non-local destination when the request's effective mode is `local_only` (the mode travels
with the request in a context variable set by the router). A test installs a network backend that
fails the test on any connect attempt to a non-local host, configures hosted providers, makes
every local path fail every way, and asserts the request fails without one such attempt. A
mutation that skips the check makes it fail.

### Triggers, and what never happens

Fallback to the next candidate happens on: a connection error, a 5xx, the first token not
arriving within the provider's budget (`first_token_budget_ms`, default 10 s for hosted, the
local tier's own timeout for local), the circuit breaker being open (the candidate is skipped),
and a tool call whose arguments are not a JSON object (**one retry on the same provider, then
the next candidate**). Not on 401/403 (a configuration problem: answered as
`provider_auth_failed`), 429 (passed through) or 4xx.

- **Never after output has started.** Tool-call deltas are held until the call's arguments
  parse, then emitted; text is emitted as it comes. The first emitted delta closes fallback for
  that request; a failure after it ends the stream with an error event. The same rule holds
  without streaming, so a request is never charged twice for partial output.
- **Never outside the plan.** Candidates are the providers whose `serves_tier` is the tier the
  request was routed to, which the plan already allows; a provider for another tier is never a
  candidate. (Downgrade between tiers stays the existing, separate `allow_downgrade`.)
- **Always visible**: `X-Fallback-From` (the first candidate's provider), `X-Fallback-Reason`
  (`connect_error`, `upstream_5xx`, `first_token_budget`, `circuit_open`, `invalid_tool_json`),
  `X-Provider` (the one that answered); the usage record gains `provider`, `fallback_from`,
  `fallback_reason` and `usage_estimated` (migration 13, `ADD COLUMN`).

### Circuit breaker and the slow-provider warning

Per (organization, provider), in memory, on the injected clock: **closed** to **open** after 5
consecutive failures of the trigger kinds above; open for 30 s, doubling up to 5 minutes while
probes keep failing; **half-open** lets one request through; **closed** again only after 2
consecutive successes (the hysteresis). Each transition is an event on the existing `EventBus`.
A hosted provider that misses its first-token budget 3 times in a row gets a warning in
`GET /providers` and `/providers/status` (`slow_since_ms`, `consecutive_misses`), cleared by a
success within budget. Monitoring still only watches: the breaker lives with the router, not in
`Monitor`.

### Mode changes, and what to tell people

`PUT /v1/admin/orgs/{org}/mode` (`mode.set_organization`) and
`PUT /v1/admin/orgs/{org}/applications/{app}/mode` (`mode.set_application`), through
`requires()` with step-up (not consume), audited with the old and new mode. Note 33 and the
README say, plainly: **a mode other than `local_only` sends prompts, and tool definitions and
results, to a third party the organization chose, under that third party's terms**; the gateway
stores none of it (hard rule 7), but the provider may.

## 6. What does not change

`/v1/generate`, `create_app(registry, clock)` without accounts, the simulated backends and
workers, the performance gate, the budgets and the protected outputs: chat and providers are
installed only alongside applications, and the gate and loadgen run without them. Every
existing test passes unchanged except those that pin the permission table (they change with it,
on purpose, in PR 2).

## 7. How the mode rule meets application plans

A plan says **which tiers** an application may use; a mode says **where** a tier may be served.
They compose, never override: the candidates for a request are the providers serving the
routed tier (plan) that the effective mode allows (mode), in the mode's order. Consequences:

- A plan without `premium` never reaches a hosted premium provider, in any mode.
- In `local_only` an application whose tier has no local backend gets 503 `no_local_provider`,
  never a hosted answer.
- A hosted provider is charged at the organization's prices to the same application budget; the
  plan's token budget counts tokens wherever they were served.

## 8. Dependencies

**None is added.** AES-GCM is in `cryptography` (already pinned).

The OpenAI Python SDK was checked first, as asked, from the package metadata on 2026-10-22:

| Package | Evidence | Verdict |
|---|---|---|
| `openai` 3.26.0 | Apache-2.0; author `OpenAI <support@openai.com>`; project URL `github.com/openai/openai-python`; uploaded 2026-10-06 (two weeks old); wheel sha256 `050597ff71ff4025177405a41fa4019d4f9feae5826645b63b8e35b6e9a3c88b`, sdist `73b470c09cf2171dce930e36af01c6632241adda0120ad84302ad229f9130a5a`; PyPI provenance attestation: published by GitHub `openai/openai-python`, workflow `create-releases.yml`, environment `publish`; the repository's `pyproject.toml` at tag `v3.26.0` declares the same dependencies as the metadata, including `httpx2>=2.12.0, <3` | canonical |
| `httpx2` (2.13.1 is the newest two weeks old; 2.16.0 was uploaded 2026-10-21) | BSD-3-Clause; the metadata names httpx's original author as author and "Pydantic Services Inc." as maintainer; project URL `github.com/pydantic/httpx2`, a repository created 2026-05-11, not a fork, whose README calls it a continuation of HTTPX under Pydantic's stewardship; **no PyPI provenance attestation** (404); `encode/httpx`'s README does not mention it; first release 2026-05-11, 22 releases since, four in the last week | **doubtful** |
| `httpcore2` (required by `httpx2`, the same versions) | no repository under `pydantic` (404); no attestation | **doubtful** |
| `jiter`, `anyio`, `sniffio`, `pydantic`, `typing-extensions` | `jiter` MIT, `github.com/pydantic/jiter`; `anyio` attested from `agronholm/anyio`; the rest already installed for FastAPI | not reached: the chain stops at `httpx2` |

So, as instructed, **nothing is added**: the OpenAI wire format is tested with plain `httpx`
against recorded fixtures. The fixtures are the documented examples of `POST /chat/completions`
("Default", "Streaming", "Functions": request bodies, responses and stream chunks) and the
`required` field lists of the response, chunk, message, tool-call and error schemas, taken from
OpenAI's published API description, `github.com/openai/openai-openapi`, `openapi.json` at commit
`234829e2b634b8fb159df7fcddbffad204173ffd` (2026-10-07; the file's sha256
`dde9abb7f7ca1e260fa05b4828984851e77f35ab89f60d0f7bbc1f44859f4724`), MIT license. Only the
extracted examples and field lists are committed, under `tests/fixtures/openai_wire/`, with the
source, commit, sha256 and the license text, and recorded in `docs/licenses.md` (test data,
not shipped). Tests check that the gateway accepts each documented request and that each answer
and chunk it sends has every required field with the documented types. If the SDK is wanted
later, the evidence above is the starting point, and `httpx2` needs its own review first.

## 9. Live tests

Marked `live_provider`, skipped unless `GATEWAY_LIVE_PROVIDERS=1`, never run by CI. They use
the gateway's own adapters (no SDK). Each
provider's are skipped unless its own variables are set: `GATEWAY_LIVE_OPENAI_BASE_URL`,
`GATEWAY_LIVE_OPENAI_MODEL`, `GATEWAY_LIVE_OPENAI_KEY`; `GATEWAY_LIVE_ANTHROPIC_MODEL`,
`GATEWAY_LIVE_ANTHROPIC_KEY`. They need an account with each provider and a key the operator
supplies in their own environment (this plan never asks for one), and network access.
Per provider they send at most 4 requests: one plain, one streamed, one tool round trip of two
turns; each at most about 60 input tokens, `max_tokens` 16. Their cost is those tokens at the
provider's list price on the day; **no dollar figure is given here because none was measured**.
They can cost money, and the test module says so at the top. The local Ollama tests stay under
the existing `real_models` marker (free, local).

## 10. Measurement

Measured on this machine (hardware recorded, three runs, ranges), in the PR that builds each:
PR 1, a chat request through the gateway to a mock provider compared with the mock directly (the
gateway's own overhead, not a provider's latency); PR 2, a vault decrypt, the egress check with
the resolver, and a provider admin request on the running server; PR 3, routing with fallback
candidates when the first fails at once. Nothing measured against a hosted provider is presented
as a property of this gateway.

## 11. Threat-model additions

For `docs/threat-model.md` (identity plan section 12):

- **Fallback modes send prompts to a third party** the organization chose; the gateway cannot
  control what that provider keeps.
- **The master key is in the process environment**, like the pepper; anyone who can read the
  environment and the database can decrypt every provider key.
- **Plaintext keys are in process memory during a request**; Python cannot erase them.
- **An org_admin can point a provider at any public HTTPS host**, and so send its own
  organization's prompts anywhere; that is their authority, and it is audited.
- **SSRF residue**: allow-listed local hosts are reachable by any organization that knows them;
  the resolver is trusted to answer honestly at the moment it is asked.
- **Test-connection spends the organization's money** (rate-limited, audited).
- **`last4` is shown to org_admins** (only for keys of 20 or more characters).
- **Lost master key**: provider keys are gone and must be re-entered.
- **A compromised provider can return hostile tool calls**; the gateway checks only that the
  arguments are a JSON object. The application runs the tools.

## 12. Questions, answered

1. **Where is each key decrypted?** Only inside `Vault.lease()`, called by the provider router
   immediately around one adapter call, after every check (authorization, plan, mode, breaker,
   egress). Test-connection uses the same lease. Nothing else imports `Vault` (AST test), and
   nothing caches plaintext.
2. **How is plaintext kept out of everything?** The `Secret` wrapper (redacted `repr`, `str`,
   `format`; not serializable), one `reveal()` call site enforced by an AST test, upstream
   bodies never passed on, the redaction filter on the root logger (exact leased values and
   patterns), and the leak scan with a planted control (section 3). The limit: memory during a
   request.
3. **The vault and master-key design**: section 3. AES-256-GCM with the master key from
   `GATEWAY_VAULT_KEY`, associated data binding each ciphertext to its organization, provider,
   type, URL origin and master key id; key id and last 4 stored beside it; rotation with a
   previous key and a re-encrypting command; loss is unrecoverable by design, and the server
   still starts.
4. **Which local Ollama models support tools, and how probing is recorded**: section 4. Probed
   on this machine: the three tier models made a correct tool call (one probe each); declared
   by Ollama for those and `llama3.2:1b`, `qwen2.5`, `qwen2:7b`. Probing records each flag
   with its source (`declared`, `probed`, `configured`, `unknown`) and the time; unknown is
   `null`, never `false`.
5. **What live tests need and cost**: section 9. Accounts and keys the operator supplies in
   their own environment, network access, at most 4 tiny requests per provider; the cost is the
   provider's list price for those tokens, not estimated here.
6. **How consume-on-use fits `requires()`**: section 4. A `consume` column; `requires` claims
   the window with one conditional `UPDATE` before the handler (race-free: two simultaneous
   changes give exactly one success) and gives it back only if the handler failed before
   changing anything, as a `yield` dependency; pinned to the FastAPI version by a test.
7. **How the mode rule interacts with plans**: section 7. Plans choose tiers, modes choose
   where; candidates are their intersection, in the mode's order; fallback never changes tier.
8. **Anthropic thinking blocks** (answered): thinking off where the model allows it; a model
   that needs its thinking blocks sent back and cannot disable them is recorded
   `supports_tools: false` with the reason, and tool requests to it get 422; blocks are never
   dropped silently; no extension field (section 2).
9. **Identity steps 8 and 9** (answered): after A5. Each A5 pull request's note has its own
   threat-model section; step 9 consolidates them into `docs/threat-model.md`.
