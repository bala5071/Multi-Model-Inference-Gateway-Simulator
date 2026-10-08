"""Plans, tenants and the router that picks a model a tenant's plan allows.

See docs/09-routing.md.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from importlib import resources
from pathlib import Path

import yaml

from gateway.registry import ModelRegistry


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
class PlansConfig:
    plans: Mapping[str, Plan]
    # tenant id -> plan name
    tenants: Mapping[str, str]


class PlansConfigError(ValueError):
    """The plans file is malformed or inconsistent."""


class UnknownTenantError(LookupError):
    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id
        super().__init__(f"Unknown tenant {tenant_id!r}.")


class ModelNotAllowedError(Exception):
    """A tenant asked for a model that exists but is not on their plan."""

    def __init__(
        self,
        tenant_id: str,
        plan: Plan,
        model: str,
        plans_with_model: Sequence[str],
    ) -> None:
        self.tenant_id = tenant_id
        self.plan = plan
        self.model = model
        self.plans_with_model = tuple(plans_with_model)
        message = (
            f"Tenant {tenant_id!r} is on the {plan.name!r} plan, which does not include "
            f"model {model!r}. Models on this plan: {', '.join(plan.models)}."
        )
        if self.plans_with_model:
            message += f" Plans that include {model!r}: {', '.join(self.plans_with_model)}."
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class Route:
    tenant_id: str
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
        raise PlansConfigError("the plans file must be a mapping with 'plans' and 'tenants'")
    raw_plans = data.get("plans")
    raw_tenants = data.get("tenants", {})
    if not isinstance(raw_plans, dict) or not raw_plans:
        raise PlansConfigError("'plans' must be a non-empty mapping")
    if not isinstance(raw_tenants, dict):
        raise PlansConfigError("'tenants' must be a mapping of tenant id to plan name")

    plans: dict[str, Plan] = {}
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

    tenants: dict[str, str] = {}
    for tenant_id, plan_name in raw_tenants.items():
        if plan_name not in plans:
            raise PlansConfigError(
                f"tenant {tenant_id!r} is on unknown plan {plan_name!r}; "
                f"plans are: {', '.join(plans)}"
            )
        tenants[str(tenant_id)] = str(plan_name)
    return PlansConfig(plans, tenants)


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
    """Decides which model serves a tenant's request, within the tenant's plan."""

    def __init__(self, config: PlansConfig, registry: ModelRegistry) -> None:
        for plan in config.plans.values():
            for model in plan.models:
                if model not in registry.names:
                    raise PlansConfigError(
                        f"plan {plan.name!r} lists model {model!r}, which is not a registered "
                        f"model id; registered ids are: {', '.join(registry.names)}"
                    )
        self._config = config
        self._registry = registry

    def plan_for(self, tenant_id: str) -> Plan:
        plan_name = self._config.tenants.get(tenant_id)
        if plan_name is None:
            raise UnknownTenantError(tenant_id)
        return self._config.plans[plan_name]

    def route(
        self,
        tenant_id: str,
        *,
        model: str | None = None,
        quality: Quality | None = None,
    ) -> Route:
        """Pick the model for a request that names either a model or a quality.

        Raises ``UnknownTenantError``, ``UnknownModelError`` (the model does not exist at
        all) or ``ModelNotAllowedError`` (it exists but the plan does not include it).
        """
        if (model is None) == (quality is None):
            raise ValueError("give exactly one of model and quality")
        plan = self.plan_for(tenant_id)
        if quality is not None:
            return Route(tenant_id, plan, pick_by_quality(plan.models, quality))

        assert model is not None
        resolved = self._registry.resolve(model)
        if resolved not in plan.models:
            plans_with_model = [p.name for p in self._config.plans.values() if resolved in p.models]
            raise ModelNotAllowedError(tenant_id, plan, resolved, plans_with_model)
        return Route(tenant_id, plan, resolved)
