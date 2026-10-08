"""Per-tenant spend and token accounting, and budget enforcement.

See docs/09-routing.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from gateway.clock import Clock
from gateway.schema import RequestResult

THIRTY_DAYS_MS = 30 * 24 * 60 * 60 * 1000.0


@dataclass(frozen=True, slots=True)
class ModelUsage:
    requests: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    base_cost_usd: float = 0.0
    billed_cost_usd: float = 0.0

    @property
    def billed_tokens(self) -> int:
        """Tokens that count against the budget: uncached prompt tokens plus completion."""
        return self.prompt_tokens - self.cached_tokens + self.completion_tokens

    def plus(self, result: RequestResult, billed_cost_usd: float) -> ModelUsage:
        return ModelUsage(
            requests=self.requests + 1,
            prompt_tokens=self.prompt_tokens + result.tokens_in,
            cached_tokens=self.cached_tokens + result.cached_tokens,
            completion_tokens=self.completion_tokens + result.tokens_out,
            base_cost_usd=self.base_cost_usd + result.cost_usd,
            billed_cost_usd=self.billed_cost_usd + billed_cost_usd,
        )


@dataclass(frozen=True, slots=True)
class TenantUsage:
    """One tenant's usage in the current budget period."""

    tenant_id: str
    period_start_ms: float
    period_end_ms: float
    total: ModelUsage
    by_model: dict[str, ModelUsage]


class BudgetExceededError(Exception):
    def __init__(
        self, tenant_id: str, used_tokens: int, budget_tokens: int, retry_after_ms: float
    ) -> None:
        self.tenant_id = tenant_id
        self.used_tokens = used_tokens
        self.budget_tokens = budget_tokens
        self.retry_after_ms = retry_after_ms
        super().__init__(
            f"Tenant {tenant_id!r} has used {used_tokens} of its {budget_tokens} token "
            f"budget for this period."
        )


@dataclass(slots=True)
class _Account:
    period: int
    total: ModelUsage = field(default_factory=ModelUsage)
    by_model: dict[str, ModelUsage] = field(default_factory=dict)


class CostLedger:
    """Tracks what each tenant has used in the current budget period.

    Periods are fixed windows of ``period_ms`` on the injected clock, counted from when
    the ledger was created. They are not calendar months. A tenant's usage starts again
    from zero in each new period; earlier periods are not kept.
    """

    def __init__(self, clock: Clock, *, period_ms: float = THIRTY_DAYS_MS) -> None:
        if period_ms <= 0:
            raise ValueError("period_ms must be positive")
        self._clock = clock
        self._period_ms = period_ms
        self._started_ms = clock.now_ms()
        self._accounts: dict[str, _Account] = {}

    def _current_period(self) -> int:
        return int((self._clock.now_ms() - self._started_ms) // self._period_ms)

    def _account(self, tenant_id: str) -> _Account:
        period = self._current_period()
        account = self._accounts.get(tenant_id)
        if account is None or account.period != period:
            account = _Account(period)
            self._accounts[tenant_id] = account
        return account

    def usage(self, tenant_id: str) -> TenantUsage:
        account = self._account(tenant_id)
        start_ms = self._started_ms + account.period * self._period_ms
        return TenantUsage(
            tenant_id=tenant_id,
            period_start_ms=start_ms,
            period_end_ms=start_ms + self._period_ms,
            total=account.total,
            by_model=dict(account.by_model),
        )

    def check_budget(self, tenant_id: str, budget_tokens: int) -> None:
        """Raise ``BudgetExceededError`` if the tenant has no budget left this period.

        The check is made before a request runs and the charge after it finishes, so the
        last request admitted, and any others in flight with it, can take usage past the
        budget.
        """
        usage = self.usage(tenant_id)
        if usage.total.billed_tokens >= budget_tokens:
            raise BudgetExceededError(
                tenant_id,
                usage.total.billed_tokens,
                budget_tokens,
                retry_after_ms=usage.period_end_ms - self._clock.now_ms(),
            )

    def record(
        self, tenant_id: str, result: RequestResult, *, model: str, price_multiplier: float
    ) -> float:
        """Charge a finished request to a tenant. Returns the amount billed in USD."""
        billed_cost_usd = result.cost_usd * price_multiplier
        account = self._account(tenant_id)
        account.total = account.total.plus(result, billed_cost_usd)
        account.by_model[model] = account.by_model.get(model, ModelUsage()).plus(
            result, billed_cost_usd
        )
        return billed_cost_usd
