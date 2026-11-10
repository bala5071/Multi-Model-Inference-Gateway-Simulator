"""An email channel's recipients and their verification (docs/48, the A5 plan's sections 4 to 6).

**A recipient is ``unverified`` until its confirmation code is entered,** and gets nothing but its
confirmation until then. The code is 10 base32 characters (50 bits, ``gateway.alerts.email``),
stored only as an HMAC under its own derived key (``KeyPurpose.EMAIL_CONFIRMATION``) over the
channel, the address and the code, so it confirms only the (channel, address) it was sent for.
It is valid 24 hours on the store clock, used once, and void after 5 wrong entries; a re-send
makes a new one. It is never logged, audited, returned or put in an event: it exists in clear
only between being drawn and being handed to the mailer.

**One answer for every refusal of a code:** wrong, expired, used, voided, unknown, another
channel's or another organization's recipient all answer ``confirmation_invalid`` (409), byte for
byte, after the same steps: one lookup, one HMAC, one comparison against the stored digest or a
dummy one (``confirm``; a structural test and the early-return mutation, M47).

**The confirmation caps** (a send over any answers 429 ``confirmation_limited`` and sends
nothing; a counter that cannot answer refuses). Each confirmation goes to one address, so each
counts one per recipient:

- per address, over 24 hours, across every channel and organization: a durable row keyed by an
  HMAC of the normalised address (``KeyPurpose.EMAIL_ADDRESS``), counted by one conditional
  upsert on the store clock (``ConfirmationStore.count``), so a Valkey restart releases nothing;
- per channel and per organization, an hour: the shared window counters (Valkey, or memory);
- per instance, all organizations, an hour: memory (``InstanceConfirmations``), a backstop.

**The mailer** is handed each confirmation after the write that made it has committed: no store
connection is held while it sends (a test). Group 1 wires none into the server.
"""

from __future__ import annotations

import contextlib
import hmac
import logging
import random
import re
import sqlite3
from collections import deque
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

from gateway.alerts.email import (
    MAX_RECIPIENTS,
    AddressError,
    CheckedAddress,
    EmailSettings,
    address_digest,
    check_address,
    code_digest,
    entered,
    new_code,
    shown,
)
from gateway.audit import AuditAction, AuditEvent, AuditLog, Outcome, Reason, TargetType
from gateway.clock import Clock, EpochSource, StoreClock
from gateway.database import write_transaction
from gateway.management import ManagementRefusal
from gateway.permissions import Actor
from gateway.providers import ChangeMarker

LOG = logging.getLogger("gateway.alerts")

RECIPIENT_ID_PREFIX = "rcp_"
RECIPIENT_ID = re.compile(r"rcp_[0-9a-f]{16}")
UNVERIFIED = "unverified"
VERIFIED = "verified"
CODE_VALID_MS = 24 * 60 * 60 * 1000.0
MAX_WRONG_ENTRIES = 5
ADDRESS_WINDOW_MS = 24 * 60 * 60 * 1000.0
HOUR_MS = 60 * 60 * 1000.0
# Compared against when there is no stored digest, so every refusal takes the same steps.
DUMMY_DIGEST = bytes(32)


class ConfirmationInvalidError(ManagementRefusal):
    """Every refused code, one answer (409)."""

    def __init__(self) -> None:
        super().__init__(Reason.CONFIRMATION_INVALID, "the code is not valid for this recipient")


def limited(what: str, cap: int, per: str) -> ManagementRefusal:
    return ManagementRefusal(
        Reason.CONFIRMATION_LIMITED,
        f"at most {cap} confirmations {what} {per}; nothing was sent, try again later",
    )


@dataclass(frozen=True, slots=True)
class Recipient:
    id: str
    channel_id: str
    organization_id: str
    address: str
    state: str
    code_digest: bytes | None
    code_expires_at_ms: float | None
    wrong_entries: int
    added_at_ms: float
    verified_at_ms: float | None = None
    last_refused_at_ms: float | None = None

    def __repr__(self) -> str:
        return f"Recipient(id={self.id!r}, channel_id={self.channel_id!r}, state={self.state!r})"


COLUMNS = (
    "id, channel_id, organization_id, address, state, code_digest, code_expires_at_ms,"
    " wrong_entries, added_at_ms, verified_at_ms, last_refused_at_ms"
)


def recipient_row(r: Recipient) -> tuple[Any, ...]:
    return (
        r.id, r.channel_id, r.organization_id, r.address, r.state, r.code_digest,
        r.code_expires_at_ms, r.wrong_entries, r.added_at_ms, r.verified_at_ms,
        r.last_refused_at_ms,
    )  # fmt: skip


def recipient_from_row(row: Sequence[Any]) -> Recipient:
    return Recipient(
        row[0], row[1], row[2], row[3], row[4], bytes(row[5]) if row[5] is not None else None,
        row[6], int(row[7]), float(row[8]), row[9], row[10],
    )  # fmt: skip


class RecipientTakenError(Exception):
    """The channel has that address already (its unique index)."""


class RecipientStore(Protocol):
    def write_transaction(self) -> contextlib.AbstractAsyncContextManager[None]: ...

    async def get_recipient(self, recipient_id: str) -> Recipient | None: ...

    async def channel_recipients(self, channel_id: str) -> list[Recipient]:
        """The channel's recipients, by id."""
        ...

    async def insert_recipient(self, recipient: Recipient) -> None:
        """``RecipientTakenError`` if the channel has the address."""
        ...

    async def put_recipient(self, recipient: Recipient) -> None:
        """Write over the recipient of its id (in the caller's transaction)."""
        ...

    async def delete_recipient(self, recipient_id: str) -> bool: ...

    async def delete_channel_recipients(self, channel_id: str) -> int: ...


class ConfirmationStore(Protocol):
    async def count(self, digest: str, cap: int, window_ms: float) -> bool:
        """Count one confirmation to the address of ``digest`` in its window (``window_ms``,
        starting at the first one counted, on the store clock), or count nothing and return
        False when ``cap`` were counted already: one conditional upsert."""
        ...


class InMemoryRecipientStore:
    def __init__(self) -> None:
        self._rows: dict[str, Recipient] = {}

    @contextlib.asynccontextmanager
    async def write_transaction(self) -> AsyncIterator[None]:
        yield

    async def get_recipient(self, recipient_id: str) -> Recipient | None:
        return self._rows.get(recipient_id)

    async def channel_recipients(self, channel_id: str) -> list[Recipient]:
        return sorted((r for r in self._rows.values() if r.channel_id == channel_id),
                      key=lambda r: r.id)  # fmt: skip

    async def insert_recipient(self, recipient: Recipient) -> None:
        if any(
            r.channel_id == recipient.channel_id and r.address == recipient.address
            for r in self._rows.values()
        ):
            raise RecipientTakenError()
        self._rows[recipient.id] = recipient

    async def put_recipient(self, recipient: Recipient) -> None:
        self._rows[recipient.id] = recipient

    async def delete_recipient(self, recipient_id: str) -> bool:
        return self._rows.pop(recipient_id, None) is not None

    async def delete_channel_recipients(self, channel_id: str) -> int:
        gone = [k for k, r in self._rows.items() if r.channel_id == channel_id]
        for key in gone:
            del self._rows[key]
        return len(gone)


class InMemoryConfirmationStore:
    def __init__(self, store_clock: StoreClock) -> None:
        self._clock = store_clock
        self._rows: dict[str, tuple[float, int]] = {}

    async def count(self, digest: str, cap: int, window_ms: float) -> bool:
        now = await self._clock.now_ms()
        start, sent = self._rows.get(digest, (now, 0))
        if start <= now - window_ms:
            start, sent = now, 0
        if sent >= cap:
            return False
        self._rows[digest] = (start, sent + 1)
        return True


class SqliteRecipientStore:
    """Over migration 18's ``alert_recipients``. Coroutines that never suspend (docs/34)."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection

    def write_transaction(self) -> contextlib.AbstractAsyncContextManager[None]:
        return write_transaction(self._db)

    def _rows(self, where: str, args: Sequence[Any]) -> list[Recipient]:
        rows = self._db.execute(
            f"SELECT {COLUMNS} FROM alert_recipients WHERE {where}", tuple(args)
        ).fetchall()
        return [recipient_from_row(r) for r in rows]

    async def get_recipient(self, recipient_id: str) -> Recipient | None:
        found = self._rows("id = ?", (recipient_id,))
        return found[0] if found else None

    async def channel_recipients(self, channel_id: str) -> list[Recipient]:
        return self._rows("channel_id = ? ORDER BY id", (channel_id,))

    async def insert_recipient(self, recipient: Recipient) -> None:
        try:
            self._db.execute(
                f"INSERT INTO alert_recipients ({COLUMNS}) VALUES ({', '.join('?' * 11)})",
                recipient_row(recipient),
            )
        except sqlite3.IntegrityError as exc:
            if "alert_recipients.channel_id" in str(exc):
                raise RecipientTakenError() from None
            raise

    async def put_recipient(self, recipient: Recipient) -> None:
        row = recipient_row(recipient)
        self._db.execute(
            "UPDATE alert_recipients SET state = ?, code_digest = ?, code_expires_at_ms = ?,"
            " wrong_entries = ?, verified_at_ms = ?, last_refused_at_ms = ? WHERE id = ?",
            (*row[4:8], row[9], row[10], recipient.id),
        )

    async def delete_recipient(self, recipient_id: str) -> bool:
        done = self._db.execute("DELETE FROM alert_recipients WHERE id = ?", (recipient_id,))
        return done.rowcount == 1

    async def delete_channel_recipients(self, channel_id: str) -> int:
        done = self._db.execute("DELETE FROM alert_recipients WHERE channel_id = ?", (channel_id,))
        return done.rowcount


class SqliteConfirmationStore:
    """Over migration 18's ``confirmation_caps``, on the store clock: one statement counts or
    refuses (docs/48)."""

    def __init__(self, connection: sqlite3.Connection, store_clock: StoreClock) -> None:
        self._db = connection
        self._clock = store_clock

    async def count(self, digest: str, cap: int, window_ms: float) -> bool:
        now = await self._clock.now_ms()
        done = self._db.execute(
            "INSERT INTO confirmation_caps (address_digest, window_start_ms, sent)"
            " VALUES (:digest, :now, 1) ON CONFLICT (address_digest) DO UPDATE SET"
            " sent = CASE WHEN confirmation_caps.window_start_ms <= :now - :window THEN 1"
            "  ELSE confirmation_caps.sent + 1 END,"
            " window_start_ms = CASE WHEN confirmation_caps.window_start_ms <= :now - :window"
            "  THEN :now ELSE confirmation_caps.window_start_ms END"
            " WHERE confirmation_caps.window_start_ms <= :now - :window"
            " OR confirmation_caps.sent < :cap",
            {"digest": digest, "now": now, "window": window_ms, "cap": cap},
        )
        return done.rowcount == 1


class InstanceConfirmations:
    """This instance's confirmations to any organization in the last hour, in memory, on the
    injected clock: a backstop against a bug, never the only limit."""

    def __init__(self, clock: Clock, cap: int) -> None:
        self._clock = clock
        self.cap = cap
        self._sent: deque[float] = deque()

    def take(self, n: int) -> list[float]:
        """Count ``n`` now, or refuse and count nothing. Returns what was counted (to give
        back if a later cap refuses)."""
        now = self._clock.now_ms()
        while self._sent and now - self._sent[0] >= HOUR_MS:
            self._sent.popleft()
        if len(self._sent) + n > self.cap:
            raise limited("from this instance", self.cap, "an hour")
        taken = [now] * n
        self._sent.extend(taken)
        return taken

    def give_back(self, taken: Sequence[float]) -> None:
        for at in taken:
            with contextlib.suppress(ValueError):
                self._sent.remove(at)


class WindowCounter(Protocol):
    async def count(self, channel_id: str, organization_id: str) -> None:
        """One confirmation of the channel and of the organization this hour, or
        ``ManagementRefusal`` (``confirmation_limited``) counting nothing."""
        ...


@dataclass(frozen=True, slots=True)
class Confirmation:
    """What the mailer is handed: in memory only, between the commit and the send."""

    recipient_id: str
    channel_id: str
    address: str
    organization_id: str
    organization_name: str
    code: str

    def __repr__(self) -> str:
        return f"Confirmation(recipient_id={self.recipient_id!r}, <address and code redacted>)"

    @property
    def shown_code(self) -> str:
        return shown(self.code)


class Mailer(Protocol):
    async def send_confirmation(self, confirmation: Confirmation) -> str:
        """Send it; the outcome code (``delivered`` or a failure's). Never raises for a send
        that failed."""
        ...


class Transactional(Protocol):
    def holds_transaction(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class ConfirmationCaps:
    per_address: int
    per_channel: int
    per_organization: int
    per_instance: int


class RecipientService:
    """Recipients below HTTP: added, re-sent, confirmed and removed, each audited by channel and
    recipient id (never the address or the code)."""

    def __init__(
        self,
        store: RecipientStore,
        confirmations: ConfirmationStore,
        windows: WindowCounter,
        instance: InstanceConfirmations,
        audit: AuditLog,
        epoch: EpochSource,
        store_clock: StoreClock,
        rng: random.Random,
        *,
        code_key: bytes,
        address_key: bytes,
        caps: ConfirmationCaps,
        settings: EmailSettings | None,
        mailer: Mailer | None,
        organization_name: Any,
        held: Transactional | None = None,
    ) -> None:
        """``organization_name``: an async function from an organization id to its name.
        ``held``: what says whether a store transaction is open (the mailer is never called
        while one is)."""
        self.store = store
        self._confirmations = confirmations
        self._windows = windows
        self._instance = instance
        self._audit = audit
        self._epoch = epoch
        self._store_clock = store_clock
        self._rng = rng
        self._code_key = code_key
        self._address_key = address_key
        self.caps = caps
        self.settings = settings
        self.mailer = mailer
        self._organization_name = organization_name
        self._held = held

    def __repr__(self) -> str:
        return "RecipientService(<keys redacted>)"

    @property
    def available(self) -> bool:
        """Whether email channels can be made here: the operator's settings and a mailer."""
        return self.settings is not None and self.mailer is not None

    def check_addresses(self, addresses: object) -> list[CheckedAddress]:
        """1 to 10 addresses, each valid and in an allowed domain, no two the same as stored."""
        assert self.settings is not None
        if not isinstance(addresses, list) or not 1 <= len(addresses) <= MAX_RECIPIENTS:
            raise AddressError(f"a list of 1 to {MAX_RECIPIENTS} addresses")
        checked = [check_address(a, self.settings.domains) for a in addresses]
        if len({c.address for c in checked}) != len(checked):
            raise AddressError("an address appears twice")
        return checked

    def new_id(self) -> str:
        return RECIPIENT_ID_PREFIX + "".join(f"{self._rng.randrange(256):02x}" for _ in range(8))

    def give_back(self, taken: Sequence[float]) -> None:
        """What ``admit`` counted on this instance, given back when the write refused."""
        self._instance.give_back(taken)

    async def record(self, action: AuditAction, recipient: Recipient, actor: Actor | None) -> None:
        await self._audit.write(
            AuditEvent(
                action, Outcome.SUCCESS, TargetType.ALERT_CHANNEL, recipient.channel_id,
                actor.user_id if actor is not None else None,
                {"organization_id": recipient.organization_id, "recipient_id": recipient.id},
            ),
            self._epoch.epoch_ms(),
        )  # fmt: skip

    # --- the caps, and the code ----------------------------------------------------------------

    async def admit(self, organization_id: str, channel_id: str, n: int) -> list[float]:
        """The hourly caps for ``n`` confirmations, before the write: the window counters (a
        counter that cannot answer refuses), then this instance's. Returns what the instance
        counted, to give back if the write refuses."""
        for _ in range(n):
            await self._windows.count(channel_id, organization_id)
        return self._instance.take(n)

    async def issue(self, recipient: Recipient) -> tuple[Recipient, str]:
        """In the caller's transaction: the per-address cap counted (or ``confirmation_limited``,
        and the transaction is undone), and a new code: its digest stored, its 24 hours
        started, the wrong entries reset. Returns the recipient as stored and the code."""
        digest = address_digest(self._address_key, recipient.address)
        if not await self._confirmations.count(digest, self.caps.per_address, ADDRESS_WINDOW_MS):
            raise limited("to one address", self.caps.per_address, "a day")
        code = new_code(self._rng)
        now = await self._store_clock.now_ms()
        issued = replace(
            recipient,
            state=UNVERIFIED,
            code_digest=code_digest(self._code_key, recipient.channel_id, recipient.address, code),
            code_expires_at_ms=now + CODE_VALID_MS,
            wrong_entries=0,
            verified_at_ms=None,
        )
        return issued, code

    async def send(self, issued: Sequence[tuple[Recipient, str]]) -> dict[str, str]:
        """After the commit: each confirmation handed to the mailer, never while a store
        transaction is open. The outcome code of each, by recipient id."""
        if self._held is not None and self._held.holds_transaction():
            raise RuntimeError("a confirmation is never sent while a store transaction is open")
        assert self.mailer is not None
        outcomes: dict[str, str] = {}
        for recipient, code in issued:
            name = await self._organization_name(recipient.organization_id)
            confirmation = Confirmation(
                recipient.id, recipient.channel_id, recipient.address, recipient.organization_id,
                name or recipient.organization_id, code,
            )  # fmt: skip
            outcomes[recipient.id] = await self.mailer.send_confirmation(confirmation)
        return outcomes

    def recipient(self, channel_id: str, organization_id: str, address: str) -> Recipient:
        return Recipient(
            self.new_id(), channel_id, organization_id, address, UNVERIFIED, None, None, 0,
            self._epoch.epoch_ms(),
        )  # fmt: skip

    # --- the actions ---------------------------------------------------------------------------

    async def add(
        self,
        organization_id: str,
        channel_id: str,
        address: object,
        *,
        actor: Actor,
        marker: ChangeMarker | None = None,
    ) -> tuple[Recipient, str]:
        """One more recipient, unverified, and its confirmation sent (capped). ``marker``: the
        step-up window this change used, marked in the transaction that makes it."""
        if not self.available:
            raise EmailUnavailableError()
        assert self.settings is not None
        checked = check_address(address, self.settings.domains)
        taken = await self.admit(organization_id, channel_id, 1)
        try:
            async with self.store.write_transaction():
                existing = await self.store.channel_recipients(channel_id)
                if len(existing) >= MAX_RECIPIENTS:
                    raise ManagementRefusal(
                        Reason.RECIPIENT_LIMIT,
                        f"an email channel has at most {MAX_RECIPIENTS} recipients",
                    )
                fresh = self.recipient(channel_id, organization_id, checked.address)
                try:
                    await self.store.insert_recipient(fresh)
                except RecipientTakenError:
                    raise ManagementRefusal(
                        Reason.RECIPIENT_TAKEN, "the channel already has that address"
                    ) from None
                issued, code = await self.issue(fresh)
                await self.store.put_recipient(issued)
                await self.record(AuditAction.ALERT_RECIPIENT_ADDED, issued, actor)
                await self.record(AuditAction.ALERT_RECIPIENT_CONFIRMATION_SENT, issued, actor)
                if marker is not None:
                    marker.changed()
        except BaseException:
            self.give_back(taken)
            raise
        outcome = await self.send([(issued, code)])
        return issued, outcome[issued.id]

    async def resend(
        self, organization_id: str, channel_id: str, recipient_id: str, *, actor: Actor
    ) -> tuple[Recipient, str]:
        """A new code to an unverified recipient (the old one void), capped as any."""
        if not self.available:
            raise EmailUnavailableError()
        taken = await self.admit(organization_id, channel_id, 1)
        try:
            async with self.store.write_transaction():
                found = await self.store.get_recipient(recipient_id)
                if found is None or found.channel_id != channel_id:
                    raise RecipientGoneError()
                if found.state == VERIFIED:
                    raise ManagementRefusal(
                        Reason.RECIPIENT_VERIFIED, "the recipient is verified already"
                    )
                issued, code = await self.issue(found)
                await self.store.put_recipient(issued)
                await self.record(AuditAction.ALERT_RECIPIENT_CONFIRMATION_SENT, issued, actor)
        except BaseException:
            self.give_back(taken)
            raise
        outcome = await self.send([(issued, code)])
        return issued, outcome[issued.id]

    async def confirm(
        self, organization_id: str, channel_id: str, recipient_id: str, code: object,
        *, actor: Actor,
    ) -> Recipient:  # fmt: skip
        """The code entered. Every refusal (wrong, expired, used, voided, unknown, another
        channel's or organization's) is one ``ConfirmationInvalidError`` after the same steps:
        one lookup, one HMAC, one comparison against the stored digest or ``DUMMY_DIGEST``. A
        wrong entry to a recipient with a live code counts; the fifth voids the code."""
        text = entered(code) if isinstance(code, str) else ""
        outcome: Recipient | None = None
        async with self.store.write_transaction():
            found = await self.store.get_recipient(recipient_id)
            now = await self._store_clock.now_ms()
            mine = (
                found is not None
                and found.channel_id == channel_id
                and found.organization_id == organization_id
            )
            stored = found.code_digest if mine and found and found.code_digest else DUMMY_DIGEST
            given = code_digest(
                self._code_key,
                found.channel_id if found is not None else channel_id,
                found.address if found is not None else "",
                text,
            )
            matches = hmac.compare_digest(given, stored)
            live = (
                mine
                and found is not None
                and found.state == UNVERIFIED
                and found.code_digest is not None
                and found.code_expires_at_ms is not None
                and now < found.code_expires_at_ms
                and found.wrong_entries < MAX_WRONG_ENTRIES
            )
            if live and matches and found is not None:
                outcome = replace(
                    found, state=VERIFIED, code_digest=None, code_expires_at_ms=None,
                    wrong_entries=0, verified_at_ms=self._epoch.epoch_ms(),
                )  # fmt: skip
                await self.store.put_recipient(outcome)
                await self.record(AuditAction.ALERT_RECIPIENT_VERIFIED, outcome, actor)
            elif live and found is not None:
                wrong = found.wrong_entries + 1
                await self.store.put_recipient(
                    replace(
                        found,
                        wrong_entries=wrong,
                        # The fifth wrong entry voids the code: a re-send makes a new one.
                        code_digest=found.code_digest if wrong < MAX_WRONG_ENTRIES else None,
                        code_expires_at_ms=(
                            found.code_expires_at_ms if wrong < MAX_WRONG_ENTRIES else None
                        ),
                    )
                )
        if outcome is None:
            raise ConfirmationInvalidError()
        return outcome

    async def remove(self, channel_id: str, recipient_id: str, *, actor: Actor) -> None:
        """Removed: no step-up (it narrows where data goes), audited, no notice."""
        async with self.store.write_transaction():
            found = await self.store.get_recipient(recipient_id)
            if found is None or found.channel_id != channel_id:
                raise RecipientGoneError()
            await self.store.delete_recipient(found.id)
            await self.record(AuditAction.ALERT_RECIPIENT_REMOVED, found, actor)


class EmailUnavailableError(Exception):
    """Email is not set up on this server (no SMTP settings, no allowed domain, or no mailer):
    answered 422."""


class RecipientGoneError(Exception):
    """Resolved a moment ago, removed since: answered as missing."""


async def resolve_recipient_in(
    recipients: RecipientStore, channel_id: str, recipient_id: str
) -> bool:
    """Whether the recipient exists and belongs to the channel (a recipient id not in its form
    is missing)."""
    if not RECIPIENT_ID.fullmatch(recipient_id):
        return False
    found = await recipients.get_recipient(recipient_id)
    return found is not None and found.channel_id == channel_id
