"""What the console's Applications list costs to read (docs/51), as PR 1's description quotes.

MEASURED on the machine it runs on: real time, a SQLite file set up as the server's. One
organization with 50 or 100 applications on the pro plan, every one with an hour of rollups in
memory (30 requests a minute, one in ten 5xx, latencies spread over the histogram) and one in ten
with a firing incident; an org_admin logged in. Each read goes through the whole app in process
(``httpx`` over ASGI: the session, ``requires``, the handler), as the page sends it:

- ``GET .../metrics?limit=N``: a page of N applications and their figures over the last 60
  minutes, from the in-memory rollups (``read_many``, then ``signals.evaluate`` three times an
  application);
- ``GET .../incidents/counts``: the open incidents by application and state, one ``GROUP BY``;
- ``GET .../applications?limit=N``: the list, now with each application's mode;
- "mode reads alone": what the mode adds to the list, the organization's mode and each
  application's, straight from the store.

Rollups in Valkey and stores on Postgres are not measured here: neither runs on this machine.

    uv run python -m benchmarks.console_reads
    uv run python -m benchmarks.console_reads --reads 300 --runs 3

The account, its password and its session are made here and never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from benchmarks.calibrate import describe_machine
from gateway.alertrecorder import MINUTE_MS, AlertRecorder
from gateway.alertrollups import InMemoryAlertRollups
from gateway.alertrules import AlertRule, SqliteRuleStore, validate_rule
from gateway.apikeys import ApiKeyService, ApplicationAccess
from gateway.app import create_app
from gateway.applications import SqliteApplicationStore
from gateway.audit import AuditLog, SqliteAuditStore
from gateway.auth import AccountAccess, AccountService
from gateway.clock import SystemEpoch, WallClock
from gateway.continuous import build_continuous_registry
from gateway.database import open_database
from gateway.evaluator import Series, decide
from gateway.incidents import Fence, SqliteIncidentStore
from gateway.management import ManagementService
from gateway.metrics import percentile
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.modes import SqliteModeStore
from gateway.organizations import new_organization
from gateway.passwords import CHEAP, InlineHashRunner, PasswordHasher, Passwords
from gateway.roles import SqliteRoleStore
from gateway.routing import load_plans
from gateway.settings import SqliteSettingsStore
from gateway.signals import SignalValue
from gateway.users import Bootstrap, SqliteAccountStore

ORGANIZATION = "bench"
WARMUP = 20
PER_MINUTE = 30


class FixedEpoch:
    """Calendar time this script sets: the minute each recorded request falls in."""

    def __init__(self, at: float) -> None:
        self.at = at

    def epoch_ms(self) -> float:
        return self.at


def fill_rollups(rollups: InMemoryAlertRollups, ids: list[str], now_ms: float) -> None:
    """An hour of requests for each application, as the recorder writes them."""
    epoch = FixedEpoch(now_ms)
    recorder = AlertRecorder(epoch, sink=rollups)
    for minute in range(62, 0, -1):
        epoch.at = now_ms - minute * MINUTE_MS
        for application_id in ids:
            for i in range(PER_MINUTE):
                failed = i % 10 == 0
                recorder.record(
                    application_id,
                    kind="generate",
                    status=503 if failed else 200,
                    code="upstream_unavailable" if failed else None,
                    total_ms=None if failed else float(5 + 37 * i),
                )


async def open_incidents(store: SqliteIncidentStore, ids: list[str], now_ms: float) -> None:
    lease = await store.attempt_lease("bench")
    fence = Fence("bench", lease.epoch)
    body = {
        "name": "Errors", "signal": "error_rate", "comparison": ">", "threshold": 5,
        "window": 5, "for_minutes": 0, "severity": "critical",
    }  # fmt: skip
    counter = iter(range(1, 1_000_000))
    for n, application_id in enumerate(ids[::10]):
        rule = AlertRule(f"rul_{n:016x}", application_id, validate_rule(body), 1, None, 0, 0)
        change = decide(
            Series(rule, ORGANIZATION, "", SignalValue(9.0)), None, None, now_ms,
            lambda prefix: f"{prefix}{next(counter):016x}",
        )  # fmt: skip
        assert change is not None
        assert await store.apply(fence, change)


async def timed(reads: int, step: Callable[[], Awaitable[object]]) -> dict[str, float]:
    clock = WallClock()
    for _ in range(WARMUP):
        await step()
    times = []
    for _ in range(reads):
        started = clock.now_ms()
        await step()
        times.append(clock.now_ms() - started)
    return {
        "p50_ms": round(percentile(times, 50), 3),
        "p99_ms": round(percentile(times, 99), 3),
    }


async def one_run(size: int, reads: int, directory: Path) -> dict[str, dict[str, float]]:
    epoch = SystemEpoch()
    clock = WallClock()
    connection = open_database(directory / f"console-{size}.db")
    try:
        MigrationRunner(connection, MIGRATIONS, epoch).migrate()
        applications = SqliteApplicationStore(connection)
        await applications.add_organization(
            new_organization(ORGANIZATION, "Bench", "business", created_at_ms=epoch.epoch_ms())
        )
        keys = ApiKeyService(applications, secrets.token_bytes(32), epoch)
        templates = load_plans()
        audit = AuditLog(SqliteAuditStore(connection))
        operator = ManagementService(keys, templates, audit, epoch)
        ids = [f"bench-{i:04d}" for i in range(size)]
        for i, application_id in enumerate(ids):
            await operator.create_application(
                ORGANIZATION, f"App {i}", "pro", application_id=application_id
            )
        service = AccountService(
            SqliteAccountStore(connection),
            audit,
            Passwords(PasswordHasher(CHEAP), InlineHashRunner()),
            roles=SqliteRoleStore(connection),
            organizations=applications,
            clock=clock,
            epoch=epoch,
            secret=secrets.token_bytes(32),
        )
        password = secrets.token_urlsafe(18)
        accounts = SqliteAccountStore(connection)
        user = await service.create_user("bench-admin", password, ORGANIZATION)
        # As its first login would: its own password, nothing left to change.
        await accounts.set_password(
            user.id,
            PasswordHasher(CHEAP).hash(password),
            must_change=False,
            bootstrap=Bootstrap.NONE,
        )
        await service.grant_role("bench-admin", "org_admin", "organization", ORGANIZATION)
        incidents = SqliteIncidentStore(connection, epoch)
        modes = SqliteModeStore(connection)
        registry = build_continuous_registry(clock, seed=7)
        app = create_app(
            registry,
            clock,
            applications=ApplicationAccess(keys, templates),
            accounts=AccountAccess(service),
            epoch=epoch,
            incidents=incidents,
            modes=modes,
            rules=SqliteRuleStore(connection),
            settings=SqliteSettingsStore(connection),
        )
        rollups = app.state.alert_rollups
        assert isinstance(rollups, InMemoryAlertRollups)
        now_ms = epoch.epoch_ms()
        fill_rollups(rollups, ids, now_ms)
        await open_incidents(incidents, ids, now_ms)
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            login = await client.post(
                "/v1/auth/login", json={"username": "bench-admin", "password": password}
            )
            assert login.status_code == 200
            base = f"/v1/admin/orgs/{ORGANIZATION}"

            def reading(path: str) -> Callable[[], Awaitable[object]]:
                async def step() -> object:
                    answer = await client.get(base + path)
                    assert answer.status_code == 200, answer.text
                    return answer

                return step

            async def mode_reads() -> object:
                await modes.organization_mode(ORGANIZATION)
                return [await modes.application_mode(a) for a in ids]

            first = await client.get(f"{base}/metrics?limit={size}")
            assert first.status_code == 200, (first.status_code, first.text)
            figured = first.json()
            judged = [a for a in figured["applications"] if a["error_rate"]["value"] is not None]
            assert len(judged) == size, "every application's figures are judged"
            results = {
                f"metrics, page of {size}": await timed(reads, reading(f"/metrics?limit={size}")),
                "incident counts": await timed(reads, reading("/incidents/counts")),
                f"list with modes, page of {size}": await timed(
                    reads, reading(f"/applications?limit={size}")
                ),
                f"mode reads alone, {size}": await timed(reads, mode_reads),
            }
        await registry.aclose()
        return results
    finally:
        connection.close()


async def main_async(sizes: list[int], reads: int, runs: int) -> None:
    print(json.dumps({"machine": describe_machine()}, indent=2))
    for size in sizes:
        for run in range(1, runs + 1):
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
                results = await one_run(size, reads, Path(folder))
            print(json.dumps({"applications": size, "run": run, "reads": reads, **results}))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--sizes", type=int, nargs="+", default=[50, 100])
    parser.add_argument("--reads", type=int, default=300)
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args(argv)
    asyncio.run(main_async(args.sizes, args.reads, args.runs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
