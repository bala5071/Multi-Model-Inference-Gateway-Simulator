"""A test SMTP server on asyncio streams, its certificates, and clocks for email tests (docs/48).

The server speaks enough SMTP for ``smtplib``: a greeting, EHLO (advertising STARTTLS unless told
not to, and AUTH PLAIN), STARTTLS (``StreamWriter.start_tls``), implicit TLS, AUTH PLAIN, MAIL,
RCPT, DATA and QUIT. Each step's reply can be scripted (``replies``: a 4xx or 5xx), or the
server can stop answering at a step (``hang_at``).
It records every envelope (sender, recipients, message), every login, whether TLS was on, and
every reply it sent (with ``marker`` in each, for the leak scan's control).

The certificate is made here at run time with ``cryptography`` (already a dependency): a CA
and a server certificate for a name; the client trusts the CA through the context it is given.
Nothing here is a secret.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime
import ipaddress
import ssl
import threading
from dataclasses import dataclass, field
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from gateway.clock import SimClock

SERVER_NAME = "smtp.test"


@dataclass(frozen=True)
class Certificates:
    ca_pem: bytes
    cert_file: Path
    key_file: Path

    def client_context(self) -> ssl.SSLContext:
        """What the gateway is given in tests: TLS 1.2 or higher, the name checked, the test CA
        the only one trusted."""
        context = ssl.create_default_context(cadata=self.ca_pem.decode("ascii"))
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context

    def server_context(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert_file, self.key_file)
        return context


def make_certificates(directory: Path, name: str = SERVER_NAME) -> Certificates:
    """A CA, and a certificate for ``name`` signed by it, written to ``directory``."""
    now = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "gateway test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    try:
        san: x509.GeneralName = x509.IPAddress(ipaddress.ip_address(name))
    except ValueError:
        san = x509.DNSName(name)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(2)
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName([san]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    directory.mkdir(parents=True, exist_ok=True)
    cert_file, key_file = directory / f"{name}.crt", directory / f"{name}.key"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return Certificates(ca.public_bytes(serialization.Encoding.PEM), cert_file, key_file)


@dataclass
class Envelope:
    sender: str
    recipients: list[str]
    data: bytes
    tls: bool
    user: str | None


@dataclass
class SmtpTestServer:
    certificates: Certificates
    starttls: bool = True
    implicit: bool = False
    username: str | None = None
    password: str | None = None
    replies: dict[str, tuple[int, str]] = field(default_factory=dict)
    hang_at: str | None = None
    marker: str = "planted-server-reply-5c71"
    envelopes: list[Envelope] = field(default_factory=list)
    logins: list[tuple[str, str]] = field(default_factory=list)
    plain_commands: list[str] = field(default_factory=list)
    sent: list[str] = field(default_factory=list)
    connections: int = 0
    port: int = 0
    _server: asyncio.base_events.Server | None = None
    _release: asyncio.Event = field(default_factory=asyncio.Event)

    async def start(self) -> int:
        context = self.certificates.server_context() if self.implicit else None
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0, ssl=context)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        self._release.set()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _reply(self, writer: asyncio.StreamWriter, step: str, code: int, text: str) -> None:
        if self.hang_at == step:
            await self._release.wait()
            return
        code, text = self.replies.get(step, (code, text))
        line = f"{code} {text} {self.marker}\r\n"
        self.sent.append(line)
        writer.write(line.encode())
        await writer.drain()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        tls = self.implicit
        user: str | None = None
        sender, recipients = "", list[str]()
        try:
            await self._reply(writer, "GREETING", 220, f"{SERVER_NAME} ESMTP")
            while True:
                raw = await reader.readline()
                if not raw:
                    return
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                verb = line.split(" ", 1)[0].upper()
                if not tls:
                    self.plain_commands.append(verb)
                if verb in ("EHLO", "HELO"):
                    if self.hang_at == "EHLO":
                        await self._release.wait()
                        return
                    features = [f"250-{SERVER_NAME} {self.marker}"]
                    if self.starttls and not tls:
                        features.append("250-STARTTLS")
                    features.append("250 AUTH PLAIN")
                    for feature in features:
                        self.sent.append(feature)
                        writer.write((feature + "\r\n").encode())
                    await writer.drain()
                elif verb == "STARTTLS":
                    await self._reply(writer, "STARTTLS", 220, "ready")
                    await writer.start_tls(self.certificates.server_context())
                    tls = True
                elif verb == "AUTH":
                    parts = line.split(" ")
                    decoded = base64.b64decode(parts[2]).split(b"\0") if len(parts) > 2 else []
                    given = (
                        (decoded[1].decode(), decoded[2].decode())
                        if len(decoded) == 3
                        else ("", "")
                    )
                    self.logins.append(given)
                    if given == (self.username, self.password):
                        user = given[0]
                        await self._reply(writer, "AUTH", 235, "authenticated")
                    else:
                        await self._reply(writer, "AUTH", 535, "authentication failed")
                elif verb == "MAIL":
                    sender, recipients = line.split(":", 1)[1].strip().strip("<>"), []
                    await self._reply(writer, "MAIL", 250, "sender ok")
                elif verb == "RCPT":
                    recipients.append(line.split(":", 1)[1].strip().strip("<>"))
                    await self._reply(writer, "RCPT", 250, "recipient ok")
                elif verb == "DATA":
                    await self._reply(writer, "DATA", 354, "go ahead")
                    data = bytearray()
                    while True:
                        chunk = await reader.readline()
                        if chunk in (b".\r\n", b""):
                            break
                        data += chunk
                    code, _ = self.replies.get("DOT", (250, ""))
                    if 200 <= code < 300:
                        self.envelopes.append(Envelope(sender, recipients, bytes(data), tls, user))
                    await self._reply(writer, "DOT", 250, "queued")
                elif verb == "RSET":
                    await self._reply(writer, "RSET", 250, "reset")
                elif verb == "QUIT":
                    await self._reply(writer, "QUIT", 221, "bye")
                    return
                else:
                    await self._reply(writer, "OTHER", 502, "not implemented")
        except (ConnectionError, ssl.SSLError, asyncio.IncompleteReadError):
            return
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, ssl.SSLError):
                await writer.wait_closed()


async def real_pause(seconds: float) -> None:
    """A real pause on the event loop's own timer, giving the loop back meanwhile (the test
    server's trickle, and a test waiting for threads it released to finish)."""
    loop = asyncio.get_running_loop()
    done: asyncio.Future[None] = loop.create_future()

    def wake() -> None:
        if not done.done():
            done.set_result(None)

    loop.call_later(seconds, wake)
    await done


class StepClock:
    """A clock that moves on by ``step_ms`` each time it is read, from any thread; its sleeps
    never end (so nothing on the event loop times out by it). Each socket call of an SMTP
    attempt reads it once, so the attempt's budget is spent after so many calls, whatever the
    server does, with no real time in the test."""

    def __init__(self, step_ms: float, start_ms: float = 0.0) -> None:
        self._lock = threading.Lock()
        self.step_ms = step_ms
        self._now = start_ms
        self.reads = 0

    def now_ms(self) -> float:
        with self._lock:
            self.reads += 1
            self._now += self.step_ms
            return self._now

    async def sleep_ms(self, duration_ms: float) -> None:
        await asyncio.get_running_loop().create_future()


class NeverClock:
    """A clock frozen at ``SimClock``'s start whose sleeps never end: an attempt bounded by it
    never times out on the event loop, and its thread's budget never shrinks."""

    def __init__(self) -> None:
        self._clock = SimClock()

    def now_ms(self) -> float:
        return self._clock.now_ms()

    async def sleep_ms(self, duration_ms: float) -> None:
        await asyncio.get_running_loop().create_future()
