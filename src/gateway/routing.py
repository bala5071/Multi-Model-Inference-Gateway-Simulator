"""Plans, plan templates and the router that picks a model an application's plan allows.

See docs/09-routing.md and docs/20-applications-api-keys.md.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from gateway.registry import ModelRegistry

if TYPE_CHECKING:
    from gateway.applications import Application


class Quality(StrEnum):
    CHEAPEST = "cheapest"
    BALANCED = "balanced"
    BEST = "best"


@dataclass(frozen=True, slots=True)
class Plan:
    name: str
    # Registered model ids this plan may use, cheapest first.
    models: tuple[str, ...]
    # The tenant is billed the model's base cost times this.
    price_multiplier: float
    # Billed tokens (uncached prompt + completion) allowed per budget period.
    monthly_token_budget: int


@dataclass(frozen=True, slots=True)
class PlanLimits:
    """Per-application limits a plan template gives; ``None`` means no limit."""

    # The largest max_tokens a request may ask for; also used when it asks for none.
    max_tokens: int | None = None
    requests_per_minute: int | None = None


@dataclass(frozen=True, slots=True)
class DemoApplication:
    name: str
    plan: str


@dataclass(frozen=True, slots=True)
class PlansConfig:
    """The plans file: named plans, which are templates applications are created from, and
    the demo applications. Applications themselves live in an ``ApplicationStore``."""

    plans: Mapping[str, Plan]
    # application id -> name and template plan; created by the CLI and simulated live mode
    applications: Mapping[str, DemoApplication] = field(default_factory=dict)
    # plan name -> its limits; a plan with no entry has none
    limits: Mapping[str, PlanLimits] = field(default_factory=dict)

    def limits_for(self, plan_name: str) -> PlanLimits:
        return self.limits.get(plan_name, PlanLimits())


class PlansConfigError(ValueError):
    """The plans file is malformed or inconsistent."""


class ModelNotAllowedError(Exception):
    """An application asked for a model that exists but is not on its plan."""

    def __init__(
        self,
        application_id: str,
        plan: Plan,
        model: str,
        plans_with_model: Sequence[str],
    ) -> None:
        self.application_id = application_id
        self.plan = plan
        self.model = model
        self.plans_with_model = tuple(plans_with_model)
        message = (
            f"Application {application_id!r} is on the {plan.name!r} plan, "
            f"which does not include "
            f"model {model!r}. Models on this plan: {', '.join(plan.models)}."
        )
        if self.plans_with_model:
            message += f" Plans that include {model!r}: {', '.join(self.plans_with_model)}."
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class Route:
    application_id: str
    plan: Plan
    model: str


def pick_by_quality(models: Sequence[str], quality: Quality) -> str:
    """Pick from ``models``, which must be ordered cheapest first.

    ``cheapest`` is the first, ``best`` the last and ``balanced`` the middle one (the
    upper middle when the count is even).
    """
    if not models:
        raise ValueError("no models to pick from")
    if quality is Quality.CHEAPEST:
        return models[0]
    if quality is Quality.BEST:
        return models[-1]
    return models[len(models) // 2]


def parse_plans(data: object) -> PlansConfig:
    """Validate the parsed YAML document and turn it into a ``PlansConfig``."""
    if not isinstance(data, dict):
        raise PlansConfigError("the plans file must be a mapping with 'plans' and 'applications'")
    if "tenants" in data:
        raise PlansConfigError(
            "'tenants' is no longer read: applications replaced tenants "
            "(docs/20-applications-api-keys.md); list them under 'applications'"
        )
    raw_plans = data.get("plans")
    raw_applications = data.get("applications", {})
    if not isinstance(raw_plans, dict) or not raw_plans:
        raise PlansConfigError("'plans' must be a non-empty mapping")
    if not isinstance(raw_applications, dict):
        raise PlansConfigError("'applications' must be a mapping of application id to settings")

    plans: dict[str, Plan] = {}
    limits: dict[str, PlanLimits] = {}
    for name, raw in raw_plans.items():
        if not isinstance(raw, dict):
            raise PlansConfigError(f"plan {name!r} must be a mapping")
        models = raw.get("models")
        multiplier = raw.get("price_multiplier")
        budget = raw.get("monthly_token_budget")
        if (
            not isinstance(models, list)
            or not models
            or not all(isinstance(m, str) and m for m in models)
        ):
            raise PlansConfigError(f"plan {name!r}: 'models' must be a non-empty list of names")
        if len(set(models)) != len(models):
            raise PlansConfigError(f"plan {name!r}: 'models' lists a model twice")
        if isinstance(multiplier, bool) or not isinstance(multiplier, int | float):
            raise PlansConfigError(f"plan {name!r}: 'price_multiplier' must be a number")
        if multiplier < 0:
            raise PlansConfigError(f"plan {name!r}: 'price_multiplier' must not be negative")
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 0:
            raise PlansConfigError(
                f"plan {name!r}: 'monthly_token_budget' must be a non-negative integer"
            )
        plans[str(name)] = Plan(str(name), tuple(models), float(multiplier), budget)
        limit_values: dict[str, int | None] = {}
        for key in ("max_tokens", "requests_per_minute"):
            value = raw.get(key)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 1
            ):
                raise PlansConfigError(f"plan {name!r}: {key!r} must be a positive integer")
            limit_values[key] = value
        if any(v is not None for v in limit_values.values()):
            limits[str(name)] = PlanLimits(
                limit_values["max_tokens"], limit_values["requests_per_minute"]
            )

    applications: dict[str, DemoApplication] = {}
    for app_id, raw in raw_applications.items():
        if not isinstance(raw, dict) or not isinstance(raw.get("name"), str) or not raw["name"]:
            raise PlansConfigError(f"application {app_id!r} needs a 'name'")
        plan_name = raw.get("plan")
        if plan_name not in plans:
            raise PlansConfigError(
                f"application {app_id!r} is on unknown plan {plan_name!r}; "
                f"plans are: {', '.join(plans)}"
            )
        applications[str(app_id)] = DemoApplication(raw["name"], str(plan_name))
    return PlansConfig(plans, applications, limits)


def load_plans(path: Path | None = None) -> PlansConfig:
    """Load a plans file, or the one packaged with the gateway if no path is given."""
    if path is None:
        text = resources.files("gateway").joinpath("plans.yaml").read_text(encoding="utf-8")
    else:
        text = path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PlansConfigError(f"the plans file is not valid YAML: {exc}") from exc
    return parse_plans(data)


class Router:
    """Decides which model serves an application's request, within the application's plan.

    ``config`` holds the plan templates; they are only used to say, in a refusal, which
    plans include the model asked for.
    """

    def __init__(self, config: PlansConfig, registry: ModelRegistry) -> None:
        for plan in config.plans.values():
            self.check_plan(plan, registry)
        self._config = config
        self._registry = registry

    @staticmethod
    def check_plan(plan: Plan, registry: ModelRegistry) -> None:
        """Raise ``PlansConfigError`` unless every model the plan lists is registered."""
        for model in plan.models:
            if model not in registry.names:
                raise PlansConfigError(
                    f"plan {plan.name!r} lists model {model!r}, which is not a registered "
                    f"model id; registered ids are: {', '.join(registry.names)}"
                )

    def route(
        self,
        application: Application,
        *,
        model: str | None = None,
        quality: Quality | None = None,
    ) -> Route:
        """Pick the model for a request that names either a model or a quality.

        Raises ``UnknownModelError`` (the model does not exist at all) or
        ``ModelNotAllowedError`` (it exists but the plan does not include it).
        """
        if (model is None) == (quality is None):
            raise ValueError("give exactly one of model and quality")
        plan = application.plan
        if quality is not None:
            return Route(application.id, plan, pick_by_quality(plan.models, quality))

        assert model is not None
        resolved = self._registry.resolve(model)
        if resolved not in plan.models:
            plans_with_model = [p.name for p in self._config.plans.values() if resolved in p.models]
            raise ModelNotAllowedError(application.id, plan, resolved, plans_with_model)
        return Route(application.id, plan, resolved)
