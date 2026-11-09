"""An email channel's recipients and their verification, below HTTP (docs/48, the A5 plan's
sections 5 and 6), on memory, SQLite and Postgres.

- The code: verified once (A5-5), valid 24 hours (A5-4), void after 5 wrong entries (A5-6),
  bound to (channel, address) (A5-7), stored only as an HMAC and never logged (M50).
- One answer for every refusal, after the same steps: a structural test of every case (M47).
- The caps: per address (durable, across organizations and instances, and after the window
  counters restart: M49), per channel, per organization and per instance (A5-8); a counter that
  cannot answer refuses; nothing is sent or stored for a refused send.
- The mailer is never called while a store transaction is open (A5-17's rule for confirmations).
- The server's code generator is the operating system's (A5-18).

Test values, not secrets.
"""

from __future__ import annotations

import contextlib
import hmac
import logging
import random
import sqlite3
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace
from typing import Any

import pytest

from gateway.alerts import recipients as recipients_module
from gateway.alerts.channels import Channel, InMemoryChannelStore, SqliteChannelStore
from gateway.alerts.email import EmailSettings, code_digest
from gateway.alerts.outbox import SqliteOutboxStore
from gateway.alerts.recipients import (
    CODE_VALID_MS,
    MAX_WRONG_ENTRIES,
    UNVERIFIED,
    VERIFIED,
    Confirmation,
    ConfirmationCaps,
    ConfirmationInvalidError,
    InMemoryConfirmationStore,
    InMemoryRecipientStore,
    InstanceConfirmations,
    RecipientService,
    SqliteConfirmationStore,
    SqliteRecipientStore,
)
from gateway.audit import AuditLog, Reason, SqliteAuditStore
from gateway.clock import ClockEpoch, EpochStoreClock, SimClock
from gateway.database import open_database
from gateway.management import ManagementRefusal
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.permissions import Actor
from gateway.providers import InMemoryConnectionTestCounter
from gateway.state import StateUnavailableError

postgres = pytest.mark.postgres
ACTOR = Actor("usr_00000000000000a1", "north", ())
CODE_KEY = b"c" * 32
ADDRESS_KEY = b"a" * 32
SETTINGS = EmailSettings(
    "smtp.example.com", 587, "starttls", "gateway", "alerts@example.com",
    frozenset({"example.com"}),
)  # fmt: skip
MAIL = "chn_00000000000000e1"
MAIL_TWO = "chn_00000000000000e2"
SOUTH_MAIL = "chn_00000000000000f1"
ADDRESS = "planted-recipient-31@example.com"
DEFAULT_CAPS = ConfirmationCaps(3, 10, 20, 200)


def _audit() -> AuditLog:
    """An audit log of its own (an in-memory SQLite database): there is no in-memory store."""
    connection = open_database(":memory:")
    MigrationRunner(connection, MIGRATIONS, ClockEpoch(SimClock())).migrate()
    return AuditLog(SqliteAuditStore(connection))


class Held:
    """What says whether a store transaction is open on the connection."""

    def __init__(self, connection: sqlite3.Connection | None) -> None:
        self.connection = connection

    def holds_transaction(self) -> bool:
        return self.connection is not None and self.connection.in_transaction


@dataclass
class RecordingMailer:
    """Records what it is handed, and whether a store transaction was open when it was."""

    held: Held
    sent: list[Confirmation] = field(default_factory=list)
    open_transactions: list[bool] = field(default_factory=list)
    outcome: str = "delivered"

    async def send_confirmation(self, confirmation: Confirmation) -> str:
        self.open_transactions.append(self.held.holds_transaction())
        self.sent.append(confirmation)
        return self.outcome


class Unreachable:
    async def count(self, channel_id: str, organization_id: str) -> None:
        raise StateUnavailableError("timeout")


@dataclass
class Rig:
    service: RecipientService
    mailer: RecordingMailer
    clock: SimClock
    connection: sqlite3.Connection | None
    channels: Any

    async def add(self, address: str = ADDRESS, channel: str = MAIL, org: str = "north") -> Any:
        return await self.service.add(org, channel, address, actor=ACTOR)

    def code(self, n: int = -1) -> str:
        return self.mailer.sent[n].code


def _service(
    store: Any,
    confirmations: Any,
    clock: SimClock,
    mailer: RecordingMailer,
    *,
    caps: ConfirmationCaps = DEFAULT_CAPS,
    windows: Any = None,
    audit: AuditLog | None = None,
    seed: int = 4,
    held: Held | None = None,
) -> RecipientService:
    epoch = ClockEpoch(clock)

    async def name(organization_id: str) -> str:
        return organization_id.title()

    return RecipientService(
        store,
        confirmations,
        windows
        if windows is not None
        else InMemoryConnectionTestCounter(
            clock,
            caps.per_channel,
            caps.per_organization,
            reason=Reason.CONFIRMATION_LIMITED,
            what=("one channel's recipients", "an organization's recipients"),
            noun="confirmations to",
        ),
        InstanceConfirmations(clock, caps.per_instance),
        audit if audit is not None else _audit(),
        epoch,
        EpochStoreClock(epoch),
        random.Random(seed),
        code_key=CODE_KEY,
        address_key=ADDRESS_KEY,
        caps=caps,
        settings=SETTINGS,
        mailer=mailer,
        organization_name=name,
        held=held,
    )


def _channels(connection: sqlite3.Connection | None) -> Any:
    return SqliteChannelStore(connection) if connection is not None else InMemoryChannelStore()


@contextlib.asynccontextmanager
async def rig(kind: str = "sqlite", **options: Any) -> AsyncIterator[Rig]:
    clock = SimClock()
    connection: sqlite3.Connection | None = None
    if kind == "sqlite":
        connection = open_database(":memory:")
        MigrationRunner(connection, MIGRATIONS, ClockEpoch(clock)).migrate()
        store: Any = SqliteRecipientStore(connection)
        confirmations: Any = SqliteConfirmationStore(connection, EpochStoreClock(ClockEpoch(clock)))
        audit = AuditLog(SqliteAuditStore(connection))
    else:
        store = InMemoryRecipientStore()
        confirmations = InMemoryConfirmationStore(EpochStoreClock(ClockEpoch(clock)))
        audit = _audit()
    held = Held(connection)
    mailer = RecordingMailer(held)
    channels = _channels(connection)
    for channel_id, org in ((MAIL, "north"), (MAIL_TWO, "north"), (SOUTH_MAIL, "south")):
        await channels.insert_channel(
            Channel(channel_id, org, channel_id, "email", True, False, None, 1, 0.0, 0.0)
        )
    service = _service(store, confirmations, clock, mailer, audit=audit, held=held, **options)
    try:
        yield Rig(service, mailer, clock, connection, channels)
    finally:
        if connection is not None:
            connection.close()


KINDS = ["memory", "sqlite"]


# --- the code -------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
async def test_an_added_recipient_is_unverified_and_its_code_is_stored_only_as_an_hmac(
    kind: str,
) -> None:
    async with rig(kind) as r:
        made, outcome = await r.add()
        assert (made.state, outcome) == (UNVERIFIED, "delivered")
        [sent] = r.mailer.sent
        assert sent.address == ADDRESS
        assert sent.organization_name == "North"
        code = sent.code
        stored = await r.service.store.get_recipient(made.id)
        assert stored is not None
        assert stored.code_digest is not None
        assert stored.code_digest == code_digest(CODE_KEY, MAIL, ADDRESS, code)
        assert code.encode() not in stored.code_digest
        assert code not in repr(stored)
        assert code not in repr(sent)
        assert stored.code_expires_at_ms == CODE_VALID_MS


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("typed", ["{code}", "{lower}", "{shown}", " {spaced} "])
async def test_the_right_code_verifies_in_any_case_with_spaces_and_hyphens(
    kind: str, typed: str
) -> None:
    async with rig(kind) as r:
        made, _ = await r.add()
        code = r.code()
        entry = typed.format(
            code=code, lower=code.lower(), shown=f"{code[:5]}-{code[5:]}",
            spaced=f"{code[:3]} {code[3:]}",
        )  # fmt: skip
        verified = await r.service.confirm("north", MAIL, made.id, entry, actor=ACTOR)
        assert verified.state == VERIFIED
        assert verified.code_digest is None
        assert verified.verified_at_ms is not None


async def _refused(r: Rig, recipient_id: str, code: str, channel: str = MAIL,
                   org: str = "north") -> ConfirmationInvalidError:  # fmt: skip
    with pytest.raises(ConfirmationInvalidError) as refused:
        await r.service.confirm(org, channel, recipient_id, code, actor=ACTOR)
    return refused.value


@pytest.mark.parametrize("kind", KINDS)
async def test_a_code_is_used_once(kind: str) -> None:
    """A5-5."""
    async with rig(kind) as r:
        made, _ = await r.add()
        code = r.code()
        await r.service.confirm("north", MAIL, made.id, code, actor=ACTOR)
        await _refused(r, made.id, code)


@pytest.mark.parametrize("kind", KINDS)
async def test_a_code_is_valid_24_hours_on_the_store_clock(kind: str) -> None:
    """A5-4: a moment before its 24 hours it verifies; at 24 hours it does not."""
    async with rig(kind) as r:
        made, _ = await r.add()
        await r.clock.sleep_ms(CODE_VALID_MS - 1.0)
        assert (await r.service.confirm("north", MAIL, made.id, r.code(), actor=ACTOR)).state == (
            VERIFIED
        )
        late, _ = await r.add("late@example.com")
        await r.clock.sleep_ms(CODE_VALID_MS)
        await _refused(r, late.id, r.code())


@pytest.mark.parametrize("kind", KINDS)
async def test_the_fifth_wrong_entry_voids_the_code_and_a_resend_makes_a_new_one(
    kind: str,
) -> None:
    """A5-6: four wrong entries leave the code alive; the fifth voids it, so even the right
    code is refused; a re-send's new code works."""
    async with rig(kind) as r:
        made, _ = await r.add()
        code = r.code()
        for _ in range(MAX_WRONG_ENTRIES - 1):
            await _refused(r, made.id, "AAAAAAAAAA")
        assert (await r.service.store.get_recipient(made.id)).code_digest is not None  # type: ignore[union-attr]
        await _refused(r, made.id, "AAAAAAAAAA")
        stored = await r.service.store.get_recipient(made.id)
        assert stored is not None
        assert stored.code_digest is None
        assert stored.wrong_entries == MAX_WRONG_ENTRIES
        await _refused(r, made.id, code)
        again, _ = await r.service.resend("north", MAIL, made.id, actor=ACTOR)
        assert again.wrong_entries == 0
        assert r.code() != code
        await _refused(r, made.id, code)  # the old code stays void
        assert (await r.service.confirm("north", MAIL, made.id, r.code(),
                                        actor=ACTOR)).state == VERIFIED  # fmt: skip


@pytest.mark.parametrize("kind", KINDS)
async def test_a_code_confirms_only_the_channel_and_address_it_was_sent_for(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A5-7: the same address on two channels, both sent the same code; one recipient's digest
    moved to the other's row does not verify it (it is bound to its channel and address)."""
    monkeypatch.setattr(recipients_module, "new_code", lambda rng: "SAMECODE23")
    async with rig(kind) as r:
        first, _ = await r.add(channel=MAIL)
        second, _ = await r.add(channel=MAIL_TWO)
        one = await r.service.store.get_recipient(first.id)
        two = await r.service.store.get_recipient(second.id)
        assert one is not None
        assert two is not None
        assert one.code_digest != two.code_digest
        await r.service.store.put_recipient(replace(two, code_digest=one.code_digest))
        await _refused(r, second.id, "SAMECODE23", channel=MAIL_TWO)
        # The control: the first verifies with its own digest.
        assert (await r.service.confirm("north", MAIL, first.id, "SAMECODE23",
                                        actor=ACTOR)).state == VERIFIED  # fmt: skip


@pytest.mark.parametrize("kind", KINDS)
async def test_every_refusal_is_one_answer(kind: str) -> None:
    async with rig(kind) as r:
        made, _ = await r.add()
        code = r.code()
        south, _ = await r.add(channel=SOUTH_MAIL, org="south")
        answers = [
            await _refused(r, made.id, "WRONGCODE2"),
            await _refused(r, made.id, ""),
            await _refused(r, made.id, "not a code at all"),
            await _refused(r, "rcp_00000000000000ff", code),  # unknown
            await _refused(r, "not-a-recipient-id", code),
            await _refused(r, south.id, r.code(), channel=MAIL),  # another channel's
            await _refused(r, made.id, code, org="south"),  # another organization's path
        ]
        assert {(a.reason, str(a)) for a in answers} == {
            (Reason.CONFIRMATION_INVALID, "the code is not valid for this recipient")
        }


# --- one answer, after the same steps -------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
async def test_every_refusal_takes_the_same_steps(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M47: for every case (right, wrong, expired, used, voided, unknown, another channel's,
    another organization's), one lookup, one HMAC and one comparison, against the stored digest
    or a dummy one."""
    async with rig(kind) as r:
        live, _ = await r.add()
        live_code = r.code()
        used, _ = await r.add("used@example.com")
        used_code = r.code()
        await r.service.confirm("north", MAIL, used.id, used_code, actor=ACTOR)
        voided, _ = await r.add("voided@example.com")
        voided_code = r.code()
        for _ in range(MAX_WRONG_ENTRIES):
            await _refused(r, voided.id, "AAAAAAAAAA")
        expired, _ = await r.add("expired@example.com")
        expired_code = r.code()
        south, _ = await r.add(channel=SOUTH_MAIL, org="south")
        await r.clock.sleep_ms(1.0)
        steps: list[str] = []
        store = r.service.store
        real_get, real_digest, real_compare = (
            store.get_recipient, code_digest, hmac.compare_digest,
        )  # fmt: skip

        async def get(recipient_id: str) -> Any:
            steps.append("lookup")
            return await real_get(recipient_id)

        def digest(*args: Any) -> bytes:
            steps.append("hmac")
            return bytes(real_digest(*args))

        def compare(a: Any, b: Any) -> bool:
            steps.append("compare")
            return real_compare(a, b)

        monkeypatch.setattr(store, "get_recipient", get)
        monkeypatch.setattr(recipients_module, "code_digest", digest)
        monkeypatch.setattr(hmac, "compare_digest", compare)
        cases: list[tuple[str, str, str, str]] = [
            ("wrong", live.id, "WRONGCODE2", "north"),
            ("used", used.id, used_code, "north"),
            ("voided", voided.id, voided_code, "north"),
            ("unknown", "rcp_00000000000000ff", live_code, "north"),
            ("another channel", south.id, live_code, "north"),
            ("another organization", live.id, live_code, "south"),
        ]
        for name, recipient_id, code, org in cases:
            steps.clear()
            await _refused(r, recipient_id, code, org=org)
            assert steps == ["lookup", "hmac", "compare"], name
        await r.clock.sleep_ms(CODE_VALID_MS)
        steps.clear()
        await _refused(r, expired.id, expired_code)
        assert steps == ["lookup", "hmac", "compare"], "expired"
        # The control: the right code takes the same steps, and verifies.
        live_after, _ = await r.service.resend("north", MAIL, live.id, actor=ACTOR)
        steps.clear()
        await r.service.confirm("north", MAIL, live_after.id, r.code(), actor=ACTOR)
        assert steps == ["lookup", "hmac", "compare"]


# --- never in clear ----------------------------------------------------------------------------


async def test_the_code_is_in_no_row_and_no_log_record(caplog: pytest.LogCaptureFixture) -> None:
    """M50: the code and the address in no log record at DEBUG through add, resend and
    confirm; the code in no stored column. The control: both are in the mailer's record."""
    with caplog.at_level(logging.DEBUG):
        async with rig("sqlite") as r:
            made, _ = await r.add()
            first = r.code()
            await r.service.resend("north", MAIL, made.id, actor=ACTOR)
            second = r.code()
            await _refused(r, made.id, first)
            await r.service.confirm("north", MAIL, made.id, second, actor=ACTOR)
            assert r.connection is not None
            dump = "\n".join(map(str, r.connection.iterdump()))
            sent = repr([(c.address, c.code) for c in r.mailer.sent])
    text = "\n".join(f"{rec.name} {rec.getMessage()}" for rec in caplog.records)
    for planted in (first, second, ADDRESS):
        assert planted not in text
    for code in (first, second):
        assert code not in dump
    assert first in sent
    assert second in sent
    assert ADDRESS in sent  # the control


# --- the caps ------------------------------------------------------------------------------------


async def test_the_per_address_cap_holds_across_organizations_instances_and_a_counter_restart(
) -> None:  # fmt: skip
    """M49: two instances sharing one database, two organizations, the window counters started
    afresh (a Valkey restart): three confirmations to one address a day, then 429 and nothing
    sent or stored."""
    clock = SimClock()
    connection = open_database(":memory:")
    MigrationRunner(connection, MIGRATIONS, ClockEpoch(clock)).migrate()
    channels = SqliteChannelStore(connection)
    for channel_id, org in ((MAIL, "north"), (SOUTH_MAIL, "south")):
        await channels.insert_channel(
            Channel(channel_id, org, channel_id, "email", True, False, None, 1, 0.0, 0.0)
        )

    def instance(seed: int) -> tuple[RecipientService, RecordingMailer]:
        held = Held(connection)
        mailer = RecordingMailer(held)
        service = _service(
            SqliteRecipientStore(connection),
            SqliteConfirmationStore(connection, EpochStoreClock(ClockEpoch(clock))),
            clock, mailer, held=held, seed=seed,
        )  # fmt: skip
        return service, mailer

    one, one_mail = instance(1)
    two, two_mail = instance(2)
    made, _ = await one.add("north", MAIL, ADDRESS, actor=ACTOR)
    await two.add("south", SOUTH_MAIL, ADDRESS.upper().replace("@EXAMPLE.COM", "@example.com"),
                  actor=ACTOR)  # fmt: skip
    restarted, restarted_mail = instance(3)  # fresh window counters and instance count
    await restarted.resend("north", MAIL, made.id, actor=ACTOR)
    sent = len(one_mail.sent) + len(two_mail.sent) + len(restarted_mail.sent)
    assert sent == 3
    with pytest.raises(ManagementRefusal) as refused:
        await restarted.resend("north", MAIL, made.id, actor=ACTOR)
    assert refused.value.reason == Reason.CONFIRMATION_LIMITED
    again, again_mail = instance(4)
    with pytest.raises(ManagementRefusal):
        await again.add("north", MAIL, "planted-recipient-31@EXAMPLE.com", actor=ACTOR)
    assert again_mail.sent == []
    assert len(restarted_mail.sent) == 1
    count = connection.execute("SELECT count(*) FROM alert_recipients").fetchone()[0]
    assert count == 2  # the refused add stored nothing
    # A day later the address may be sent to again.
    await clock.sleep_ms(24 * 60 * 60 * 1000.0)
    await again.resend("north", MAIL, made.id, actor=ACTOR)
    assert len(again_mail.sent) == 1
    connection.close()


@pytest.mark.parametrize(
    ("caps", "plan", "what"),
    [
        # Per channel: three addresses on one channel, the third refused.
        (ConfirmationCaps(18, 2, 120, 1200), [(MAIL, "north")] * 3, "one channel's"),
        # Per organization: two channels of north, the third refused.
        (ConfirmationCaps(18, 60, 2, 1200), [(MAIL, "north"), (MAIL_TWO, "north"),
                                              (MAIL_TWO, "north")], "an organization's"),
        # Per instance: across organizations, the third refused.
        (ConfirmationCaps(18, 60, 120, 2), [(MAIL, "north"), (SOUTH_MAIL, "south"),
                                             (MAIL_TWO, "north")], "this instance"),
    ],
)  # fmt: skip
async def test_each_hourly_cap_refuses_and_sends_and_stores_nothing(
    caps: ConfirmationCaps, plan: list[tuple[str, str]], what: str
) -> None:
    """A5-8, one for each of the per-channel, per-organization and per-instance caps; and an hour
    later they allow again."""
    async with rig("sqlite", caps=caps) as r:
        for n, (channel, org) in enumerate(plan[:-1]):
            await r.add(f"r{n}@example.com", channel=channel, org=org)
        channel, org = plan[-1]
        with pytest.raises(ManagementRefusal) as refused:
            await r.add("last@example.com", channel=channel, org=org)
        assert refused.value.reason == Reason.CONFIRMATION_LIMITED
        assert what in str(refused.value)
        assert len(r.mailer.sent) == len(plan) - 1
        assert r.connection is not None
        rows = r.connection.execute("SELECT count(*) FROM alert_recipients").fetchone()[0]
        assert rows == len(plan) - 1
        await r.clock.sleep_ms(60 * 60 * 1000.0)
        await r.add("later@example.com", channel=channel, org=org)  # the control


async def test_a_counter_that_cannot_answer_refuses_and_sends_nothing() -> None:
    async with rig("sqlite", windows=Unreachable()) as r:
        with pytest.raises(StateUnavailableError):
            await r.add()
        assert r.mailer.sent == []
        assert r.connection is not None
        assert r.connection.execute("SELECT count(*) FROM alert_recipients").fetchone()[0] == 0


async def test_a_refusal_by_the_per_address_cap_gives_the_instances_count_back() -> None:
    async with rig("sqlite", caps=ConfirmationCaps(1, 60, 120, 2)) as r:
        made, _ = await r.add()
        with pytest.raises(ManagementRefusal):
            await r.service.resend("north", MAIL, made.id, actor=ACTOR)  # the address's cap
        # The instance's count of 2 has one left: the refused send was given back.
        await r.add("other@example.com")
        with pytest.raises(ManagementRefusal, match="this instance"):
            await r.add("third@example.com")


# --- the mailer and the store's transaction ------------------------------------------------------


async def test_the_mailer_is_never_called_while_a_store_transaction_is_open() -> None:
    """No database connection held during a network call: every confirmation (adding,
    re-sending) is handed to the mailer after its write has committed."""
    async with rig("sqlite") as r:
        made, _ = await r.add()
        await r.service.resend("north", MAIL, made.id, actor=ACTOR)
        assert r.mailer.open_transactions == [False, False]
        # The guard: a send attempted inside a transaction is refused before the mailer.
        assert r.connection is not None
        async with r.service.store.write_transaction():
            r.connection.execute("UPDATE alert_recipients SET wrong_entries = wrong_entries")
            with pytest.raises(RuntimeError, match="never sent while a store transaction"):
                await r.service.send([])
        assert len(r.mailer.sent) == 2


async def test_the_outbox_store_says_when_its_transaction_is_open() -> None:
    connection = open_database(":memory:")
    MigrationRunner(connection, MIGRATIONS, ClockEpoch(SimClock())).migrate()
    outbox = SqliteOutboxStore(connection)
    assert not outbox.holds_transaction()
    async with outbox.write_transaction():
        connection.execute("UPDATE alert_channels SET version = version")
        assert outbox.holds_transaction()
    connection.close()


# --- the rest of the service ---------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
async def test_resend_voids_the_old_code_and_refuses_a_verified_recipient(kind: str) -> None:
    async with rig(kind) as r:
        made, _ = await r.add()
        old = r.code()
        await r.service.resend("north", MAIL, made.id, actor=ACTOR)
        await _refused(r, made.id, old)
        await r.service.confirm("north", MAIL, made.id, r.code(), actor=ACTOR)
        with pytest.raises(ManagementRefusal) as refused:
            await r.service.resend("north", MAIL, made.id, actor=ACTOR)
        assert refused.value.reason == Reason.RECIPIENT_VERIFIED


@pytest.mark.parametrize("kind", KINDS)
async def test_ten_recipients_a_channel_and_one_address_once(kind: str) -> None:
    caps = ConfirmationCaps(18, 60, 120, 1200)
    async with rig(kind, caps=caps) as r:
        await r.add("a0@example.com")
        with pytest.raises(ManagementRefusal) as taken:
            await r.add("a0@example.com")
        assert taken.value.reason == Reason.RECIPIENT_TAKEN
        for n in range(1, 10):
            await r.add(f"a{n}@example.com")
        with pytest.raises(ManagementRefusal) as full:
            await r.add("eleventh@example.com")
        assert full.value.reason == Reason.RECIPIENT_LIMIT


@pytest.mark.parametrize("kind", KINDS)
async def test_removing_a_recipient_is_audited_by_id_and_sends_nothing(kind: str) -> None:
    async with rig(kind) as r:
        made, _ = await r.add()
        await r.service.remove(MAIL, made.id, actor=ACTOR)
        assert await r.service.store.get_recipient(made.id) is None
        assert len(r.mailer.sent) == 1


def test_the_server_draws_codes_and_ids_from_the_operating_systems_generator() -> None:
    """A5-18."""
    from gateway.app import build_channels
    from gateway.redaction import Redactor
    from gateway.state import sqlite_state
    from gateway.vault import Vault

    connection = open_database(":memory:")
    epoch = ClockEpoch(SimClock())
    MigrationRunner(connection, MIGRATIONS, epoch).migrate()
    channels = build_channels(
        sqlite_state(connection, epoch), Vault(bytes(32)), _audit(), epoch,
        {}, pepper=b"p" * 40, redactor=Redactor(), clock=SimClock(),
    )  # fmt: skip
    assert channels.recipients is not None
    assert isinstance(channels.recipients._rng, random.SystemRandom)
    assert channels.recipients.mailer is None  # group 1: no mailer, so no email channel
    assert not channels.recipients.available
    connection.close()


# --- Postgres ------------------------------------------------------------------------------------


@postgres
async def test_the_per_address_cap_is_one_conditional_upsert_on_postgres() -> None:
    """M49: two instances counting one address at once, 24 times: exactly the cap are counted
    (the check and the count are one statement on the database's clock)."""
    import asyncio

    from gateway.postgres import PostgresConfirmationStore
    from postgres_helpers import pg_world

    async with pg_world(instances=2) as world:
        stores = [PostgresConfirmationStore(db) for db in world.instances]
        results = await asyncio.gather(
            *(stores[n % 2].count("addr-digest", 3, 86_400_000.0) for n in range(24))
        )
        assert sum(results) == 3
        assert await world.rows("SELECT sent FROM confirmation_caps") == [(3,)]


@postgres
async def test_recipients_on_postgres_take_the_same_steps_and_verify() -> None:
    from gateway.postgres import (
        PostgresChannelStore,
        PostgresConfirmationStore,
        PostgresRecipientStore,
    )
    from postgres_helpers import pg_world

    async with pg_world() as world:
        [db] = world.instances
        await world.set_time(1_000.0)
        clock = SimClock()
        mailer = RecordingMailer(Held(None))
        channels = PostgresChannelStore(db)
        await channels.insert_channel(
            Channel(MAIL, "north", "Mail", "email", True, False, None, 1, 0.0, 0.0)
        )
        service = _service(
            PostgresRecipientStore(db), PostgresConfirmationStore(db), clock, mailer,
        )  # fmt: skip
        made, _ = await service.add("north", MAIL, ADDRESS, actor=ACTOR)
        with pytest.raises(ConfirmationInvalidError):
            await service.confirm("north", MAIL, made.id, "WRONGCODE2", actor=ACTOR)
        verified = await service.confirm("north", MAIL, made.id, mailer.sent[0].code, actor=ACTOR)
        assert verified.state == VERIFIED
        assert [r.state for r in await PostgresRecipientStore(db).channel_recipients(MAIL)] == [
            VERIFIED
        ]


def _unused(_: Callable[..., Any]) -> None:
    return None
