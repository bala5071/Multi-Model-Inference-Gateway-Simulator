"""Email delivery (docs/48, the A5 plan's section 7): the SMTP sender against the test server, and
the delivery worker through the outbox.

- TLS always: STARTTLS required (A5-1), the certificate checked (A5-2), implicit TLS, and TLS
  without a login too (A5-23); 4xx and 5xx (A5-13).
- The hard deadline: a server sending a byte at a time ends ``timeout`` once the budget is spent
  (A5-19), a hanging one at the budget, and the attempt holds its slot until its thread returns
  (A5-20).
- The name lookup: a resolver that never answers; past ``LOOKUP_THREADS`` abandoned lookups an
  attempt is refused ``resolver_busy`` before any lookup (A5-25), counted toward health (A5-26).
- One message per recipient naming only it (A5-9), headers that refuse injection (A5-10), the
  Message-ID the same across retries; verified recipients only, in notifications, summaries
  and test sends (M48); the caps per recipient (A5-21); a domain no longer allowed at send
  (A5-3); no store connection held while sending (A5-17).

Test values, not secrets.
"""

from __future__ import annotations

import asyncio
import email
import email.policy
import json
import random
import smtplib
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest

from delivery_helpers import ACTOR, T0, World, sqlite_world
from gateway.alerts.caps import AlertCaps
from gateway.alerts.channels import Channel
from gateway.alerts.delivery import DeliveryWorker, EmailSetup, TestSends
from gateway.alerts.email import EmailSettings
from gateway.alerts.outbox import DeliveryState
from gateway.alerts.payload import AlertPayload, PayloadApplication, PayloadIncident, PayloadRule
from gateway.alerts.recipients import UNVERIFIED, VERIFIED, Confirmation, Recipient
from gateway.alerts.smtp import (
    LOOKUP_THREADS,
    BoundedResolver,
    Deadline,
    DeadlineSMTP,
    SmtpMailer,
    SmtpSender,
    confirmation_message,
    notification_message,
)
from gateway.audit import AuditLog, Reason
from gateway.clock import ClockEpoch, SimClock, WallClock
from gateway.platformsecrets import SMTP_PASSWORD, InMemoryPlatformSecretStore, StoredPlatformSecret
from gateway.providers import InMemoryConnectionTestCounter
from gateway.redaction import Redactor
from gateway.vault import PlatformBinding, Vault
from smtp_helpers import (
    SERVER_NAME,
    Certificates,
    NeverClock,
    SmtpTestServer,
    StepClock,
    make_certificates,
    real_pause,
)

MASTER = bytes(range(32))
PASSWORD = "planted-smtp-password-7a2e"
USERNAME = "gateway"
SENDER = "alerts@example.com"
ONE, TWO, THREE = "ops@example.com", "oncall@example.com", "late@example.com"
DOMAINS = frozenset({"example.com"})


def _settings(port: int, *, tls: str = "starttls", username: str | None = USERNAME,
              host: str = SERVER_NAME) -> EmailSettings:  # fmt: skip
    return EmailSettings(host, port, tls, username, SENDER, DOMAINS)


def _sender(
    certificates: Certificates,
    port: int,
    *,
    clock: Any = None,
    tls: str = "starttls",
    username: str | None = USERNAME,
    password: str | None = PASSWORD,
    lookup: Callable[[str, int], str] | None = None,
    resolver: BoundedResolver | None = None,
    budget_ms: float = 10_000.0,
    held: Any = None,
    sealed_for: PlatformBinding | None = None,
    factory: Any = None,
) -> SmtpSender:
    settings = _settings(port, tls=tls, username=username)
    store = InMemoryPlatformSecretStore()
    vault = Vault(MASTER)
    if password is not None and username is not None:
        where = sealed_for or PlatformBinding(SMTP_PASSWORD, SERVER_NAME, port, username)
        asyncio.get_event_loop()
        store._rows[SMTP_PASSWORD] = StoredPlatformSecret(
            SMTP_PASSWORD, vault.seal(password, where), where.host, where.port, where.username, 1.0
        )
    return SmtpSender(
        settings, store, vault, Redactor(), clock if clock is not None else NeverClock(),
        context=certificates.client_context(),
        resolver=resolver or BoundedResolver(lookup or (lambda host, port: "127.0.0.1")),
        held=held, budget_ms=budget_ms, smtp_factory=factory,
    )  # fmt: skip


def _payload(kind: str = "firing", app_name: str = "North Shop") -> dict[str, Any]:
    return dict(json.loads(
        AlertPayload(
            "ntf_00000000000000a1", kind, T0, "north",
            application=PayloadApplication("north-shop", app_name),
            rule=PayloadRule("rul_00000000000000a1", "Errors", "error_rate", ">", 5.0, "%", 300,
                             0, "critical"),
            incident=PayloadIncident("inc_00000000000000a1", "firing", None, 7.5,
                                     "2026-01-01T00:00:00Z", None, None),
            console_path="/v1/admin/orgs/north/applications/north-shop/incidents/x",
        ).body()
    ))  # fmt: skip


def _message(recipient: str = ONE, recipient_id: str = "rcp_0000000000000001") -> EmailMessage:
    return notification_message(SENDER, recipient, recipient_id, _payload(), T0)


@asynccontextmanager
async def smtp(tmp_path: Path, name: str = SERVER_NAME, **options: Any) -> AsyncIterator[
    tuple[SmtpTestServer, Certificates]
]:  # fmt: skip
    certificates = make_certificates(tmp_path / name, name)
    server = SmtpTestServer(certificates, username=USERNAME, password=PASSWORD, **options)
    await server.start()
    try:
        yield server, certificates
    finally:
        await server.stop()


def _parsed(data: bytes) -> email.message.Message:
    return email.message_from_bytes(data, policy=email.policy.default)


# --- TLS, always ---------------------------------------------------------------------------------


async def test_a_message_goes_over_starttls_after_a_login_to_one_recipient(tmp_path: Path) -> None:
    async with smtp(tmp_path) as (server, certificates):
        code = await _sender(certificates, server.port).send(ONE, _message())
        assert code == "delivered"
        [envelope] = server.envelopes
        assert (envelope.tls, envelope.user, envelope.sender, envelope.recipients) == (
            True, USERNAME, SENDER, [ONE],
        )  # fmt: skip
        assert server.logins == [(USERNAME, PASSWORD)]
        # Nothing but the greeting's EHLO and STARTTLS was said in the clear.
        assert server.plain_commands == ["EHLO", "STARTTLS"]
        parsed = _parsed(envelope.data)
        assert parsed["To"] == ONE
        assert parsed["From"] == SENDER


async def test_a_server_that_does_not_offer_starttls_is_refused_before_anything_is_sent(
    tmp_path: Path,
) -> None:
    """A5-1: no STARTTLS offered: ``tls``, before any credential or message."""
    async with smtp(tmp_path, starttls=False) as (server, certificates):
        assert await _sender(certificates, server.port).send(ONE, _message()) == "tls"
        assert server.logins == []
        assert server.envelopes == []
        assert "MAIL" not in server.plain_commands
        assert "AUTH" not in server.plain_commands


async def test_a_certificate_for_another_name_is_refused(tmp_path: Path) -> None:
    """A5-2: the certificate is checked against ``GATEWAY_SMTP_HOST``."""
    async with smtp(tmp_path, name="other.test") as (server, certificates):
        assert await _sender(certificates, server.port).send(ONE, _message()) == "tls"
        assert server.logins == []
        assert server.envelopes == []


@pytest.mark.parametrize(("name", "code"), [(SERVER_NAME, "delivered"), ("other.test", "tls")])
async def test_implicit_tls_from_the_first_byte(tmp_path: Path, name: str, code: str) -> None:
    async with smtp(tmp_path, name=name, implicit=True) as (server, certificates):
        implicit = _sender(certificates, server.port, tls="implicit")
        assert await implicit.send(ONE, _message()) == code
        assert len(server.envelopes) == (1 if code == "delivered" else 0)
        assert server.plain_commands == []


async def test_without_a_login_tls_is_still_required(tmp_path: Path) -> None:
    """A5-23: an operator's relay with no username: no AUTH, and TLS all the same."""
    async with smtp(tmp_path) as (server, certificates):
        relay = _sender(certificates, server.port, username=None)
        assert await relay.send(ONE, _message()) == "delivered"
        assert server.logins == []
        assert server.envelopes[0].tls
        assert server.plain_commands == ["EHLO", "STARTTLS"]
    async with smtp(tmp_path / "plain", starttls=False) as (server, certificates):
        relay = _sender(certificates, server.port, username=None)
        assert await relay.send(ONE, _message()) == "tls"
        assert server.envelopes == []


@pytest.mark.parametrize(
    ("replies", "code"),
    [
        ({"RCPT": (450, "mailbox busy")}, "smtp_4xx"),
        ({"RCPT": (550, "no such user")}, "smtp_5xx"),
        ({"MAIL": (421, "closing")}, "smtp_4xx"),
        ({"DOT": (451, "try again")}, "smtp_4xx"),
        ({"DOT": (554, "rejected")}, "smtp_5xx"),
        ({"AUTH": (535, "bad credentials")}, "smtp_5xx"),
        ({"GREETING": (554, "go away")}, "smtp_5xx"),
    ],
)
async def test_the_servers_refusals_are_codes_never_its_words(
    tmp_path: Path, replies: dict[str, tuple[int, str]], code: str
) -> None:
    async with smtp(tmp_path, replies=replies) as (server, certificates):
        assert await _sender(certificates, server.port).send(ONE, _message()) == code


async def test_a_password_that_cannot_be_read_sends_nothing(tmp_path: Path) -> None:
    async with smtp(tmp_path) as (server, certificates):
        none = _sender(certificates, server.port, password=None)
        assert await none.send(ONE, _message()) == "smtp_credential_unreadable"
        moved = _sender(
            certificates, server.port,
            sealed_for=PlatformBinding(SMTP_PASSWORD, SERVER_NAME, 2525, USERNAME),
        )  # fmt: skip
        moved.settings = replace(moved.settings, port=server.port)
        assert await moved.send(ONE, _message()) == "smtp_credential_unreadable"
        assert server.connections == 0


async def test_an_email_is_never_sent_while_a_store_transaction_is_open(tmp_path: Path) -> None:
    class Open:
        def holds_transaction(self) -> bool:
            return True

    async with smtp(tmp_path) as (server, certificates):
        with pytest.raises(RuntimeError, match="store transaction"):
            await _sender(certificates, server.port, held=Open()).send(ONE, _message())
        assert server.connections == 0


async def test_an_address_as_the_host_needs_no_lookup(tmp_path: Path) -> None:
    async with smtp(tmp_path, name="127.0.0.1") as (server, certificates):

        def never(host: str, port: int) -> str:
            raise AssertionError("no lookup for an address")

        sender = _sender(certificates, server.port, lookup=never)
        sender.settings = replace(sender.settings, host="127.0.0.1")
        store = InMemoryPlatformSecretStore()
        store._rows[SMTP_PASSWORD] = StoredPlatformSecret(
            SMTP_PASSWORD,
            Vault(MASTER).seal(PASSWORD, PlatformBinding(SMTP_PASSWORD, "127.0.0.1", server.port,
                                                         USERNAME)),
            "127.0.0.1", server.port, USERNAME, 1.0,
        )  # fmt: skip
        sender._secrets = store
        assert await sender.send(ONE, _message()) == "delivered"


# --- the hard deadline ---------------------------------------------------------------------------


async def test_a_server_sending_a_byte_at_a_time_cannot_stretch_the_attempt(tmp_path: Path) -> None:
    """A5-19: each socket call reads the clock, which moves on 400 ms each time it is read, so
    the 10 s budget allows about 25 reads. A whole conversation is about 22 SMTP commands, but
    the trickled greeting alone takes one read a byte: it times out within it. (A timeout set
    once per command would let the conversation through.) The control: the same server, on a
    clock that does not move, delivers."""
    async with smtp(tmp_path, trickle=True) as (server, certificates):
        clock = StepClock(400.0)
        assert await _sender(certificates, server.port, clock=clock).send(ONE, _message()) == (
            "timeout"
        )
        assert server.envelopes == []
        assert clock.reads <= 30
        assert await _sender(certificates, server.port).send(ONE, _message()) == "delivered"


@pytest.mark.parametrize("step", ["GREETING", "EHLO", "MAIL", "DOT"])
async def test_a_server_that_stops_answering_ends_at_the_budget(tmp_path: Path, step: str) -> None:
    """Real socket timeouts need real time: the budget is 300 ms on the wall clock."""
    async with smtp(tmp_path, hang_at=step) as (server, certificates):
        sender = _sender(certificates, server.port, clock=WallClock(), budget_ms=300.0)
        assert await sender.send(ONE, _message()) == "timeout"


async def test_an_attempt_holds_its_slot_until_its_thread_returns(tmp_path: Path) -> None:
    """A5-20: the attempt is awaited to the end, on a clock whose time jumps whenever the loop
    is idle. The loop is left idle for 300 ms of real time while the thread is busy: the
    attempt is still not done, and when the thread returns its own outcome is the attempt's
    (a refused connection), never a timeout declared over it."""
    gate = threading.Event()

    def blocked(*_: Any) -> smtplib.SMTP:
        gate.wait(10)
        raise ConnectionRefusedError("released")

    certificates = make_certificates(tmp_path, SERVER_NAME)
    sender = _sender(certificates, 25, clock=SimClock(), factory=blocked)
    task = asyncio.create_task(sender.send(ONE, _message()))
    done, _ = await asyncio.wait({task}, timeout=0.3)
    assert done == set()
    gate.set()
    assert await task == "connect_failed"


def test_the_deadline_is_read_before_every_socket_call() -> None:
    clock = StepClock(4_000.0)
    deadline = Deadline(clock, 10_000.0)
    assert deadline.left_s() == pytest.approx(6.0)
    assert deadline.left_s() == pytest.approx(2.0)
    with pytest.raises(TimeoutError):
        deadline.left_s()
    assert deadline.expired


# --- the name lookup -----------------------------------------------------------------------------


async def test_a_resolver_that_never_answers_is_bounded_and_its_threads_capped(
    tmp_path: Path,
) -> None:
    """Q7, A5-25: each lookup ends ``timeout`` at the budget on the injected clock, its thread
    abandoned; after ``LOOKUP_THREADS`` of them the next attempt is refused ``resolver_busy``
    with no lookup made; released, the threads come back and the next attempt is delivered."""
    release = threading.Event()
    calls: list[str] = []

    def stuck(host: str, port: int) -> str:
        calls.append(host)
        release.wait(30)
        return "127.0.0.1"

    async with smtp(tmp_path) as (server, certificates):
        resolver = BoundedResolver(stuck)
        sender = _sender(certificates, server.port, clock=SimClock(), resolver=resolver)
        try:
            for n in range(LOOKUP_THREADS):
                assert await sender.send(ONE, _message()) == "timeout"
                assert resolver.abandoned == n + 1
            assert await sender.send(ONE, _message()) == "resolver_busy"
            assert len(calls) == LOOKUP_THREADS  # no lookup was started for it
            assert server.connections == 0
        finally:
            release.set()
        for _ in range(500):
            if resolver.abandoned == 0 and resolver.busy == 0:
                break
            await real_pause(0.01)
        assert (resolver.abandoned, resolver.busy) == (0, 0)
        free = _sender(certificates, server.port, resolver=resolver)
        assert await free.send(ONE, _message()) == "delivered"
        resolver.close()


# --- the messages ---------------------------------------------------------------------------------


def test_a_notification_is_plain_text_for_one_recipient_with_a_stable_message_id() -> None:
    one = _message(ONE, "rcp_0000000000000001")
    again = _message(ONE, "rcp_0000000000000001")
    other = _message(TWO, "rcp_0000000000000002")
    expected = "<ntf_00000000000000a1.rcp_0000000000000001@example.com>"
    assert one["Message-ID"] == again["Message-ID"] == expected
    assert other["Message-ID"] != one["Message-ID"]
    assert one["To"] == ONE
    assert one.get_content_type() == "text/plain"
    body = one.get_content()
    assert one["Subject"] == "[gateway] FIRING critical: Errors on North Shop"
    assert "http" not in body
    assert "<a" not in body
    assert "north-shop" in body
    assert "inc_00000000000000a1" in body


@pytest.mark.parametrize("name", ['Sh"op \\ {x} \u00dcml\u00e4ut \u65e5\u672c'])
def test_names_with_quotes_backslashes_braces_and_non_ascii_letters_are_encoded(name: str) -> None:
    message = notification_message(SENDER, ONE, "rcp_0000000000000001", _payload(app_name=name), T0)
    data = message.as_bytes()
    assert data.isascii()
    assert _parsed(data)["Subject"] == f"[gateway] FIRING critical: Errors on {name}"


def test_a_header_value_with_a_line_break_is_refused() -> None:
    """A5-10: headers are set through the library under the SMTP policy, never formatted in."""
    payload = _payload()
    payload["application"]["name"] = "Shop\r\nBcc: someone@example.org"
    with pytest.raises(ValueError, match=r"linefeed|carriage return|newline"):
        notification_message(SENDER, ONE, "rcp_0000000000000001", payload, T0).as_bytes()


def test_the_confirmation_says_who_asked_and_holds_the_code_and_nothing_more() -> None:
    confirmation = Confirmation("rcp_0000000000000001", "chn_00000000000000e1", ONE, "north",
                                "North", "ABCDEFGH23")  # fmt: skip
    message = confirmation_message(SENDER, confirmation, T0)
    body = message.get_content()
    assert '"North" (north)' in body
    assert "ABCDE-FGH23" in body
    assert "Nothing else will be sent" in body
    assert "http" not in body
    assert message["To"] == ONE


async def test_the_mailer_sends_each_confirmation_over_smtp(tmp_path: Path) -> None:
    async with smtp(tmp_path) as (server, certificates):
        mailer = SmtpMailer(_sender(certificates, server.port), ClockEpoch(SimClock(), T0))
        confirmation = Confirmation("rcp_0000000000000001", "chn_00000000000000e1", ONE,
                                    "north", "North", "ABCDEFGH23")  # fmt: skip
        assert await mailer.send_confirmation(confirmation) == "delivered"
        assert b"ABCDE-FGH23" in server.envelopes[0].data


def test_the_conversation_is_never_printed_whatever_the_debug_level(
    capfd: pytest.CaptureFixture[str],
) -> None:
    smtp_object = DeadlineSMTP.__new__(DeadlineSMTP)
    smtp_object.debuglevel = 2
    smtp_object._print_debug("AUTH PLAIN", PASSWORD)
    out, err = capfd.readouterr()
    assert PASSWORD not in out + err
    assert out + err == ""


# --- through the outbox --------------------------------------------------------------------------

MAIL = "chn_00000000000000e1"


@dataclass
class EmailWorld:
    world: World
    server: SmtpTestServer
    sender: SmtpSender

    def worker(self, sender: SmtpSender | None = None) -> DeliveryWorker:
        base = self.world.worker()
        base.email = EmailSetup(sender or self.sender, self.world.state.recipients)
        return base

    async def recipient(self, address: str, state: str = VERIFIED, n: int = 1) -> Recipient:
        made = Recipient(
            f"rcp_000000000000000{n}", MAIL, "north", address, state, None, None, 0, T0,
            T0 if state == VERIFIED else None,
        )  # fmt: skip
        await self.world.state.recipients.insert_recipient(made)
        return made

    async def notify(self, kind: str = "firing", counted: bool = True) -> int:
        payload = AlertPayload(
            f"ntf_00000000000000{random.Random(kind).randrange(16, 255):02x}", kind, T0, "north",
            application=PayloadApplication("north-shop", "North Shop"),
            rule=PayloadRule("rul_00000000000000a1", "Errors", "error_rate", ">", 5.0, "%", 300,
                             0, "critical"),
        )  # fmt: skip
        channel = await self.world.state.channels.get_channel(MAIL)
        assert channel is not None
        async with self.world.state.outbox.write_transaction():
            return await self.world.notices.queue(
                payload, [channel], rule_id="rul_00000000000000a1", counted=counted
            )

    def rows(self) -> list[tuple[Any, ...]]:
        return self.world.rows(
            "SELECT recipient_id, state, last_code, attempts FROM notification_deliveries"
            " ORDER BY rowid"
        )


@asynccontextmanager
async def email_world(
    tmp_path: Path, caps: AlertCaps | None = None, **server: Any
) -> AsyncIterator[
    EmailWorld
]:  # fmt: skip
    world = await sqlite_world(**({"caps": caps} if caps is not None else {}))
    world.notices.recipients = world.state.recipients
    await world.state.channels.insert_channel(
        Channel(MAIL, "north", "Mail", "email", True, True, None, 1, T0, T0)
    )
    async with smtp(tmp_path, **server) as (smtp_server, certificates):
        sender = _sender(certificates, smtp_server.port, held=world.state.outbox)
        yield EmailWorld(world, smtp_server, sender)


async def _deliver(w: EmailWorld, worker: DeliveryWorker | None = None) -> None:
    worker = worker or w.worker()
    await worker.step()
    await worker.drain()


async def test_only_verified_recipients_get_a_notification_each_its_own_message(
    tmp_path: Path,
) -> None:
    """M48 (notifications), A5-9: two verified and one unverified recipient: two deliveries, two
    messages, each naming only its own recipient; the unverified one gets nothing."""
    async with email_world(tmp_path) as w:
        await w.recipient(ONE, n=1)
        await w.recipient(TWO, n=2)
        await w.recipient(THREE, UNVERIFIED, n=3)
        assert await w.notify() == 2
        assert [r[0] for r in w.rows()] == ["rcp_0000000000000001", "rcp_0000000000000002"]
        await _deliver(w)
        assert sorted(e.recipients[0] for e in w.server.envelopes) == sorted([ONE, TWO])
        for envelope in w.server.envelopes:
            assert envelope.recipients == [_parsed(envelope.data)["To"]]
        assert THREE not in repr([e.data for e in w.server.envelopes])
        assert [r[1] for r in w.rows()] == ["delivered", "delivered"]


async def test_a_summary_goes_to_verified_recipients_only(tmp_path: Path) -> None:
    """M48 (summaries): a summary is queued as any notice is, not counted."""
    async with email_world(tmp_path) as w:
        await w.recipient(ONE, n=1)
        await w.recipient(THREE, UNVERIFIED, n=3)
        assert await w.notify("suppressed_summary", counted=False) == 1
        await _deliver(w)
        assert [e.recipients for e in w.server.envelopes] == [[ONE]]


async def test_a_recipient_no_longer_verified_when_claimed_is_not_sent_to_or_counted(
    tmp_path: Path,
) -> None:
    async with email_world(tmp_path) as w:
        made = await w.recipient(ONE, n=1)
        await w.notify()
        await w.world.state.recipients.delete_recipient(made.id)
        await _deliver(w)
        assert w.rows() == [("rcp_0000000000000001", "failed", "recipient_not_verified", 1)]
        assert w.server.connections == 0
        channel = await w.world.state.channels.get_channel(MAIL)
        assert channel is not None
        assert channel.consecutive_failures == 0


@pytest.mark.parametrize("change", ["unverified", "another channel"])
async def test_a_recipient_row_changed_after_queueing_is_not_sent_to(
    tmp_path: Path, change: str
) -> None:
    """M48, checked again when the row is claimed: a recipient whose stored row no longer says
    verified, or says another channel (no flow makes either; the check is a second guard), is
    not sent to and not counted toward health."""
    async with email_world(tmp_path) as w:
        made = await w.recipient(ONE, n=1)
        await w.notify()
        altered = (
            replace(made, state=UNVERIFIED, verified_at_ms=None)
            if change == "unverified"
            else replace(made, channel_id="chn_00000000000000e9")
        )
        assert w.world.db is not None
        w.world.db.execute(
            "UPDATE alert_recipients SET state = ?, verified_at_ms = ?, channel_id = ?"
            " WHERE id = ?",
            (altered.state, altered.verified_at_ms, altered.channel_id, made.id),
        )
        await _deliver(w)
        assert w.rows() == [("rcp_0000000000000001", "failed", "recipient_not_verified", 1)]
        assert w.server.connections == 0


async def test_a_channel_with_no_verified_recipient_fails_and_counts_toward_its_health(
    tmp_path: Path,
) -> None:
    async with email_world(tmp_path) as w:
        await w.recipient(ONE, UNVERIFIED, n=1)
        assert await w.notify() == 1
        await _deliver(w)
        assert w.rows() == [(None, "failed", "no_verified_recipient", 1)]
        channel = await w.world.state.channels.get_channel(MAIL)
        assert channel is not None
        assert channel.consecutive_failures == 1


async def test_a_4xx_is_tried_again_with_the_same_message_and_a_5xx_is_not(tmp_path: Path) -> None:
    """A5-13."""
    async with email_world(tmp_path, replies={"RCPT": (450, "busy")}) as w:
        await w.recipient(ONE, n=1)
        await w.notify()
        await _deliver(w)
        [(_, state, code, attempts)] = w.rows()
        assert (state, code, attempts) == ("queued", "smtp_4xx", 1)
        w.server.replies.clear()
        await w.world.clock.sleep_ms(10 * 60 * 1000.0)
        await _deliver(w)
        assert w.rows()[0][1:3] == ("delivered", "delivered")
        first_id = _parsed(w.server.envelopes[0].data)["Message-ID"]
        assert first_id.startswith("<ntf_")
    async with email_world(tmp_path / "five", replies={"RCPT": (550, "no such user")}) as w:
        await w.recipient(ONE, n=1)
        await w.notify()
        await _deliver(w)
        assert w.rows() == [("rcp_0000000000000001", "failed", "smtp_5xx", 1)]


async def test_the_message_id_is_the_same_across_retries(tmp_path: Path) -> None:
    async with email_world(tmp_path, replies={"DOT": (451, "later")}) as w:
        await w.recipient(ONE, n=1)
        await w.notify()
        await _deliver(w)
        w.server.replies.clear()
        await w.world.clock.sleep_ms(10 * 60 * 1000.0)
        await _deliver(w)
        assert len(w.server.envelopes) == 1
        retried = _parsed(w.server.envelopes[0].data)["Message-ID"]
        assert retried == "<ntf_" + retried[5:]
        assert ".rcp_0000000000000001@example.com>" in retried


async def test_the_caps_count_each_recipients_message(tmp_path: Path) -> None:
    """A5-21: a rule's cap of 2 and three verified recipients: two queued, one suppressed."""
    async with email_world(tmp_path, caps=AlertCaps(per_rule=2)) as w:
        for n, address in enumerate((ONE, TWO, THREE), start=1):
            await w.recipient(address, n=n)
        await w.notify()
        assert [r[1:3] for r in w.rows()] == [
            ("queued", None), ("queued", None), ("suppressed", "rule_cap"),
        ]  # fmt: skip


async def test_a_domain_the_operator_no_longer_allows_is_not_sent_to(tmp_path: Path) -> None:
    """A5-3, at send."""
    async with email_world(tmp_path) as w:
        await w.recipient(ONE, n=1)
        await w.notify()
        w.sender.settings = replace(w.sender.settings, domains=frozenset({"example.net"}))
        await _deliver(w)
        assert w.rows() == [("rcp_0000000000000001", "failed", "domain_not_allowed", 1)]
        assert w.server.connections == 0


async def test_no_store_connection_is_held_while_an_email_is_sent(tmp_path: Path) -> None:
    """A5-17: the SMTP conversation starts with no store transaction open."""
    async with email_world(tmp_path) as w:
        await w.recipient(ONE, n=1)
        await w.notify()
        seen: list[bool] = []
        real = w.sender._connect

        def connect(address: str, deadline: Deadline) -> smtplib.SMTP:
            assert w.world.db is not None
            seen.append(w.world.db.in_transaction)
            return real(address, deadline)

        w.sender._connect = connect  # type: ignore[method-assign]
        await _deliver(w)
        assert seen == [False]
        assert w.rows()[0][1] == "delivered"


async def test_resolver_busy_is_tried_again_and_counts_toward_the_channels_health(
    tmp_path: Path,
) -> None:
    """A5-26: a refused attempt is a counted failure."""
    async with email_world(tmp_path) as w:
        await w.recipient(ONE, n=1)
        await w.notify()
        w.sender.resolver.busy = w.sender.resolver.threads  # every thread held
        await _deliver(w)
        assert w.rows() == [("rcp_0000000000000001", "queued", "resolver_busy", 1)]
        channel = await w.world.state.channels.get_channel(MAIL)
        assert channel is not None
        assert channel.consecutive_failures == 1
        assert [r.code for r in w.world.reports] == ["resolver_busy"]
        assert w.server.connections == 0


async def test_a_test_send_goes_to_each_verified_recipient_each_counted(tmp_path: Path) -> None:
    """M48 (test sends): one test message to each verified recipient, each counted against
    the test sends' caps; none to an unverified one."""
    async with email_world(tmp_path) as w:
        await w.recipient(ONE, n=1)
        await w.recipient(TWO, n=2)
        await w.recipient(THREE, UNVERIFIED, n=3)
        counted: list[str] = []

        class Counter:
            async def count(self, channel_id: str, organization_id: str) -> None:
                counted.append(channel_id)

        sends = TestSends(w.worker(), Counter(), AuditLog(w.world.state.audit), w.world.epoch,
                          random.Random(1).randbytes)  # fmt: skip
        assert await sends.send("north", MAIL, ACTOR["north"]) == "delivered"
        assert sorted(e.recipients[0] for e in w.server.envelopes) == sorted([ONE, TWO])
        assert counted == [MAIL, MAIL]
        limited = InMemoryConnectionTestCounter(SimClock(), 1, 30, reason=Reason.TEST_SEND_LIMITED)
        refused = TestSends(w.worker(), limited, AuditLog(w.world.state.audit), w.world.epoch,
                            random.Random(2).randbytes)  # fmt: skip
        with pytest.raises(Exception, match="at most 1"):
            await refused.send("north", MAIL, ACTOR["north"])


async def test_a_test_send_to_a_channel_with_no_verified_recipient(tmp_path: Path) -> None:
    async with email_world(tmp_path) as w:
        await w.recipient(ONE, UNVERIFIED, n=1)
        sends = TestSends(w.worker(), InMemoryConnectionTestCounter(SimClock(), 10, 30),
                          AuditLog(w.world.state.audit), w.world.epoch,
                          random.Random(1).randbytes)  # fmt: skip
        assert await sends.send("north", MAIL, ACTOR["north"]) == "no_verified_recipient"
        assert w.server.connections == 0


def test_every_email_code_is_counted_by_monitoring() -> None:
    from gateway.alerts.smtp import EMAIL_CODES
    from gateway.monitoring import EMAIL_DELIVERY_RESULTS

    assert set(EMAIL_DELIVERY_RESULTS) == EMAIL_CODES | {"channel_gone", "channel_disabled"}


def _unused(*_: Any) -> None:
    return None


_unused(DeliveryState)
