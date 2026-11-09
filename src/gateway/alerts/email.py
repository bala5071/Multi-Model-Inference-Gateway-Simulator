"""Email alert channels: the operator's SMTP settings, recipient addresses and the confirmation
code (docs/48, the A5 plan's sections 2, 4 and 5). Pure: no I/O, no clock, no randomness of its
own (the code is drawn from the random source passed in).

**The operator's settings** come from the environment of each instance. Unset
``GATEWAY_SMTP_HOST``, or an empty ``GATEWAY_ALERT_EMAIL_DOMAINS``, means no email channel can be
created; the start does not fail. Any value set that is not valid refuses the start, naming the
variable. There is no plain-text option: ``GATEWAY_SMTP_TLS`` is ``starttls`` or ``implicit``.

**An address** is checked when it is added: one ``@``; a local part of letters, digits and
``.!#$%&'*+/=?^_`{|}~-`` (no quoted string, comment or display name, no dot at an end or two
together), at most 64; a domain that encodes as IDNA, at most 253, **exactly** one of the
allowed domains (``example.com`` allows ``ops@example.com``, not ``ops@eu.example.com``); at
most 254 in all; no control character, whitespace or backslash anywhere. It is stored as entered
with its domain lowercased and in ASCII.

**The confirmation code** is 10 characters of RFC 4648's base32 alphabet (32 symbols, 5 bits
each: 50 bits), each drawn with ``rng.randrange(32)``; the server passes
``random.SystemRandom``. It is shown as two groups of five and entered in any case, spaces and
hyphens ignored.
"""

from __future__ import annotations

import hashlib
import hmac
import random
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass

SMTP_HOST_ENV = "GATEWAY_SMTP_HOST"
SMTP_PORT_ENV = "GATEWAY_SMTP_PORT"
SMTP_TLS_ENV = "GATEWAY_SMTP_TLS"
SMTP_USERNAME_ENV = "GATEWAY_SMTP_USERNAME"
SMTP_FROM_ENV = "GATEWAY_SMTP_FROM"
EMAIL_DOMAINS_ENV = "GATEWAY_ALERT_EMAIL_DOMAINS"
TLS_MODES = ("starttls", "implicit")
DEFAULT_PORTS = {"starttls": 587, "implicit": 465}

MAX_ADDRESS = 254
MAX_LOCAL = 64
MAX_DOMAIN = 253
MAX_RECIPIENTS = 10

CODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
CODE_LENGTH = 10
CODE_BITS = 50

_LOCAL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+")
_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")


class AddressError(ValueError):
    """An address refused when it is added. The message says which rule, never the address."""


def _no_bad_characters(text: str, what: str) -> None:
    for character in text:
        if (
            character.isspace()
            or character == "\\"
            or unicodedata.category(character) in ("Cc", "Cf", "Zl", "Zp")
        ):
            raise AddressError(f"{what} has no control character, whitespace or backslash")


def _ascii_domain(domain: str, what: str) -> str:
    """A domain in lowercase ASCII (IDNA), each label valid, or ``AddressError``."""
    try:
        ascii_domain = domain.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise AddressError(f"{what}'s domain is not a valid name") from None
    if not ascii_domain or len(ascii_domain) > MAX_DOMAIN:
        raise AddressError(f"{what}'s domain is 1 to {MAX_DOMAIN} characters")
    labels = ascii_domain.split(".")
    if len(labels) < 2 or not all(_LABEL.fullmatch(label) for label in labels):
        raise AddressError(f"{what}'s domain is not a valid name")
    return ascii_domain


@dataclass(frozen=True, slots=True)
class CheckedAddress:
    """An address that passed the checks: as stored (its domain lowercased, in ASCII), and its
    domain."""

    address: str
    domain: str

    def __repr__(self) -> str:
        return f"CheckedAddress(domain={self.domain!r}, <address redacted>)"


def check_syntax(address: object, what: str = "an address") -> CheckedAddress:
    """An address's form, without the allowlist (the sender's own address is checked this way),
    or ``AddressError`` naming the rule broken."""
    if not isinstance(address, str) or not address:
        raise AddressError(f"{what} is a non-empty string")
    if len(address) > MAX_ADDRESS:
        raise AddressError(f"{what} is at most {MAX_ADDRESS} characters")
    _no_bad_characters(address, what)
    if address.count("@") != 1:
        raise AddressError(f"{what} has exactly one @ and no display name")
    local, domain = address.split("@")
    if not local or len(local) > MAX_LOCAL or not _LOCAL.fullmatch(local):
        raise AddressError(
            f"{what}'s local part is 1 to {MAX_LOCAL} of letters, digits and "
            ".!#$%&'*+/=?^_`{|}~- (no quotes, comments or display name)"
        )
    if local.startswith(".") or local.endswith(".") or ".." in local:
        raise AddressError(f"{what}'s local part has no dot at an end or two together")
    ascii_domain = _ascii_domain(domain, what)
    stored = f"{local}@{ascii_domain}"
    if len(stored) > MAX_ADDRESS:
        raise AddressError(f"{what} is at most {MAX_ADDRESS} characters")
    return CheckedAddress(stored, ascii_domain)


def check_address(address: object, domains: frozenset[str]) -> CheckedAddress:
    """A recipient address: its form, and its domain **exactly** one of ``domains``."""
    checked = check_syntax(address)
    if checked.domain not in domains:
        raise AddressError("an address's domain is not one of the allowed domains")
    return checked


def normalised(address: str) -> str:
    """The form the per-address cap counts by: NFC, stripped, lowercased whole (stricter than
    mail servers: two spellings that may be one mailbox count together)."""
    return unicodedata.normalize("NFC", address).strip().lower()


def address_digest(key: bytes, address: str) -> str:
    """The per-address cap's row key: an HMAC of the normalised address, never the address."""
    return hmac.new(key, normalised(address).encode(), hashlib.sha256).hexdigest()


# --- the confirmation code ----------------------------------------------------------------------


def new_code(rng: random.Random) -> str:
    """Ten characters of the base32 alphabet, each from ``rng``: 50 bits."""
    return "".join(CODE_ALPHABET[rng.randrange(len(CODE_ALPHABET))] for _ in range(CODE_LENGTH))


def shown(code: str) -> str:
    """As the confirmation shows it: two groups of five (``ABCDE-FGH23``)."""
    return f"{code[:5]}-{code[5:]}"


def entered(text: str) -> str:
    """What was entered, as compared: upper case, spaces and hyphens removed. Nothing else is
    judged here: anything that is not a code simply does not match, after the same steps."""
    return re.sub(r"[\s-]", "", text).upper()


def code_digest(key: bytes, channel_id: str, address: str, code: str) -> bytes:
    """What is stored for a code: an HMAC over the channel, the address and the code, so a code
    confirms only the (channel, address) it was sent for."""
    message = "\x1f".join((channel_id, normalised(address), code)).encode()
    return hmac.new(key, message, hashlib.sha256).digest()


# --- the operator's settings ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EmailSettings:
    """The operator's SMTP server and the domains recipients may be in."""

    host: str
    port: int
    tls: str
    username: str | None
    sender: str
    domains: frozenset[str]


def _host(value: str) -> str:
    """The operator's SMTP host: a name (one label allowed: it may be on the operator's own
    network) or an address. The certificate is checked against it."""
    _no_bad_characters_host(value)
    if re.fullmatch(r"[0-9.]+|[0-9A-Fa-f:]+", value):
        return value.lower()
    try:
        name = value.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ValueError(f"{SMTP_HOST_ENV} is a host name or an address") from None
    if not name or len(name) > MAX_DOMAIN or not all(_LABEL.fullmatch(p) for p in name.split(".")):
        raise ValueError(f"{SMTP_HOST_ENV} is a host name or an address")
    return name


def _no_bad_characters_host(value: str) -> None:
    try:
        _no_bad_characters(value, "the SMTP host")
    except AddressError:
        raise ValueError(
            f"{SMTP_HOST_ENV} has no control character, whitespace or backslash"
        ) from None


def domains_from(text: str) -> frozenset[str]:
    """``GATEWAY_ALERT_EMAIL_DOMAINS``: exact domains, comma-separated. A ``*`` anywhere, or an
    entry that is not a domain, refuses the start."""
    found: set[str] = set()
    for entry in text.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "*" in entry:
            raise ValueError(f"{EMAIL_DOMAINS_ENV} lists exact domains only; no wildcard")
        try:
            found.add(_ascii_domain(entry, "an allowed domain"))
        except AddressError:
            raise ValueError(f"{EMAIL_DOMAINS_ENV} lists domains, comma-separated") from None
    return frozenset(found)


def email_settings_from(environ: Mapping[str, str]) -> EmailSettings | None:
    """The operator's email settings, or None when email is not set up (no host, or no allowed
    domain). Every variable that is set is checked, and one that is not valid refuses the
    start, naming it; its value is never repeated."""
    domains = domains_from(environ.get(EMAIL_DOMAINS_ENV, ""))
    raw_host = environ.get(SMTP_HOST_ENV, "").strip()
    if not raw_host:
        return None
    host = _host(raw_host)
    tls = environ.get(SMTP_TLS_ENV, "starttls").strip() or "starttls"
    if tls not in TLS_MODES:
        raise ValueError(f"{SMTP_TLS_ENV} is starttls or implicit; there is no plain option")
    raw_port = environ.get(SMTP_PORT_ENV, "").strip()
    if raw_port:
        if not raw_port.isdigit() or not 1 <= int(raw_port) <= 65535:
            raise ValueError(f"{SMTP_PORT_ENV} is a whole number from 1 to 65535")
        port = int(raw_port)
    else:
        port = DEFAULT_PORTS[tls]
    username = environ.get(SMTP_USERNAME_ENV, "")
    if username and any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in username):
        raise ValueError(f"{SMTP_USERNAME_ENV} has no control character")
    sender = environ.get(SMTP_FROM_ENV, "")
    if not sender:
        raise ValueError(f"{SMTP_FROM_ENV} must be set with {SMTP_HOST_ENV}: one address")
    try:
        checked_sender = check_syntax(sender, "the sender")
    except AddressError:
        raise ValueError(f"{SMTP_FROM_ENV} is one address, checked as a recipient's") from None
    if not domains:
        return None
    return EmailSettings(host, port, tls, username or None, checked_sender.address, domains)
