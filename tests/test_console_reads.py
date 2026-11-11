"""The console's Applications list and Add application (docs/51): its three reads, the mode on
every application row, and the HTTP path of a new application and its first key.

What is pinned here:

- each route and its action equal note 51's table, with no step-up;
- who gets each read, for every role of ``test_capabilities``' world, and that a missing
  organization and a forbidden one are refused with the same bytes, for every user and route;
- the figures: fixed field names, the alert signals' own values and minimums, the evaluator's
  window (complete minutes only), ``incomplete``, ``unavailable``, ``above_range``, ``since_ms``
  after a restart, the list's paging, and the windows and limits refused;
- the open incident counts: by application and state, resolved ones, another organization's
  and the built-in empty application id left out, on memory, SQLite and Postgres;
- the plan templates, and the mode in force on each application row;
- no read is audited; every answer is ``no-store`` and local only;
- creating an application and issuing its first key over HTTP: step-up on the key, both audit
  rows, and a leak scan of the key with its planted control.

Test values, not secrets."""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.routing import APIRoute

from accounts_helpers import PASSWORD
from gateway.alertrecorder import EDGES, MINUTE_MS, AlertRecorder, bucket
from gateway.alertrollups import InMemoryAlertRollups, MinuteRollup
from gateway.alertrules import COMPLETE_AFTER_MS, AlertRule, SqliteRuleStore, validate_rule
from gateway.apikeys import ApplicationAccess
from gateway.app import create_app, declaration_of
from gateway.audit import AuditAction
from gateway.auth import AccountAccess
from gateway.clock import SystemEpoch
from gateway.continuous import build_continuous_registry
from gateway.database import open_database
from gateway.evaluator import Series, decide
from gateway.incidents import (
    Fence,
    IncidentStore,
    InMemoryIncidentStore,
    SqliteIncidentStore,
)
from gateway.localonly import LocalOnlyPolicy
from gateway.management import ManagementService, NotPermittedError
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.modes import InMemoryModeStore, Mode
from gateway.permissions import PERMISSIONS
from gateway.routing import load_plans
from gateway.settings import SqliteSettingsStore
from gateway.signals import FROM_ROLLUPS, SIGNALS, SignalValue, evaluate, evaluate_many
from gateway.state import StateUnavailableError
from test_capabilities import USERS, extend_world
from test_management_api import World, make_world

REPO = Path(__file__).resolve().parent.parent
FORBIDDEN = b'{"error":{"code":"forbidden","message":"Not permitted, or not found."}}'
T0 = 1_793_613_600_000.0  # a minute's start
Session = tuple[str, str]


# --- the endpoints, as documented --------------------------------------------------------------


@dataclass(frozen=True)
class Endpoint:
    method: str
    path: str
    action: str
    step_up: bool
    success: int


def documented_endpoints() -> list[Endpoint]:
    text = (REPO / "docs" / "51-console-applications.md").read_text(encoding="utf-8")
    pattern = re.compile(r"^\| `(GET|POST)` \| `([^`]+)` \| `([a-z_.]+)` \| (yes|) ?\| (\d{3}) \|$")
    found = []
    for line in text.splitlines():
        match = pattern.match(line.strip())
        if match:
            method, path, action, step_up, success = match.groups()
            found.append(Endpoint(method, "/{org}" + path, action, step_up == "yes", int(success)))
    return found


ENDPOINTS = documented_endpoints()
PATHS = {e.path for e in ENDPOINTS}


# --- the world --------------------------------------------------------------------------------


class Epoch:
    """Calendar time the test moves: the recorder's minutes and the figures' window."""

    def __init__(self, at: float = T0) -> None:
        self.at = at

    def epoch_ms(self) -> float:
        return self.at


@dataclass
class Console:
    client: httpx.AsyncClient
    world: World
    app: Any
    epoch: Epoch
    modes: InMemoryModeStore
    incidents: SqliteIncidentStore

    @property
    def recorder(self) -> AlertRecorder:
        recorder = self.app.state.alert_recorder
        assert isinstance(recorder, AlertRecorder)
        return recorder

    @property
    def rollups(self) -> InMemoryAlertRollups:
        rollups = self.app.state.alert_rollups
        assert isinstance(rollups, InMemoryAlertRollups)
        return rollups

    async def login(self, username: str) -> Session:
        response = await self.client.post(
            "/v1/auth/login", json={"username": username, "password": PASSWORD}
        )
        self.client.cookies.clear()
        assert response.status_code == 200, response.text
        sid = response.headers["set-cookie"].split(";")[0].split("=", 1)[1]
        return sid, response.json()["csrf_token"]

    def headers(self, session: Session) -> dict[str, str]:
        return {"Cookie": f"gw_session={session[0]}", "X-CSRF-Token": session[1]}

    async def get(self, session: Session, path: str) -> httpx.Response:
        return await self.client.get("/v1/admin/orgs" + path, headers=self.headers(session))

    async def post(self, session: Session, path: str, body: Any) -> httpx.Response:
        return await self.client.post(
            "/v1/admin/orgs" + path, json=body, headers=self.headers(session)
        )

    async def step_up(self, session: Session) -> None:
        answer = await self.client.post(
            "/v1/auth/step-up", json={"password": PASSWORD}, headers=self.headers(session)
        )
        assert answer.status_code == 200, answer.text

    def at_minute(self, minute: int, second: float = 1.0) -> None:
        """Calendar time ``second`` s into ``minute`` minutes after T0."""
        self.epoch.at = T0 + minute * MINUTE_MS + second * 1000

    def evaluated_after(self, minute: int) -> None:
        """Calendar time at which ``minute`` (after T0) is the window's last complete one."""
        self.epoch.at = T0 + (minute + 1) * MINUTE_MS + COMPLETE_AFTER_MS

    def record(self, application_id: str, n: int, **kind: Any) -> None:
        for _ in range(n):
            self.recorder.record(application_id, kind="generate", **kind)


@asynccontextmanager
async def console(seed: int = 5) -> AsyncIterator[Console]:
    """``test_capabilities``' world (alice and carol org_admins of north, vera its viewer, nina
    app_admin of north-shop, olive viewer of north-blog, cleo an application-defined role, bob
    no role, sid org_admin of south, sam no role, root platform_admin), its calendar time the
    test's own from T0, so this process's rollups began at T0."""
    world = make_world()
    extend_world(world)
    accounts = world.accounts
    connection = accounts.connection
    epoch = Epoch()
    modes = InMemoryModeStore()
    incidents = SqliteIncidentStore(connection, epoch)
    registry = build_continuous_registry(accounts.clock, seed=7)
    app = create_app(
        registry,
        accounts.clock,
        applications=ApplicationAccess(world.keys, world.templates),
        accounts=AccountAccess(accounts.service),
        local_only=LocalOnlyPolicy(),
        epoch=epoch,
        rules=SqliteRuleStore(connection),
        incidents=incidents,
        settings=SqliteSettingsStore(connection),
        modes=modes,
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield Console(client, world, app, epoch, modes, incidents)
    await registry.aclose()


def path(endpoint: Endpoint, organization: str) -> str:
    return endpoint.path.replace("{org}", organization)


# --- the routes -------------------------------------------------------------------------------


async def test_each_route_is_documented_and_declares_its_action() -> None:
    assert len(ENDPOINTS) == 3
    async with console() as c:
        routes = set()
        for route in c.app.routes:
            if isinstance(route, APIRoute) and route.path.removeprefix("/v1/admin/orgs") in PATHS:
                [declaration] = declaration_of(route)
                assert declaration.kind == "requires", route.path
                for method in route.methods or ():
                    routes.add(
                        (method, route.path.removeprefix("/v1/admin/orgs"), declaration.action)
                    )
    assert routes == {(e.method, e.path, e.action) for e in ENDPOINTS}
    for endpoint in ENDPOINTS:
        assert PERMISSIONS[endpoint.action].step_up is endpoint.step_up is False, endpoint


# Who gets 200 from each read in their own organization (north, or south for sid and sam).
ALLOWED: dict[str, frozenset[str]] = {
    "/{org}/plans": frozenset({"alice", "sid"}),
    "/{org}/metrics": frozenset({"alice", "vera", "sid"}),
    "/{org}/incidents/counts": frozenset({"alice", "vera", "sid"}),
}


@pytest.mark.parametrize("username", USERS)
async def test_who_gets_each_read_and_missing_is_forbidden(username: str) -> None:
    async with console() as c:
        session = await c.login(username)
        own = "south" if username in ("sid", "sam") else "north"
        other = "north" if own == "south" else "south"
        for endpoint in ENDPOINTS:
            answer = await c.get(session, path(endpoint, own))
            if username in ALLOWED[endpoint.path]:
                assert answer.status_code == 200, (endpoint.path, answer.text)
            else:
                assert (answer.status_code, answer.content) == (403, FORBIDDEN), endpoint.path
            # Another organization and a missing one: the same bytes.
            for organization in (other, "nowhere"):
                refused = await c.get(session, path(endpoint, organization))
                assert (refused.status_code, refused.content) == (403, FORBIDDEN), (
                    endpoint.path,
                    organization,
                )
            assert answer.headers["cache-control"] == "no-store"


def test_allowed_is_the_permission_table() -> None:
    """The table above, written by hand, is today's permission table: who holds each action."""

    def pairs(action: str) -> set[str]:
        return {f"{role.value}@{relation.value}" for role, relation in PERMISSIONS[action].allowed}

    assert ALLOWED["/{org}/plans"] == {"alice", "sid"}  # org_admins only
    assert pairs("applications.create") == {"org_admin@own_organization"}
    for action in ("usage.read_organization", "incidents.read_organization"):
        assert pairs(action) == {"org_admin@own_organization", "viewer@own_organization"}


@pytest.mark.parametrize("endpoint", ENDPOINTS, ids=lambda e: e.path)
@pytest.mark.parametrize(
    ("client", "headers", "code"),
    [
        (("10.0.0.5", 50000), {}, "local_only"),
        (("127.0.0.1", 50000), {"X-Forwarded-For": "127.0.0.1"}, "local_only"),
        (("127.0.0.1", 50000), {"Host": "gateway.example.com"}, "host_not_allowed"),
    ],
)
async def test_each_read_is_local_only(
    endpoint: Endpoint, client: tuple[str, int], headers: dict[str, str], code: str
) -> None:
    async with console() as c:
        session = await c.login("alice")
        transport = httpx.ASGITransport(app=c.app, client=client)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as remote:
            answer = await remote.get(
                "/v1/admin/orgs" + path(endpoint, "north"),
                headers={"Cookie": f"gw_session={session[0]}", **headers},
            )
    assert answer.status_code == 403
    assert answer.json()["error"]["code"] == code
    assert answer.headers["cache-control"] == "no-store"


async def test_no_read_is_audited() -> None:
    async with console() as c:
        sessions = [await c.login(u) for u in ("alice", "vera", "bob")]
        connection = c.world.accounts.connection
        before = connection.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        for session in sessions:
            for endpoint in ENDPOINTS:
                for organization in ("north", "nowhere"):
                    await c.get(session, path(endpoint, organization))
        after = connection.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    assert after == before


async def test_behind_requires_the_services_own_checks_still_refuse() -> None:
    """The second layer (docs/30): the management service checks the action itself, so a route
    whose ``requires`` were broken would still refuse."""
    async with console() as c:
        service = c.world.accounts.service
        actors = {u: await service.actor_for(await _user(c, u)) for u in ("alice", "vera", "sid")}
        management = ManagementService(c.world.keys, c.world.templates, c.world.accounts.audit,
                                       c.world.accounts.epoch)  # fmt: skip
        assert (await management.page_for_usage("north", None, 10, actor=actors["vera"])).items
        assert management.plan_templates("north", actor=actors["alice"])
        with pytest.raises(NotPermittedError):
            await management.page_for_usage("north", None, 10, actor=actors["sid"])
        with pytest.raises(NotPermittedError):
            management.plan_templates("north", actor=actors["vera"])
        with pytest.raises(NotPermittedError):
            management.plan_templates("north", actor=actors["sid"])


async def _user(c: Console, username: str) -> Any:
    found = await c.world.accounts.store.find_user(username)
    assert found is not None
    return found


# --- the figures ------------------------------------------------------------------------------

FIGURE_FIELDS = {"requests", "error_rate", "latency_total_p50"}


def rows(answer: httpx.Response) -> dict[str, dict[str, Any]]:
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert set(body) == {"window", "applications", "next"}
    assert set(body["window"]) == {"minutes", "first_minute_ms", "end_ms", "since_ms"}
    found = {}
    for row in body["applications"]:
        assert set(row) == {"application_id"} | FIGURE_FIELDS
        for name in FIGURE_FIELDS:
            assert set(row[name]) == {"value", "why_not"}
        found[row["application_id"]] = row
    return found


async def test_no_traffic_is_zero_requests_and_too_few_to_judge() -> None:
    async with console() as c:
        c.evaluated_after(100)
        found = rows(await c.get(await c.login("alice"), "/north/metrics"))
    assert set(found) == {"north-blog", "north-shop"}
    for row in found.values():
        assert row["requests"] == {"value": 0.0, "why_not": None}
        assert row["error_rate"] == {"value": None, "why_not": "too_few"}
        assert row["latency_total_p50"] == {"value": None, "why_not": "too_few"}


async def test_the_figures_are_the_alert_signals_over_the_complete_minutes() -> None:
    async with console() as c:
        c.at_minute(100)
        c.record("north-shop", 13, status=200, total_ms=10.0)
        c.at_minute(130)
        c.record("north-shop", 7, status=200, total_ms=100.0)
        c.record("north-shop", 5, status=503, code="upstream_unavailable")
        c.record("south-shop", 30, status=500)  # another organization's: never north's
        c.at_minute(150)
        c.record("north-shop", 40, status=500)  # the minute in progress: not yet counted
        # 20 s into minute 150: minute 149 ended less than 25 s ago, so 148 is the last.
        c.epoch.at = T0 + 150 * MINUTE_MS + 20_000
        assert int((c.epoch.at - COMPLETE_AFTER_MS) // MINUTE_MS) - 1 == int(T0 // MINUTE_MS) + 148
        found = rows(await c.get(await c.login("alice"), "/north/metrics"))
        window = (await c.get(await c.login("alice"), "/north/metrics")).json()["window"]
    shop = found["north-shop"]
    assert shop["requests"] == {"value": 25.0, "why_not": None}
    assert shop["error_rate"] == {"value": 20.0, "why_not": None}
    # 20 answered latencies: the 10th (nearest rank) is in 10 ms's bucket, its upper edge.
    assert shop["latency_total_p50"] == {"value": EDGES[bucket(10.0)], "why_not": None}
    assert found["north-blog"]["requests"] == {"value": 0.0, "why_not": None}
    assert window["minutes"] == 60
    assert window["end_ms"] - window["first_minute_ms"] == 60 * MINUTE_MS
    assert window["end_ms"] == T0 + 149 * MINUTE_MS  # minute 148's end
    assert window["since_ms"] is None


async def test_a_minute_that_lost_counts_makes_every_figure_incomplete() -> None:
    async with console() as c:
        c.at_minute(100)
        c.record("north-shop", 30, status=200, total_ms=10.0)
        minute = int(c.epoch.at // MINUTE_MS)
        await c.rollups.mark_incomplete([("north-shop", minute)])
        c.evaluated_after(120)
        found = rows(await c.get(await c.login("alice"), "/north/metrics"))
    for name in FIGURE_FIELDS:
        assert found["north-shop"][name] == {"value": None, "why_not": "incomplete"}
    assert found["north-blog"]["requests"] == {"value": 0.0, "why_not": None}


async def test_a_p50_above_the_last_edge_is_above_range_not_infinity() -> None:
    async with console() as c:
        c.at_minute(100)
        c.record("north-shop", 12, status=200, total_ms=1e9)
        c.evaluated_after(120)
        answer = await c.get(await c.login("alice"), "/north/metrics")
    shop = rows(answer)["north-shop"]
    assert shop["latency_total_p50"] == {"value": None, "why_not": "above_range"}
    assert "Infinity" not in answer.text


async def test_rollups_that_cannot_be_read_make_every_figure_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with console() as c:

        async def down(*_: Any) -> Any:
            raise StateUnavailableError("valkey down")

        monkeypatch.setattr(c.rollups, "read_many", down)
        c.evaluated_after(120)
        found = rows(await c.get(await c.login("alice"), "/north/metrics"))
    for row in found.values():
        for name in FIGURE_FIELDS:
            assert row[name] == {"value": None, "why_not": "unavailable"}


async def test_a_window_reaching_before_this_process_started_says_since_when() -> None:
    async with console() as c:
        c.at_minute(10)
        c.record("north-shop", 3, status=200, total_ms=10.0)
        c.evaluated_after(10)
        body = (await c.get(await c.login("alice"), "/north/metrics")).json()
    assert body["window"]["since_ms"] == T0
    assert rows_of(body)["north-shop"]["requests"] == {"value": 3.0, "why_not": None}


def rows_of(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {r["application_id"]: r for r in body["applications"]}


async def test_the_page_is_the_lists_page() -> None:
    async with console() as c:
        alice = await c.login("alice")
        c.evaluated_after(100)
        listed = (await c.get(alice, "/north/applications?limit=1")).json()
        figured = (await c.get(alice, "/north/metrics?limit=1")).json()
        assert [a["application_id"] for a in figured["applications"]] == [
            a["application_id"] for a in listed["applications"]
        ]
        assert figured["next"] == listed["next"] is not None
        after = listed["next"]
        listed2 = (await c.get(alice, f"/north/applications?limit=1&after={after}")).json()
        figured2 = (await c.get(alice, f"/north/metrics?limit=1&after={after}")).json()
    assert [a["application_id"] for a in figured2["applications"]] == [
        a["application_id"] for a in listed2["applications"]
    ]
    assert figured2["next"] == listed2["next"]


@pytest.mark.parametrize("query", ["window=24h", "window=", "limit=0", "limit=101"])
async def test_a_window_or_page_size_it_does_not_offer_is_refused(query: str) -> None:
    async with console() as c:
        answer = await c.get(await c.login("alice"), f"/north/metrics?{query}")
    assert answer.status_code == 422
    assert answer.json()["error"]["code"] == "invalid_request"


def test_evaluate_many_gives_evaluate_s_own_values() -> None:
    """The figures' one pass over a window (``evaluate_many``) against ``evaluate`` itself, for
    every signal that takes no parameter, at each window it takes, over windows of random counts,
    with and without a minute that lost counts."""
    rng = random.Random(11)
    for window in (5, 15, 60):
        names = [
            n for n in FROM_ROLLUPS if not SIGNALS[n].parameters and window in SIGNALS[n].windows
        ]
        assert "requests" in names
        for trial in range(100):
            rollups = []
            for minute in range(window):
                n = rng.randrange(0, 8)
                chat = rng.randrange(0, n + 1)
                errors = rng.randrange(0, n + 1)
                fields = {
                    "requests": n,
                    "kind:chat": chat,
                    "kind:generate": n - chat,
                    "status:5xx": errors,
                    "status:504": rng.randrange(0, errors + 1),
                    "hosted_served": rng.randrange(0, chat + 1),
                    "downgraded": rng.randrange(0, n - chat + 1),
                    "code:state_unavailable": rng.randrange(0, errors + 1),
                    f"total:{rng.randrange(0, 73)}": n,
                    f"ttft:{rng.randrange(0, 73)}": n,
                }
                lost = trial % 7 == 0 and minute == window // 2
                rollups.append(MinuteRollup("a", 1000 + minute, fields, lost))
            many = evaluate_many(names, "a", rollups, window=window)
            assert many == {n: evaluate(n, "a", rollups, window=window) for n in names}


# --- open incident counts ---------------------------------------------------------------------


class Opener:
    """Incidents made as the evaluator makes them, one rule a series."""

    def __init__(self, store: IncidentStore) -> None:
        self.store = store
        self.counter = iter(range(1, 10_000))

    def _id(self, prefix: str) -> str:
        return prefix + f"{next(self.counter):016x}"

    async def open(self, organization: str, application: str, state: str) -> None:
        lease = await self.store.attempt_lease("test")
        fence = Fence("test", lease.epoch)
        digest = hashlib.sha256(f"{application}/{state}/{next(self.counter)}".encode()).hexdigest()
        body = {
            "name": "Errors", "signal": "error_rate", "comparison": ">", "threshold": 5,
            "window": 5, "for_minutes": 5 if state == "pending" else 0, "severity": "critical",
        }  # fmt: skip
        rule = AlertRule("rul_" + digest[:16], application, validate_rule(body), 1, None, 0, 0)
        change = decide(Series(rule, organization, "", SignalValue(9.0)), None, None, T0, self._id)
        assert change is not None
        assert await self.store.apply(fence, change)
        incident = change.incident
        if state == "acknowledged":
            assert (
                await self.store.acknowledge(application, incident.id, "usr_x", T0 + 1) is not None
            )
        if state == "resolved":
            # Two evaluations with the condition false resolve it (``FALSE_TO_RESOLVE``).
            for at in (T0 + 30_000, T0 + 60_000):
                change = decide(
                    Series(rule, organization, "", SignalValue(1.0)), incident, None, at, self._id
                )
                assert change is not None
                assert await self.store.apply(fence, change)
                incident = change.incident
            assert not incident.open


async def seed_incidents(store: IncidentStore) -> None:
    opener = Opener(store)
    for organization, application, state in (
        ("north", "north-shop", "firing"),
        ("north", "north-shop", "firing"),
        ("north", "north-shop", "pending"),
        ("north", "north-blog", "acknowledged"),
        ("north", "north-blog", "resolved"),
        ("north", "north-gone", "resolved"),
        ("north", "", "firing"),  # the built-in channel_delivery_failing: no application's
        ("south", "south-shop", "firing"),
    ):
        await opener.open(organization, application, state)


EXPECTED_COUNTS = {
    "north-blog": {"acknowledged": 1},
    "north-shop": {"firing": 2, "pending": 1},
}


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[IncidentStore]:
    epoch = Epoch()
    if request.param == "memory":
        yield InMemoryIncidentStore(epoch)
        return
    connection = open_database(tmp_path / "gateway.db")
    MigrationRunner(connection, MIGRATIONS, SystemEpoch()).migrate()
    yield SqliteIncidentStore(connection, epoch)
    connection.close()


async def test_open_counts_by_application_and_state(store: IncidentStore) -> None:
    await seed_incidents(store)
    assert await store.open_counts("north") == EXPECTED_COUNTS
    assert await store.open_counts("south") == {"south-shop": {"firing": 1}}
    assert await store.open_counts("nowhere") == {}


@pytest.mark.postgres
async def test_open_counts_on_postgres() -> None:
    from gateway.postgres import PostgresIncidentStore
    from postgres_helpers import pg_world

    async with pg_world() as world:
        await world.set_time(T0)
        store = PostgresIncidentStore(world.instances[0])
        await seed_incidents(store)
        assert await store.open_counts("north") == EXPECTED_COUNTS
        assert await store.open_counts("south") == {"south-shop": {"firing": 1}}


async def test_the_counts_route_answers_ids_and_counts_only() -> None:
    async with console() as c:
        await seed_incidents(c.incidents)
        answer = await c.get(await c.login("vera"), "/north/incidents/counts")
    assert answer.status_code == 200
    assert answer.json() == {
        "applications": [
            {"application_id": "north-blog", "pending": 0, "firing": 0, "acknowledged": 1},
            {"application_id": "north-shop", "pending": 1, "firing": 2, "acknowledged": 0},
        ]
    }


# --- the plan templates, and the mode --------------------------------------------------------


async def test_the_plans_are_the_templates_with_their_limits() -> None:
    templates = load_plans()
    async with console() as c:
        answer = await c.get(await c.login("alice"), "/north/plans")
    assert answer.status_code == 200
    plans = answer.json()["plans"]
    assert [p["name"] for p in plans] == list(templates.plans)
    for plan in plans:
        assert set(plan) == {
            "name", "allowed_models", "monthly_token_budget", "max_tokens", "requests_per_minute",
        }  # fmt: skip
        template = templates.plans[plan["name"]]
        limits = templates.limits_for(plan["name"])
        assert plan["allowed_models"] == list(template.models)
        assert plan["monthly_token_budget"] == template.monthly_token_budget
        assert (plan["max_tokens"], plan["requests_per_minute"]) == (
            limits.max_tokens,
            limits.requests_per_minute,
        )


@pytest.mark.parametrize(
    ("organization", "application", "effective"),
    [
        (Mode.LOCAL_ONLY, Mode.LOCAL_ONLY, Mode.LOCAL_ONLY),
        (Mode.LOCAL_ONLY, Mode.LOCAL_WITH_HOSTED_FALLBACK, Mode.LOCAL_ONLY),
        (Mode.LOCAL_WITH_HOSTED_FALLBACK, Mode.LOCAL_ONLY, Mode.LOCAL_ONLY),
        (
            Mode.HOSTED_PRIMARY_WITH_LOCAL_FALLBACK,
            Mode.LOCAL_WITH_HOSTED_FALLBACK,
            Mode.LOCAL_WITH_HOSTED_FALLBACK,
        ),
    ],
)
async def test_each_application_row_carries_the_mode_in_force(
    organization: Mode, application: Mode, effective: Mode
) -> None:
    async with console() as c:
        await c.modes.set_organization_mode("north", organization, T0)
        await c.modes.set_application_mode("north-shop", application, T0)
        vera = await c.login("vera")
        listed = rows_of_list((await c.get(vera, "/north/applications")).json())
        read = (await c.get(vera, "/north/applications/north-shop")).json()
    assert listed["north-shop"]["mode"] == effective.value
    # Its own mode unset: local only, so local only whatever the organization's.
    assert listed["north-blog"]["mode"] == Mode.LOCAL_ONLY.value
    assert read["mode"] == effective.value


def rows_of_list(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {a["application_id"]: a for a in body["applications"]}


# --- a new application and its first key, over HTTP -------------------------------------------


async def test_a_new_application_and_its_first_key_with_both_audit_rows_and_no_leak(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    async with console() as c:
        alice = await c.login("alice")
        made = await c.post(alice, "/north/applications", {"name": "Fresh", "plan": "pro"})
        assert made.status_code == 201, made.text
        application = made.json()
        assert application["mode"] == "local_only"
        app_id = application["application_id"]
        # The first key needs a step-up, as the table says; then it is shown once.
        refused = await c.post(alice, f"/north/applications/{app_id}/keys", {"label": "first"})
        assert refused.status_code == 403
        assert refused.json()["error"]["code"] == "step_up_required"
        await c.step_up(alice)
        issued = await c.post(alice, f"/north/applications/{app_id}/keys", {"label": "first"})
        assert issued.status_code == 201, issued.text
        key, key_id = issued.json()["key"], issued.json()["key_id"]
        listed = await c.get(alice, f"/north/applications/{app_id}/keys")
        entries = await c.world.accounts.audit.entries()
        audit_text = repr(c.world.accounts.connection.execute("SELECT * FROM audit_log").fetchall())
    created = [e for e in entries if e.event.action is AuditAction.APPLICATION_CREATED]
    keyed = [e for e in entries if e.event.action is AuditAction.KEY_ISSUED]
    assert [e.event.target_id for e in created][-1] == app_id
    assert [e.event.target_id for e in keyed][-1] == key_id
    alice_id = (await c.world.accounts.store.find_user("alice")).id  # type: ignore[union-attr]
    assert created[-1].event.actor_user_id == keyed[-1].event.actor_user_id == alice_id
    # The key is in its one answer and nowhere else: not the list, the audit, logs or output.
    assert key not in listed.text

    def leaks() -> list[str]:
        out, err = capsys.readouterr()
        captured["out"] += out
        captured["err"] += err
        places = {
            "list": listed.text,
            "audit": audit_text,
            "logs": "\n".join(f"{r.name} {r.getMessage()} {r.args!r}" for r in caplog.records),
            "stdout": captured["out"],
            "stderr": captured["err"],
        }
        return sorted(where for where, text in places.items() if key in text)

    captured = {"out": "", "err": ""}
    assert leaks() == []
    # The control: the key logged by mistake, and printed, is found where it is.
    logging.getLogger("gateway").debug("oops: %s", key)
    print(f"and printed: {json.dumps({'k': key})}")
    assert leaks() == ["logs", "stdout"]
