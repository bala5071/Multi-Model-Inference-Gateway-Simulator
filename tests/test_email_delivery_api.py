"""Email delivery through the whole app (docs/48): the confirmation sent over SMTP when a channel
is made, the email test send's endpoint, the ``resolver_busy`` metric and event (A5-26), the gauge
of abandoned lookups, and the leak scan with ``smtplib``'s debug output forced on.

Planted: the SMTP password (sealed in the platform scope), a confirmation code (the generator
injected), a recipient address, and a marker in every reply of the test SMTP server. Scanned:
every log record at DEBUG, standard output and error, every stored row, every audit row, the
monitoring snapshot with its events, the Prometheus output and every admin answer. The address
may appear only in its recipient row and the org_admin's answers. The controls: each planted
value is found where it is meant to be (the server's records). Test values, not secrets."""

from __future__ import annotations

import json
import logging
import random
import smtplib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from accounts_helpers import PASSWORD as LOGIN_PASSWORD
from gateway.alerts import recipients as recipients_module
from gateway.alerts.caps import AlertCaps
from gateway.alerts.channels import ChannelService
from gateway.alerts.delivery import DeliverySetup, DeliveryWorker, EmailSetup
from gateway.alerts.email import EmailSettings
from gateway.alerts.notices import Notices
from gateway.alerts.payload import AlertPayload, PayloadApplication, PayloadRule
from gateway.alerts.recipients import ConfirmationCaps, InstanceConfirmations, RecipientService
from gateway.alerts.smtp import BoundedResolver, SmtpMailer, SmtpSender
from gateway.apikeys import ApplicationAccess
from gateway.app import create_app
from gateway.audit import Reason
from gateway.auth import AccountAccess
from gateway.continuous import build_continuous_registry
from gateway.egress import webhook_client
from gateway.localonly import LocalOnlyPolicy
from gateway.platformsecrets import SMTP_PASSWORD, StoredPlatformSecret
from gateway.providers import InMemoryConnectionTestCounter
from gateway.redaction import Redactor
from gateway.state import StateBackend, sqlite_state
from gateway.vault import PlatformBinding, Vault
from mock_network import FakeResolver, ScriptedNetwork
from smtp_helpers import SERVER_NAME, NeverClock, SmtpTestServer, make_certificates
from test_management_api import World, make_world

MASTER = bytes(range(32))
SMTP_SECRET = "planted-smtp-password-3b8d"
USERNAME = "gateway"
SENDER = "alerts@example.com"
ADDRESS = "planted-recipient-88@example.com"
PLANTED_CODE = "PLANTZQR23"
MARKER = "planted-server-reply-91aa"
Session = tuple[str, str]


@dataclass
class Console:
    client: httpx.AsyncClient
    world: World
    app: Any
    state: StateBackend
    server: SmtpTestServer
    sender: SmtpSender
    notices: Notices

    @property
    def worker(self) -> DeliveryWorker:
        found: DeliveryWorker = self.app.state.alert_delivery
        return found

    async def login(self, username: str) -> Session:
        response = await self.client.post(
            "/v1/auth/login", json={"username": username, "password": LOGIN_PASSWORD}
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

    async def deliver(self) -> None:
        await self.worker.step()
        await self.worker.drain()

    async def notify(self, channel_id: str) -> None:
        payload = AlertPayload(
            "ntf_00000000000000c1", "firing", 1.0, "north",
            application=PayloadApplication("north-shop", "North Shop"),
            rule=PayloadRule("rul_00000000000000a1", "Errors", "error_rate", ">", 5.0, "%", 300,
                             0, "critical"),
        )  # fmt: skip
        channel = await self.state.channels.get_channel(channel_id)
        assert channel is not None
        async with self.state.outbox.write_transaction():
            await self.notices.queue(payload, [channel], rule_id="rul_00000000000000a1")


@asynccontextmanager
async def console(tmp_path: Path, **server_options: Any) -> AsyncIterator[Console]:
    world = make_world()
    accounts = world.accounts
    state = sqlite_state(accounts.connection, accounts.epoch)
    certificates = make_certificates(tmp_path, SERVER_NAME)
    server = SmtpTestServer(
        certificates, username=USERNAME, password=SMTP_SECRET, marker=MARKER, **server_options
    )
    port = await server.start()
    settings = EmailSettings(SERVER_NAME, port, "starttls", USERNAME, SENDER,
                             frozenset({"example.com"}))  # fmt: skip
    vault = Vault(MASTER)
    where = PlatformBinding(SMTP_PASSWORD, SERVER_NAME, port, USERNAME)
    await state.platform_secrets.put(
        StoredPlatformSecret(
            SMTP_PASSWORD, vault.seal(SMTP_SECRET, where), SERVER_NAME, port, USERNAME, 1.0
        )
    )
    redactor = Redactor()
    sender = SmtpSender(
        settings, state.platform_secrets, vault, redactor, NeverClock(),
        context=certificates.client_context(),
        resolver=BoundedResolver(lambda host, port: "127.0.0.1"), held=state.outbox,
    )  # fmt: skip
    caps = AlertCaps()
    notices = Notices(
        state.channels, state.outbox, accounts.organizations, accounts.epoch,
        random.Random(3).randbytes, caps=caps, store_clock=state.clock,
        recipients=state.recipients,
    )  # fmt: skip

    async def name(organization_id: str) -> str | None:
        found = await accounts.organizations.get_organization(organization_id)
        return found.name if found is not None else None

    recipients = RecipientService(
        state.recipients, state.confirmations,
        InMemoryConnectionTestCounter(accounts.clock, 10, 20, reason=Reason.CONFIRMATION_LIMITED),
        InstanceConfirmations(accounts.clock, 200), accounts.service.audit, accounts.epoch,
        state.clock, random.Random(4), code_key=b"c" * 32, address_key=b"a" * 32,
        caps=ConfirmationCaps(3, 10, 20, 200), settings=settings,
        mailer=SmtpMailer(sender, accounts.epoch), organization_name=name, held=state.outbox,
    )  # fmt: skip
    clock = accounts.clock
    service = ChannelService(
        state.channels, vault, accounts.service.audit, accounts.epoch,
        random.Random(5).randbytes, notices=notices, store_clock=state.clock,
        recipients=recipients,
        delivery=DeliverySetup(
            lambda: webhook_client(FakeResolver({}), clock, frozenset({443}),
                                   inner=ScriptedNetwork([])),
            redactor, caps, state.ceilings, EmailSetup(sender, state.recipients),
        ),
    )  # fmt: skip
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
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            yield Console(client, world, app, state, server, sender, notices)
    finally:
        await server.stop()
        await registry.aclose()


async def _email_channel(c: Console, session: Session) -> dict[str, Any]:
    await c.call(session, "POST", "/v1/auth/step-up", {"password": LOGIN_PASSWORD})
    made = await c.call(session, "POST", "/north/alert-channels",
                        {"type": "email", "name": "Mail", "recipients": [ADDRESS]})  # fmt: skip
    assert made.status_code == 201, made.text
    return dict(made.json())


async def test_making_an_email_channel_sends_the_confirmation_over_smtp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recipients_module, "new_code", lambda rng: PLANTED_CODE)
    async with console(tmp_path) as c:
        alice = await c.login("alice")
        made = await _email_channel(c, alice)
        assert list(made["confirmations"].values()) == ["delivered"]
        [envelope] = c.server.envelopes
        assert envelope.recipients == [ADDRESS]
        assert envelope.tls
        assert b"PLANT-ZQR23" in envelope.data


async def test_the_email_test_send_goes_to_verified_recipients_over_smtp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recipients_module, "new_code", lambda rng: PLANTED_CODE)
    async with console(tmp_path) as c:
        alice = await c.login("alice")
        made = await _email_channel(c, alice)
        channel = made["id"]
        none = await c.call(alice, "POST", f"/north/alert-channels/{channel}/test")
        assert none.json()["outcome"] == "no_verified_recipient"
        recipient = made["recipients"][0]["id"]
        await c.call(alice, "POST",
                     f"/north/alert-channels/{channel}/recipients/{recipient}/confirm",
                     {"code": PLANTED_CODE})  # fmt: skip
        sent = await c.call(alice, "POST", f"/north/alert-channels/{channel}/test")
        assert sent.json() == {"id": channel, "outcome": "delivered", "delivered": True}
        assert [e.recipients for e in c.server.envelopes] == [[ADDRESS], [ADDRESS]]
        assert b"[gateway] TEST" in c.server.envelopes[1].data


async def test_resolver_busy_is_counted_published_and_seen_on_the_gauge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A5-26: a refused attempt has its metric and its event (the channel id, never an address);
    the gauge says how many lookups are abandoned and running."""
    monkeypatch.setattr(recipients_module, "new_code", lambda rng: PLANTED_CODE)
    async with console(tmp_path) as c:
        alice = await c.login("alice")
        made = await _email_channel(c, alice)
        channel, recipient = made["id"], made["recipients"][0]["id"]
        confirm = f"/north/alert-channels/{channel}/recipients/{recipient}/confirm"
        await c.call(alice, "POST", confirm, {"code": PLANTED_CODE})
        await c.notify(channel)
        resolver = c.sender.resolver
        resolver.busy = resolver.threads
        resolver.abandoned = 2
        await c.deliver()
        snapshot = (await c.client.get("/v1/monitor/snapshot?events=1000")).json()
        busy = [e for e in snapshot["events"]["recent"] if e["type"] == "alert_smtp_resolver_busy"]
        assert len(busy) == 1
        assert busy[0]["data"] == {"channel_id": channel}
        assert ADDRESS not in json.dumps(busy)
        assert snapshot["alert_deliveries"]["attempts"]["email"]["resolver_busy"] == 1
        assert snapshot["alert_deliveries"]["smtp_lookups_abandoned"] == 2
        metrics = (await c.client.get("/metrics")).text
        assert 'gateway_alert_deliveries_total{channel_type="email",result="resolver_busy"} 1' in (
            metrics
        )
        assert "gateway_alert_smtp_lookups_abandoned 2" in metrics


async def test_the_smtp_password_a_code_an_address_and_the_servers_words_leak_nowhere(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(recipients_module, "new_code", lambda rng: PLANTED_CODE)
    # smtplib's debug output prints the whole conversation, AUTH included: forced on, it must
    # still print nothing.
    monkeypatch.setattr(smtplib.SMTP, "debuglevel", 2)
    answers: list[str] = []
    with caplog.at_level(logging.DEBUG):
        async with console(tmp_path) as c:
            alice = await c.login("alice")
            made = await _email_channel(c, alice)
            answers.append(json.dumps(made))
            channel, recipient = made["id"], made["recipients"][0]["id"]
            base = f"/north/alert-channels/{channel}/recipients/{recipient}"
            for method, path, body in (
                ("POST", f"{base}/confirm", {"code": "WRONGCODE2"}),
                ("POST", f"{base}/confirm", {"code": PLANTED_CODE}),
                ("POST", f"/north/alert-channels/{channel}/test", None),
                ("GET", f"/north/alert-channels/{channel}", None),
            ):
                answers.append((await c.call(alice, method, path, body)).text)
            # A notification the server refuses with its own words (and the marker in them).
            c.server.replies["DOT"] = (554, "rejected by policy")
            await c.notify(channel)
            await c.deliver()
            answers.append(
                (await c.call(alice, "GET", f"/north/alert-channels/{channel}/deliveries")).text
            )
            snapshot = (await c.client.get("/v1/monitor/snapshot?events=1000")).text
            metrics = (await c.client.get("/metrics")).text
            db = c.world.accounts.connection
            rows = "\n".join(map(str, db.iterdump()))
            tables = [t for (t,) in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name NOT IN ('alert_recipients', 'sqlite_sequence')")]  # fmt: skip
            other_rows = "\n".join(str(r) for t in tables for r in db.execute(f"SELECT * FROM {t}"))
            recipient_rows = str(db.execute("SELECT * FROM alert_recipients").fetchall())
            audit = str(db.execute("SELECT * FROM audit_log").fetchall())
            server_sent = "\n".join(c.server.sent)
            server_logins = list(c.server.logins)
            server_mail = b"\n".join(e.data for e in c.server.envelopes).decode("utf-8", "replace")
            envelopes = repr([e.recipients for e in c.server.envelopes])
    out, err = capfd.readouterr()
    logs = "\n".join(f"{r.name} {r.getMessage()} {r.exc_text or ''}" for r in caplog.records)
    places = {
        "logs": logs, "stdout": out, "stderr": err, "rows": rows, "audit": audit,
        "snapshot": snapshot, "metrics": metrics, "answers": "\n".join(answers),
    }  # fmt: skip
    for name, value in (("password", SMTP_SECRET), ("code", PLANTED_CODE), ("marker", MARKER)):
        for place, text in places.items():
            assert value not in text, (name, place)
    assert "PLANT-ZQR23" not in logs + out + err + audit + snapshot
    for place in ("logs", "stdout", "stderr", "audit", "snapshot", "metrics"):
        assert ADDRESS not in places[place], place
    assert ADDRESS not in other_rows  # only in its recipient row
    # The controls: each planted value is where it is meant to be.
    assert (USERNAME, SMTP_SECRET) in server_logins
    assert "PLANT-ZQR23" in server_mail
    assert MARKER in server_sent
    assert ADDRESS in envelopes
    assert ADDRESS in recipient_rows
    assert "smtp_5xx" in answers[-1]
