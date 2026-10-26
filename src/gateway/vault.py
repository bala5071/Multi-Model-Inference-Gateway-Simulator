"""The provider-key vault: authenticated encryption under a master key from the environment.

See docs/32-provider-settings-and-vault.md and docs/provider-plan.md, section 3.

- **AES-256-GCM** (``cryptography``), a fresh 96-bit random nonce per encryption.
- **The master key** is ``GATEWAY_VAULT_KEY`` (32 random bytes, base64url); the server refuses to
  start without it. During a rotation the old one is ``GATEWAY_VAULT_KEY_PREVIOUS``. A master
  key is named by its id, ``HMAC-SHA256(key, "gateway vault key id")``'s first 16 hex
  characters, which identifies it without revealing it.
- **Associated data binds a ciphertext to where it may be used**: its organization, provider,
  the provider's type, the origin of its base URL, and the master key's id. A ciphertext copied to
  another organization's row, or left behind when a provider's type or URL changed, does not
  decrypt.
- **No trial decryption.** A row is decrypted only with the master key its ``key_id`` names; if
  that key is not loaded, the row is ``key_unreadable`` and no other key is tried.
- What is stored: ``key_id``, ``nonce``, ``ciphertext``, and ``last4`` only for keys of 20 or
  more characters. Decryption gives a ``Secret``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from gateway.redaction import Redactor
from gateway.secret import Secret

VAULT_KEY_ENV = "GATEWAY_VAULT_KEY"
VAULT_KEY_PREVIOUS_ENV = "GATEWAY_VAULT_KEY_PREVIOUS"
MASTER_KEY_BYTES = 32
NONCE_BYTES = 12
LAST4_MIN_LENGTH = 20
_KEY_ID_LABEL = b"gateway vault key id"


class VaultKeyError(RuntimeError):
    """The master key is missing or malformed. The message never contains its value."""


class KeyUnreadableError(Exception):
    """A stored key was encrypted under a master key that is not loaded."""

    def __init__(self, key_id: str) -> None:
        self.key_id = key_id
        super().__init__(f"the provider key was encrypted under master key {key_id}, not loaded")


class VaultIntegrityError(Exception):
    """A ciphertext does not match what it is bound to (another row's, or changed)."""


def master_key_id(master: bytes) -> str:
    return hmac.new(master, _KEY_ID_LABEL, hashlib.sha256).hexdigest()[:16]


def _decode(name: str, value: str) -> bytes:
    try:
        raw = base64.urlsafe_b64decode(value.strip() + "=" * (-len(value.strip()) % 4))
    except (binascii.Error, ValueError):
        raise VaultKeyError(f"{name} is not base64url") from None
    if len(raw) != MASTER_KEY_BYTES:
        raise VaultKeyError(f"{name} must decode to {MASTER_KEY_BYTES} bytes")
    return raw


def load_master_keys(environ: Mapping[str, str]) -> tuple[bytes, bytes | None]:
    """(current, previous) from the environment, or ``VaultKeyError``."""
    current = environ.get(VAULT_KEY_ENV, "")
    if not current:
        raise VaultKeyError(
            f"{VAULT_KEY_ENV} must be set: 32 random bytes, base64url (for example the output of "
            '`python -c "import secrets; print(secrets.token_urlsafe(32))"`). See '
            "docs/32-provider-settings-and-vault.md."
        )
    previous = environ.get(VAULT_KEY_PREVIOUS_ENV, "")
    return (
        _decode(VAULT_KEY_ENV, current),
        _decode(VAULT_KEY_PREVIOUS_ENV, previous) if previous else None,
    )


def binding(
    organization_id: str, provider_id: str, provider_type: str, base_url: str, key_id: str
) -> bytes:
    """The associated data a ciphertext is bound to."""
    parts = urlsplit(base_url)
    origin = f"{parts.scheme}://{parts.netloc}".lower()
    return "|".join(
        ("vault-v1", organization_id, provider_id, provider_type, origin, key_id)
    ).encode()


@dataclass(frozen=True, slots=True)
class SealedKey:
    """What is stored for a provider key: never the key."""

    key_id: str
    nonce: bytes
    ciphertext: bytes
    last4: str | None

    def __repr__(self) -> str:
        return f"SealedKey(key_id={self.key_id!r}, <ciphertext>, last4={self.last4!r})"


@dataclass(frozen=True, slots=True)
class Binding:
    organization_id: str
    provider_id: str
    provider_type: str
    base_url: str


class Vault:
    """Seals and opens provider keys. ``master`` encrypts; ``previous``, during a rotation,
    only decrypts."""

    def __init__(
        self,
        master: bytes,
        previous: bytes | None = None,
        *,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        if len(master) != MASTER_KEY_BYTES or (
            previous is not None and len(previous) != MASTER_KEY_BYTES
        ):
            raise VaultKeyError(f"a master key is {MASTER_KEY_BYTES} bytes")
        self._keys: dict[str, bytes] = {master_key_id(master): master}
        self.current_key_id = master_key_id(master)
        if previous is not None:
            self._keys[master_key_id(previous)] = previous
        self._random_bytes = random_bytes

    def __repr__(self) -> str:
        return f"Vault(current_key_id={self.current_key_id!r}, <keys redacted>)"

    @property
    def key_ids(self) -> frozenset[str]:
        return frozenset(self._keys)

    def readable(self, sealed: SealedKey) -> bool:
        return sealed.key_id in self._keys

    def seal(self, plaintext: str, where: Binding) -> SealedKey:
        """Encrypt a key as it arrived (the admin endpoint's body) under the current master
        key. The plaintext is never wrapped and unwrapped here: ``Secret.reveal()`` keeps its
        one call site (the adapters' header builder)."""
        nonce = self._random_bytes(NONCE_BYTES)
        ciphertext = AESGCM(self._keys[self.current_key_id]).encrypt(
            nonce, plaintext.encode(), self._aad(where, self.current_key_id)
        )
        last4 = plaintext[-4:] if len(plaintext) >= LAST4_MIN_LENGTH else None
        return SealedKey(self.current_key_id, nonce, ciphertext, last4)

    def open(self, sealed: SealedKey, where: Binding) -> Secret:
        """The key, decrypted with the master key ``sealed`` names and no other."""
        return Secret(self._decrypt(sealed, where).decode())

    @contextmanager
    def lease(self, sealed: SealedKey, where: Binding, redactor: Redactor) -> Iterator[Secret]:
        """The key for one use (docs/32): decrypted at the last moment, scrubbed from every log
        record while the block runs, and no reference kept after it. Python cannot erase the
        string; nothing here holds it once the block ends."""
        plaintext = self._decrypt(sealed, where).decode()
        with redactor.watching(plaintext):
            yield Secret(plaintext)
        del plaintext

    def reseal(self, sealed: SealedKey, where: Binding) -> SealedKey:
        """The same key under the current master key (rotation), keeping its ``last4``."""
        plaintext = self._decrypt(sealed, where)
        nonce = self._random_bytes(NONCE_BYTES)
        ciphertext = AESGCM(self._keys[self.current_key_id]).encrypt(
            nonce, plaintext, self._aad(where, self.current_key_id)
        )
        return SealedKey(self.current_key_id, nonce, ciphertext, sealed.last4)

    @staticmethod
    def _aad(where: Binding, key_id: str) -> bytes:
        return binding(
            where.organization_id, where.provider_id, where.provider_type, where.base_url, key_id
        )

    def _decrypt(self, sealed: SealedKey, where: Binding) -> bytes:
        master = self._keys.get(sealed.key_id)
        if master is None:
            raise KeyUnreadableError(sealed.key_id)
        try:
            return AESGCM(master).decrypt(
                sealed.nonce, sealed.ciphertext, self._aad(where, sealed.key_id)
            )
        except InvalidTag:
            raise VaultIntegrityError("the stored key does not match its provider") from None
