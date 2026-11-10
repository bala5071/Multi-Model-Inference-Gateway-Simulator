"""Email delivery through the operator's SMTP server (docs/48, the A5 plan's sections 2 and 7).

**One attempt, one message, one recipient,** within a hard budget (``SMTP_ATTEMPT_MS``, 10 s):

1. **The name lookup** runs on the event loop, ``getaddrinfo`` on a pool of its own
   (``BoundedResolver``), bounded on the injected clock by the budget. A lookup past it is
   abandoned: the attempt is ``timeout`` and the lookup's thread runs on until the system resolver
   gives up, holding no delivery slot, socket or credential. **At most ``LOOKUP_THREADS`` (4)
   lookups run at once; when every thread is held, the attempt is refused before any lookup,
   ``resolver_busy``** (counted toward the channel's health and retried, with a metric and an
   event).
2. **The SMTP conversation** runs in a thread of its own pool (``SMTP_THREADS``), with
   ``smtplib`` subclasses (``DeadlineSMTP``, ``DeadlineSMTP_SSL``) that set the socket's timeout
   to what is left of the budget before **every** blocking socket call: the connect, each send,
   each read of a reply, the TLS handshake. One reply takes several reads, so a server sending a
   byte at a time cannot stretch the attempt: it ends ``timeout`` once the budget is spent. The
   attempt awaits its thread to the end, so a delivery's slot is held until the thread returns.
3. **TLS always:** STARTTLS (required when chosen: a server that does not offer it fails ``tls``
   before any credential or message is sent) or implicit TLS, with the pinned context, the
   certificate checked against ``GATEWAY_SMTP_HOST``. There is no plain path.
4. **The password,** if a username is set, is leased from the platform scope for the attempt
   (scrubbed from every log record meanwhile) and revealed only to log in (``_login``).

**Nothing of the conversation is logged:** ``smtplib``'s debug output is dropped whatever its
level, and a failure is kept as a fixed code only, never the server's words.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import smtplib
import socket
import ssl
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.policy import SMTP as SMTP_POLICY
from email.utils import format_datetime
from typing import TYPE_CHECKING, Any, Protocol

from gateway.alerts.email import EmailSettings
from gateway.alerts.recipients import Confirmation
from gateway.clock import Clock, EpochSource
from gateway.overload import RequestTimeoutError, run_with_timeout
from gateway.platformsecrets import SMTP_PASSWORD, PlatformSecretStore
from gateway.redaction import Redactor
from gateway.secret import Secret
from gateway.vault import KeyUnreadableError, PlatformBinding, Vault, VaultIntegrityError

if TYPE_CHECKING:
    from _typeshed import ReadableBuffer

LOG = logging.getLogger("gateway.alerts")

SMTP_ATTEMPT_MS = 10_000.0
LOOKUP_THREADS = 4
SMTP_THREADS = 2

DELIVERED = "delivered"
TLS = "tls"
TIMEOUT = "timeout"
CONNECT_FAILED = "connect_failed"
SMTP_4XX = "smtp_4xx"
SMTP_5XX = "smtp_5xx"
SMTP_CREDENTIAL_UNREADABLE = "smtp_credential_unreadable"
RESOLVER_BUSY = "resolver_busy"
NO_VERIFIED_RECIPIENT = "no_verified_recipient"
RECIPIENT_NOT_VERIFIED = "recipient_not_verified"
DOMAIN_NOT_ALLOWED = "domain_not_allowed"
# Every code an email attempt can come to, and whether it is tried again.
RETRIED = frozenset({TIMEOUT, CONNECT_FAILED, SMTP_4XX, RESOLVER_BUSY})
EMAIL_CODES = frozenset(
    {
        DELIVERED, TLS, TIMEOUT, CONNECT_FAILED, SMTP_4XX, SMTP_5XX, SMTP_CREDENTIAL_UNREADABLE,
        RESOLVER_BUSY, NO_VERIFIED_RECIPIENT, RECIPIENT_NOT_VERIFIED, DOMAIN_NOT_ALLOWED,
    }
)  # fmt: skip


class Deadline:
    """An attempt's hard end, on the injected clock: what is left of it, in seconds, for the
    next blocking socket call, or ``TimeoutError`` once nothing is (and ``expired`` set)."""

    def __init__(self, clock: Clock, at_ms: float) -> None:
        self._clock = clock
        self.at_ms = at_ms
        self.expired = False

    def left_s(self) -> float:
        left = (self.at_ms - self._clock.now_ms()) / 1000.0
        if left <= 0:
            self.expired = True
            raise TimeoutError("the attempt's budget is spent")
        return left


class _DeadlineFile:
    """What ``smtplib`` reads replies from: a line at a time, each ``recv`` given what is left
    of the attempt's budget, so no reply can take longer than the attempt has."""

    def __init__(self, sock: socket.socket, deadline: Deadline) -> None:
        self._sock = sock
        self._deadline = deadline
        self._buffer = bytearray()

    def readline(self, limit: int = -1) -> bytes:
        while True:
            end = self._buffer.find(b"\n")
            if end >= 0 or (0 < limit <= len(self._buffer)):
                cut = end + 1 if end >= 0 else limit
                if 0 < limit < cut:
                    cut = limit
                line = bytes(self._buffer[:cut])
                del self._buffer[:cut]
                return line
            self._sock.settimeout(self._deadline.left_s())
            try:
                chunk = self._sock.recv(4096)
            except TimeoutError:
                # The call took what was left: the attempt's budget is spent.
                self._deadline.expired = True
                raise
            if not chunk:
                line = bytes(self._buffer)
                self._buffer.clear()
                return line
            self._buffer += chunk

    def close(self) -> None:
        self._buffer.clear()


class _Bounded:
    """What the two subclasses share: every blocking socket call given what is left."""

    sock: Any
    file: Any
    _deadline: Deadline

    def _print_debug(self, *args: object) -> None:
        # The conversation (AUTH included) is never printed, whatever the debug level.
        return None

    def send(self, s: ReadableBuffer | str) -> None:
        if not self.sock:
            raise smtplib.SMTPServerDisconnected("please run connect() first")
        view = memoryview(s.encode("ascii") if isinstance(s, str) else s).cast("B")
        while view:
            self.sock.settimeout(self._deadline.left_s())
            try:
                sent = self.sock.send(view)
            except TimeoutError:
                self._deadline.expired = True
                raise
            view = view[sent:]

    def getreply(self) -> tuple[int, bytes]:
        if self.file is None:
            self.file = _DeadlineFile(self.sock, self._deadline)
        return smtplib.SMTP.getreply(self)  # type: ignore[arg-type]


class DeadlineSMTP(_Bounded, smtplib.SMTP):
    """``smtplib.SMTP`` with the hard deadline, connecting to an address resolved beforehand and
    checking the certificate against the server's name."""

    def __init__(
        self, server_name: str, address: str, port: int, deadline: Deadline, context: ssl.SSLContext
    ) -> None:
        self._deadline = deadline
        self._context = context
        super().__init__(local_hostname="gateway.invalid")
        self._host = server_name
        self.sock = socket.create_connection((address, port), timeout=deadline.left_s())
        self.file = None
        code, message = self.getreply()
        if code != 220:
            self.close()
            raise smtplib.SMTPConnectError(code, message)

    def starttls(self, *args: Any, **kwargs: Any) -> tuple[int, bytes]:
        """STARTTLS with the pinned context, the handshake bounded by what is left; refused (as
        ``SMTPNotSupportedError``) by a server that does not offer it."""
        self.ehlo_or_helo_if_needed()
        if not self.has_extn("starttls"):
            raise smtplib.SMTPNotSupportedError("the server does not offer STARTTLS")
        code, reply = self.docmd("STARTTLS")
        if code != 220:
            raise smtplib.SMTPResponseException(code, reply)
        self.sock.settimeout(self._deadline.left_s())
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self._host)
        self.file = None
        self.helo_resp = None
        self.ehlo_resp = None
        self.esmtp_features = {}
        self.does_esmtp = False
        return code, reply


class DeadlineSMTP_SSL(_Bounded, smtplib.SMTP):
    """Implicit TLS from the first byte, with the hard deadline."""

    def __init__(
        self, server_name: str, address: str, port: int, deadline: Deadline, context: ssl.SSLContext
    ) -> None:
        self._deadline = deadline
        super().__init__(local_hostname="gateway.invalid")
        self._host = server_name
        raw = socket.create_connection((address, port), timeout=deadline.left_s())
        raw.settimeout(deadline.left_s())
        self.sock = context.wrap_socket(raw, server_hostname=server_name)
        self.file = None
        code, message = self.getreply()
        if code != 220:
            self.close()
            raise smtplib.SMTPConnectError(code, message)


# --- the name lookup -----------------------------------------------------------------------------


class ResolverBusyError(Exception):
    """Every lookup thread is held (by lookups past their budget): refused before any lookup."""


Lookup = Callable[[str, int], str]


def system_lookup(host: str, port: int) -> str:
    """The first address the system resolver gives for a stream connection."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not infos:
        raise OSError("no address")
    return str(infos[0][4][0])


class BoundedResolver:
    """``LOOKUP_THREADS`` threads for name lookups, taken without waiting. A lookup past its
    budget is abandoned and keeps its thread until the resolver returns; ``abandoned`` counts
    those still running (the gauge)."""

    def __init__(self, lookup: Lookup = system_lookup, threads: int = LOOKUP_THREADS) -> None:
        self._lookup = lookup
        self.threads = threads
        self._pool = ThreadPoolExecutor(max_workers=threads, thread_name_prefix="smtp-lookup")
        self._lock = threading.Lock()
        self.busy = 0
        self.abandoned = 0

    def _run(self, host: str, port: int) -> str:
        try:
            return self._lookup(host, port)
        finally:
            with self._lock:
                self.busy -= 1

    async def resolve(self, host: str, port: int, clock: Clock, budget_ms: float) -> str:
        """The address, or ``ResolverBusyError`` (no thread free: nothing looked up),
        ``RequestTimeoutError`` (past the budget: abandoned) or ``OSError``."""
        with self._lock:
            if self.busy >= self.threads:
                raise ResolverBusyError()
            self.busy += 1
        future = asyncio.get_running_loop().run_in_executor(self._pool, self._run, host, port)
        try:
            return await run_with_timeout(clock, asyncio.shield(future), budget_ms)
        except RequestTimeoutError:
            with self._lock:
                self.abandoned += 1

            def finished(_: object) -> None:
                with self._lock:
                    self.abandoned -= 1

            future.add_done_callback(finished)
            raise

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


# --- the sender ---------------------------------------------------------------------------------


class Transactional(Protocol):
    def holds_transaction(self) -> bool: ...


SmtpFactory = Callable[[str, str, int, Deadline, ssl.SSLContext], smtplib.SMTP]


def _login(smtp: smtplib.SMTP, username: str, password: Secret) -> None:
    """The one place the SMTP password is revealed: to log in (docs/48)."""
    smtp.login(username, password.reveal())


def _code_of(exc: BaseException, deadline: Deadline) -> str:
    """A failure, by its kind alone: nothing of the server's words is kept."""
    if deadline.expired or isinstance(exc, TimeoutError | socket.timeout):
        return TIMEOUT
    if isinstance(exc, ssl.SSLError | ssl.CertificateError | smtplib.SMTPNotSupportedError):
        return TLS
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        codes = [code for code, _ in exc.recipients.values()]
        return SMTP_4XX if codes and all(400 <= c < 500 for c in codes) else SMTP_5XX
    if isinstance(exc, smtplib.SMTPResponseException):
        return SMTP_4XX if 400 <= exc.smtp_code < 500 else SMTP_5XX
    return CONNECT_FAILED


class SmtpSender:
    """Sends one message to one recipient within ``budget_ms``; the outcome's code."""

    def __init__(
        self,
        settings: EmailSettings,
        secrets: PlatformSecretStore,
        vault: Vault,
        redactor: Redactor,
        clock: Clock,
        *,
        context: ssl.SSLContext,
        resolver: BoundedResolver | None = None,
        held: Transactional | None = None,
        budget_ms: float = SMTP_ATTEMPT_MS,
        smtp_factory: SmtpFactory | None = None,
        threads: int = SMTP_THREADS,
    ) -> None:
        self.settings = settings
        self._secrets = secrets
        self._vault = vault
        self._redactor = redactor
        self._clock = clock
        self._context = context
        self.resolver = resolver if resolver is not None else BoundedResolver()
        self._held = held
        self.budget_ms = budget_ms
        self._factory = smtp_factory
        self._pool = ThreadPoolExecutor(max_workers=threads, thread_name_prefix="smtp")

    def __repr__(self) -> str:
        return f"SmtpSender(host={self.settings.host!r}, <secrets redacted>)"

    def _connect(self, address: str, deadline: Deadline) -> smtplib.SMTP:
        if self._factory is not None:
            return self._factory(self.settings.host, address, self.settings.port, deadline,
                                 self._context)  # fmt: skip
        kind = DeadlineSMTP_SSL if self.settings.tls == "implicit" else DeadlineSMTP
        return kind(self.settings.host, address, self.settings.port, deadline, self._context)

    def _exchange(
        self, address: str, recipient: str, message: bytes, password: Secret | None,
        deadline: Deadline,
    ) -> str:  # fmt: skip
        """In the SMTP thread: the whole conversation, within the deadline."""
        smtp: smtplib.SMTP | None = None
        try:
            smtp = self._connect(address, deadline)
            if self.settings.tls == "starttls":
                smtp.starttls()
            smtp.ehlo()
            if self.settings.username is not None and password is not None:
                _login(smtp, self.settings.username, password)
            smtp.sendmail(self.settings.sender, [recipient], message)
            return DELIVERED
        except Exception as exc:
            return _code_of(exc, deadline)
        finally:
            if smtp is not None:
                with contextlib.suppress(Exception):
                    smtp.close()

    async def send(self, recipient: str, message: EmailMessage) -> str:
        """One attempt: the lookup, the password leased, the conversation in a thread, all
        within the budget; never while a store transaction is open."""
        if self._held is not None and self._held.holds_transaction():
            raise RuntimeError("an email is never sent while a store transaction is open")
        deadline = Deadline(self._clock, self._clock.now_ms() + self.budget_ms)
        host = self.settings.host
        try:
            ipaddress.ip_address(host)
            address = host
        except ValueError:
            try:
                address = await self.resolver.resolve(
                    host, self.settings.port, self._clock, deadline.at_ms - self._clock.now_ms()
                )
            except ResolverBusyError:
                return RESOLVER_BUSY
            except RequestTimeoutError:
                return TIMEOUT
            except OSError:
                return CONNECT_FAILED
        with contextlib.ExitStack() as leases:
            opened: Secret | None = None
            if self.settings.username is not None:
                stored = await self._secrets.get(SMTP_PASSWORD)
                if stored is None:
                    return SMTP_CREDENTIAL_UNREADABLE
                # Bound to the server it is for, as the settings say now: a password set for
                # another host, port or username does not open.
                where = PlatformBinding(
                    SMTP_PASSWORD, self.settings.host, self.settings.port, self.settings.username
                )
                try:
                    opened = leases.enter_context(
                        self._vault.lease(stored.sealed, where, self._redactor)
                    )
                except (KeyUnreadableError, VaultIntegrityError):
                    return SMTP_CREDENTIAL_UNREADABLE
            data = message.as_bytes(policy=SMTP_POLICY)
            loop = asyncio.get_running_loop()
            # Awaited to the end: the delivery's slot is held until the thread returns.
            return await loop.run_in_executor(
                self._pool, self._exchange, address, recipient, data, opened, deadline
            )

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
        self.resolver.close()


# --- the messages ---------------------------------------------------------------------------------

_KIND_WORDS = {
    "firing": "FIRING",
    "firing_again": "FIRING AGAIN",
    "reminder": "STILL FIRING",
    "resolved": "RESOLVED",
    "quiet_hours_summary": "QUIET HOURS SUMMARY",
    "rule_weakened": "RULE WEAKENED",
    "channel_removed": "CHANNEL REMOVED",
    "suppressed_summary": "SUPPRESSED SUMMARY",
    "test": "TEST",
    "channel_delivery_failing": "CHANNEL DELIVERY FAILING",
}


def _domain_of(address: str) -> str:
    return address.rsplit("@", 1)[-1]


def _headers(message: EmailMessage, sender: str, recipient: str, subject: str, at_ms: float,
             message_id: str) -> None:  # fmt: skip
    # Set through the library under the SMTP policy, which refuses a CR or LF in a header.
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message["Date"] = format_datetime(datetime.fromtimestamp(at_ms / 1000.0, UTC))
    message["Message-ID"] = message_id
    message["Auto-Submitted"] = "auto-generated"


def notification_message(
    sender: str, recipient: str, recipient_id: str, payload: dict[str, Any], at_ms: float
) -> EmailMessage:
    """A notification as plain text, made by a fixed template from the stored payload's
    allowlisted fields: no HTML, no link, no image. One recipient in ``To``; the Message-ID from
    the notification and the recipient, so a retried message is the same message."""
    kind = str(payload.get("kind", ""))
    rule = payload.get("rule") or {}
    application = payload.get("application") or {}
    incident = payload.get("incident") or {}
    details = payload.get("details") or {}
    words = _KIND_WORDS.get(kind, kind.upper())
    severity = rule.get("severity")
    subject = f"[gateway] {words}"
    if severity:
        subject += f" {severity}"
    if rule.get("name"):
        subject += f": {rule['name']}"
    if application.get("name"):
        subject += f" on {application['name']}"
    lines = [f"{words}", ""]
    lines.append(f"Organization: {payload.get('organization', {}).get('id')}")
    if application:
        lines.append(f"Application: {application.get('name')} ({application.get('id')})")
    if rule:
        lines.append(
            f"Rule: {rule.get('name')} ({rule.get('id')}): {rule.get('signal')} "
            f"{rule.get('comparison')} {rule.get('threshold')} {rule.get('unit')}"
        )
        lines.append(f"Severity: {severity}")
    if incident:
        lines.append(f"Incident: {incident.get('id')}, {incident.get('state')}")
        if incident.get("value") is not None:
            lines.append(f"Value: {incident.get('value')}")
        for key, label in (("fired_at", "Fired at"), ("resolved_at", "Resolved at"),
                           ("resolve_reason", "Resolved because")):  # fmt: skip
            if incident.get(key):
                lines.append(f"{label}: {incident.get(key)}")
    for key in ("fields", "channel", "notifications", "rules", "held", "period_start",
                "period_end"):  # fmt: skip
        if details.get(key) is not None:
            value = details[key]
            lines.append(f"{key.replace('_', ' ').capitalize()}: "
                         f"{', '.join(value) if isinstance(value, list) else value}")  # fmt: skip
    lines.append(f"Sent at: {payload.get('sent_at')}")
    if payload.get("console_path"):
        lines.append(f"In the console: {payload['console_path']}")
    lines += ["", "This message was sent by the gateway's alerting. Do not reply."]
    message = EmailMessage(policy=SMTP_POLICY)
    notification = str(payload.get("delivery_id", "ntf_unknown"))
    _headers(message, sender, recipient, subject, at_ms,
             f"<{notification}.{recipient_id}@{_domain_of(sender)}>")  # fmt: skip
    message.set_content("\n".join(lines) + "\n", cte="quoted-printable")
    return message


def confirmation_message(sender: str, confirmation: Confirmation, at_ms: float) -> EmailMessage:
    """The confirmation: who asked (the organization's name in quotes and its id), that nothing
    else is sent unless the code is entered, and the code. Plain text, no link."""
    body = (
        f'The organization "{confirmation.organization_name}" ({confirmation.organization_id}) '
        "asked to send its alerts to this address.\n\n"
        f"Confirmation code: {confirmation.shown_code}\n\n"
        "The code is valid for 24 hours. An administrator of that organization enters it in the "
        "gateway's console. Nothing else will be sent to this address unless it is entered; if "
        "you did not expect this message, ignore it.\n"
    )
    message = EmailMessage(policy=SMTP_POLICY)
    _headers(message, sender, confirmation.address, "[gateway] Confirm this address for alerts",
             at_ms, f"<{confirmation.recipient_id}.confirm@{_domain_of(sender)}>")  # fmt: skip
    message.set_content(body, cte="quoted-printable")
    return message


@dataclass
class SmtpMailer:
    """The recipients' mailer: each confirmation sent as its own message."""

    sender: SmtpSender
    epoch: EpochSource

    async def send_confirmation(self, confirmation: Confirmation) -> str:
        message = confirmation_message(
            self.sender.settings.sender, confirmation, self.epoch.epoch_ms()
        )
        return await self.sender.send(confirmation.address, message)
