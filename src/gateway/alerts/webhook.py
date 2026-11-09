"""One webhook attempt: the request, its signature, and what it comes to (docs/46, the A4 plan's
section 4).

**The request** is ``POST`` with the stored ``json-v1`` body, exactly as it was queued, and a
fixed set of headers whose values the gateway makes: ``Content-Type: application/json``,
``X-Gateway-Delivery`` (the notification's id: receivers deduplicate on it),
``X-Gateway-Timestamp`` (Unix seconds at this attempt) and ``X-Gateway-Signature``, one
``v1=<hex HMAC-SHA256(key, timestamp + "." + body)>`` per signing key (two during a rotation).
Every attempt is signed afresh with its own timestamp; the delivery id stays the same across
retries. Nothing from a user goes into a header.

**``build_request``** is the one place in the alert modules that reveals a secret (the URL and
the signing keys: one ``reveal()`` call, M36), inside the vault's lease.

**What it comes to** is one of the fixed codes of ``CODES``, chosen by the kind of failure and
never formatted from an exception (``httpx``'s messages carry the URL, A4-13). At most
``MAX_ANSWER_BYTES`` of the answer is read, then discarded: only its status class is kept.

**The HTTP libraries log nothing of an attempt.** ``httpx`` logs every request's full URL at
INFO, and ``httpcore``'s DEBUG trace logs the answer's headers (a redirect's ``Location``); the
URL is a secret and the answer is the receiver's. While an attempt runs (``send``, and the tasks
it starts), ``QuietAttempt`` drops those loggers' records, and only those (a context variable,
so other requests at the same time log as before). The leak scan found both (docs/46).
"""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import logging
from collections.abc import Sequence
from dataclasses import dataclass

import httpx

from gateway.chat import ProviderError, ProviderErrorCode
from gateway.clock import Clock
from gateway.egress import (
    PORT_REFUSED,
    WEBHOOK_ATTEMPT_MS,
    EgressRefused,
    StageTimeout,
    TlsFailure,
)
from gateway.overload import RequestTimeoutError, run_with_timeout
from gateway.secret import Secret

MAX_ANSWER_BYTES = 4 * 1024
SIGNATURE_VERSION = "v1"

DELIVERED = "delivered"
EGRESS_REFUSED = "egress_refused"
PORT_REFUSED_CODE = "port_refused"
DNS_FAILURE = "dns_failure"
TIMEOUT = "timeout"
CONNECT_FAILED = "connect_failed"
TLS = "tls"
HTTP_3XX = "http_3xx"
HTTP_4XX = "http_4xx"
HTTP_5XX = "http_5xx"
PAYLOAD_TOO_LARGE = "payload_too_large"
CHANNEL_SECRET_UNREADABLE = "channel_secret_unreadable"
CHANNEL_GONE = "channel_gone"
CHANNEL_DISABLED = "channel_disabled"
RULE_CAP = "rule_cap"
ORGANIZATION_CAP = "organization_cap"
# Every code a delivery, its event, its metric or a test send can carry, and nothing else.
CODES = frozenset(
    {
        DELIVERED, EGRESS_REFUSED, PORT_REFUSED_CODE, DNS_FAILURE, TIMEOUT, CONNECT_FAILED, TLS,
        HTTP_3XX, HTTP_4XX, HTTP_5XX, PAYLOAD_TOO_LARGE, CHANNEL_SECRET_UNREADABLE,
        CHANNEL_GONE, CHANNEL_DISABLED, RULE_CAP, ORGANIZATION_CAP,
    }
)  # fmt: skip
# Retried: a connection failure, a timeout, a name that did not resolve, a 5xx, a 408, a 429.
RETRIED_STATUSES = frozenset({408, 429})
RETRY_AFTER_MAX_S = 3600.0


def signature(key: bytes, timestamp: int, body: str) -> str:
    """``hex(HMAC-SHA256(key, timestamp + "." + body))``."""
    return hmac.new(key, f"{timestamp}.{body}".encode("ascii"), hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class Prepared:
    """One attempt's request, its URL and keys revealed: made and sent inside the leases, and
    never kept."""

    url: str
    headers: tuple[tuple[str, str], ...]
    body: bytes

    def __repr__(self) -> str:
        return "Prepared(<url and signature redacted>)"


def build_request(
    url: Secret, keys: Sequence[Secret], body: str, delivery_id: str, timestamp: int
) -> Prepared:
    """The request of one attempt: ``body`` as stored, signed with each key (hex, as shown when
    it was made) over this attempt's ``timestamp``. The one ``reveal()`` of the alert modules
    (M36)."""
    revealed = [s.reveal() for s in (url, *keys)]
    signatures = ",".join(
        f"{SIGNATURE_VERSION}={signature(bytes.fromhex(k), timestamp, body)}" for k in revealed[1:]
    )
    return Prepared(
        revealed[0],
        (
            ("Content-Type", "application/json"),
            ("X-Gateway-Delivery", delivery_id),
            ("X-Gateway-Timestamp", str(timestamp)),
            ("X-Gateway-Signature", signatures),
        ),
        body.encode("ascii"),
    )


@dataclass(frozen=True, slots=True)
class Attempt:
    """What one attempt came to: a code of ``CODES``, whether it may be retried, and the
    receiver's ``Retry-After`` (seconds, at most an hour) if it gave one."""

    code: str
    retry: bool
    retry_after_s: float | None = None

    @property
    def delivered(self) -> bool:
        return self.code == DELIVERED


def _retry_after(value: str | None) -> float | None:
    if value is None or not value.strip().isdigit():
        return None
    return min(float(value.strip()), RETRY_AFTER_MAX_S)


def judged(status: int, retry_after: str | None) -> Attempt:
    """An answer's status, as a code: 2xx delivered; 3xx permanent (its ``Location`` neither
    fetched, stored nor logged); 408, 429 and 5xx retried; any other 4xx permanent."""
    if 200 <= status < 300:
        return Attempt(DELIVERED, False)
    if 300 <= status < 400:
        return Attempt(HTTP_3XX, False)
    if 500 <= status < 600:
        return Attempt(HTTP_5XX, True, _retry_after(retry_after))
    if status in RETRIED_STATUSES:
        return Attempt(HTTP_4XX, True, _retry_after(retry_after))
    return Attempt(HTTP_4XX, False)


def failure(exc: BaseException) -> Attempt:
    """A failure, by its kind alone: nothing of its message is kept (A4-13)."""
    if isinstance(exc, EgressRefused):
        code = PORT_REFUSED_CODE if exc.detail == PORT_REFUSED else EGRESS_REFUSED
        return Attempt(code, False)
    if isinstance(exc, TlsFailure):
        return Attempt(TLS, False)
    if isinstance(exc, StageTimeout | RequestTimeoutError | httpx.TimeoutException):
        return Attempt(TIMEOUT, True)
    if isinstance(exc, ProviderError):
        if exc.code is ProviderErrorCode.UPSTREAM_UNAVAILABLE and exc.detail == "dns_failure":
            return Attempt(DNS_FAILURE, True)
        return Attempt(CONNECT_FAILED, True)
    return Attempt(CONNECT_FAILED, True)


async def _exchange(client: httpx.AsyncClient, request: Prepared) -> Attempt:
    async with client.stream(
        "POST", request.url, content=request.body, headers=list(request.headers)
    ) as response:
        read = 0
        async for chunk in response.aiter_raw():
            # Read at most 4 KiB of the answer, then let it go: never stored or logged.
            read += len(chunk)
            if read >= MAX_ANSWER_BYTES:
                break
        return judged(response.status_code, response.headers.get("retry-after"))


# The loggers of httpx and httpcore (every one they make).
HTTP_LOGGERS = (
    "httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2", "httpcore.proxy",
    "httpcore.socks",
)  # fmt: skip
_IN_ATTEMPT: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "gateway_webhook_attempt", default=False
)


class QuietAttempt(logging.Filter):
    """Drops the HTTP libraries' records made inside a webhook attempt."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not _IN_ATTEMPT.get()


QUIET = QuietAttempt()


def quiet_http_logs() -> None:
    """``QUIET`` on each HTTP logger, once (idempotent: a logger keeps one of it)."""
    for name in HTTP_LOGGERS:
        logger = logging.getLogger(name)
        if QUIET not in logger.filters:
            logger.addFilter(QUIET)


async def send(client: httpx.AsyncClient, request: Prepared, clock: Clock) -> Attempt:
    """One attempt through ``client`` (a ``webhook_client``, closed here), at most
    ``WEBHOOK_ATTEMPT_MS`` in all on ``clock``, the HTTP libraries' log records of it dropped."""
    quiet_http_logs()
    token = _IN_ATTEMPT.set(True)
    try:
        async with client:
            return await run_with_timeout(clock, _exchange(client, request), WEBHOOK_ATTEMPT_MS)
    except Exception as exc:  # every failure becomes a fixed code
        return failure(exc)
    finally:
        _IN_ATTEMPT.reset(token)
