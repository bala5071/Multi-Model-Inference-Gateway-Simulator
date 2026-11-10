"""Email channels and their recipients over HTTP (docs/48): creating one (with step-up), its view
for the org_admin, no URL and no key; the four recipient endpoints as documented in note 48;
step-up used once on adding; one answer, byte for byte, for every refused code; nothing across
organizations and missing equal to forbidden on every new endpoint; only org_admins (the new
permission row's refusal test); email not set up answered 422; and the leak scan of the SMTP
password, the confirmation code and a recipient address, with its planted controls.

The world of ``test_management_api``: north (alice and carol org_admins, vera viewer, nina
app_admin of north-shop, bob no role) and south (sid org_admin). Test values, not secrets."""

from __future__ import annotations

import json
import logging
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from gateway.alertrules import SqliteRuleStore
from gateway.alerts import recipients as recipients_module
from gateway.alerts.channels import ChannelService, SqliteChannelStore
from gateway.alerts.email import EmailSettings
from gateway.alerts.notices import Notices
from gateway.alerts.outbox import SqliteOutboxStore
from gateway.alerts.recipients import (
    Confirmation,
    ConfirmationCaps,
    InstanceConfirmations,
    RecipientService,
    SqliteConfirmationStore,
    SqliteRecipientStore,
)
from gateway.apikeys import ApplicationAccess
from gateway.app import create_app
from gateway.audit import Reason
from gateway.auth import AccountAccess
from gateway.clock import EpochStoreClock
from gateway.continuous import build_continuous_registry
from gateway.incidents import SqliteIncidentStore
from gateway.localonly import LocalOnlyPolicy
from gateway.platformsecrets import SMTP_PASSWORD, SqlitePlatformSecretStore, StoredPlatformSecret
from gateway.providers import InMemoryConnectionTestCounter
from gateway.vault import PlatformBinding, Vault
from test_alert_channels_api import (
    FORBIDDEN_BYTES,
    MASTER,
    STEP_UP_REQUIRED,
    URL,
    Console,
    Endpoint,
    documented_endpoints,
)
from test_management_api import make_world

ENDPOINTS = documented_endpoints("48-email-channels.md")
SETTINGS = EmailSettings(
    "smtp.example.com", 587, "starttls", "gateway", "alerts@example.com",
    frozenset({"example.com"}),
)  # fmt: skip
ADDRESS = "planted-recipient-55@example.com"
OTHER_ADDRESS = "second-recipient@example.com"
SMTP_SECRET = "planted-smtp-password-9d3f20"
PLANTED_CODE = "PLANTXDZ23"
INVALID = (
    b'{"error":{"code":"confirmation_invalid",'
    b'"message":"the code is not valid for this recipient"}}'
)


@dataclass
class RecordingMailer:
    sent: list[Confirmation] = field(default_factory=list)

    async def send_confirmation(self, confirmation: Confirmation) -> str:
        self.sent.append(confirmation)
        return "delivered"


@dataclass
class EmailConsole(Console):
    mailer: RecordingMailer = field(default_factory=RecordingMailer)

    async def create_email(
        self, session: tuple[str, str], addresses: list[str] | None = None, org: str = "north",
        name: str = "Mail",
    ) -> dict[str, Any]:  # fmt: skip
        await self.step_up(session)
        made = await self.call(
            session, "POST", f"/{org}/alert-channels",
            {"type": "email", "name": name, "recipients": addresses or [ADDRESS]},
        )  # fmt: skip
        assert made.status_code == 201, made.text
        return dict(made.json())


@asynccontextmanager
async def console(
    seed: int = 5, *, mailer: bool = True, caps: ConfirmationCaps | None = None
) -> AsyncIterator[EmailConsole]:
    world = make_world()
    accounts = world.accounts
    connection = accounts.connection
    store = SqliteChannelStore(connection)
    outbox = SqliteOutboxStore(connection)
    notices = Notices(
        store, outbox, accounts.organizations, accounts.epoch, random.Random(seed + 1).randbytes
    )
    recorder = RecordingMailer()
    caps = caps or ConfirmationCaps(3, 10, 20, 200)

    async def organization_name(organization_id: str) -> str | None:
        found = await accounts.organizations.get_organization(organization_id)
        return found.name if found is not None else None

    store_clock = EpochStoreClock(accounts.epoch)
    recipients = RecipientService(
        SqliteRecipientStore(connection),
        SqliteConfirmationStore(connection, store_clock),
        InMemoryConnectionTestCounter(
            accounts.clock, caps.per_channel, caps.per_organization,
            reason=Reason.CONFIRMATION_LIMITED,
            what=("one channel's recipients", "an organization's recipients"),
            noun="confirmations to",
        ),
        InstanceConfirmations(accounts.clock, caps.per_instance),
        accounts.service.audit, accounts.epoch, store_clock, random.Random(seed + 2),
        code_key=b"c" * 32, address_key=b"a" * 32, caps=caps, settings=SETTINGS,
        mailer=recorder if mailer else None, organization_name=organization_name, held=outbox,
    )  # fmt: skip
    service = ChannelService(
        store, Vault(MASTER), accounts.service.audit, accounts.epoch,
        random.Random(seed).randbytes, notices=notices, recipients=recipients,
    )  # fmt: skip
    registry = build_continuous_registry(accounts.clock, seed=7)
    app = create_app(
        registry,
        accounts.clock,
        applications=ApplicationAccess(world.keys, world.templates),
        accounts=AccountAccess(accounts.service),
        local_only=LocalOnlyPolicy(),
        epoch=accounts.epoch,
        rules=SqliteRuleStore(connection),
        incidents=SqliteIncidentStore(connection, accounts.epoch),
        channels=service,
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield EmailConsole(client, world, app, service, recorder)
    await registry.aclose()


def _path(endpoint: Endpoint, org: str, channel: str, recipient: str) -> str:
    return (
        endpoint.path.replace("/{org}", f"/{org}")
        .replace("{channel}", channel)
        .replace("{recipient}", recipient)
    )


def _body(endpoint: Endpoint) -> dict[str, Any] | None:
    if endpoint.path.endswith("/recipients"):
        return {"address": OTHER_ADDRESS}
    if endpoint.path.endswith("/confirm"):
        return {"code": PLANTED_CODE}
    return None


# --- an email channel ----------------------------------------------------------------------------


async def test_an_email_channel_is_made_with_step_up_and_unverified_recipients_no_url_no_key(
) -> None:  # fmt: skip
    async with console() as c:
        alice = await c.login("alice")
        body = {"type": "email", "name": "Mail", "recipients": [ADDRESS, OTHER_ADDRESS]}
        refused = await c.call(alice, "POST", "/north/alert-channels", body)
        assert (refused.status_code, refused.json()["error"]["code"]) == (403, STEP_UP_REQUIRED)
        assert c.mailer.sent == []
        made = await c.create_email(alice, [ADDRESS, OTHER_ADDRESS])
        assert made["type"] == "email"
        assert made["url_origin"] is None
        assert "signing_key" not in made
        assert [(r["address"], r["state"]) for r in made["recipients"]] == [
            (ADDRESS, "unverified"), (OTHER_ADDRESS, "unverified"),
        ]  # fmt: skip
        assert set(made["confirmations"].values()) == {"delivered"}
        assert sorted(s.address for s in c.mailer.sent) == sorted([ADDRESS, OTHER_ADDRESS])
        assert all(s.organization_name == "North" for s in c.mailer.sent)
        for code in (s.code for s in c.mailer.sent):
            assert code not in json.dumps(made)
        # The step-up window was used up.
        again = await c.call(alice, "POST", "/north/alert-channels", body | {"name": "Mail 2"})
        assert again.json()["error"]["code"] == STEP_UP_REQUIRED
        # The org_admin's views list the recipients, never a code or a digest.
        viewed = (await c.call(alice, "GET", f"/north/alert-channels/{made['id']}")).json()
        assert sorted(r["address"] for r in viewed["recipients"]) == sorted(
            [ADDRESS, OTHER_ADDRESS]
        )
        assert all("code" not in key for r in viewed["recipients"] for key in r if key != "id"
                   and key != "confirmation_expires_at_ms")  # fmt: skip
        listed = (await c.call(alice, "GET", "/north/alert-channels")).json()["channels"]
        assert listed[0]["recipients"] == viewed["recipients"]


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"type": "email", "name": "M", "recipients": []}, "recipients"),
        ({"type": "email", "name": "M", "recipients": [f"r{n}@example.com" for n in range(11)]},
         "recipients"),
        ({"type": "email", "name": "M", "recipients": [ADDRESS, ADDRESS]}, "recipients"),
        ({"type": "email", "name": "M", "recipients": ["ops@eu.example.com"]}, "recipients"),
        ({"type": "email", "name": "M", "recipients": ["Ops <ops@example.com>"]}, "recipients"),
        ({"type": "email", "name": "M", "recipients": [ADDRESS], "url": URL}, "url"),
        ({"type": "webhook", "name": "W", "url": URL, "recipients": [ADDRESS]}, "recipients"),
    ],
)  # fmt: skip
async def test_an_email_channel_that_is_not_valid_is_refused_naming_the_field_never_the_value(
    body: dict[str, Any], field: str
) -> None:
    async with console() as c:
        alice = await c.login("alice")
        await c.step_up(alice)
        answer = await c.call(alice, "POST", "/north/alert-channels", body)
        assert answer.status_code == 422, answer.text
        assert answer.json()["error"]["details"][0]["field"] == field
        for value in body.get("recipients", []):
            assert value not in answer.text
        assert c.mailer.sent == []
        # The window was given back: nothing changed.
        made = await c.call(alice, "POST", "/north/alert-channels",
                            {"type": "email", "name": "Ok", "recipients": [ADDRESS]})  # fmt: skip
        assert made.status_code == 201


async def test_without_a_mailer_email_is_not_set_up_and_answers_422() -> None:
    async with console(mailer=False) as c:
        alice = await c.login("alice")
        await c.step_up(alice)
        answer = await c.call(alice, "POST", "/north/alert-channels",
                              {"type": "email", "name": "M", "recipients": [ADDRESS]})  # fmt: skip
        assert answer.status_code == 422
        assert answer.json()["error"]["details"] == [
            {"field": "type", "message": "email is not set up on this server"}
        ]
        assert await c.service.store.count_channels("north") == 0


async def test_an_email_channel_has_no_url_and_no_key_to_rotate_and_its_recipients_go_with_it(
) -> None:  # fmt: skip
    async with console() as c:
        alice = await c.login("alice")
        made = await c.create_email(alice)
        await c.step_up(alice)
        patched = await c.call(alice, "PATCH", f"/north/alert-channels/{made['id']}",
                               {"expected_version": 1, "url": URL})  # fmt: skip
        assert patched.status_code == 422
        assert patched.json()["error"]["details"][0]["field"] == ("url")
        await c.step_up(alice)
        rotated = await c.call(alice, "POST", f"/north/alert-channels/{made['id']}/rotate-key")
        assert (rotated.status_code, rotated.json()["error"]["code"]) == (
            409, "channel_has_no_key",
        )  # fmt: skip
        renamed = await c.call(alice, "PATCH", f"/north/alert-channels/{made['id']}",
                               {"expected_version": 1, "name": "Mail renamed"})  # fmt: skip
        if renamed.json().get("error", {}).get("code") == STEP_UP_REQUIRED:
            await c.step_up(alice)
            renamed = await c.call(alice, "PATCH", f"/north/alert-channels/{made['id']}",
                                   {"expected_version": 1, "name": "Mail renamed"})  # fmt: skip
        assert renamed.status_code == 200, renamed.text
        deleted = await c.call(alice, "DELETE", f"/north/alert-channels/{made['id']}")
        assert deleted.status_code == 200
        assert c.db.execute("SELECT count(*) FROM alert_recipients").fetchone()[0] == 0


# --- the recipient endpoints ----------------------------------------------------------------------


async def test_adding_a_recipient_needs_a_step_up_used_once() -> None:
    async with console() as c:
        alice = await c.login("alice")
        made = await c.create_email(alice)
        path = f"/north/alert-channels/{made['id']}/recipients"
        refused = await c.call(alice, "POST", path, {"address": OTHER_ADDRESS})
        assert refused.json()["error"]["code"] == STEP_UP_REQUIRED
        await c.step_up(alice)
        added = await c.call(alice, "POST", path, {"address": OTHER_ADDRESS})
        assert added.status_code == 201, added.text
        assert (added.json()["state"], added.json()["confirmation"]) == ("unverified", "delivered")
        third = await c.call(alice, "POST", path, {"address": "third@example.com"})
        assert third.json()["error"]["code"] == STEP_UP_REQUIRED
        # On a webhook channel there are no recipients.
        hook = await c.create(alice)
        await c.step_up(alice)
        webhook = await c.call(alice, "POST", f"/north/alert-channels/{hook['id']}/recipients",
                               {"address": OTHER_ADDRESS})  # fmt: skip
        assert (webhook.status_code, webhook.json()["error"]["code"]) == (409, "channel_not_email")


async def test_resend_confirm_and_remove_over_http() -> None:
    async with console() as c:
        alice = await c.login("alice")
        made = await c.create_email(alice)
        [recipient] = made["recipients"]
        base = f"/north/alert-channels/{made['id']}/recipients/{recipient['id']}"
        first = c.mailer.sent[-1].code
        resent = await c.call(alice, "POST", base + "/resend")
        assert resent.status_code == 200
        assert resent.json()["confirmation"] == "delivered"
        second = c.mailer.sent[-1].code
        old = await c.call(alice, "POST", base + "/confirm", {"code": first})
        assert (old.status_code, old.content) == (409, INVALID)
        confirmed = await c.call(alice, "POST", base + "/confirm",
                                 {"code": f"{second[:5]}-{second[5:].lower()}"})  # fmt: skip
        assert confirmed.status_code == 200
        assert confirmed.json()["state"] == "verified"
        verified = await c.call(alice, "POST", base + "/resend")
        assert (verified.status_code, verified.json()["error"]["code"]) == (
            409, "recipient_verified",
        )  # fmt: skip
        removed = await c.call(alice, "DELETE", base)
        assert removed.json() == {"deleted": recipient["id"]}
        gone = await c.call(alice, "POST", base + "/resend")
        assert (gone.status_code, gone.content) == (403, FORBIDDEN_BYTES)


async def test_every_refused_code_is_one_answer_byte_for_byte() -> None:
    async with console() as c:
        alice, sid = await c.login("alice"), await c.login("sid")
        made = await c.create_email(alice, [ADDRESS, OTHER_ADDRESS])
        south = await c.create_email(sid, [ADDRESS], org="south")
        one, two = made["recipients"]
        base = f"/north/alert-channels/{made['id']}/recipients"
        code_two = next(s.code for s in c.mailer.sent if s.recipient_id == two["id"])
        await c.call(alice, "POST", f"{base}/{two['id']}/confirm", {"code": code_two})
        answers = []
        for recipient, code in (
            (one["id"], "WRONGCODE2"),  # wrong
            (one["id"], ""),
            (two["id"], code_two),  # used
            ("rcp_00000000000000ff", code_two),  # unknown
            ("not-a-recipient-id", code_two),
            (south["recipients"][0]["id"], c.mailer.sent[-1].code),  # another organization's
        ):
            answer = await c.call(alice, "POST", f"{base}/{recipient}/confirm", {"code": code})
            answers.append((answer.status_code, answer.content))
        assert set(answers) == {(409, INVALID)}


async def test_the_confirmation_caps_answer_429_over_http() -> None:
    async with console(caps=ConfirmationCaps(2, 60, 120, 1200)) as c:
        alice = await c.login("alice")
        made = await c.create_email(alice)
        [recipient] = made["recipients"]
        base = f"/north/alert-channels/{made['id']}/recipients/{recipient['id']}"
        assert (await c.call(alice, "POST", base + "/resend")).status_code == 200
        limited = await c.call(alice, "POST", base + "/resend")
        assert (limited.status_code, limited.json()["error"]["code"]) == (
            429, "confirmation_limited",
        )  # fmt: skip
        assert len(c.mailer.sent) == 2


async def test_nothing_crosses_organizations_and_missing_looks_forbidden() -> None:
    """Every endpoint of note 48: another organization's channel or recipient, the wrong
    organization's path, a missing or malformed channel, and (where the path resolves it) a
    missing, malformed or another channel's recipient: 403, byte for byte, as forbidden."""
    async with console() as c:
        alice, sid = await c.login("alice"), await c.login("sid")
        north = await c.create_email(alice)
        other = await c.create_email(alice, [OTHER_ADDRESS], name="Mail two")
        south = await c.create_email(sid, [ADDRESS], org="south")
        n_rcp, o_rcp, s_rcp = (x["recipients"][0]["id"] for x in (north, other, south))
        before = c.db.execute("SELECT * FROM alert_recipients ORDER BY id").fetchall()
        sent = len(c.mailer.sent)
        checked = 0
        for endpoint in ENDPOINTS:
            body = _body(endpoint)
            tries = [
                (sid, _path(endpoint, "north", north["id"], n_rcp)),  # another organization's
                (alice, _path(endpoint, "south", south["id"], s_rcp)),
                (alice, _path(endpoint, "north", south["id"], s_rcp)),  # the wrong path
                (alice, _path(endpoint, "north", "chn_00000000000000ff", n_rcp)),  # missing
                (alice, _path(endpoint, "north", "not-a-channel-id", n_rcp)),
            ]
            if "{recipient}" in endpoint.path and not endpoint.path.endswith("/confirm"):
                tries += [
                    (alice, _path(endpoint, "north", north["id"], "rcp_00000000000000ff")),
                    (alice, _path(endpoint, "north", north["id"], "not-a-recipient-id")),
                    (alice, _path(endpoint, "north", north["id"], o_rcp)),  # another channel's
                ]
            for who, path in tries:
                if endpoint.success == 201 or endpoint.method == "DELETE":
                    await c.step_up(who)
                answer = await c.call(who, endpoint.method, path, body)
                assert (answer.status_code, answer.content) == (403, FORBIDDEN_BYTES), (
                    endpoint, path, answer.text,
                )  # fmt: skip
                checked += 1
        assert checked == 4 * 5 + 2 * 3
        assert c.db.execute("SELECT * FROM alert_recipients ORDER BY id").fetchall() == before
        assert len(c.mailer.sent) == sent


async def test_only_org_admins_reach_the_recipient_endpoints() -> None:
    """The new permission row's refusal test: a viewer, an app_admin, a user with no role and a
    platform_admin are refused every recipient endpoint, as forbidden."""
    async with console() as c:
        alice = await c.login("alice")
        made = await c.create_email(alice)
        recipient = made["recipients"][0]["id"]
        sent = len(c.mailer.sent)
        for username in ("vera", "nina", "bob", "root"):
            session = await c.login(username)
            for endpoint in ENDPOINTS:
                answer = await c.call(
                    session, endpoint.method, _path(endpoint, "north", made["id"], recipient),
                    _body(endpoint),
                )  # fmt: skip
                assert (answer.status_code, answer.content) == (403, FORBIDDEN_BYTES), (
                    username, endpoint,
                )  # fmt: skip
        assert len(c.mailer.sent) == sent
        # The control: carol, the other org_admin, may.
        carol = await c.login("carol")
        resent = await c.call(
            carol, "POST", f"/north/alert-channels/{made['id']}/recipients/{recipient}/resend"
        )
        assert resent.status_code == 200


async def test_every_recipient_endpoint_answers_no_store() -> None:
    async with console() as c:
        alice = await c.login("alice")
        made = await c.create_email(alice)
        recipient = made["recipients"][0]["id"]
        for endpoint in ENDPOINTS:
            if endpoint.success == 201:
                await c.step_up(alice)
            answer = await c.call(alice, endpoint.method,
                                  _path(endpoint, "north", made["id"], recipient),
                                  _body(endpoint))  # fmt: skip
            assert answer.headers["cache-control"] == "no-store", endpoint


# --- the leak scan --------------------------------------------------------------------------------


async def test_the_smtp_password_a_code_and_an_address_leak_nowhere(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Planted: the SMTP password (sealed in the platform scope), a confirmation code (the
    generator injected) and a recipient address. Exercised: create, add, re-send, confirm
    (wrong, then right), remove, delete. Scanned: every stored row (the platform row's
    ciphertext included), every log record at DEBUG, every audit row, every admin answer. The
    address may appear only in its recipient row and the org_admin's answers. The controls:
    each planted value is found in its unredacted copy."""
    monkeypatch.setattr(recipients_module, "new_code", lambda rng: PLANTED_CODE)
    answers: list[str] = []
    with caplog.at_level(logging.DEBUG):
        async with console() as c:
            vault = Vault(MASTER)
            where = PlatformBinding(SMTP_PASSWORD, "smtp.example.com", 587, "gateway")
            platform = SqlitePlatformSecretStore(c.db)
            await platform.put(
                StoredPlatformSecret(
                    SMTP_PASSWORD,
                    vault.seal(SMTP_SECRET, where),
                    "smtp.example.com",
                    587,
                    "gateway",
                    1.0,
                )
            )
            alice = await c.login("alice")
            made = await c.create_email(alice, [ADDRESS])
            answers.append(json.dumps(made))
            channel = made["id"]
            recipient = made["recipients"][0]["id"]
            base = f"/north/alert-channels/{channel}/recipients"
            await c.step_up(alice)
            added = await c.call(alice, "POST", base, {"address": OTHER_ADDRESS})
            answers.append(added.text)
            for method, path, body in (
                ("POST", f"{base}/{recipient}/resend", None),
                ("POST", f"{base}/{recipient}/confirm", {"code": "WRONGCODE2"}),
                ("POST", f"{base}/{recipient}/confirm", {"code": PLANTED_CODE}),
                ("GET", f"/north/alert-channels/{channel}", None),
                ("DELETE", f"{base}/{added.json()['id']}", None),
            ):
                answers.append((await c.call(alice, method, path, body)).text)
            rows = "\n".join(map(str, c.db.iterdump()))
            tables = [
                t
                for (t,) in c.db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                    " AND name NOT IN ('alert_recipients', 'sqlite_sequence')"
                )
            ]
            other_rows = "\n".join(
                str(row) for t in tables for row in c.db.execute(f"SELECT * FROM {t}")
            )
            recipient_rows = "\n".join(
                map(str, c.db.execute("SELECT * FROM alert_recipients").fetchall())
            )
            audit = "\n".join(map(str, c.db.execute("SELECT * FROM audit_log").fetchall()))
            sealed = await platform.get(SMTP_PASSWORD)
            assert sealed is not None
            opened = vault.open(sealed.sealed, where).reveal()
            mailed = repr([(m.address, m.code) for m in c.mailer.sent])
            deleted = await c.call(alice, "DELETE", f"/north/alert-channels/{channel}")
            answers.append(deleted.text)
    logs = "\n".join(f"{r.name} {r.getMessage()} {r.exc_text or ''}" for r in caplog.records)
    answer_text = "\n".join(answers)
    for name, value in (("password", SMTP_SECRET), ("code", PLANTED_CODE)):
        for where_, text in (("rows", rows), ("logs", logs), ("audit", audit),
                             ("answers", answer_text)):  # fmt: skip
            assert value not in text, (name, where_)
    assert ADDRESS not in logs
    assert ADDRESS not in audit
    assert ADDRESS not in other_rows  # only in its recipient row
    # The controls: each planted value is found where it is meant to be, in clear.
    assert opened == SMTP_SECRET
    assert PLANTED_CODE in mailed
    assert ADDRESS in mailed
    assert ADDRESS in recipient_rows
    assert ADDRESS in answer_text
