"""Clients (docs/57): the organization list, the platform's usage, and one client's applications,
metrics only, for the platform owner.

What is pinned here:

- each route and its action equal note 57's table, with no step-up;
- who gets each read, for every role of ``test_capabilities``' world, and that a missing
  organization, a missing application and another organization's application are refused with
  the same bytes as a forbidden one, for every user and route;
- each answer's field names equal ``usagefigures``' allowlists exactly, and no answer carries a
  key, key id, setting, mode, channel, rule, incident id or provider (a leak scan with planted
  controls);
- every other read and write in a client stays refused to platform_admin, asked through the real
  routes (section 13);
- the figures: the organization's and the application's minutes and hours, the platform's
  totals, ``incomplete``, ``unavailable``, ``since_reason`` after a restart, and per bucket;
- a page of 50, at most 100, at the cap, and the totals' cap;
- no read is audited; every answer is ``no-store`` and local only.

Test values, not secrets."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.routing import APIRoute

from gateway.alertrecorder import HOUR_MS, MINUTE_MS
from gateway.alertrules import COMPLETE_AFTER_MS
from gateway.app import declaration_of
from gateway.management import ManagementService, NotPermittedError
from gateway.organizations import new_organization
from gateway.permissions import PERMISSIONS
from gateway.state import StateUnavailableError, run_now
from gateway.usagefigures import (
    CLIENT_APPLICATION_FIELDS,
    CLIENT_BUCKET_FIELDS,
    CLIENT_DETAIL_FIELDS,
    OPEN_INCIDENT_FIELDS,
    ORGANIZATION_LIST_FIELDS,
    ORGANIZATION_USAGE_FIELDS,
    PLATFORM_TOTAL_FIELDS,
    SPAN_FIELDS,
)
from test_capabilities import USERS, gateway
from test_console_reads import FORBIDDEN, T0, Console, Opener, Session, console
from test_providers_api import _with_key, rig

REPO = Path(__file__).resolve().parent.parent
CLIENT_ROW = "usage.read_client_applications"
FIGURE = {"value", "why_not"}


# --- the endpoints, as documented --------------------------------------------------------------


@dataclass(frozen=True)
class Endpoint:
    method: str
    path: str
    action: str
    step_up: bool
    success: int


def documented_endpoints() -> list[Endpoint]:
    text = (REPO / "docs" / "57-clients.md").read_text(encoding="utf-8")
    pattern = re.compile(r"^\| `(GET|POST)` \| `([^`]+)` \| `([a-z_.]+)` \| (yes|) ?\| (\d{3}) \|$")
    found = []
    for line in text.splitlines():
        match = pattern.match(line.strip())
        if match:
            method, path, action, step_up, success = match.groups()
            found.append(Endpoint(method, path, action, step_up == "yes", int(success)))
    return found


ENDPOINTS = documented_endpoints()
ORGS = "/v1/admin/orgs"
USAGE = "/v1/admin/platform/usage"
CLIENT = "/v1/admin/platform/orgs/{org}/applications/metrics"
ONE = "/v1/admin/platform/orgs/{org}/applications/{app}/metrics"


def at(path: str, organization: str = "north", application: str = "north-shop") -> str:
    return path.replace("{org}", organization).replace("{app}", application)


async def get(c: Console, session: Session, path: str) -> httpx.Response:
    return await c.client.get(path, headers=c.headers(session))


def record(c: Console, application_id: str, organization_id: str, n: int, **kind: Any) -> None:
    kind.setdefault("kind", "generate")
    for _ in range(n):
        c.recorder.record(application_id, organization_id=organization_id, **kind)


def complete_after_hour(c: Console, hour: int) -> None:
    """Calendar time at which ``hour`` (hours after T0's hour) is the last complete one."""
    first_hour = int(T0 // HOUR_MS)
    c.epoch.at = (first_hour + hour + 1) * HOUR_MS + COMPLETE_AFTER_MS


def ok(answer: httpx.Response) -> dict[str, Any]:
    assert answer.status_code == 200, answer.text
    assert answer.headers["cache-control"] == "no-store"
    body = answer.json()
    assert isinstance(body, dict)
    return body


# --- the routes -------------------------------------------------------------------------------


async def test_each_route_is_documented_and_declares_its_action() -> None:
    assert len(ENDPOINTS) == 4
    paths = {e.path for e in ENDPOINTS}
    async with console() as c:
        routes = set()
        for route in c.app.routes:
            if isinstance(route, APIRoute) and route.path in paths:
                [declaration] = declaration_of(route)
                assert declaration.kind == "requires", route.path
                for method in route.methods or ():
                    routes.add((method, route.path, declaration.action))
    assert routes == {(e.method, e.path, e.action) for e in ENDPOINTS}
    for endpoint in ENDPOINTS:
        assert PERMISSIONS[endpoint.action].step_up is endpoint.step_up is False, endpoint


def _asked(username: str) -> Iterator[tuple[str, bool]]:
    """Each route this test asks, and whether ``username`` gets 200 from it: only root
    (platform_admin), and only on what exists in the client named."""
    root = username == "root"
    yield ORGS, root
    yield USAGE, root
    for organization in ("north", "south", "default"):
        yield at(CLIENT, organization), root
    yield at(ONE, "north", "north-shop"), root
    yield at(ONE, "north", "north-blog"), root
    yield at(ONE, "south", "south-shop"), root
    # Missing and misplaced: refused as forbidden ones, for root too.
    yield at(CLIENT, "nowhere"), False
    yield at(ONE, "nowhere", "north-shop"), False
    yield at(ONE, "north", "ghost"), False
    yield at(ONE, "north", "south-shop"), False  # another organization's application
    yield at(ONE, "south", "north-shop"), False


@pytest.mark.parametrize("username", USERS)
async def test_only_the_platform_owner_reads_and_missing_is_forbidden(username: str) -> None:
    async with console() as c:
        session = await c.login(username)
        for path, allowed in _asked(username):
            answer = await get(c, session, path)
            assert answer.headers["cache-control"] == "no-store", path
            if allowed:
                assert answer.status_code == 200, (path, answer.text)
            else:
                assert (answer.status_code, answer.content) == (403, FORBIDDEN), path


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
        session = await c.login("root")
        transport = httpx.ASGITransport(app=c.app, client=client)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as remote:
            answer = await remote.get(
                at(endpoint.path), headers={"Cookie": f"gw_session={session[0]}", **headers}
            )
    assert answer.status_code == 403
    assert answer.json()["error"]["code"] == code
    assert answer.headers["cache-control"] == "no-store"


async def test_no_read_is_audited() -> None:
    async with console() as c:
        sessions = [await c.login(u) for u in ("root", "alice", "bob")]
        connection = c.world.accounts.connection
        before = connection.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        for session in sessions:
            for path, _ in _asked("root"):
                await get(c, session, path)
        after = connection.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    assert after == before


async def test_behind_requires_the_services_own_checks_still_refuse() -> None:
    """The second layer (docs/30): the management service checks each action itself."""
    async with console() as c:
        service = c.world.accounts.service
        actors = {}
        for username in ("root", "alice"):
            user = await c.world.accounts.store.find_user(username)
            assert user is not None
            actors[username] = await service.actor_for(user)
        management = ManagementService(c.world.keys, c.world.templates, c.world.accounts.audit,
                                       c.world.accounts.epoch)  # fmt: skip
        assert (await management.page_organizations(None, 10, actor=actors["root"])).items
        assert (await management.page_for_aggregate(None, 10, actor=actors["root"])).items
        assert (await management.page_for_client("north", None, 10, actor=actors["root"])).items
        found = await management.client_application("north", "north-shop", actor=actors["root"])
        assert found.id == "north-shop"
        with pytest.raises(KeyError):
            await management.client_application("north", "south-shop", actor=actors["root"])
        for call in (
            management.page_organizations(None, 10, actor=actors["alice"]),
            management.page_for_aggregate(None, 10, actor=actors["alice"]),
            management.page_for_client("north", None, 10, actor=actors["alice"]),
            management.client_application("north", "north-shop", actor=actors["alice"]),
        ):
            with pytest.raises(NotPermittedError):
                await call


# --- every other refusal stays (section 13) ----------------------------------------------------

# Each action section 13 names, and a real route that asks it, with the ids of things that exist
# in north: ``{key}`` an API key, ``{incident}`` an open incident, ``{provider}`` a provider with
# a key. ``requires`` runs before the body is read, so ``{}`` is enough to be refused.
A = "/v1/admin/orgs/north/applications/north-shop"
OTHER_ROUTES: list[tuple[str, str, str]] = [
    ("applications.list", "GET", "/v1/admin/orgs/north/applications"),
    ("applications.read", "GET", A),
    ("application_keys.list", "GET", A + "/keys"),
    ("application_keys.issue", "POST", A + "/keys"),
    ("application_keys.rotate", "POST", A + "/keys/{key}/rotate"),
    ("application_keys.revoke", "POST", A + "/keys/{key}/revoke"),
    ("settings.read", "GET", A + "/settings"),
    ("settings.write", "PUT", A + "/settings"),
    ("org_settings.read", "GET", "/v1/admin/orgs/north/settings"),
    ("org_settings.write", "PUT", "/v1/admin/orgs/north/settings"),
    ("alert_rules.read", "GET", A + "/alert-rules"),
    ("alert_rules.write", "POST", A + "/alert-rules"),
    ("alert_channels.read", "GET", "/v1/admin/orgs/north/alert-channels"),
    ("alert_channels.write", "POST", "/v1/admin/orgs/north/alert-channels"),
    ("incidents.read", "GET", A + "/incidents"),
    ("incidents.read_organization", "GET", "/v1/admin/orgs/north/incidents"),
    ("incidents.acknowledge", "POST", A + "/incidents/{incident}/acknowledge"),
    ("usage.read_organization", "GET", "/v1/admin/orgs/north/metrics"),
    ("usage.read_application", "GET", A + "/usage"),
    ("mode.set_organization", "PUT", "/v1/admin/orgs/north/mode"),
    ("mode.set_application", "PUT", A + "/mode"),
    ("provider_keys.set", "PUT", "/v1/admin/orgs/north/providers/{provider}/key"),
    ("provider_keys.delete", "DELETE", "/v1/admin/orgs/north/providers/{provider}/key"),
]


def _declared(app: Any) -> dict[tuple[str, str], str | None]:
    found: dict[tuple[str, str], str | None] = {}
    for route in app.routes:
        if isinstance(route, APIRoute):
            for method in route.methods or ():
                for d in declaration_of(route):
                    found[(method, route.path)] = d.action
    return found


def _template(path: str) -> str:
    """The route's own path for ``path``: each id back to its parameter's name."""
    replaced = path.replace("/orgs/north", "/orgs/{org}")
    replaced = replaced.replace("/applications/north-shop", "/applications/{app}")
    for name, parameter in (("key", "key_id"), ("provider", "provider_id")):
        replaced = replaced.replace("{" + name + "}", "{" + parameter + "}")
    return replaced


async def _refused_everywhere(
    app: Any, client: httpx.AsyncClient, headers: dict[str, str], ids: dict[str, str]
) -> set[str]:
    """Ask every route of ``OTHER_ROUTES`` this app has, as the session in ``headers``: each
    answers the forbidden bytes. The actions asked, returned."""
    declared = _declared(app)
    asked = set()
    for action, method, path in OTHER_ROUTES:
        if declared.get((method, _template(path))) is None:
            continue  # not in this world: asked in another
        assert declared[(method, _template(path))] == action, (action, path)
        filled = path.format(**ids)
        answer = await client.request(method, filled, headers=headers, json={})
        assert (answer.status_code, answer.content) == (403, FORBIDDEN), (action, filled)
        asked.add(action)
    return asked


async def test_every_other_action_in_a_client_is_still_refused_to_the_platform_owner() -> None:
    """Section 13: platform_admin asks each action through its real route on an existing client,
    and each answers 403 ``forbidden``. Three worlds hold every route between them: the console's,
    one with alert channels, and one with providers and modes."""
    asked: set[str] = set()
    async with console() as c:
        issued = await c.world.keys.issue("north-shop", "refusals")
        await Opener(c.incidents).open("north", "north-shop", "firing")
        [incident] = await c.incidents.list_incidents(
            "north", application_id=None, state=None, limit=10, after=None
        )
        ids = {"key": issued.record.key_id, "incident": incident.id, "provider": "none"}
        root = await c.login("root")
        asked |= await _refused_everywhere(c.app, c.client, c.headers(root), ids)
    async with gateway() as g:
        root = await g.login("root")
        headers = {"Cookie": f"gw_session={root[0]}", "X-CSRF-Token": root[1]}
        asked |= await _refused_everywhere(g.app, g.client, headers, ids)
    async with rig() as r:
        alice = await r.login("alice")
        provider_id = await _with_key(r, alice)
        root = await r.login("root")
        await r.step_up(root)  # a window open changes nothing: refused before step-up is read
        headers = {"Cookie": f"gw_session={root[0]}", "X-CSRF-Token": root[1]}
        asked |= await _refused_everywhere(
            r.app, r.client, headers, {**ids, "provider": provider_id}
        )
    missing = {a for a, _, _ in OTHER_ROUTES} - asked
    # usage.read_application has no route yet (wired later): the table refuses it
    # (``test_permissions``). Every other action section 13 names was asked through its route.
    assert missing <= {"usage.read_application"}, missing
    assert not PERMISSIONS["usage.read_application"].allowed & set(PERMISSIONS[CLIENT_ROW].allowed)


# --- the answers' fields ----------------------------------------------------------------------


def _check_span(span: dict[str, Any]) -> None:
    assert set(span) == SPAN_FIELDS


def _check_figures(row: dict[str, Any], fields: frozenset[str]) -> None:
    assert set(row) == fields, set(row) ^ fields
    for name, value in row.items():
        if name == "open_incidents":
            assert set(value) == OPEN_INCIDENT_FIELDS
        elif name not in {"organization_id", "application_id", "name", "start_ms"}:
            assert set(value) == FIGURE, name


async def _busy(c: Console) -> None:
    """Traffic in both organizations, and an open incident on north-shop."""
    c.at_minute(30)
    record(c, "north-shop", "north", 30, status=200, total_ms=10.0)
    record(c, "north-shop", "north", 5, status=503, code="upstream_unavailable")
    record(c, "north-blog", "north", 25, kind="chat", status=200, total_ms=20.0,
           fallbacks=("upstream_5xx",))  # fmt: skip
    record(c, "south-shop", "south", 40, status=500)
    await Opener(c.incidents).open("north", "north-shop", "firing")


async def test_every_answer_holds_its_allowlist_and_nothing_else() -> None:
    async with console() as c:
        await _busy(c)
        c.evaluated_after(40)
        session = await c.login("root")
        orgs = ok(await get(c, session, ORGS))
        assert set(orgs) == {"organizations", "next"}
        for row in orgs["organizations"]:
            assert set(row) == ORGANIZATION_LIST_FIELDS
        for window in ("60m", "24h"):
            usage = ok(await get(c, session, f"{USAGE}?window={window}"))
            assert set(usage) == {"window", "organizations", "totals", "next"}
            _check_span(usage["window"])
            for row in usage["organizations"]:
                _check_figures(row, ORGANIZATION_USAGE_FIELDS)
            _check_figures(usage["totals"], PLATFORM_TOTAL_FIELDS)
            client = ok(await get(c, session, f"{at(CLIENT)}?window={window}"))
            assert set(client) == {"window", "applications", "next"}
            _check_span(client["window"])
            for row in client["applications"]:
                _check_figures(row, CLIENT_APPLICATION_FIELDS)
            one = ok(await get(c, session, f"{at(ONE)}?window={window}"))
            assert set(one) == CLIENT_DETAIL_FIELDS
            _check_span(one["window"])
            assert set(one["open_incidents"]) == OPEN_INCIDENT_FIELDS
            for bucket in one["buckets"]:
                _check_figures(bucket, CLIENT_BUCKET_FIELDS)


def test_the_allowlists_name_no_secret_or_configuration() -> None:
    every = (
        ORGANIZATION_LIST_FIELDS | ORGANIZATION_USAGE_FIELDS | PLATFORM_TOTAL_FIELDS
        | CLIENT_APPLICATION_FIELDS | CLIENT_DETAIL_FIELDS | CLIENT_BUCKET_FIELDS | SPAN_FIELDS
    )  # fmt: skip
    for word in ("key", "setting", "mode", "channel", "target", "rule", "provider", "prompt",
                 "response", "text", "incident_id", "url", "secret", "token"):  # fmt: skip
        assert not any(word in field for field in every), word


async def test_no_answer_leaks_what_the_client_configured() -> None:
    """The leak scan, with planted controls: an API key, its id, a webhook's address and the
    incident's id are in this world; none is in any answer, and the scan finds each in a body
    that holds it, so a scan that could not see them would fail."""
    async with console() as c:
        await _busy(c)
        issued = await c.world.keys.issue("north-shop", "leak-scan")
        [incident] = await c.incidents.list_incidents(
            "north", application_id=None, state=None, limit=10, after=None
        )
        planted = [issued.key, issued.record.key_id, incident.id, incident.rule_id]
        c.evaluated_after(40)
        session = await c.login("root")
        bodies = []
        for window in ("60m", "24h"):
            for path in (ORGS, f"{USAGE}?window={window}", f"{at(CLIENT)}?window={window}",
                         f"{at(ONE)}?window={window}"):  # fmt: skip
                bodies.append(ok(await get(c, session, path)))
        text = json.dumps(bodies)
        for value in planted:
            assert value not in text, "leaked"
            # The control: the same scan finds it where it is.
            assert value in json.dumps([*bodies, {"planted": value}])


# --- the figures ------------------------------------------------------------------------------


async def test_the_organizations_minutes_and_the_platforms_totals() -> None:
    async with console() as c:
        await _busy(c)
        c.evaluated_after(40)
        usage = ok(await get(c, await c.login("root"), f"{USAGE}?window=60m"))
    rows = {r["organization_id"]: r for r in usage["organizations"]}
    north = rows["north"]
    assert north["requests"] == {"value": 60.0, "why_not": None}
    assert north["error_rate"]["value"] == pytest.approx(100 * 5 / 60)
    # 25 chat requests, each with a fallback.
    assert north["fallback_rate"] == {"value": 100.0, "why_not": None}
    assert north["open_incidents"] == {"pending": 0, "firing": 1, "acknowledged": 0}
    assert rows["south"]["requests"] == {"value": 40.0, "why_not": None}
    assert rows["south"]["error_rate"] == {"value": 100.0, "why_not": None}
    assert rows["default"]["requests"] == {"value": 0.0, "why_not": None}
    totals = usage["totals"]
    assert totals["requests"] == {"value": 100.0, "why_not": None}
    assert totals["error_rate"]["value"] == pytest.approx(100 * 45 / 100)
    assert sum(totals["open_incidents"].values()) == 1
    # The process's rollups began at T0, inside the window: said, with why.
    assert usage["window"]["since_ms"] == T0
    assert usage["window"]["since_reason"] == "restarted"


async def test_a_client_applications_figures_and_their_buckets() -> None:
    async with console() as c:
        await _busy(c)
        c.evaluated_after(40)
        session = await c.login("root")
        client = ok(await get(c, session, at(CLIENT)))
        one = ok(await get(c, session, at(ONE)))
    rows = {r["application_id"]: r for r in client["applications"]}
    assert set(rows) == {"north-blog", "north-shop"}
    assert rows["north-shop"]["name"]
    assert rows["north-shop"]["requests"] == {"value": 35.0, "why_not": None}
    assert rows["north-shop"]["error_rate"]["value"] == pytest.approx(100 * 5 / 35)
    assert rows["north-blog"]["fallback_rate"] == {"value": 100.0, "why_not": None}
    assert sum(rows["north-shop"]["open_incidents"].values()) == 1
    assert sum(rows["north-blog"]["open_incidents"].values()) == 0
    assert one["window"]["bucket_ms"] == MINUTE_MS
    assert len(one["buckets"]) == 60
    starts = [b["start_ms"] for b in one["buckets"]]
    assert starts == sorted(starts)
    assert starts[1] - starts[0] == MINUTE_MS
    busy = [b for b in one["buckets"] if b["start_ms"] == T0 + 30 * MINUTE_MS]
    assert busy[0]["requests"] == {"value": 35.0, "why_not": None}
    # A minute alone: 35 requests is enough for a rate (20), not for nothing.
    quiet = [b for b in one["buckets"] if b["start_ms"] == T0 + 31 * MINUTE_MS]
    assert quiet[0]["requests"] == {"value": 0.0, "why_not": None}
    assert quiet[0]["error_rate"] == {"value": None, "why_not": "too_few"}


async def test_the_24_hours_are_the_hour_keys_and_old_hours_are_forgotten() -> None:
    async with console() as c:
        c.at_minute(30)
        record(c, "north-shop", "north", 30, status=200, total_ms=10.0)
        c.at_minute(5 * 60 + 10)
        record(c, "north-shop", "north", 20, status=500)
        complete_after_hour(c, 6)
        session = await c.login("root")
        one = ok(await get(c, session, f"{at(ONE)}?window=24h"))
        client = ok(await get(c, session, f"{at(CLIENT)}?window=24h"))
        usage = ok(await get(c, session, f"{USAGE}?window=24h"))
        assert one["window"]["bucket_ms"] == HOUR_MS
        assert len(one["buckets"]) == 24
        shop = {r["application_id"]: r for r in client["applications"]}["north-shop"]
        assert shop["requests"] == {"value": 50.0, "why_not": None}
        assert shop["error_rate"]["value"] == pytest.approx(40.0)
        north = {r["organization_id"]: r for r in usage["organizations"]}["north"]
        assert north["requests"] == {"value": 50.0, "why_not": None}
        assert one["window"]["since_reason"] == "restarted"
        # 27 hours on: the first hour is past the 26 kept, and gone from memory.
        before = c.rollups.size_extras()
        c.at_minute(32 * 60)
        record(c, "north-blog", "north", 1, status=200, total_ms=1.0)
        assert c.rollups.size_extras() < before
        complete_after_hour(c, 32)
        later = ok(await get(c, session, f"{at(CLIENT)}?window=24h"))
    shop = {r["application_id"]: r for r in later["applications"]}["north-shop"]
    assert shop["requests"] == {"value": 0.0, "why_not": None}


async def test_a_bucket_that_lost_counts_makes_the_figures_incomplete() -> None:
    async with console() as c:
        c.at_minute(30)
        record(c, "north-shop", "north", 30, status=200, total_ms=10.0)
        hour = int(c.epoch.at // HOUR_MS)
        await c.rollups.mark_incomplete_extras([("app-hour", "north-shop", hour)])
        complete_after_hour(c, 2)
        client = ok(await get(c, await c.login("root"), f"{at(CLIENT)}?window=24h"))
    shop = {r["application_id"]: r for r in client["applications"]}["north-shop"]
    assert shop["requests"] == {"value": None, "why_not": "incomplete"}


async def test_rollups_that_cannot_be_read_make_every_figure_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with console() as c:
        c.evaluated_after(40)

        async def down(*args: object) -> object:
            raise StateUnavailableError("valkey")

        monkeypatch.setattr(c.rollups, "read_many", down)
        monkeypatch.setattr(c.rollups, "read_extras", down)
        session = await c.login("root")
        usage = ok(await get(c, session, USAGE))
        client = ok(await get(c, session, at(CLIENT)))
        one = ok(await get(c, session, at(ONE)))
    for row in [*usage["organizations"], usage["totals"], *client["applications"],
                *one["buckets"]]:  # fmt: skip
        assert row["requests"] == {"value": None, "why_not": "unavailable"}


async def test_the_windows_asked_for_are_the_only_ones() -> None:
    async with console() as c:
        session = await c.login("root")
        for path in (USAGE, at(CLIENT), at(ONE)):
            for window in ("5m", "7d", "", "60M"):
                answer = await get(c, session, f"{path}?window={window}")
                assert answer.status_code == 422, (path, window)


# --- pages and caps (15.6, condition 4) --------------------------------------------------------


def _add_organizations(c: Console, n: int) -> None:
    store = c.world.keys.store

    async def add() -> None:
        for i in range(n):
            await store.add_organization(
                new_organization(f"client-{i:04d}", f"Client {i:04d}", "business", created_at_ms=T0)
            )

    run_now(add())


async def _add_applications(c: Console, n: int) -> None:
    management = ManagementService(c.world.keys, c.world.templates, c.world.accounts.audit,
                                   c.world.accounts.epoch)  # fmt: skip
    for i in range(n):
        await management.create_application(
            "north", f"App {i:04d}", "free", application_id=f"app-{i:04d}"
        )


async def test_the_organization_list_and_usage_are_pages_of_50_at_most_100() -> None:
    async with console() as c:
        _add_organizations(c, 120)  # 124 with default, solo, north and south
        session = await c.login("root")
        for path, key in ((ORGS, "organizations"), (USAGE, "organizations")):
            first = ok(await get(c, session, path))
            assert len(first[key]) == 50
            at_cap = ok(await get(c, session, f"{path}?limit=100"))
            assert len(at_cap[key]) == 100
            assert at_cap["next"] is not None
            rest = ok(await get(c, session, f"{path}?limit=100&after={at_cap['next']}"))
            assert len(rest[key]) == 24
            assert rest["next"] is None
            ids = [r["organization_id"] for r in at_cap[key] + rest[key]]
            assert ids == sorted(ids)
            assert len(set(ids)) == 124
            for limit in (0, 101):
                refused = await get(c, session, f"{path}?limit={limit}")
                assert refused.status_code == 422, (path, limit)
        # Both name the same organizations with the same cursor.
        listed = ok(await get(c, session, f"{ORGS}?limit=7&after=client-0010"))
        used = ok(await get(c, session, f"{USAGE}?limit=7&after=client-0010"))
        assert [r["organization_id"] for r in listed["organizations"]] == [
            r["organization_id"] for r in used["organizations"]
        ]
        assert listed["next"] == used["next"]


async def test_a_clients_applications_are_pages_of_50_at_most_100() -> None:
    async with console() as c:
        await _add_applications(c, 110)  # 112 with north-blog and north-shop
        session = await c.login("root")
        first = ok(await get(c, session, at(CLIENT)))
        assert len(first["applications"]) == 50
        at_cap = ok(await get(c, session, f"{at(CLIENT)}?limit=100"))
        assert len(at_cap["applications"]) == 100
        assert at_cap["next"] is not None
        rest = ok(await get(c, session, f"{at(CLIENT)}?limit=100&after={at_cap['next']}"))
        assert len(rest["applications"]) == 12
        assert rest["next"] is None
        for limit in (0, 101):
            assert (await get(c, session, f"{at(CLIENT)}?limit={limit}")).status_code == 422


async def test_the_totals_count_up_to_the_cap_and_say_so_past_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with console() as c:
        _add_organizations(c, 1)  # 5 with default, solo, north and south
        c.at_minute(30)
        record(c, "north-shop", "north", 30, status=200, total_ms=10.0)
        c.evaluated_after(40)
        session = await c.login("root")
        monkeypatch.setattr("gateway.app.TOTALS_CAP", 5)  # exactly at the cap: counted
        counted = ok(await get(c, session, USAGE))["totals"]
        monkeypatch.setattr("gateway.app.TOTALS_CAP", 4)  # one past it: not counted
        past = ok(await get(c, session, USAGE))["totals"]
    assert counted["requests"] == {"value": 30.0, "why_not": None}
    assert past["requests"] == {"value": None, "why_not": "above_cap"}
    assert past["open_incidents"] is None
    assert set(past) == PLATFORM_TOTAL_FIELDS


async def test_the_totals_read_every_page_of_organizations() -> None:
    async with console() as c:
        _add_organizations(c, 150)
        c.at_minute(30)
        record(c, "north-shop", "north", 30, status=200, total_ms=10.0)
        record(c, "client-0149", "client-0149", 20, status=500)  # on the second page of 100
        c.evaluated_after(40)
        totals = ok(await get(c, await c.login("root"), USAGE))["totals"]
    assert totals["requests"] == {"value": 50.0, "why_not": None}
