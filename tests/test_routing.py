from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gateway.clock import SimClock
from gateway.continuous import build_continuous_registry
from gateway.registry import ModelRegistry, UnknownModelError
from gateway.routing import (
    ModelNotAllowedError,
    Plan,
    PlansConfig,
    PlansConfigError,
    Quality,
    Router,
    UnknownTenantError,
    load_plans,
    parse_plans,
    pick_by_quality,
)

ALL_MODELS = ("economy", "standard", "premium")
# plan -> (tenant on that plan in the packaged file, models the plan allows)
PLANS = {
    "free": ("initech", ("economy",)),
    "pro": ("globex", ("economy", "standard")),
    "enterprise": ("acme", ("economy", "standard", "premium")),
}
ALIASES = {"economy": "llama-3.2-1b", "standard": "llama-3.2-3b", "premium": "llama-3.1-8b"}


def _registry() -> ModelRegistry:
    return build_continuous_registry(SimClock(), seed=7)


def _router() -> Router:
    return Router(load_plans(), _registry())


def _valid_document() -> dict[str, Any]:
    return {
        "plans": {
            "basic": {"models": ["economy"], "price_multiplier": 1, "monthly_token_budget": 10}
        },
        "tenants": {"t1": "basic"},
    }


# --- the packaged plans --------------------------------------------------------------------


def test_packaged_plans_match_the_product_tiers() -> None:
    config = load_plans()

    assert {name: plan.models for name, plan in config.plans.items()} == {
        "free": ("economy",),
        "pro": ("economy", "standard"),
        "enterprise": ("economy", "standard", "premium"),
    }
    for plan in config.plans.values():
        assert plan.price_multiplier >= 0
        assert plan.monthly_token_budget > 0
    budgets = [config.plans[p].monthly_token_budget for p in ("free", "pro", "enterprise")]
    assert budgets == sorted(budgets)
    assert dict(config.tenants) == {"initech": "free", "globex": "pro", "acme": "enterprise"}


# --- every plan / model combination --------------------------------------------------------


@pytest.mark.parametrize("plan", PLANS)
@pytest.mark.parametrize("model", ALL_MODELS)
@pytest.mark.parametrize("by_alias", [False, True], ids=["id", "alias"])
def test_requesting_a_model_is_allowed_exactly_when_the_plan_includes_it(
    plan: str, model: str, by_alias: bool
) -> None:
    tenant, allowed = PLANS[plan]
    requested = ALIASES[model] if by_alias else model
    router = _router()

    if model in allowed:
        route = router.route(tenant, model=requested)
        assert (route.tenant_id, route.plan.name, route.model) == (tenant, plan, model)
        return

    with pytest.raises(ModelNotAllowedError) as excinfo:
        router.route(tenant, model=requested)
    error = excinfo.value
    assert (error.tenant_id, error.plan.name, error.model) == (tenant, plan, model)
    assert error.plans_with_model == tuple(p for p, (_, models) in PLANS.items() if model in models)
    message = str(error)
    assert f"on the {plan!r} plan" in message
    assert f"does not include model {model!r}" in message
    assert f"Models on this plan: {', '.join(allowed)}." in message
    assert "enterprise" in message  # every model is on enterprise, so it is always suggested


@pytest.mark.parametrize(
    ("plan", "quality", "expected"),
    [
        ("free", "cheapest", "economy"),
        ("free", "balanced", "economy"),
        ("free", "best", "economy"),
        ("pro", "cheapest", "economy"),
        ("pro", "balanced", "standard"),
        ("pro", "best", "standard"),
        ("enterprise", "cheapest", "economy"),
        ("enterprise", "balanced", "standard"),
        ("enterprise", "best", "premium"),
    ],
)
def test_quality_preference_picks_within_the_plan(plan: str, quality: str, expected: str) -> None:
    tenant, allowed = PLANS[plan]
    route = _router().route(tenant, quality=Quality(quality))
    assert route.model == expected
    assert route.model in allowed
    assert route.plan.name == plan


@pytest.mark.parametrize(
    ("models", "cheapest", "balanced", "best"),
    [
        (["a"], "a", "a", "a"),
        (["a", "b"], "a", "b", "b"),
        (["a", "b", "c"], "a", "b", "c"),
        (["a", "b", "c", "d"], "a", "c", "d"),
    ],
)
def test_pick_by_quality(models: list[str], cheapest: str, balanced: str, best: str) -> None:
    assert pick_by_quality(models, Quality.CHEAPEST) == cheapest
    assert pick_by_quality(models, Quality.BALANCED) == balanced
    assert pick_by_quality(models, Quality.BEST) == best


def test_pick_by_quality_needs_at_least_one_model() -> None:
    with pytest.raises(ValueError, match="no models"):
        pick_by_quality([], Quality.BEST)


# --- other routing errors ------------------------------------------------------------------


def test_unknown_tenant_is_rejected() -> None:
    router = _router()
    with pytest.raises(UnknownTenantError) as excinfo:
        router.route("nobody", model="economy")
    assert excinfo.value.tenant_id == "nobody"
    with pytest.raises(UnknownTenantError):
        router.plan_for("nobody")


def test_model_that_does_not_exist_is_unknown_not_forbidden() -> None:
    with pytest.raises(UnknownModelError):
        _router().route("acme", model="gpt-4")


@pytest.mark.parametrize(
    "kwargs", [{}, {"model": "economy", "quality": Quality.BEST}], ids=["neither", "both"]
)
def test_route_needs_exactly_one_of_model_and_quality(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        _router().route("acme", **kwargs)


def test_router_rejects_a_plan_naming_a_model_that_is_not_registered() -> None:
    def config(model: str) -> PlansConfig:
        return PlansConfig({"p": Plan("p", (model,), 1.0, 100)}, {"t": "p"})

    with pytest.raises(PlansConfigError, match="not a registered model id"):
        Router(config("gpt-4"), _registry())
    # Plans must use registered ids, not aliases, so plan checks compare like with like.
    with pytest.raises(PlansConfigError, match="not a registered model id"):
        Router(config("llama-3.2-1b"), _registry())


# --- parsing -------------------------------------------------------------------------------


def test_parse_plans_reads_a_valid_document() -> None:
    config = parse_plans(_valid_document())
    assert config.plans == {"basic": Plan("basic", ("economy",), 1.0, 10)}
    assert config.tenants == {"t1": "basic"}


def test_tenants_section_is_optional() -> None:
    document = _valid_document()
    del document["tenants"]
    assert parse_plans(document).tenants == {}


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("plans",), {}, "non-empty mapping"),
        (("plans",), ["basic"], "non-empty mapping"),
        (("plans", "basic"), "economy", "must be a mapping"),
        (("plans", "basic", "models"), [], "non-empty list"),
        (("plans", "basic", "models"), "economy", "non-empty list"),
        (("plans", "basic", "models"), ["economy", 3], "non-empty list"),
        (("plans", "basic", "models"), ["economy", "economy"], "twice"),
        (("plans", "basic", "price_multiplier"), "cheap", "must be a number"),
        (("plans", "basic", "price_multiplier"), True, "must be a number"),
        (("plans", "basic", "price_multiplier"), -0.5, "must not be negative"),
        (("plans", "basic", "monthly_token_budget"), -1, "non-negative integer"),
        (("plans", "basic", "monthly_token_budget"), 1.5, "non-negative integer"),
        (("plans", "basic", "monthly_token_budget"), None, "non-negative integer"),
        (("tenants",), ["t1"], "mapping of tenant id"),
        (("tenants", "t1"), "platinum", "unknown plan 'platinum'"),
    ],
)
def test_parse_plans_rejects_bad_documents(path: tuple[str, ...], value: Any, message: str) -> None:
    document = _valid_document()
    target = document
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    with pytest.raises(PlansConfigError, match=message):
        parse_plans(document)


@pytest.mark.parametrize("document", [None, [], "plans", 3])
def test_parse_plans_rejects_a_document_that_is_not_a_mapping(document: Any) -> None:
    with pytest.raises(PlansConfigError, match="must be a mapping"):
        parse_plans(document)


def test_load_plans_reads_a_yaml_file(tmp_path: Path) -> None:
    path = tmp_path / "plans.yaml"
    path.write_text(
        "plans:\n"
        "  solo:\n"
        "    models: [standard]\n"
        "    price_multiplier: 1.25\n"
        "    monthly_token_budget: 5000\n"
        "tenants:\n"
        "  t9: solo\n",
        encoding="utf-8",
    )
    config = load_plans(path)
    assert config.plans == {"solo": Plan("solo", ("standard",), 1.25, 5000)}
    assert config.tenants == {"t9": "solo"}


def test_load_plans_reports_invalid_yaml(tmp_path: Path) -> None:
    path = tmp_path / "plans.yaml"
    path.write_text("plans: [unclosed\n", encoding="utf-8")
    with pytest.raises(PlansConfigError, match="not valid YAML"):
        load_plans(path)
