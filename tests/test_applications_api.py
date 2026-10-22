"""Routing, billing and budgets per application, through the HTTP API.

These were the multi-tenant tests of docs/09-routing.md; since docs/20 the caller is an
application identified by its API key. Keys, limits and isolation are in test_auth_api.py.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from gateway.app import create_app
from gateway.clock import SimClock
from gateway.continuous import build_continuous_registry
from gateway.cost_model import estimate_cost
from gateway.profiles import PROFILES, Tier
from gateway.routing import Plan, PlansConfig, load_plans
from helpers import application_access, bearer

ALL_MODELS = ("economy", "standard", "premium")
# January 1970: epoch time 0 is where a SimClock's ClockEpoch starts.
JANUARY_1970_MS = 31 * 24 * 60 * 60 * 1000.0
# application -> (plan, allowed models, price multiplier), as in the packaged demo
TENANTS: dict[str, tuple[str, tuple[str, ...], float]] = {
    "initech": ("free", ("economy",), 0.0),
    "globex": ("pro", ("economy", "standard"), 1.0),
    "acme": ("enterprise", ("economy", "standard", "premium"), 0.8),
}
ALIASES = {"economy": "llama-3.2-1b", "standard": "llama-3.2-3b", "premium": "llama-3.1-8b"}

# A plan whose budget two default requests (15 billed tokens each) use up exactly.
TIGHT = PlansConfig(
    plans={
        "tiny": Plan("tiny", ("economy",), 1.0, 30),
        "odd": Plan("odd", ("economy",), 1.0, 20),
        "none": Plan("none", ("economy",), 1.0, 0),
        "roomy": Plan("roomy", ("economy",), 1.0, 1_000_000),
    }
)
TIGHT_APPS = {"small": "tiny", "uneven": "odd", "blocked": "none", "big": "roomy"}


@dataclass
class Gateway:
    client: httpx.AsyncClient
    clock: SimClock
    keys: dict[str, str]
    counter: int = 0

    async def generate(self, app: str | None = None, **body: Any) -> httpx.Response:
        if "prompt" not in body:
            # A fresh ten-word prompt each time, so nothing is served from the prefix cache.
            self.counter += 1
            body["prompt"] = " ".join(f"p{self.counter}w{i}" for i in range(10))
        body.setdefault("max_tokens", 5)
        headers = bearer(self.keys[app]) if app is not None else {}
        return await self.client.post("/v1/generate", json=body, headers=headers)

    async def usage(self, app: str) -> dict[str, Any]:
        response = await self.client.get(
            "/v1/applications/me/usage", headers=bearer(self.keys[app])
        )
        assert response.status_code == 200
        data: dict[str, Any] = response.json()
        return data


@asynccontextmanager
async def _gateway(
    templates: PlansConfig | None, apps: dict[str, str] | None = None
) -> AsyncIterator[Gateway]:
    clock = SimClock()
    registry = build_continuous_registry(clock, seed=7)
    access, keys = (
        (None, {}) if templates is None else application_access(clock, templates, apps=apps)
    )
    transport = httpx.ASGITransport(app=create_app(registry, clock, applications=access))
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        yield Gateway(client, clock, keys)
    await registry.aclose()


@pytest.fixture
async def gateway() -> AsyncIterator[Gateway]:
    async with _gateway(load_plans()) as gw:
        yield gw


@pytest.fixture
async def tight() -> AsyncIterator[Gateway]:
    async with _gateway(TIGHT, TIGHT_APPS) as gw:
        yield gw


def _base_cost(model: str, usage: dict[str, int]) -> float:
    """What the cost model charges for a response's usage, before any plan multiplier."""
    return estimate_cost(
        PROFILES[Tier(model)],
        new_prefill_tokens=usage["prompt_tokens"] - usage["cached_tokens"],
        output_tokens=usage["completion_tokens"],
        batch_size=1,
    ).cost_usd


# --- every plan / model combination --------------------------------------------------------


@pytest.mark.parametrize("tenant", TENANTS)
@pytest.mark.parametrize("model", ALL_MODELS)
@pytest.mark.parametrize("by_alias", [False, True], ids=["id", "alias"])
async def test_a_model_is_served_exactly_when_the_applications_plan_includes_it(
    gateway: Gateway, tenant: str, model: str, by_alias: bool
) -> None:
    plan, allowed, _ = TENANTS[tenant]
    response = await gateway.generate(app=tenant, model=ALIASES[model] if by_alias else model)

    if model in allowed:
        assert response.status_code == 200
        assert response.json()["model"] == model
        assert response.headers["X-Model-Used"] == model
        return

    assert response.status_code == 403
    assert "X-Model-Used" not in response.headers
    error = response.json()["error"]
    assert error["code"] == "model_not_in_plan"
    assert error["plan"] == plan
    assert error["requested_model"] == model
    assert error["allowed_models"] == list(allowed)
    assert error["plans_with_model"] == [p for p, a, _ in TENANTS.values() if model in a]
    assert error["message"].startswith(
        f"Application {tenant!r} is on the {plan!r} plan, which does not include model {model!r}. "
        f"Models on this plan: {', '.join(allowed)}."
    )
    assert "Plans that include" in error["message"]
    # Nothing ran and nothing was charged.
    assert gateway.clock.now_ms() == 0.0
    assert (await gateway.usage(tenant))["total"]["requests"] == 0


@pytest.mark.parametrize(
    ("tenant", "quality", "expected"),
    [
        ("initech", "cheapest", "economy"),
        ("initech", "balanced", "economy"),
        ("initech", "best", "economy"),
        ("globex", "cheapest", "economy"),
        ("globex", "balanced", "standard"),
        ("globex", "best", "standard"),
        ("acme", "cheapest", "economy"),
        ("acme", "balanced", "standard"),
        ("acme", "best", "premium"),
    ],
)
async def test_quality_preference_is_resolved_within_the_applications_plan(
    gateway: Gateway, tenant: str, quality: str, expected: str
) -> None:
    response = await gateway.generate(app=tenant, quality=quality)

    assert response.status_code == 200
    assert response.json()["model"] == expected
    assert response.headers["X-Model-Used"] == expected
    assert list((await gateway.usage(tenant))["by_model"]) == [expected]


# --- request validation --------------------------------------------------------------------


async def test_a_key_is_required_when_applications_are_configured(gateway: Gateway) -> None:
    response = await gateway.generate(model="economy")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "api_key_required"


async def test_tenant_id_in_the_body_is_a_422_even_with_a_valid_key(gateway: Gateway) -> None:
    response = await gateway.generate(app="acme", tenant_id="acme", model="economy")
    assert response.status_code == 422
    [detail] = response.json()["error"]["details"]
    assert detail["field"] == "tenant_id"
    assert "Authorization: Bearer" in detail["message"]
    assert gateway.clock.now_ms() == 0.0


async def test_model_that_does_not_exist_is_a_404_not_a_403(gateway: Gateway) -> None:
    response = await gateway.generate(app="initech", model="gpt-4")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_model"


@pytest.mark.parametrize(
    ("choice", "field_name"),
    [
        ({}, "model"),
        ({"model": "economy", "quality": "best"}, "quality"),
        ({"quality": "fastest"}, "quality"),
        ({"quality": ""}, "quality"),
    ],
    ids=["neither", "both", "unknown-quality", "empty-quality"],
)
async def test_exactly_one_of_model_and_a_valid_quality_is_required(
    gateway: Gateway, choice: dict[str, str], field_name: str
) -> None:
    response = await gateway.generate(app="acme", **choice)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert [d["field"] for d in error["details"]] == [field_name]
    assert gateway.clock.now_ms() == 0.0


# --- headers and billing -------------------------------------------------------------------


async def test_response_headers_report_the_model_used_and_the_billed_cost(
    gateway: Gateway,
) -> None:
    response = await gateway.generate(app="acme", quality="best")
    body = response.json()

    base = _base_cost("premium", body["usage"])
    assert response.headers["X-Model-Used"] == "premium"
    assert response.headers["X-Cost-USD"] == f"{base * 0.8:.10f}"
    assert body["cost"]["usd"] == base * 0.8
    assert float(response.headers["X-Cost-USD"]) == pytest.approx(body["cost"]["usd"])


@pytest.mark.parametrize("tenant", TENANTS)
async def test_each_plans_price_multiplier_is_applied(gateway: Gateway, tenant: str) -> None:
    _, _, multiplier = TENANTS[tenant]
    response = await gateway.generate(app=tenant, model="economy")
    body = response.json()

    base = _base_cost("economy", body["usage"])
    assert base > 0
    assert body["cost"]["usd"] == base * multiplier
    total = (await gateway.usage(tenant))["total"]
    assert (total["base_cost_usd"], total["billed_cost_usd"]) == (base, base * multiplier)


async def test_ledger_matches_the_cost_model_request_by_request(gateway: Gateway) -> None:
    repeated = "the same eleven word prompt is sent twice in a row"
    calls: list[dict[str, Any]] = [
        {"app": "acme", "model": "economy"},
        {"app": "acme", "model": "premium", "prompt": repeated},
        {"app": "acme", "model": "premium", "prompt": repeated},  # served from cache
        {"app": "globex", "model": "standard"},
        {"app": "acme", "quality": "best", "max_tokens": 9},
        {"app": "initech", "quality": "best"},
        {"app": "globex", "model": "llama-3.2-1b", "max_tokens": 3},
    ]
    expected: dict[str, dict[str, Any]] = {
        tenant: {"requests": 0, "prompt": 0, "cached": 0, "completion": 0, "base": 0.0,
                 "billed": 0.0, "by_model": {}}
        for tenant in TENANTS
    }  # fmt: skip

    for call in calls:
        response = await gateway.generate(**call)
        assert response.status_code == 200
        body = response.json()
        tenant = call["app"]
        multiplier = TENANTS[tenant][2]
        base = _base_cost(body["model"], body["usage"])
        # Each response is billed what the cost model says, times the plan's multiplier.
        assert body["cost"]["usd"] == base * multiplier
        tally = expected[tenant]
        tally["requests"] += 1
        tally["prompt"] += body["usage"]["prompt_tokens"]
        tally["cached"] += body["usage"]["cached_tokens"]
        tally["completion"] += body["usage"]["completion_tokens"]
        tally["base"] += base
        tally["billed"] += base * multiplier
        tally["by_model"][body["model"]] = tally["by_model"].get(body["model"], 0) + 1

    for tenant, tally in expected.items():
        usage = await gateway.usage(tenant)
        total = usage["total"]
        assert total["requests"] == tally["requests"]
        assert total["prompt_tokens"] == tally["prompt"]
        assert total["cached_tokens"] == tally["cached"]
        assert total["completion_tokens"] == tally["completion"]
        assert total["billed_tokens"] == tally["prompt"] - tally["cached"] + tally["completion"]
        assert total["base_cost_usd"] == pytest.approx(tally["base"], rel=1e-12)
        assert total["billed_cost_usd"] == pytest.approx(tally["billed"], rel=1e-12)
        assert usage["used_tokens"] == total["billed_tokens"]
        assert {m: u["requests"] for m, u in usage["by_model"].items()} == tally["by_model"]
        # The per-model rows add up to the total.
        for key in ("requests", "prompt_tokens", "cached_tokens", "completion_tokens"):
            assert sum(u[key] for u in usage["by_model"].values()) == total[key]
        assert sum(u["billed_cost_usd"] for u in usage["by_model"].values()) == pytest.approx(
            total["billed_cost_usd"], rel=1e-12
        )

    # The scenario really did exercise the cache, the multipliers and several models.
    assert expected["acme"]["cached"] == 11
    assert expected["acme"]["by_model"] == {"economy": 1, "premium": 3}
    assert expected["initech"]["billed"] == 0.0
    assert expected["initech"]["base"] > 0
    assert expected["globex"]["billed"] == expected["globex"]["base"]


async def test_usage_endpoint_describes_the_plan_and_the_period(gateway: Gateway) -> None:
    await gateway.generate(app="globex", model="standard")
    usage = await gateway.usage("globex")

    assert usage["application_id"] == "globex"
    assert usage["name"] == "Globex"
    assert (usage["max_tokens"], usage["requests_per_minute"]) == (None, None)
    assert usage["plan"] == "pro"
    assert usage["allowed_models"] == ["economy", "standard"]
    assert usage["price_multiplier"] == 1.0
    assert usage["period"] == {"start_ms": 0.0, "end_ms": JANUARY_1970_MS}
    assert usage["budget_tokens"] == 5_000_000
    assert usage["used_tokens"] == 15
    assert usage["remaining_tokens"] == 5_000_000 - 15
    assert usage["total"]["requests"] == 1


async def test_failed_requests_are_not_charged(gateway: Gateway) -> None:
    await gateway.generate(app="initech", model="premium")  # 403
    await gateway.generate(app="initech", model="gpt-4")  # 404
    await gateway.generate(app="initech", model="economy", prompt="")  # 422
    assert (await gateway.usage("initech"))["total"]["requests"] == 0


# --- budgets -------------------------------------------------------------------------------


async def test_requests_are_rejected_with_429_once_the_budget_is_used_up(tight: Gateway) -> None:
    first = await tight.generate(app="small", model="economy")
    second = await tight.generate(app="small", model="economy")
    time_after_two = tight.clock.now_ms()

    third = await tight.generate(app="small", model="economy")

    assert (first.status_code, second.status_code, third.status_code) == (200, 200, 429)
    error = third.json()["error"]
    assert error["code"] == "budget_exhausted"
    assert (error["used_tokens"], error["budget_tokens"]) == (30, 30)
    assert "used 30 of its 30 token budget" in error["message"]
    assert "resets in" in error["message"]
    # To the end of January (calendar months in UTC), less the few milliseconds already
    # elapsed, rounded up to whole seconds.
    assert error["retry_after_s"] == 31 * 24 * 60 * 60
    assert third.headers["Retry-After"] == str(31 * 24 * 60 * 60)
    assert "X-Cost-USD" not in third.headers
    # The rejected request did not run and was not charged.
    assert tight.clock.now_ms() == time_after_two
    usage = await tight.usage("small")
    assert (usage["used_tokens"], usage["remaining_tokens"]) == (30, 0)
    assert usage["total"]["requests"] == 2


async def test_one_applications_exhausted_budget_does_not_affect_another(tight: Gateway) -> None:
    for _ in range(2):
        await tight.generate(app="small", model="economy")
    assert (await tight.generate(app="small", model="economy")).status_code == 429

    assert (await tight.generate(app="big", model="economy")).status_code == 200


async def test_budget_is_available_again_in_the_next_period(tight: Gateway) -> None:
    for _ in range(2):
        await tight.generate(app="small", model="economy")
    assert (await tight.generate(app="small", model="economy")).status_code == 429

    await tight.clock.sleep_ms(JANUARY_1970_MS)  # into February

    assert (await tight.generate(app="small", model="economy")).status_code == 200
    usage = await tight.usage("small")
    assert usage["used_tokens"] == 15
    assert usage["period"]["start_ms"] == JANUARY_1970_MS


async def test_the_request_that_crosses_the_budget_is_served_in_full(tight: Gateway) -> None:
    # Budget 20, requests of 15: the second starts under budget and takes usage to 30.
    first = await tight.generate(app="uneven", model="economy")
    second = await tight.generate(app="uneven", model="economy")
    third = await tight.generate(app="uneven", model="economy")

    assert (first.status_code, second.status_code, third.status_code) == (200, 200, 429)
    usage = await tight.usage("uneven")
    assert (usage["used_tokens"], usage["budget_tokens"], usage["remaining_tokens"]) == (30, 20, 0)


async def test_concurrent_requests_admitted_under_budget_can_all_overshoot(tight: Gateway) -> None:
    # The budget is checked when a request arrives and charged when it finishes, so four
    # requests arriving together all pass the check. This pins the documented soft limit.
    responses = await asyncio.gather(
        *(tight.generate(app="small", model="economy") for _ in range(4))
    )
    assert [r.status_code for r in responses] == [200] * 4
    assert (await tight.usage("small"))["used_tokens"] == 60
    assert (await tight.generate(app="small", model="economy")).status_code == 429


async def test_a_plan_with_no_budget_is_rejected_at_once(tight: Gateway) -> None:
    response = await tight.generate(app="blocked", model="economy")
    assert response.status_code == 429
    assert tight.clock.now_ms() == 0.0


async def test_a_model_outside_the_plan_is_403_even_when_the_budget_is_exhausted(
    tight: Gateway,
) -> None:
    response = await tight.generate(app="blocked", model="premium")
    assert response.status_code == 403


# --- without plans -------------------------------------------------------------------------


async def test_without_applications_any_model_is_served_and_nothing_is_charged() -> None:
    async with _gateway(None) as gateway:
        by_model = await gateway.generate(model="premium")
        by_quality = await gateway.generate(quality="cheapest")
        with_tenant = await gateway.generate(model="economy", tenant_id="whoever")
        usage = await gateway.client.get("/v1/applications/me/usage")

    assert by_model.status_code == 200
    assert by_model.headers["X-Model-Used"] == "premium"
    body = by_model.json()
    assert body["cost"]["usd"] == _base_cost("premium", body["usage"])
    assert by_model.headers["X-Cost-USD"] == f"{body['cost']['usd']:.10f}"
    assert by_quality.headers["X-Model-Used"] == "economy"
    assert with_tenant.status_code == 422
    assert usage.status_code == 404
    assert usage.json()["error"]["code"] == "applications_disabled"
