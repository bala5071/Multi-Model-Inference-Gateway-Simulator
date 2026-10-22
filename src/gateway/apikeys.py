"""API keys: issuing, checking, rotating and revoking them.

See docs/20-applications-api-keys.md.

A key looks like ``gwk_<key id>_<secret>``. The key id (16 hex characters, 64 bits) is how
the record is found; the secret is 32 random bytes (256 bits). Only
``HMAC-SHA256(pepper, key)`` is stored, and it is compared with ``hmac.compare_digest``.
The pepper comes from the environment (``GATEWAY_KEY_PEPPER``) and is never written
anywhere. Every way a key can be wrong raises the same ``InvalidApiKeyError`` with the
same message, so a caller cannot tell an unknown key from a revoked or expired one.

``last_used_at`` is not written per request: the time is kept in memory and written for all
keys used since the last write in one transaction, at most once per ``last_used_flush_ms``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from gateway.applications import ApiKeyRecord, Application, ApplicationStore
from gateway.clock import EpochSource
from gateway.routing import PlansConfig

PEPPER_ENV = "GATEWAY_KEY_PEPPER"
MIN_PEPPER_LENGTH = 32
KEY_PREFIX = "gwk"
KEY_ID_BYTES = 8
SECRET_BYTES = 32
KEY_PATTERN = re.compile(r"gwk_(?P<id>[0-9a-f]{16})_[A-Za-z0-9_-]{43}")
DEFAULT_LAST_USED_FLUSH_MS = 60_000.0
INVALID_KEY_MESSAGE = "The API key is not valid."
# Compared against when there is no record, so an unknown key costs the same comparison.
_NO_RECORD = "0" * 64


class PepperError(RuntimeError):
    """The pepper is missing or too short. The message never contains its value."""


class InvalidApiKeyError(Exception):
    """Unknown, malformed, revoked or expired: deliberately indistinguishable."""

    def __init__(self) -> None:
        super().__init__(INVALID_KEY_MESSAGE)


class ApiKeyRequiredError(Exception):
    def __init__(self) -> None:
        super().__init__("This gateway needs an API key: send 'Authorization: Bearer <key>'.")


def load_pepper(environ: Mapping[str, str]) -> bytes:
    value = environ.get(PEPPER_ENV, "")
    if len(value) < MIN_PEPPER_LENGTH:
        raise PepperError(
            f"{PEPPER_ENV} must be set to a secret of at least {MIN_PEPPER_LENGTH} characters "
            '(for example the output of `python -c "import secrets; '
            'print(secrets.token_urlsafe(32))"`). See docs/20-applications-api-keys.md.'
        )
    return value.encode()


@dataclass(frozen=True, slots=True)
class IssuedKey:
    """A new key and its record. The key is shown once; ``repr`` never includes it."""

    key: str
    record: ApiKeyRecord

    def __repr__(self) -> str:
        return f"IssuedKey(key=<redacted>, record={self.record!r})"


class ApiKeyService:
    """Issues and checks keys against an ``ApplicationStore``.

    ``random_bytes`` is the source of key material (``secrets.token_bytes`` unless a test
    passes another). ``epoch`` dates keys and decides expiry.
    """

    def __init__(
        self,
        store: ApplicationStore,
        pepper: bytes,
        epoch: EpochSource,
        *,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
        last_used_flush_ms: float = DEFAULT_LAST_USED_FLUSH_MS,
    ) -> None:
        if len(pepper) < MIN_PEPPER_LENGTH:
            raise PepperError(f"the pepper must be at least {MIN_PEPPER_LENGTH} bytes")
        self._store = store
        self._pepper = pepper
        self._epoch = epoch
        self._random_bytes = random_bytes
        self._flush_ms = last_used_flush_ms
        self._pending: dict[str, float] = {}
        self._last_flush_ms = epoch.epoch_ms()

    def __repr__(self) -> str:
        return "ApiKeyService(<pepper redacted>)"

    @property
    def store(self) -> ApplicationStore:
        return self._store

    def _hmac(self, key: str) -> str:
        return hmac.new(self._pepper, key.encode(), hashlib.sha256).hexdigest()

    def issue(
        self, application_id: str, label: str, *, expires_at_ms: float | None = None
    ) -> IssuedKey:
        if self._store.get_application(application_id) is None:
            raise ValueError(f"no application {application_id!r}")
        key_id = self._random_bytes(KEY_ID_BYTES).hex()
        secret = base64.urlsafe_b64encode(self._random_bytes(SECRET_BYTES)).rstrip(b"=").decode()
        key = f"{KEY_PREFIX}_{key_id}_{secret}"
        record = ApiKeyRecord(
            key_id=key_id,
            application_id=application_id,
            label=label,
            key_hmac=self._hmac(key),
            created_at_ms=self._epoch.epoch_ms(),
            expires_at_ms=expires_at_ms,
        )
        self._store.add_key(record)
        return IssuedKey(key, record)

    def authenticate(self, presented: str) -> Application:
        """The application a key belongs to, or ``InvalidApiKeyError``."""
        match = KEY_PATTERN.fullmatch(presented)
        record = self._store.get_key(match.group("id")) if match else None
        # Always one HMAC and one constant-time comparison, whatever is wrong with the key.
        valid = hmac.compare_digest(
            self._hmac(presented), record.key_hmac if record is not None else _NO_RECORD
        )
        now_ms = self._epoch.epoch_ms()
        if (
            not valid
            or record is None
            or record.revoked
            or (record.expires_at_ms is not None and now_ms >= record.expires_at_ms)
        ):
            raise InvalidApiKeyError()
        application = self._store.get_application(record.application_id)
        if application is None:
            raise InvalidApiKeyError()
        self._pending[record.key_id] = now_ms
        if now_ms - self._last_flush_ms >= self._flush_ms:
            self.flush_last_used()
        return application

    def flush_last_used(self) -> int:
        """Write the pending last-used times in one transaction. Returns how many keys."""
        pending, self._pending = self._pending, {}
        self._last_flush_ms = self._epoch.epoch_ms()
        if pending:
            self._store.record_last_used(pending)
        return len(pending)

    def rotate(self, key_id: str, *, grace_ms: float, label: str | None = None) -> IssuedKey:
        """Issue a new key for the same application; the old one stays valid for ``grace_ms``
        and is then rejected as expired (or sooner, if it already expired sooner)."""
        if grace_ms < 0:
            raise ValueError("grace_ms must not be negative")
        old = self._store.get_key(key_id)
        if old is None or old.revoked:
            raise ValueError(f"no active key {key_id!r}")
        new = self.issue(old.application_id, label if label is not None else old.label)
        ends_ms = self._epoch.epoch_ms() + grace_ms
        if old.expires_at_ms is None or ends_ms < old.expires_at_ms:
            self._store.set_key_expiry(key_id, ends_ms)
        return new

    def revoke(self, key_id: str) -> None:
        self._store.revoke_key(key_id)


@dataclass(frozen=True, slots=True)
class ApplicationAccess:
    """What ``create_app`` needs to serve applications: the key service (and through it the
    application store) and the plan templates."""

    keys: ApiKeyService
    templates: PlansConfig

    @property
    def store(self) -> ApplicationStore:
        return self.keys.store
