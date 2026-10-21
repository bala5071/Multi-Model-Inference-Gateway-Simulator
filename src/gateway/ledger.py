"""Per-tenant spend and token accounting, and budget enforcement.

See docs/09-routing.md. Since docs/19-usage-store.md the ledger is a facade over a
``UsageStore``: every charge is stored as a usage event, and the ledger keeps the current
period's totals in memory so that a budget check never has to read the store.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from gateway.clock import Clock, ClockEpoch, EpochSource
from gateway.schema import RequestResult
from gateway.usage import InMemoryUsageStore, ModelUsage, UsageEvent, UsageStore

THIRTY_DAYS_MS = 30 * 24 * 60 * 60 * 1000.0

__all__ = [
    "THIRTY_DAYS_MS",
    "BudgetExceededError",
    "CostLedger",
    "ModelUsage",
    "TenantUsage",
]


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

    Periods are fixed windows of ``period_ms`` of epoch time, counted from
    ``period_anchor_ms`` (default: when the ledger was created). They are not calendar
    months. A tenant's usage starts again from zero in each new period.

    Every charge is appended to ``store`` (default: an in-memory store). The first time the
    ledger sees a tenant in a period it reads that period's totals from the store, which is
    how usage stored by an earlier process is picked up; after that the totals are kept in
    memory. ``epoch`` dates the events (default: the ledger's clock read as epoch time,
    which is exact and deterministic on a ``SimClock``).
    """

    def __init__(
        self,
        clock: Clock,
        *,
        period_ms: float = THIRTY_DAYS_MS,
        store: UsageStore | None = None,
        epoch: EpochSource | None = None,
        period_anchor_ms: float | None = None,
    ) -> None:
        if period_ms <= 0:
            raise ValueError("period_ms must be positive")
        self._epoch = epoch if epoch is not None else ClockEpoch(clock)
        self._store = store if store is not None else InMemoryUsageStore()
        self._period_ms = period_ms
        self._started_ms = (
            period_anchor_ms if period_anchor_ms is not None else self._epoch.epoch_ms()
        )
        self._accounts: dict[str, _Account] = {}

    def _current_period(self) -> int:
        return int((self._epoch.epoch_ms() - self._started_ms) // self._period_ms)

    def _period_start_ms(self, period: int) -> float:
        return self._started_ms + period * self._period_ms

    def _account(self, tenant_id: str) -> _Account:
        period = self._current_period()
        account = self._accounts.get(tenant_id)
        if account is None or account.period != period:
            start_ms = self._period_start_ms(period)
            by_model = self._store.usage_by_model(tenant_id, start_ms, start_ms + self._period_ms)
            total = ModelUsage()
            for usage in by_model.values():
                total = _add(total, usage)
            account = _Account(period, total, by_model)
            self._accounts[tenant_id] = account
        return account

    def usage(self, tenant_id: str) -> TenantUsage:
        account = self._account(tenant_id)
        start_ms = self._period_start_ms(account.period)
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
                retry_after_ms=usage.period_end_ms - self._epoch.epoch_ms(),
            )

    def record(
        self, tenant_id: str, result: RequestResult, *, model: str, price_multiplier: float
    ) -> float:
        """Charge a finished request to a tenant. Returns the amount billed in USD.

        The event is stored first, so a failed write leaves the totals unchanged.
        """
        billed_cost_usd = result.cost_usd * price_multiplier
        account = self._account(tenant_id)
        self._store.append(
            UsageEvent.of(
                tenant_id,
                result,
                model=model,
                billed_cost_usd=billed_cost_usd,
                occurred_at_ms=self._epoch.epoch_ms(),
            )
        )
        account.total = account.total.plus(result, billed_cost_usd)
        account.by_model[model] = account.by_model.get(model, ModelUsage()).plus(
            result, billed_cost_usd
        )
        return billed_cost_usd


def _add(a: ModelUsage, b: ModelUsage) -> ModelUsage:
    return ModelUsage(
        requests=a.requests + b.requests,
        prompt_tokens=a.prompt_tokens + b.prompt_tokens,
        cached_tokens=a.cached_tokens + b.cached_tokens,
        completion_tokens=a.completion_tokens + b.completion_tokens,
        base_cost_usd=a.base_cost_usd + b.base_cost_usd,
        billed_cost_usd=a.billed_cost_usd + b.billed_cost_usd,
    )
