# 09 — Multi-tenant, cost-aware routing

This step adds tenants, plans, a router that keeps each tenant inside its plan, a ledger of
what each tenant has used, and budget enforcement.

Code: `src/gateway/routing.py`, `src/gateway/ledger.py`, `src/gateway/plans.yaml`, changes
in `src/gateway/app.py`. Tests: `tests/test_routing.py`, `tests/test_ledger.py`,
`tests/test_tenancy_api.py`.

## Why this exists

A company selling model access to business clients does not have one customer; it has
customers at different price points. A startup on a free tier, a mid-size team paying a
flat rate and an enterprise with a negotiated contract should not all get the same thing,
because serving them costs very different amounts: on this project's illustrative profiles
the premium model costs ten times the economy model per token and is far slower.

Plans are how that difference is sold. A cheaper plan is served by cheaper models, which is
what makes the low price sustainable; a more expensive plan buys access to better models,
a larger budget and, here, a volume discount. The gateway is the place to enforce it,
because it is the one component every request passes through:

- **Entitlement:** a tenant gets the models its plan pays for and no others.
- **Choice without knowledge:** a client can ask for "cheapest", "balanced" or "best" and
  get the right model for its plan, without knowing which models exist. The provider can
  then change the models behind a plan without clients changing their code.
- **Metering:** every request is charged to the tenant that made it, which is the basis for
  invoices and for seeing which customers cost more than they pay.
- **Limits:** a budget stops one customer from running up unbounded cost.

All prices, multipliers and budgets in this repo are illustrative.

## Plans

The plans file is YAML. The default is packaged with the gateway
(`src/gateway/plans.yaml`); `GATEWAY_PLANS=/path/to/plans.yaml` overrides it for the
default server.

```yaml
plans:
  free:
    models: [economy]
    price_multiplier: 0.0
    monthly_token_budget: 100000
  pro:
    models: [economy, standard]
    price_multiplier: 1.0
    monthly_token_budget: 5000000
  enterprise:
    models: [economy, standard, premium]
    price_multiplier: 0.8
    monthly_token_budget: 100000000

tenants:
  initech: free
  globex: pro
  acme: enterprise
```

| Field                  | Meaning                                                              |
|------------------------|----------------------------------------------------------------------|
| `models`               | Registered model ids the plan may use, **cheapest first**.           |
| `price_multiplier`     | The tenant is billed the model's base cost times this.               |
| `monthly_token_budget` | Billed tokens allowed per budget period.                             |
| `tenants`              | Tenant id to plan name.                                              |

The file is validated when it is loaded, and the router checks at startup that every model
a plan lists is a registered model id (aliases are not accepted in plans). A bad file stops
the server from starting with a message naming the problem.

In the default file the free plan is billed nothing and limited by its token budget, pro
pays the base price, and enterprise gets a 20% discount.

## Routing

A generate request names **either** a `model` **or** a `quality`, never both:

```sh
curl -s -i -X POST http://127.0.0.1:8000/v1/generate -H "Content-Type: application/json" \
  -d '{"tenant_id": "acme", "quality": "best", "prompt": "Summarise the attached contract in three bullet points.", "max_tokens": 16}'
```

```
HTTP/1.1 200 OK
x-model-used: premium
x-cost-usd: 0.0000320000

{"id":"req-1","text":"[simulated output, 16 tokens]","model":"premium","usage":{...},"timing":{...},"cost":{"usd":0.000032000000000000005}}
```

**By model.** The name (id or alias) is resolved, then checked against the plan:

| Plan       | economy | standard | premium |
|------------|---------|----------|---------|
| free       | 200     | 403      | 403     |
| pro        | 200     | 200      | 403     |
| enterprise | 200     | 200      | 200     |

**By quality.** The router picks from the plan's own list: `cheapest` is the first entry,
`best` the last, `balanced` the middle one (the upper middle when the count is even).

| Plan       | cheapest | balanced | best     |
|------------|----------|----------|----------|
| free       | economy  | economy  | economy  |
| pro        | economy  | standard | standard |
| enterprise | economy  | standard | premium  |

A quality request can never be refused for the model it picks; that is the point of it.
Both tables are tested cell by cell, at the router and through HTTP, by id and by alias.

### Response headers

Every successful generate response carries:

- `X-Model-Used`: the registered id of the model that served the request;
- `X-Cost-USD`: what the tenant was billed, with ten decimal places. The body's `cost.usd`
  is the same amount at full precision.

## The ledger and the usage endpoint

`CostLedger` records every successful request against its tenant: requests, prompt tokens,
cached tokens, completion tokens, the base cost from the cost model and the billed cost
(base times the plan's multiplier), in total and per model.

```sh
curl -s http://127.0.0.1:8000/v1/tenants/acme/usage
```

```json
{
  "tenant_id": "acme",
  "plan": "enterprise",
  "allowed_models": ["economy", "standard", "premium"],
  "price_multiplier": 0.8,
  "period": {"start_ms": 514633609.0, "end_ms": 3106633609.0},
  "budget_tokens": 100000000,
  "used_tokens": 24,
  "remaining_tokens": 99999976,
  "total": {"requests": 1, "prompt_tokens": 8, "cached_tokens": 0, "completion_tokens": 16,
            "billed_tokens": 24, "base_cost_usd": 4e-05, "billed_cost_usd": 3.2000000000000005e-05},
  "by_model": {"premium": {"requests": 1, "prompt_tokens": 8, "cached_tokens": 0,
               "completion_tokens": 16, "billed_tokens": 24, "base_cost_usd": 4e-05,
               "billed_cost_usd": 3.2000000000000005e-05}}
}
```

- **Billed tokens** are uncached prompt tokens plus completion tokens: the same tokens the
  cost model charges for. Prompt tokens served from the prefix cache are counted in
  `prompt_tokens` and `cached_tokens` but are neither billed nor counted against the budget.
- **Accuracy.** `test_ledger_matches_the_cost_model_request_by_request` sends a mix of
  requests across all three tenants, including a cache hit, and checks every response and
  the final ledger against `estimate_cost` for that model, times the plan's multiplier.
- Failed requests (403, 404, 422, 429) are not charged.

## Budgets

Before a request runs, the ledger checks the tenant's billed tokens for the current period
against the plan's `monthly_token_budget`. If none is left the request is rejected:

```
HTTP/1.1 429 Too Many Requests
Retry-After: 2592000

{"error": {"code": "budget_exhausted",
           "message": "Tenant 'small' has used 30 of its 30 token budget for this period. The budget resets in 2592000 seconds.",
           "used_tokens": 30, "budget_tokens": 30, "retry_after_s": 2592000}}
```

(The values shown are those asserted in
`test_requests_are_rejected_with_429_once_the_budget_is_used_up`.)

- **429, not 402.** The budget is a quota that resets by itself, and `Retry-After` tells
  the client when; that is what 429 is for and clients already handle it. 402 would suit a
  model where the tenant must pay to continue, which this gateway does not have.
- **The limit is soft.** The check happens when a request arrives and the charge when it
  finishes, because the output length is not known in advance. The request that crosses the
  budget is served in full, and several requests arriving together while the tenant is
  under budget are all served. Two tests pin both cases. A hard limit would need to reserve
  `max_tokens` up front.
- **A period is 30 days on the gateway's clock**, counted from when the server started. It
  is not a calendar month and does not line up across restarts. Usage starts from zero in
  each period and earlier periods are not kept.
- **The ledger is in memory.** A restart forgets all usage. A real deployment needs a
  durable store, and a shared one if there is more than one gateway process.
- A model outside the plan is a 403 even if the budget is also exhausted: the plan check
  comes first.

## Errors added in this step

| Status | `code`              | When                                               | Extra fields |
|--------|---------------------|----------------------------------------------------|--------------|
| 401    | `tenant_required`   | Plans are configured and the request has no `tenant_id`. | none   |
| 403    | `model_not_in_plan` | The model exists but the tenant's plan does not include it. | `plan`, `requested_model`, `allowed_models`, `plans_with_model` |
| 404    | `unknown_tenant`    | The `tenant_id` is not in the plans file.          | none         |
| 404    | `tenancy_disabled`  | The usage endpoint on a gateway without plans.     | none         |
| 422    | `invalid_request`   | Neither or both of `model` and `quality`, or an unknown quality. | `details`, `valid_models` |
| 429    | `budget_exhausted`  | The tenant's budget for the period is used up.     | `used_tokens`, `budget_tokens`, `retry_after_s`; `Retry-After` header |

The 403 message says what to do next:

```sh
curl -s -X POST http://127.0.0.1:8000/v1/generate -H "Content-Type: application/json" \
  -d '{"tenant_id": "initech", "model": "premium", "prompt": "hello there"}'
```

```json
{"error": {"code": "model_not_in_plan",
           "message": "Tenant 'initech' is on the 'free' plan, which does not include model 'premium'. Models on this plan: economy. Plans that include 'premium': enterprise.",
           "plan": "free", "requested_model": "premium",
           "allowed_models": ["economy"], "plans_with_model": ["enterprise"]}}
```

A model that does not exist at all is still a 404 `unknown_model`, for every tenant.

The curl outputs in this note that are not attributed to a test were copied from one local
run of the default server (`uvicorn`, Windows 11, Python 3.12; recorded on the
working tree before it was committed; first committed in full as `c7e75ac`), reformatted for
reading. Under `WallClock` the `period` values are readings of a monotonic clock, so only their difference is meaningful.

## What changed for existing callers

- **The default server now requires a `tenant_id`.** `create_default_app` loads the plans,
  so a request without a tenant gets 401. The demo tenants are `initech`, `globex`, `acme`.
- **`create_app(registry, clock)` without `plans` behaves as before**: no tenants, any
  model, nothing charged. It now also accepts `quality`, choosing among all registered
  models in registration order, and sets the two headers. The load generator's in-process
  mode and the performance gate use this form, so their numbers are unchanged.
- **`model` is no longer required in the request body**, since `quality` can replace it.
- **Live load runs need a tenant:** `python -m benchmarks.loadgen --url ... --tenant acme`.

## Design decisions

- **The plan's model list is the ordering.** "Cheapest first" is stated by whoever writes
  the plans file, not derived from prices. The router does not look inside workers, and the
  operator may want an order that is not strictly by price.
- **`tenant_id` in the body is a claim, not proof.** There is no authentication: anyone can
  send any tenant id and read any tenant's usage. A real deployment would derive the tenant
  from an API key and restrict the usage endpoint.
- **Plan check, then budget check, then run.** Rejected requests take no simulated time.
- **The multiplier applies to the whole base cost**, input and output alike.
- **Routing adds no latency in the model.** It is a dictionary lookup; the simulator does
  not charge time for it.
- **Tenants are isolated by budget, not by scheduling.** Two tenants on the same model share
  that model's queue; one can still delay the other. Step 7 covers isolation between
  models; fairness between tenants within a model is not addressed.
- **PyYAML** (`yaml.safe_load`) parses the plans file. It is the one new runtime dependency.
