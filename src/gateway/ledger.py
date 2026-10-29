"""Per-application spend and token accounting, and budget enforcement.

See docs/09-routing.md. Since docs/19-usage-store.md the ledger is a facade over a
``UsageStore``: every charge is stored as a usage event, and the ledger keeps the current
period's totals in memory so that a budget check never has to read the store. Since
docs/21-persistence.md periods are calendar months in UTC by default, and old events can be
pruned after a retention period. Since docs/26-organizations.md it also rolls the usage of
an organization's applications up, for reporting only: budgets are enforced per application.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from gateway.clock import Clock, ClockEpoch, EpochSource
from gateway.periods import BudgetPeriods, CalendarMonthsUTC
from gateway.schema import RequestResult
from gateway.usage import ChatSource, InMemoryUsageStore, ModelUsage, UsageEvent, UsageStore

# How often the retention loop prunes, and how many events it deletes in one go: a batch is
# one short transaction, and the loop yields to the event loop between batches.
PRUNE_EVERY_MS = 60 * 60 * 1000.0
PRUNE_BATCH = 1000

__all__ = [
    "ApplicationShare",
    "ApplicationUsage",
    "BudgetExceededError",
    "Budgets",
    "CostLedger",
    "ModelUsage",
    "OrganizationUsage",
]


@dataclass(frozen=True, slots=True)
class ApplicationUsage:
    """One application's usage in the current budget period."""

    application_id: str
    period_start_ms: float
    period_end_ms: float
    total: ModelUsage
    by_model: dict[str, ModelUsage]


@dataclass(frozen=True, slots=True)
class ApplicationShare:
    """One application's part of an organization's usage this period."""

    application_id: str
    used_tokens: int
    budget_tokens: int


@dataclass(frozen=True, slots=True)
class OrganizationUsage:
    """An organization's usage in the current budget period: its applications' usage added
    up. Reporting only: there is no organization budget, and ``budget_tokens`` (the sum of
    its applications' budgets) is the most it can be billed for in a period."""

    organization_id: str
    period_start_ms: float
    period_end_ms: float
    total: ModelUsage
    by_model: dict[str, ModelUsage]
    applications: tuple[ApplicationShare, ...]

    @property
    def budget_tokens(self) -> int:
        return sum(a.budget_tokens for a in self.applications)


class BudgetExceededError(Exception):
    def __init__(
        self, application_id: str, used_tokens: int, budget_tokens: int, retry_after_ms: float
    ) -> None:
        self.application_id = application_id
        self.used_tokens = used_tokens
        self.budget_tokens = budget_tokens
        self.retry_after_ms = retry_after_ms
        super().__init__(
            f"Application {application_id!r} has used {used_tokens} of its {budget_tokens} token "
            f"budget for this period."
        )


class Budgets(Protocol):
    """What the request path and monitoring ask of budgets (docs/34-state-interfaces.md):
    ``CostLedger`` in one process. Scale-out PR 2 replaces check-then-record with reserve,
    settle and release (docs/scale-out-plan.md, section 3.1)."""

    async def check_budget(self, application_id: str, budget_tokens: int) -> None: ...

    async def record(
        self,
        application_id: str,
        result: RequestResult,
        *,
        model: str,
        price_multiplier: float,
        source: ChatSource | None = None,
    ) -> float: ...

    async def usage(self, application_id: str) -> ApplicationUsage: ...


@dataclass(slots=True)
class _Account:
    period_start_ms: float
    period_end_ms: float
    total: ModelUsage = field(default_factory=ModelUsage)
    by_model: dict[str, ModelUsage] = field(default_factory=dict)


class CostLedger:
    """Tracks what each application has used in the current budget period.

    ``periods`` decides the periods (default: calendar months in UTC), read on ``epoch``
    (default: the ledger's clock read as epoch time, exact and deterministic on a
    ``SimClock``). An application's usage starts again from zero in each new period.

    Every charge is appended to ``store`` (default: an in-memory store). The first time the
    ledger sees an application in a period it reads that period's totals from the store, which is
    how usage stored by an earlier process is picked up; after that the totals are kept in
    memory.

    With ``retention_ms``, ``prune`` deletes events older than that from the store; events
    of the current period are always kept, whatever the retention. Pruning is not done on
    the request path: ``run_retention`` does it in bounded batches.
    """

    def __init__(
        self,
        clock: Clock,
        *,
        periods: BudgetPeriods | None = None,
        store: UsageStore | None = None,
        epoch: EpochSource | None = None,
        retention_ms: float | None = None,
    ) -> None:
        if retention_ms is not None and retention_ms <= 0:
            raise ValueError("retention_ms must be positive")
        self._epoch = epoch if epoch is not None else ClockEpoch(clock)
        self._store = store if store is not None else InMemoryUsageStore()
        self._periods = periods if periods is not None else CalendarMonthsUTC()
        self._retention_ms = retention_ms
        self._accounts: dict[str, _Account] = {}

    async def _account(self, application_id: str) -> _Account:
        start_ms, end_ms = self._periods.bounds(self._epoch.epoch_ms())
        account = self._accounts.get(application_id)
        if account is None or account.period_start_ms != start_ms:
            by_model = await self._store.usage_by_model(application_id, start_ms, end_ms)
            total = ModelUsage()
            for usage in by_model.values():
                total = _add(total, usage)
            account = _Account(start_ms, end_ms, total, by_model)
            self._accounts[application_id] = account
        return account

    async def usage(self, application_id: str) -> ApplicationUsage:
        account = await self._account(application_id)
        return ApplicationUsage(
            application_id=application_id,
            period_start_ms=account.period_start_ms,
            period_end_ms=account.period_end_ms,
            total=account.total,
            by_model=dict(account.by_model),
        )

    async def organization_usage(
        self, organization_id: str, budgets: Mapping[str, int]
    ) -> OrganizationUsage:
        """The usage of an organization's applications, added up. ``budgets`` maps each of
        its application ids (and only its own: the caller lists them from the application
        store) to that application's budget. The totals are the ledger's own, the ones its
        budget checks use, added in application id order."""
        start_ms, end_ms = self._periods.bounds(self._epoch.epoch_ms())
        total = ModelUsage()
        by_model: dict[str, ModelUsage] = {}
        shares = []
        for application_id in sorted(budgets):
            usage = await self.usage(application_id)
            total = _add(total, usage.total)
            for model, model_usage in usage.by_model.items():
                by_model[model] = _add(by_model.get(model, ModelUsage()), model_usage)
            shares.append(
                ApplicationShare(application_id, usage.total.billed_tokens, budgets[application_id])
            )
        return OrganizationUsage(organization_id, start_ms, end_ms, total, by_model, tuple(shares))

    @property
    def retention_ms(self) -> float | None:
        return self._retention_ms

    async def prune(self, limit: int | None = None) -> int:
        """Delete events older than the retention (never the current period's), at most
        ``limit`` of them. Returns how many; 0 without a retention."""
        if self._retention_ms is None:
            return 0
        now_ms = self._epoch.epoch_ms()
        period_start_ms, _ = self._periods.bounds(now_ms)
        return await self._store.prune(min(now_ms - self._retention_ms, period_start_ms), limit)

    async def check_budget(self, application_id: str, budget_tokens: int) -> None:
        """Raise ``BudgetExceededError`` if the application has no budget left this period.

        The check is made before a request runs and the charge after it finishes, so the
        last request admitted, and any others in flight with it, can take usage past the
        budget.
        """
        usage = await self.usage(application_id)
        if usage.total.billed_tokens >= budget_tokens:
            raise BudgetExceededError(
                application_id,
                usage.total.billed_tokens,
                budget_tokens,
                retry_after_ms=usage.period_end_ms - self._epoch.epoch_ms(),
            )

    async def record(
        self,
        application_id: str,
        result: RequestResult,
        *,
        model: str,
        price_multiplier: float,
        source: ChatSource | None = None,
    ) -> float:
        """Charge a finished request to an application. Returns the amount billed in USD.

        The event is stored first, so a failed write leaves the totals unchanged.
        """
        billed_cost_usd = result.cost_usd * price_multiplier
        account = await self._account(application_id)
        await self._store.append(
            UsageEvent.of(
                application_id,
                result,
                model=model,
                billed_cost_usd=billed_cost_usd,
                occurred_at_ms=self._epoch.epoch_ms(),
                source=source,
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


async def run_retention(
    ledger: CostLedger,
    clock: Clock,
    *,
    batch: int = PRUNE_BATCH,
    every_ms: float = PRUNE_EVERY_MS,
) -> None:
    """Prune the ledger's store now and then every ``every_ms``, until cancelled.

    Each pass deletes ``batch`` events at a time and yields to the event loop between
    batches (``clock.sleep_ms(0)``), so no request waits behind more than one batch. Run as
    a task beside the server; nothing on the request path prunes.
    """
    if batch < 1:
        raise ValueError("batch must be at least 1")
    while True:
        while await ledger.prune(batch) == batch:
            await clock.sleep_ms(0)
        await clock.sleep_ms(every_ms)
