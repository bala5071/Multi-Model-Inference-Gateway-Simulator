# Operating the plain server

What the [README](../README.md) leaves out: the commands and endpoints an operator uses after
the quick start. Each section names the note with the detail. Everything here is local only
until A8 ([note 25](25-local-only.md)): the server binds `127.0.0.1` and must not be exposed to a
network.

The quick start's `gateway.env` (a pepper, the vault's master key and a first admin) is assumed
throughout.

## Organizations

Applications belong to organizations ([docs/26](26-organizations.md)). The ones above
are in `default`, which every database has; others are created with the CLI. Application
names are unique within an organization, and a disabled organization's keys stop working:

```sh
uv run python scripts/manage.py create-organization --id north --name "North" --kind business
uv run python scripts/manage.py create-application --id north-shop --name "Shop" --plan pro --organization north
uv run python scripts/manage.py list-organizations
uv run python scripts/manage.py organization-usage --organization north
uv run python scripts/manage.py disable-organization --organization north
uv run python scripts/manage.py enable-organization --organization north
```

Upgrading a database from before organizations: stop the server and run
`uv run python scripts/manage.py migrate --dry-run` to see what would change, then
`uv run python scripts/manage.py migrate`, which backs the database up first and prints where.
The server refuses to start until then, rather than rebuild a table without a backup.

## Console accounts, roles, providers, modes and settings

People log in to a console with a username and password ([docs/27](27-users-and-sessions.md)).
There is no sign-up: the first admin comes from `GATEWAY_ADMIN_USERNAME` and
`GATEWAY_ADMIN_PASSWORD` (as in the quickstart's `gateway.env`) when none exists, and must
change that password at its first login; the server refuses to start without an admin.
Further accounts come from the CLI, which asks for the password and never prints it; one the
operator creates must change its password at first login:

```sh
uv run python scripts/manage.py create-user --username alice --organization north
uv run python scripts/manage.py reset-password --username alice   # a temporary one
uv run python scripts/manage.py unlock-user --username alice      # after 5 failed logins
uv run python scripts/manage.py disable-user --username alice     # ends every session
```

Roles decide what an account may do ([docs/28](28-roles.md)): `platform_admin`
(organizations; users in any organization), `org_admin` (its organization), `app_admin` (its
application) and `viewer` (usage, read only). Membership alone grants nothing. Roles are granted
from the CLI; `--as USERNAME` keeps the operator within that user's roles (it can only
narrow what the operator may do), and the audit log records the operator as having acted,
with that user as `acting_as`:

```sh
uv run python scripts/manage.py grant-role --username alice --role org_admin --organization north
uv run python scripts/manage.py list-roles --username alice
```

Upgrading a database from before roles: run `uv run python scripts/manage.py migrate` once before
starting this version (it backs the database up first).

`POST /v1/auth/login` with `{"username", "password"}` sets an `HttpOnly`, `SameSite=Strict`
session cookie and returns a CSRF token, which `POST /v1/auth/logout` and
`POST /v1/auth/change-password` need in `X-CSRF-Token`; `GET /v1/auth/me` and
`GET /v1/auth/sessions` read. Sensitive actions (creating, disabling, enabling, unlocking or
resetting users, application keys) also need a recent password: `POST /v1/auth/step-up`
opens a 5-minute window for that one session ([docs/29](29-step-up.md)).

A session manages users, applications and application keys under
`/v1/admin/orgs/{org}/...`, within its roles ([docs/30](30-management-endpoints.md)).
These endpoints never take a password: creating a user or resetting one returns a temporary
password once, which must be changed at the first login; a new key is returned once too. Roles
and organizations stay in the CLI.

An organization's admin can configure the organization's own model providers (any
OpenAI-compatible endpoint, or Anthropic) and give the gateway their keys, under
`/v1/admin/orgs/{org}/providers` ([docs/32](32-provider-settings-and-vault.md)). Keys are
encrypted with the master key `GATEWAY_VAULT_KEY` (the server refuses to start without it; keep
it apart from the database: if it is lost, every provider key must be entered again), never
returned, and each change needs a fresh password. Provider URLs must be public `https` hosts.
Testing a provider's connection spends tokens on the organization's account with it.

Chat is served by the gateway's own tiers unless the organization **and** the application both
choose a fallback mode ([docs/33](33-fallback-modes.md)): `local_only` (the default),
`local_with_hosted_fallback` or `hosted_primary_with_local_fallback`, set with
`PUT /v1/admin/orgs/{org}/mode` and `PUT .../applications/{app}/mode` (each change needs a
fresh password). **A mode other than `local_only` sends prompts, and tool definitions and
results, to a third party the organization chose, under that third party's terms**; the gateway
stores none of it, but the provider may. An answer already streaming from a hosted provider is
not cut when the mode becomes stricter: its prompt has already been sent (the mode change's
answer says so too). Each answer says where it came from: `X-Provider` (`gateway` for the
gateway's own tier, or the organization provider's id) and `X-Provider-Class: local | hosted`,
which alone says whether it was local, and `X-Fallback-From`/`X-Fallback-Reason` after a
fallback. The usage record says the same (rows stored before PR 20 call the gateway's tier
`local`; see [docs/33](33-fallback-modes.md)). The operator's own model servers count as local
when they are on this machine (loopback in the backends file) or listed in
`GATEWAY_PROVIDER_LOCAL_ALLOW` (`host:port`, comma-separated).

Each application can have settings of its own under its plan's and its organization's caps
(the stricter wins): which tiers it may use, its own model names, a default quality, its rate
limit, budget and `max_tokens`, a `default_max_tokens`, and its timeouts. The operator sets
them with `scripts/manage.py settings-set` ([docs/39](39-settings.md)). When a request
names no `max_tokens`, the application's `default_max_tokens` is applied as if it had, and
every answer of `/v1/generate` and `/v1/chat/completions` says what was applied in
`X-Max-Tokens-Applied` (a number, or `none` when no limit applies).

The admin console and the monitoring are one page, at `/dashboard/` once it is built
([note 47](47-console-settings.md), [note 49](49-console-alerts.md),
[note 50](50-console-integration.md)): an application's settings and its alert rules, the
organization's caps, channels and incidents, and the monitoring. It logs in with the accounts
above, shows each person only the places the gateway says their roles allow (the platform's
admins see the monitoring; an organization's admins its settings, rules, channels and
incidents), and asks for the password again where the gateway does. The old address,
`/dashboard/console.html#...`, opens the same place on the page. In simulated live mode there
are no accounts: the page shows the monitoring, and says so where a login would be.

## Alert channels and email

An organization's admin creates webhook and email channels under
`/v1/admin/orgs/{org}/alert-channels` (step-up on create; [note 46](46-alert-channels.md)). Email
is off until the operator sets `GATEWAY_SMTP_HOST`, `GATEWAY_SMTP_FROM` and
`GATEWAY_ALERT_EMAIL_DOMAINS`; mail then goes over STARTTLS or implicit TLS only, and only to
recipients who entered the code mailed to them. The SMTP password, if the server needs one, is
set with `uv run python scripts/manage.py set-smtp-password`, which asks for it twice and never
prints it ([note 48](48-email-channels.md)).

## Several instances

`GATEWAY_STATE=shared` with `GATEWAY_POSTGRES_URL` and `GATEWAY_COUNTERS=valkey` (with
`GATEWAY_VALKEY_URL`) runs the gateway on Postgres and Valkey, so several instances can serve
together; a store off this machine needs verified TLS, or
`GATEWAY_STATE_PRIVATE_NETWORK=1` on a private network ([note 37](37-budgets-on-postgres.md)).
`uv run python scripts/manage.py migrate-to-postgres --help` copies a SQLite database across and
compares every table before keeping anything ([note 36](36-postgres.md)).

## Upgrading

Stop the server, then `uv run python scripts/manage.py migrate --dry-run` to see what would
change, and `uv run python scripts/manage.py migrate`, which backs the database up first and
prints where. The server refuses to start on a database that needs a migration that rebuilds a
table (4, 8 and 18), rather than rebuild it without a backup. See the
[release notes](release-notes.md).

## Real-model mode

1. Get three models running, either as three llama.cpp servers
   (`scripts/run_local_models.sh start`) or through Ollama
   (`ollama pull llama3.2:1b-instruct-q4_K_M && ollama pull llama3.2:3b && ollama pull llama3.1:8b`).
2. Start the gateway pointing at them, with the pepper and keys of the quickstart above:

   ```sh
   set -a; . ./gateway.env; set +a
   GATEWAY_BACKEND=llamacpp make serve                                # llama-server
   GATEWAY_BACKENDS=examples/backends.ollama.yaml make serve          # Ollama
   ```

Hardware needs, the Llama license, and what changes in this mode are in
[docs/12-real-models.md](12-real-models.md). The model weights are under Meta's Llama
license, which is not an open-source license.

## Try the chat endpoint with Ollama

`POST /v1/chat/completions` ([docs/31](31-chat-and-adapters.md)) serving the gateway's
three tiers through a local Ollama, with tools. In Git Bash (or any bash), from the repository,
with Ollama running (`ollama serve`) and the three tier models pulled (`ollama pull
llama3.2:1b-instruct-q4_K_M && ollama pull llama3.2:3b && ollama pull llama3.1:8b`).

What it needs: the backends file `examples/backends.ollama.yaml` (every tier at
`http://127.0.0.1:11434`, `chat_api: ollama`), named by `GATEWAY_BACKENDS`; a database
(`GATEWAY_DATABASE`, here a throwaway folder from `mktemp -d`, removed by a trap that names
exactly that folder if the shell exits early); the pepper (`GATEWAY_KEY_PEPPER`); the vault's
master key (`GATEWAY_VAULT_KEY`, [docs/32](32-provider-settings-and-vault.md)); and a first
admin (`GATEWAY_ADMIN_USERNAME`, `GATEWAY_ADMIN_PASSWORD`), which the server needs to start. The
secrets are generated into variables, never typed, so they are not in the shell's history, and
never written to a file. The application key is read silently (`read -rs`) into a variable that
is not exported.

```sh
# 1. A throwaway folder for the database, removed on exit by a trap naming exactly it (the
#    path is expanded now, so the trap removes that folder whatever TRY holds later), and
#    secrets generated into variables.
TRY=$(mktemp -d) && trap "rm -rf -- '$TRY'" EXIT
export GATEWAY_DATABASE="$TRY/gateway.db"
export GATEWAY_BACKENDS=examples/backends.ollama.yaml
export GATEWAY_KEY_PEPPER=$(uv run python -c "import secrets; print(secrets.token_urlsafe(32))")
export GATEWAY_VAULT_KEY=$(uv run python -c "import secrets; print(secrets.token_urlsafe(32))")
export GATEWAY_ADMIN_USERNAME=admin
export GATEWAY_ADMIN_PASSWORD=$(uv run python -c "import secrets; print(secrets.token_urlsafe(24))")

# 2. The demo applications, and a key for Acme (enterprise: every tier), read silently.
uv run python scripts/manage.py seed-demo
IFS= read -rs KEY < <(uv run python scripts/manage.py create-key --application acme --label try-it)

# 3. Start the gateway; every tier's chat goes to the local Ollama.
uv run python -m gateway.serve --port 8000 &
SERVER=$!
until curl -sf http://127.0.0.1:8000/healthz > /dev/null; do sleep 1; done

# 4. A plain chat request (economy: llama3.2:1b-instruct-q4_K_M), with the headers.
curl -s -D - -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model": "economy", "messages": [{"role": "user", "content": "Say hello in five words."}], "max_tokens": 16}'

# 5. Streamed: text arrives chunk by chunk, then the usage, then data: [DONE].
curl -s -N -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model": "economy", "messages": [{"role": "user", "content": "Count to five."}], "max_tokens": 16, "stream": true, "stream_options": {"include_usage": true}}'

# 6. A tool with tool_choice auto: the answer is a tool call, finish_reason "tool_calls".
curl -s -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model": "economy", "messages": [{"role": "user", "content": "What is the weather in Paris? Use the tool."}], "tools": [{"type": "function", "function": {"name": "get_weather", "description": "Current weather for a city", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}], "tool_choice": "auto"}'

# 7. An unsupported field: 422 unsupported_field, naming it.
curl -s -w '\nHTTP %{http_code}\n' -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model": "economy", "messages": [{"role": "user", "content": "hi"}], "n": 2}'

# 8. A wrong key: 401 invalid_api_key, the same answer for every bad key.
curl -s -w '\nHTTP %{http_code}\n' -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer gwk_0000000000000000_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" \
  -H "Content-Type: application/json" \
  -d '{"model": "economy", "messages": [{"role": "user", "content": "hi"}]}'

# 9. tool_choice required: Ollama cannot be made to call a tool, so 422 tools_not_supported
#    (tool_choice_not_supported), before anything is sent to Ollama.
curl -s -w '\nHTTP %{http_code}\n' -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model": "economy", "messages": [{"role": "user", "content": "Weather in Paris?"}], "tools": [{"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}], "tool_choice": "required"}'

# 10. Clean up: stop the gateway, forget the secrets, remove the folder. Nothing is left.
kill $SERVER; wait $SERVER 2> /dev/null || true
unset KEY SERVER GATEWAY_KEY_PEPPER GATEWAY_VAULT_KEY GATEWAY_ADMIN_USERNAME GATEWAY_ADMIN_PASSWORD GATEWAY_DATABASE GATEWAY_BACKENDS
rm -rf -- "$TRY"; trap - EXIT; unset TRY
```

What to look at, from a run on this machine (Ollama 0.12.6; the text is the model's and varies):

- **4**: `HTTP/1.1 200 OK`, `x-model-used: economy`, `x-provider: gateway` (the gateway's own
  tier), `x-provider-class: local`, and
  `"finish_reason":"stop"` with `"usage":{"prompt_tokens":31,"completion_tokens":7,...}` (the
  counts are Ollama's; `x-usage-estimated: true` would say they were estimated). No
  `x-cost-usd`: chat is priced only with prices an organization enters.
- **5**: a first chunk `{"role": "assistant", "content": ""}`, then one chunk per piece of text,
  a chunk with `"finish_reason": "stop"`, one with `"choices": []` and the usage, then
  `data: [DONE]`.
- **6**: `"content":null`, `"tool_calls":[{"id":"call_...","type":"function","function":
  {"name":"get_weather","arguments":"{\"city\": \"Paris\"}"}}]`, `"finish_reason":"tool_calls"`.
  The id is the gateway's: Ollama gives none.
- **7**: `{"error":{"message":"The field 'n' is not supported by this gateway.",
  "type":"invalid_request_error","param":"n","code":"unsupported_field"}}`, `HTTP 422`.
- **8**: `{"error":{"message":"The API key is not valid.","type":"authentication_error",
  "param":null,"code":"invalid_api_key"}}`, `HTTP 401`.
- **9**: `{"error":{"message":"This model cannot be used with tools here.
  (tool_choice_not_supported)",...,"code":"tools_not_supported"}}`, `HTTP 422`.

If Ollama is not running, steps 4 to 6 answer `502` with `upstream_unavailable` (`connect`).
Wire compatibility was checked against OpenAI's published API description and its recorded
examples; no client library has been run against the endpoint
([docs/31](31-chat-and-adapters.md)).

## Curl demo

With the server running and `GATEWAY_API_KEY` set as in the [quick start](../README.md#quick-start)
(simulator mode shown). Every request carries an application's key; there is no
`tenant_id` (sending one is a 422). Each application has its own keys.

```sh
KEY="Authorization: Bearer $GATEWAY_API_KEY"
INITECH_KEY=$(uv run python scripts/manage.py create-key --application initech --label demo)

# The best model the application's plan includes, with the model used and the cost.
curl -s -i -X POST http://127.0.0.1:8000/v1/generate -H "$KEY" -H "Content-Type: application/json" \
  -d '{"quality": "best", "prompt": "What is a radix tree?", "max_tokens": 32}'
# HTTP/1.1 200 OK
# x-model-used: premium
# x-cost-usd: ...

# A model outside the plan (with a key of Initech, on the free plan).
curl -s -X POST http://127.0.0.1:8000/v1/generate -H "Authorization: Bearer $INITECH_KEY" \
  -H "Content-Type: application/json" -d '{"model": "premium", "prompt": "hello"}'
# {"error": {"code": "model_not_in_plan", "message": "Application 'initech' is on the 'free'
#   plan, which does not include model 'premium'. Models on this plan: economy. Plans that
#   include 'premium': enterprise.", ...}}

# No key, or a bad one: 401. Every bad key gets the same answer.
curl -s -X POST http://127.0.0.1:8000/v1/generate -H "Content-Type: application/json" \
  -d '{"model": "economy", "prompt": "hello"}'
# {"error": {"code": "api_key_required", ...}}

# Streaming, one event per token.
curl -s -N -X POST http://127.0.0.1:8000/v1/generate -H "$KEY" -H "Content-Type: application/json" \
  -d '{"model": "economy", "prompt": "hello", "max_tokens": 4, "stream": true}'

# The same tiers in the OpenAI chat format (docs/31): an OpenAI-compatible client can use
# http://127.0.0.1:8000/v1 as its base URL with the key. Tools need a tier served by a real model.
curl -s -X POST http://127.0.0.1:8000/v1/chat/completions -H "$KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "economy", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 8}'

# What this application has used this month, and the gateway's own view of latency and load.
curl -s http://127.0.0.1:8000/v1/applications/me/usage -H "$KEY"
curl -s http://127.0.0.1:8000/metrics/summary
curl -s http://127.0.0.1:8000/v1/overload

# For a dashboard: everything now with the last minute per second, the same as a live
# stream, and Prometheus text. Schema in docs/15-monitoring-api.md. Not authenticated yet:
# see the README's "What it is not".
curl -s "http://127.0.0.1:8000/v1/monitor/snapshot?history_s=60"
curl -s -N http://127.0.0.1:8000/v1/monitor/stream
curl -s http://127.0.0.1:8000/metrics
```

In simulator mode the `text` field is a placeholder; in real-model mode it is the model's
answer. The full API is in [docs/06-api.md](06-api.md) and the notes after it; keys and
applications in [docs/20](20-applications-api-keys.md).
