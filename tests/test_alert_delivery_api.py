"""A4 group 2 through HTTP (docs/46): test send (its action, its caps, a counter that cannot answer
refusing, a disabled channel refused, its audit row); each notification's deliveries on an
incident; the built-in ``channel_delivery_failing``'s incidents seen only by the organization's
org_admins and viewers, and never through an application; the delivery metric and event; no
store connection held during a network call (four webhooks that never answer delay neither the
evaluator nor chat requests); and the two leak scans of section 15 with their planted controls.

The world of ``test_management_api``: north (alice and carol org_admins, vera viewer, nina
app_admin of north-shop, bob no role) and south (sid org_admin). Every attempt goes through
``webhook_client`` onto an injected resolver and network. Test values, not secrets."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from accounts_helpers import PASSWORD
from delivery_helpers import HOSTS, OK, OTHER_URL, THIRD_URL, TLS, URL, FrozenClock
from gateway.alerts.caps import AlertCaps
from gateway.alerts.channels import ChannelService
from gateway.alerts.delivery import DeliverySetup, DeliveryWorker
from gateway.alerts.notices import Notices, builtin_rule_ids
from gateway.apikeys import ApplicationAccess
from gateway.app import create_app
from gateway.auth import AccountAccess
from gateway.clock import WallClock
from gateway.continuous import build_continuous_registry
from gateway.egress import webhook_client
from gateway.evaluator import AlertEvaluator, Series, decide
from gateway.localonly import LocalOnlyPolicy
from gateway.providers import ConnectionTestCounter
from gateway.redaction import Redactor
from gateway.signals import SignalValue
from gateway.state import StateBackend, StateUnavailableError, sqlite_state
from gateway.vault import Vault
from mock_network import HANG, FakeResolver, ScriptedNetwork, http_response
from test_management_api import World, make_world

FORBIDDEN_BYTES = b'{"error":{"code":"forbidden","message":"Not permitted, or not found."}}'
MASTER = bytes(range(32))
BUILTIN_KEY = bytes(range(64, 96))
MARKER = "planted-receiver-answer-6e2f"
Session = tuple[str, str]


@dataclass
class Console:
    client: httpx.AsyncClient
    world: World
    app: Any
    state: StateBackend
    service: ChannelService
    network: ScriptedNetwork
    resolver: FakeResolver
    redactor: Redactor

    @property
    def worker(self) -> DeliveryWorker:
        found: DeliveryWorker = self.app.state.alert_delivery
        return found

    @property
    def evaluator(self) -> AlertEvaluator:
        found: AlertEvaluator = self.app.state.alert_evaluator
        return found

    async def login(self, username: str) -> Session:
        response = await self.client.post(
            "/v1/auth/login", json={"username": username, "password": PASSWORD}
        )
        self.client.cookies.clear()
        assert response.status_code == 200, response.text
        sid = response.headers["set-cookie"].split(";")[0].split("=", 1)[1]
        return sid, response.json()["csrf_token"]

    async def call(
        self, session: Session, method: str, path: str, body: dict[str, Any] | None = None
    ) -> httpx.Response:
        headers = {"Cookie": f"gw_session={session[0]}", "X-CSRF-Token": session[1]}
        url = path if path.startswith("/v1/auth") else "/v1/admin/orgs" + path
        if method in ("GET", "DELETE") and body is None:
            return await self.client.request(method, url, headers=headers)
        return await self.client.request(method, url, json=body or {}, headers=headers)

    async def create(
        self,
        session: Session,
        name: str = "Ops",
        url: str = URL,
        default: bool = True,
        org: str = "north",
    ) -> dict[str, Any]:
        stepped = await self.call(session, "POST", "/v1/auth/step-up", {"password": PASSWORD})
        assert stepped.status_code == 200, stepped.text
        made = await self.call(
            session,
            "POST",
            f"/{org}/alert-channels",
            {"type": "webhook", "name": name, "url": url, "default": default},
        )
        assert made.status_code == 201, made.text
        return dict(made.json())

    async def deliver(self) -> int:
        started = await self.worker.step()
        await self.worker.drain()
        return started


@asynccontextmanager
async def console(
    *, responses: list[Any] | None = None, caps: AlertCaps | None = None
) -> AsyncIterator[Console]:
    world = make_world()
    accounts = world.accounts
    state = sqlite_state(accounts.connection, accounts.epoch)
    caps = caps if caps is not None else AlertCaps()
    rng = random.Random(9)
    network = ScriptedNetwork(list(responses) if responses is not None else [])
    resolver = FakeResolver({h: list(a) for h, a in HOSTS.items()})
    redactor = Redactor()
    notices = Notices(
        state.channels,
        state.outbox,
        accounts.organizations,
        accounts.epoch,
        rng.randbytes,
        caps=caps,
        store_clock=state.clock,
        builtin_rule_id=builtin_rule_ids(BUILTIN_KEY),
    )
    clock = accounts.clock
    service = ChannelService(
        state.channels,
        Vault(MASTER),
        accounts.service.audit,
        accounts.epoch,
        rng.randbytes,
        notices=notices,
        store_clock=state.clock,
        delivery=DeliverySetup(
            lambda: webhook_client(
                resolver, clock, frozenset({443}), inner=network, ssl_context=TLS
            ),
            redactor,
            caps,
            state.ceilings,
        ),
    )
    registry = build_continuous_registry(clock, seed=7)
    app = create_app(
        registry,
        clock,
        applications=ApplicationAccess(world.keys, world.templates),
        accounts=AccountAccess(accounts.service),
        local_only=LocalOnlyPolicy(),
        epoch=accounts.epoch,
        rules=state.rules,
        incidents=state.incidents,
        channels=service,
        usage_store=state.usage,
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield Console(client, world, app, state, service, network, resolver, redactor)
    await registry.aclose()


async def _fire_application_incident(c: Console, channels: list[str]) -> str:
    """An incident of north-shop that fires now, through the evaluator's own steps."""
    from gateway.alertrules import AlertRule, validate_rule
    from gateway.incidents import Fence

    spec = validate_rule(
        {
            "name": "Budget",
            "signal": "budget_used",
            "comparison": ">=",
            "threshold": 80,
            "window": None,
            "for_minutes": 0,
            "severity": "critical",
            "channels": channels,
        }
    )
    rule = AlertRule("rul_00000000000000a1", "north-shop", spec, 1, None, 0.0, 0.0)
    await c.state.rules.insert_rule(rule)
    view = await c.state.incidents.attempt_lease(c.evaluator.holder)
    counter = iter(range(1, 100))
    change = decide(
        Series(rule, "north", "", SignalValue(95.0)),
        None,
        None,
        view.now_ms,
        lambda prefix: f"{prefix}{next(counter):016x}",
    )
    assert change is not None
    notices = c.service.notices
    assert notices is not None
    prepared, then = await notices.prepare(change, rule, view.now_ms)
    assert await c.state.incidents.apply(Fence(c.evaluator.holder, view.epoch), prepared, then)
    return change.incident.id


async def _fail_until_builtin_fires(c: Console, channel: str, org: str = "north") -> str:
    for _ in range(3):
        await c.state.channels.record_health(channel, False, c.world.accounts.epoch.epoch_ms())
    assert await c.evaluator.step()
    found = [i for i in await c.state.incidents.open_incidents() if i.series_key == channel]
    assert found
    assert (found[0].application_id, found[0].organization_id) == ("", org)
    return found[0].id


# --- test send ------------------------------------------------------------------------------------


async def test_a_test_send_answers_its_code_and_is_audited() -> None:
    async with console(responses=[OK]) as c:
        alice = await c.login("alice")
        made = await c.create(alice)
        sent = await c.call(alice, "POST", f"/north/alert-channels/{made['id']}/test")
        assert sent.status_code == 200, sent.text
        assert sent.json() == {"id": made["id"], "outcome": "delivered", "delivered": True}
        [connection] = c.network.connections
        body = json.loads(bytes(connection.written).partition(b"\r\n\r\n")[2])
        assert (body["kind"], body["organization"]) == ("test", {"id": "north"})
        rows = c.world.accounts.connection.execute(
            "SELECT target_id, details FROM audit_log WHERE action = 'alert_channel_tested'"
        ).fetchall()
        assert [(t, json.loads(d)["delivery_code"]) for t, d in rows] == [(made["id"], "delivered")]
        # It changes neither the outbox nor the channel's health.
        assert c.world.accounts.connection.execute(
            "SELECT count(*) FROM notification_deliveries"
        ).fetchone() == (0,)
        found = await c.call(alice, "GET", f"/north/alert-channels/{made['id']}")
        assert found.json()["health"]["last_delivered_at_ms"] is None


async def test_a_failing_test_send_answers_its_fixed_code_and_nothing_of_the_answer() -> None:
    answer = http_response(
        MARKER.encode(), "500 Internal Server Error", Content_Length=str(len(MARKER))
    )
    async with console(responses=[answer]) as c:
        alice = await c.login("alice")
        made = await c.create(alice)
        sent = await c.call(alice, "POST", f"/north/alert-channels/{made['id']}/test")
        assert sent.json()["outcome"] == "http_5xx"
        assert MARKER not in sent.text


async def test_test_sends_are_capped_per_channel_and_per_organization() -> None:
    caps = AlertCaps(test_sends_per_channel=2, test_sends_per_organization=3)
    async with console(responses=[OK] * 10, caps=caps) as c:
        alice = await c.login("alice")
        one = (await c.create(alice))["id"]
        two = (await c.create(alice, "Two", OTHER_URL))["id"]
        codes = []
        for channel in (one, one, one, two, two):
            sent = await c.call(alice, "POST", f"/north/alert-channels/{channel}/test")
            codes.append((sent.status_code, sent.json().get("error", {}).get("code")))
        assert codes == [
            (200, None),
            (200, None),
            (429, "test_send_limited"),
            (200, None),
            (429, "test_send_limited"),
        ]
        assert len(c.network.connections) == 3  # nothing sent when refused


async def test_a_counter_that_cannot_answer_refuses_the_test_send() -> None:
    class Down:
        async def count(self, provider_id: str, organization_id: str) -> None:
            raise StateUnavailableError("timeout")

    async with console(responses=[OK]) as c:
        alice = await c.login("alice")
        made = await c.create(alice)
        counter: ConnectionTestCounter = Down()
        c.app.state.alert_test_sends._counter = counter
        sent = await c.call(alice, "POST", f"/north/alert-channels/{made['id']}/test")
        assert sent.status_code == 503
        assert c.network.connections == []


async def test_a_disabled_channel_is_sent_no_test() -> None:
    async with console(responses=[OK]) as c:
        alice = await c.login("alice")
        made = await c.create(alice)
        await c.call(alice, "POST", f"/north/alert-channels/{made['id']}/disable")
        sent = await c.call(alice, "POST", f"/north/alert-channels/{made['id']}/test")
        assert (sent.status_code, sent.json()["error"]["code"]) == (409, "channel_disabled")
        assert c.network.connections == []


async def test_only_org_admins_may_test_and_never_across_organizations() -> None:
    """The ``alert_channels.test`` row, and missing equal to forbidden, byte for byte."""
    async with console(responses=[OK] * 5) as c:
        alice, sid = await c.login("alice"), await c.login("sid")
        north = (await c.create(alice))["id"]
        south = (await c.create(sid, org="south", url=OTHER_URL))["id"]
        for username in ("vera", "nina", "bob", "root"):
            session = await c.login(username)
            refused = await c.call(session, "POST", f"/north/alert-channels/{north}/test")
            assert (refused.status_code, refused.content) == (403, FORBIDDEN_BYTES), username
        for who, path in (
            (sid, f"/north/alert-channels/{north}/test"),
            (alice, f"/south/alert-channels/{south}/test"),
            (alice, f"/north/alert-channels/{south}/test"),
            (alice, "/north/alert-channels/chn_00000000000000ff/test"),
            (alice, "/north/alert-channels/not-a-channel/test"),
        ):
            refused = await c.call(who, "POST", path)
            assert (refused.status_code, refused.content) == (403, FORBIDDEN_BYTES), path
        assert c.network.connections == []


# --- an incident's deliveries ---------------------------------------------------------------------


async def test_an_incident_shows_each_notifications_deliveries() -> None:
    async with console(
        responses=[OK, http_response(b"", "404 Not Found", Content_Length="0")]
    ) as c:
        alice = await c.login("alice")
        a = (await c.create(alice))["id"]
        b = (await c.create(alice, "Two", OTHER_URL))["id"]
        incident = await _fire_application_incident(c, [a, b])
        await c.deliver()
        found = await c.call(alice, "GET", f"/north/applications/north-shop/incidents/{incident}")
        assert found.status_code == 200, found.text
        [notification] = found.json()["notifications"]
        deliveries = sorted(notification["deliveries"], key=lambda d: d["channel_id"])
        assert [set(d) for d in deliveries] == [
            {"channel_id", "state", "attempts", "last_code"}
        ] * 2
        assert sorted((d["state"], d["last_code"]) for d in deliveries) == [
            ("delivered", "delivered"),
            ("failed", "http_4xx"),
        ]


# --- the built-in's incidents: who sees them -----------------------------------------------------


async def test_the_built_ins_incidents_are_the_organizations_only() -> None:
    """Section 8.1: north's org_admins and viewer see it; an app_admin without an organization
    role does not; south's list is an empty page; no application endpoint answers it (the same
    403 bytes as an id that does not exist)."""
    async with console() as c:
        alice, sid = await c.login("alice"), await c.login("sid")
        failing = (await c.create(alice))["id"]
        await c.create(alice, "Other", OTHER_URL)
        incident = await _fail_until_builtin_fires(c, failing)
        for username in ("alice", "carol", "vera"):
            listed = await c.call(await c.login(username), "GET", "/north/incidents")
            assert listed.status_code == 200, username
            [shown] = listed.json()["incidents"]
            assert (shown["id"], shown["application_id"], shown["series"]) == (
                incident,
                None,
                failing,
            )
        nina = await c.login("nina")
        refused = await c.call(nina, "GET", "/north/incidents")
        assert (refused.status_code, refused.content) == (403, FORBIDDEN_BYTES)
        empty = await c.call(sid, "GET", "/south/incidents")
        assert empty.json() == {"incidents": [], "next": None}
        for app in ("north-shop", "north-blog"):
            listed = await c.call(alice, "GET", f"/north/applications/{app}/incidents")
            assert listed.json() == {"incidents": [], "next": None}
            for method, suffix in (("GET", ""), ("POST", "/acknowledge")):
                path = f"/north/applications/{app}/incidents/{incident}{suffix}"
                missing = f"/north/applications/{app}/incidents/inc_00000000000000ff{suffix}"
                answer = await c.call(alice, method, path)
                assert (answer.status_code, answer.content) == (403, FORBIDDEN_BYTES), path
                answer = await c.call(alice, method, missing)
                assert (answer.status_code, answer.content) == (403, FORBIDDEN_BYTES), missing


async def test_one_organizations_failing_channel_is_invisible_to_another() -> None:
    """CDF-1 and CDF-4: south's built-in incident is south's alone."""
    async with console() as c:
        alice, sid = await c.login("alice"), await c.login("sid")
        await c.create(alice)
        south_channel = (await c.create(sid, org="south", url=OTHER_URL))["id"]
        await c.create(sid, "South two", THIRD_URL, org="south")
        await _fail_until_builtin_fires(c, south_channel, org="south")
        north = await c.call(alice, "GET", "/north/incidents")
        assert north.json() == {"incidents": [], "next": None}
        south = await c.call(sid, "GET", "/south/incidents")
        assert [i["series"] for i in south.json()["incidents"]] == [south_channel]


# --- the metric and the event --------------------------------------------------------------------


async def test_a_failed_attempt_is_counted_with_an_event_and_a_log_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with console(responses=[http_response(b"", "503 Busy", Content_Length="0")]) as c:
        alice = await c.login("alice")
        a = (await c.create(alice))["id"]
        await _fire_application_incident(c, [a])
        with caplog.at_level(logging.WARNING, logger="gateway.alerts"):
            await c.deliver()
        metrics = (await c.client.get("/metrics")).text
        assert (
            'gateway_alert_deliveries_total{channel_type="webhook",result="http_5xx"} 1' in metrics
        )
        snapshot = (await c.client.get("/v1/monitor/snapshot")).json()
        [event] = [e for e in snapshot["events"]["recent"] if e["type"] == "alert_delivery_failed"]
        assert event["data"] == {"channel_id": a, "code": "http_5xx"}
        assert any(a in r.getMessage() and "http_5xx" in r.getMessage() for r in caplog.records)


# --- no store connection held during a network call ----------------------------------------------


async def test_webhooks_that_never_answer_delay_neither_the_evaluator_nor_chat_requests() -> None:
    """The pool rule on SQLite (one connection, whose transactions must never wait across an
    await): four deliveries hang at their receivers, and meanwhile an evaluator cycle completes
    and a chat request and a generate request are answered."""
    async with console() as c:
        alice = await c.login("alice")
        channels = [
            (await c.create(alice, f"C{n}", url))["id"]
            for n, url in enumerate((URL, OTHER_URL, THIRD_URL))
        ]
        await _fire_application_incident(c, channels)
        notices = c.service.notices
        assert notices is not None
        extra = await c.state.channels.get_channel(channels[0])
        assert extra is not None
        from gateway.alerts.payload import AlertPayload

        async with c.state.outbox.delivery_transaction():
            await notices.queue(
                AlertPayload("ntf_00000000000000f1", "test", 0.0, "north"), [extra], counted=False
            )
        hanging = ScriptedNetwork([HANG] * 4)
        frozen = FrozenClock(WallClock())
        worker = DeliveryWorker(
            c.state.outbox,
            c.state.channels,
            c.service.vault,
            c.redactor,
            store_clock=c.state.clock,
            clock=frozen,
            epoch=c.world.accounts.epoch,
            client=lambda: webhook_client(
                c.resolver, frozen, frozenset({443}), inner=hanging, ssl_context=TLS
            ),
            rng=random.Random(1),
            random_bytes=random.Random(2).randbytes,
        )
        assert await worker.step() == 4
        for _ in range(200):
            if len(hanging.connections) == 4:
                break
            await c.world.accounts.clock.sleep_ms(0.0)
        assert len(hanging.connections) == 4  # all four are at their receivers
        assert await c.evaluator.step()
        key = c.world.api_keys["north-shop"]
        chat = await c.client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": "economy", "messages": [{"role": "user", "content": "Hello."}]},
        )
        generated = await c.client.post(
            "/v1/generate",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": "economy", "prompt": "hello", "max_tokens": 1},
        )
        assert (chat.status_code, generated.status_code) == (200, 200)
        assert worker.running == 4  # still hanging
        for task in list(worker._running):
            task.cancel()
        await asyncio.gather(*list(worker._running), return_exceptions=True)


# --- the leak scans -------------------------------------------------------------------------------


def _scan(text: str, planted: dict[str, str]) -> list[str]:
    return sorted(name for name, value in planted.items() if value in text)


def _every_row(connection: Any) -> str:
    tables = [
        r[0]
        for r in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    ]
    parts = []
    for table in tables:
        for row in connection.execute(f"SELECT * FROM {table}").fetchall():
            parts.append(repr([v.hex() if isinstance(v, bytes) else v for v in row]))
    return "\n".join(parts)


async def test_the_channel_secrets_leak_nowhere(caplog: pytest.LogCaptureFixture) -> None:
    """Section 15's first scan. Planted: a URL with a token in its path and its query, the
    signing key, the previous key during a rotation, and a marker in the receiver's answer.
    Exercised: create, test, deliver (delivered, failing, timing out, redirected), rotate,
    re-enable, delete. Scanned: every stored row, every log record, every audit row, the
    events, the Prometheus output, /healthz, every admin answer but the two that return a key,
    and every outgoing request's headers and body. The control: each planted value is found in
    its unredacted copy."""
    token, query = "planted-leak-path-3c9e", "planted-leak-query-71fa"
    url = f"https://hooks.example.com/hook/{token}?token={query}"
    answers = [
        http_response(MARKER.encode(), Content_Length=str(len(MARKER))),  # the test send
        http_response(MARKER.encode(), Content_Length=str(len(MARKER))),  # delivered
        http_response(MARKER.encode(), "500 Oops", Content_Length=str(len(MARKER))),
        HANG,  # timing out
        http_response(
            MARKER.encode(),
            "302 Found",
            Location=f"https://10.0.0.9/{MARKER}",
            Content_Length=str(len(MARKER)),
        ),
        OK,
    ]
    async with console(responses=answers) as c:
        alice = await c.login("alice")
        answers_seen: list[str] = []
        keys: list[str] = []
        with caplog.at_level(logging.DEBUG):
            made = await c.create(alice, url=url)
            keys.append(made["signing_key"])
            channel = made["id"]
            second = (await c.create(alice, "Two", OTHER_URL))["id"]
            keys.append(
                (await c.call(alice, "POST", "/v1/auth/step-up", {"password": PASSWORD})).text
                and ""
            )
            keys.pop()
            rotated = await c.call(alice, "POST", f"/north/alert-channels/{channel}/rotate-key")
            keys.append(rotated.json()["signing_key"])
            answers_seen.append(
                (await c.call(alice, "POST", f"/north/alert-channels/{channel}/test")).text
            )
            for n in range(4):
                if n:
                    await _fire_application_incident_n(c, [channel], n)
                else:
                    await _fire_application_incident(c, [channel])
                await c.deliver()
                await c.world.accounts.clock.sleep_ms(1.0)
            for path in ("", "/deliveries"):
                answers_seen.append(
                    (await c.call(alice, "GET", f"/north/alert-channels/{channel}{path}")).text
                )
            answers_seen.append((await c.call(alice, "GET", "/north/alert-channels")).text)
            answers_seen.append(
                (await c.call(alice, "POST", f"/north/alert-channels/{channel}/disable")).text
            )
            await c.call(alice, "POST", "/v1/auth/step-up", {"password": PASSWORD})
            answers_seen.append(
                (
                    await c.call(
                        alice,
                        "PATCH",
                        f"/north/alert-channels/{channel}",
                        {"expected_version": 2, "enabled": True},
                    )
                ).text
            )
            answers_seen.append(
                (await c.call(alice, "DELETE", f"/north/alert-channels/{second}")).text
            )
            answers_seen.append((await c.client.get("/healthz")).text)
            metrics = (await c.client.get("/metrics")).text
            snapshot = (await c.client.get("/v1/monitor/snapshot?events=1000")).text
            answers_seen.append(
                (await c.call(alice, "DELETE", f"/north/alert-channels/{channel}")).text
            )
        planted = {
            "path token": token,
            "query token": query,
            "signing key": keys[0],
            "previous key": keys[0],
            "new key": keys[1],
            "marker": MARKER,
        }
        assert keys[0] != keys[1]
        outgoing = []
        for connection in c.network.connections:
            head, _, body = bytes(connection.written).partition(b"\r\n\r\n")
            lines = head.decode("ascii").split("\r\n")
            outgoing.append("\n".join(lines[1:]) + "\n" + body.decode("ascii"))
            assert lines[0].startswith(f"POST /hook/{token}?token={query} ")  # its one place
        assert len(outgoing) == 5  # test, delivered, failing, timing out, redirected
        logs = "\n".join(f"{r.getMessage()} {r.args!r} {r.exc_text or ''}" for r in caplog.records)
        scanned = {
            "stored rows": _every_row(c.world.accounts.connection),
            "logs": logs,
            "events": snapshot,
            "metrics": metrics,
            "answers": "\n".join(answers_seen),
            "outgoing headers and bodies": "\n".join(outgoing),
        }
        for where, text in scanned.items():
            assert _scan(text, planted) == [], where
        # The controls: each found where it was put on purpose.
        assert _scan(url + "".join(keys) + MARKER, planted) == sorted(planted)
        received = "\n".join(bytes(conn.written).decode("ascii") for conn in c.network.connections)
        assert _scan(received, {"path token": token, "query token": query}) == [
            "path token",
            "query token",
        ]


async def _fire_application_incident_n(c: Console, channels: list[str], n: int) -> str:
    """Another incident of another rule of north-shop, firing now."""
    from gateway.alertrules import AlertRule, validate_rule
    from gateway.incidents import Fence

    spec = validate_rule(
        {
            "name": f"Budget {n}",
            "signal": "budget_used",
            "comparison": ">=",
            "threshold": 80,
            "window": None,
            "for_minutes": 0,
            "severity": "critical",
            "channels": channels,
        }
    )
    rule = AlertRule(f"rul_{n:016x}", "north-shop", spec, 1, None, 0.0, 0.0)
    await c.state.rules.insert_rule(rule)
    view = await c.state.incidents.attempt_lease(c.evaluator.holder)
    counter = iter(range(100 * n, 100 * n + 99))
    change = decide(
        Series(rule, "north", "", SignalValue(95.0)),
        None,
        None,
        view.now_ms,
        lambda prefix: f"{prefix}{next(counter):016x}",
    )
    assert change is not None
    notices = c.service.notices
    assert notices is not None
    prepared, then = await notices.prepare(change, rule, view.now_ms)
    assert await c.state.incidents.apply(Fence(c.evaluator.holder, view.epoch), prepared, then)
    return change.incident.id


async def test_the_alert_contents_carry_no_request_data(caplog: pytest.LogCaptureFixture) -> None:
    """Section 15's second scan. Planted: a prompt, the response the gateway gave, the API key,
    a provider key and a username, all through real requests before a firing and a resolved
    notification. Scanned: every stored payload, every delivered body and header, and the
    rollups. The control: a payload with the prompt added on purpose is flagged."""
    prompt = "planted prompt about the secret harvest 4d2b"
    provider_key = "sk-planted-provider-key-8a1f"
    async with console(responses=[OK, OK]) as c:
        alice = await c.login("alice")
        channel = (await c.create(alice))["id"]
        api_key = c.world.api_keys["north-shop"]
        chat = await c.client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": "economy", "messages": [{"role": "user", "content": prompt}]},
        )
        assert chat.status_code == 200, chat.text
        response_text = chat.json()["choices"][0]["message"]["content"]
        assert response_text
        sealed = c.service.vault.seal(provider_key, _binding())
        assert sealed.ciphertext  # the provider key exists, in the vault's provider scope
        incident = await _fire_application_incident(c, [channel])
        await c.deliver()
        # Resolved: the rule is deleted, and the next cycle resolves the incident.
        await c.state.rules.delete_rule("north-shop", "rul_00000000000000a1", 1)
        await c.world.accounts.clock.sleep_ms(30_000.0)
        assert await c.evaluator.step()
        await c.deliver()
        kinds = [
            json.loads(p)["kind"]
            for (p,) in c.world.accounts.connection.execute(
                "SELECT payload FROM notification_deliveries ORDER BY rowid"
            ).fetchall()
        ]
        assert kinds == ["firing", "resolved"]
        assert incident
        planted = {
            "prompt": prompt,
            "response": response_text,
            "api key": api_key,
            "provider key": provider_key,
            "username": "alice",
        }
        stored = "\n".join(
            p
            for (p,) in c.world.accounts.connection.execute(
                "SELECT payload FROM notification_deliveries"
            ).fetchall()
        )
        sent = "\n".join(bytes(conn.written).decode("ascii") for conn in c.network.connections)
        rollups = repr(c.app.state.alert_rollups.__dict__)
        for where, text in (("stored payloads", stored), ("delivered", sent), ("rollups", rollups)):
            assert _scan(text, planted) == [], where
        # The control: the same scan flags a payload that carries the prompt.
        tampered = json.loads(stored.splitlines()[0])
        tampered["rule"]["name"] = prompt
        assert _scan(json.dumps(tampered), planted) == ["prompt"]


def _binding() -> Any:
    from gateway.vault import Binding

    return Binding(
        "north", "prv_00000000000000a1", "openai_compatible", "https://api.provider.example/v1"
    )
