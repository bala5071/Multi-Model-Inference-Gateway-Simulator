from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gateway.applications import Application, application_from_template
from gateway.clock import SimClock
from gateway.continuous import build_continuous_registry
from gateway.registry import ModelRegistry, UnknownModelError
from gateway.routing import (
    DemoApplication,
    ModelNotAllowedError,
    Plan,
    PlanLimits,
    PlansConfig,
    PlansConfigError,
    Quality,
    Router,
    load_plans,
    parse_plans,
    pick_by_quality,
)

ALL_MODELS = ("economy", "standard", "premium")
# plan -> (demo application on that plan in the packaged file, models the plan allows)
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


def _app(app_id: str) -> Application:
    """A demo application, created from its template in the packaged plans."""
    templates = load_plans()
    demo = templates.applications[app_id]
    return application_from_template(
        app_id, demo.name, templates, demo.plan, organization_id="default", created_at_ms=0.0
    )


def _valid_document() -> dict[str, Any]:
    return {
        "plans": {
            "basic": {"models": ["economy"], "price_multiplier": 1, "monthly_token_budget": 10}
        },
        "applications": {"a1": {"name": "App One", "plan": "basic"}},
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
    assert dict(config.applications) == {
        "initech": DemoApplication("Initech", "free"),
        "globex": DemoApplication("Globex", "pro"),
        "acme": DemoApplication("Acme", "enterprise"),
    }
    # No limits in the packaged plans: the demo runs as it did before limits existed.
    assert dict(config.limits) == {}


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
        route = router.route(_app(tenant), model=requested)
        assert (route.application_id, route.plan.name, route.model) == (tenant, plan, model)
        return

    with pytest.raises(ModelNotAllowedError) as excinfo:
        router.route(_app(tenant), model=requested)
    error = excinfo.value
    assert (error.application_id, error.plan.name, error.model) == (tenant, plan, model)
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
    route = _router().route(_app(tenant), quality=Quality(quality))
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


def test_the_router_uses_the_applications_own_plan_not_the_template() -> None:
    # An application keeps the settings it was created with, even if the template changes.
    custom = Application(
        "custom", "Custom", Plan("enterprise", ("standard",), 1.0, 10), None, None, 0.0, "default"
    )
    assert _router().route(custom, quality=Quality.BEST).model == "standard"
    with pytest.raises(ModelNotAllowedError) as excinfo:
        _router().route(custom, model="premium")
    assert excinfo.value.plan.models == ("standard",)


def test_model_that_does_not_exist_is_unknown_not_forbidden() -> None:
    with pytest.raises(UnknownModelError):
        _router().route(_app("acme"), model="gpt-4")


@pytest.mark.parametrize(
    "kwargs", [{}, {"model": "economy", "quality": Quality.BEST}], ids=["neither", "both"]
)
def test_route_needs_exactly_one_of_model_and_quality(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        _router().route(_app("acme"), **kwargs)


def test_router_rejects_a_plan_naming_a_model_that_is_not_registered() -> None:
    def config(model: str) -> PlansConfig:
        return PlansConfig({"p": Plan("p", (model,), 1.0, 100)})

    with pytest.raises(PlansConfigError, match="not a registered model id"):
        Router(config("gpt-4"), _registry())
    # Plans must use registered ids, not aliases, so plan checks compare like with like.
    with pytest.raises(PlansConfigError, match="not a registered model id"):
        Router(config("llama-3.2-1b"), _registry())


# --- parsing -------------------------------------------------------------------------------


def test_parse_plans_reads_a_valid_document() -> None:
    config = parse_plans(_valid_document())
    assert config.plans == {"basic": Plan("basic", ("economy",), 1.0, 10)}
    assert config.applications == {"a1": DemoApplication("App One", "basic")}
    assert config.limits_for("basic") == PlanLimits()


def test_applications_section_is_optional() -> None:
    document = _valid_document()
    del document["applications"]
    assert parse_plans(document).applications == {}


def test_plan_limits_are_read_when_given() -> None:
    document = _valid_document()
    document["plans"]["basic"].update(max_tokens=256, requests_per_minute=60)
    assert parse_plans(document).limits_for("basic") == PlanLimits(256, 60)


def test_a_tenants_section_is_refused_with_a_pointer_to_applications() -> None:
    document = _valid_document()
    document["tenants"] = {"t1": "basic"}
    with pytest.raises(PlansConfigError, match="applications replaced tenants"):
        parse_plans(document)


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
        (("plans", "basic", "max_tokens"), 0, "positive integer"),
        (("plans", "basic", "requests_per_minute"), 1.5, "positive integer"),
        (("plans", "basic", "requests_per_minute"), True, "positive integer"),
        (("applications",), ["a1"], "mapping of application id"),
        (("applications", "a1"), "basic", "needs a 'name'"),
        (("applications", "a1", "plan"), "platinum", "unknown plan 'platinum'"),
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
        "applications:\n"
        "  a9: {name: Nine, plan: solo}\n",
        encoding="utf-8",
    )
    config = load_plans(path)
    assert config.plans == {"solo": Plan("solo", ("standard",), 1.25, 5000)}
    assert config.applications == {"a9": DemoApplication("Nine", "solo")}


def test_load_plans_reports_invalid_yaml(tmp_path: Path) -> None:
    path = tmp_path / "plans.yaml"
    path.write_text("plans: [unclosed\n", encoding="utf-8")
    with pytest.raises(PlansConfigError, match="not valid YAML"):
        load_plans(path)
