"""A world for the delivery tests (docs/46): one state (SQLite, or Postgres for the tests marked
``postgres``), organizations north and south with an application each, channels made through
the channel service, an alert rule that fires when the test says so, the evaluator, and delivery
workers whose every attempt goes through ``webhook_client`` onto an injected resolver and a
scripted network. No socket is opened and no production guard is relaxed. Test values, not
secrets."""

from __future__ import annotations

import asyncio
import random
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from gateway.alertrules import AlertRule, validate_rule
from gateway.alerts.caps import AlertCaps, CeilingsWatch
from gateway.alerts.channels import ChannelService
from gateway.alerts.delivery import AttemptReport, DeliveryWorker
from gateway.alerts.notices import Notices, builtin_rule_ids
from gateway.applications import application_from_template
from gateway.audit import AuditLog
from gateway.clock import Clock, ClockEpoch, SimClock
from gateway.database import open_database
from gateway.egress import provider_tls_context, webhook_client
from gateway.evaluator import AlertEvaluator, ApplicationSignals
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.organizations import new_organization
from gateway.permissions import Actor
from gateway.redaction import Redactor
from gateway.routing import load_plans
from gateway.signals import SignalValue
from gateway.state import StateBackend, sqlite_state
from gateway.vault import Vault
from mock_network import FakeResolver, ScriptedNetwork, http_response

# 2026-11-02 (a Monday) 10:00 UTC, in ms.
T0 = 1_793_613_600_000.0
PLANS = load_plans()
MASTER = bytes(range(32))
BUILTIN_KEY = bytes(range(64, 96))
HOSTS = {
    "hooks.example.com": ["93.184.216.34"],
    "other.example.net": ["93.184.216.35"],
    "third.example.org": ["93.184.216.36"],
}
URL = "https://hooks.example.com/services/planted-delivery-token-0f3a?token=planted-q-77"
OTHER_URL = "https://other.example.net/hook/planted-other-token-1b2c"
THIRD_URL = "https://third.example.org/hook/planted-third-token-9e8d"
OK = http_response(b"{}", Content_Length="2")
TLS = provider_tls_context()
ACTOR = {
    "north": Actor("usr_00000000000000a1", "north", ()),
    "south": Actor("usr_00000000000000b1", "south", ()),
}
RULE_BODY = {
    "name": "Budget",
    "signal": "budget_used",
    "comparison": ">=",
    "threshold": 80,
    "window": None,
    "for_minutes": 0,
    "severity": "critical",
}


class _Marker:
    def changed(self) -> None:
        return None


@dataclass
class Signals:
    """``BulkSignals`` with each application's budget the test sets."""

    organization_of: Mapping[str, str]
    budget: dict[str, float] = field(default_factory=dict)

    async def read_many(
        self, wanted: Mapping[str, frozenset[str]]
    ) -> Mapping[str, ApplicationSignals]:
        return {
            a: ApplicationSignals(
                self.organization_of.get(a),
                budget_used=SignalValue(self.budget[a]) if a in self.budget else SignalValue(0.0),
            )
            for a in wanted
        }


@dataclass
class World:
    state: StateBackend
    clock: Clock
    epoch: Any
    vault: Vault
    notices: Notices
    service: ChannelService
    network: ScriptedNetwork
    resolver: FakeResolver
    signals: Signals
    redactor: Redactor = field(default_factory=Redactor)
    reports: list[AttemptReport] = field(default_factory=list)
    rng: random.Random = field(default_factory=lambda: random.Random(3))
    db: sqlite3.Connection | None = None
    evaluators: dict[str, AlertEvaluator] = field(default_factory=dict)

    def worker(
        self, *, clock: Clock | None = None, network: Any = None, max_running: int = 4
    ) -> DeliveryWorker:
        """A worker of an instance; each attempt a new ``webhook_client`` (M45)."""
        on = clock if clock is not None else self.clock
        inner = network if network is not None else self.network
        return DeliveryWorker(
            self.state.outbox,
            self.state.channels,
            self.vault,
            self.redactor,
            store_clock=self.state.clock,
            clock=on,
            epoch=self.epoch,
            client=lambda: webhook_client(
                self.resolver, on, frozenset({443}), inner=inner, ssl_context=TLS
            ),
            rng=random.Random(self.rng.random()),
            random_bytes=self.rng.randbytes,
            on_attempt=self.reports.append,
            max_running=max_running,
        )

    def evaluator(
        self,
        holder: str = "a",
        *,
        ceilings: CeilingsWatch | None = None,
        notices: Notices | None = None,
    ) -> AlertEvaluator:
        """Holder ``holder``'s evaluator: one per holder, as an instance keeps its own."""
        if holder in self.evaluators and ceilings is None and notices is None:
            return self.evaluators[holder]
        made = self.evaluators[holder] = AlertEvaluator(
            incidents=self.state.incidents,
            rules=self.state.rules,
            rollups=None,
            signals=self.signals,
            clock=self.clock,
            holder=holder,
            random_bytes=random.Random(holder).randbytes,
            notices=notices if notices is not None else self.notices,
            ceilings=ceilings,
        )
        return made

    async def channel(
        self, name: str, url: str = URL, *, org: str = "north", default: bool = True
    ) -> str:
        made = await self.service.create(
            org,
            name=name,
            url=url,
            is_default=default,
            channel_type="webhook",
            actor=ACTOR[org],
            marker=_Marker(),
        )
        return made.channel.id

    async def rule(
        self,
        channels: list[str],
        *,
        app: str = "north-shop",
        rule_id: str = "rul_00000000000000a1",
        **change: Any,
    ) -> AlertRule:
        spec = validate_rule({**RULE_BODY, "channels": channels, **change})
        rule = AlertRule(rule_id, app, spec, 1, None, T0, T0)
        await self.state.rules.insert_rule(rule)
        return rule

    async def change_rule(self, rule: AlertRule, **change: Any) -> AlertRule:
        body = {**RULE_BODY, "channels": list(rule.spec.channels), **change}
        done = replace(rule, spec=validate_rule(body), version=rule.version + 1)
        await self.state.rules.put_rule(done, rule.version)
        return done

    async def deliver(self, worker: DeliveryWorker | None = None) -> int:
        """Claim what is due and wait for it to finish."""
        worker = worker if worker is not None else self.worker()
        started = await worker.step()
        await worker.drain()
        return started

    def rows(self, sql: str, *args: Any) -> list[tuple[Any, ...]]:
        assert self.db is not None
        return [tuple(r) for r in self.db.execute(sql, args).fetchall()]

    async def deliveries(self) -> list[tuple[str, str, str | None, int]]:
        """Every delivery as (channel, state, code, attempts), in the order queued."""
        if self.db is not None:
            return [
                (r[0], r[1], r[2], int(r[3]))
                for r in self.db.execute(
                    "SELECT channel_id, state, last_code, attempts FROM notification_deliveries"
                    " ORDER BY rowid"
                ).fetchall()
            ]
        found = []
        for channel in await self.state.channels.organization_channels(
            "north"
        ) + await self.state.channels.organization_channels("south"):
            found += await self.state.outbox.channel_deliveries(channel.id, 1000)
        found.sort(key=lambda d: (d.created_at_ms, d.id))
        return [(d.channel_id, d.state.value, d.last_code, d.attempts) for d in found]


async def seed(state: StateBackend, now: float) -> None:
    for org in ("north", "south"):
        await state.applications.add_organization(
            new_organization(org, org.title(), "business", created_at_ms=now)
        )
        await state.applications.add_application(
            replace(
                application_from_template(
                    "x", f"{org.title()} Shop", PLANS, "pro", organization_id=org, created_at_ms=now
                ),
                id=f"{org}-shop",
            )
        )


def build(
    state: StateBackend,
    clock: Clock,
    epoch: Any,
    *,
    caps: AlertCaps | None = None,
    responses: list[Any] | None = None,
    db: sqlite3.Connection | None = None,
) -> World:
    rng = random.Random(11)
    vault = Vault(MASTER, random_bytes=rng.randbytes)
    notices = Notices(
        state.channels,
        state.outbox,
        state.applications,
        epoch,
        rng.randbytes,
        caps=caps if caps is not None else AlertCaps(),
        store_clock=state.clock,
        builtin_rule_id=builtin_rule_ids(BUILTIN_KEY),
    )
    service = ChannelService(
        state.channels,
        vault,
        AuditLog(state.audit),
        epoch,
        rng.randbytes,
        notices=notices,
        store_clock=state.clock,
    )
    return World(
        state,
        clock,
        epoch,
        vault,
        notices,
        service,
        ScriptedNetwork(list(responses) if responses is not None else []),
        FakeResolver({h: list(a) for h, a in HOSTS.items()}),
        Signals({"north-shop": "north", "south-shop": "south"}),
        db=db,
    )


async def sqlite_world(**kwargs: Any) -> World:
    clock = SimClock()
    epoch = ClockEpoch(clock, T0)
    db = open_database(":memory:")
    MigrationRunner(db, MIGRATIONS, epoch).migrate()
    state = sqlite_state(db, epoch)
    await seed(state, T0)
    return build(state, clock, epoch, db=db, **kwargs)


class FrozenClock:
    """Real time for ``now_ms``, and a sleep that never ends: an attempt on it hangs until it is
    cancelled (the pool tests, where a simulated clock would move while Postgres answers)."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def now_ms(self) -> float:
        return self._clock.now_ms()

    async def sleep_ms(self, duration_ms: float) -> None:
        if duration_ms > 0:
            await asyncio.get_running_loop().create_future()
