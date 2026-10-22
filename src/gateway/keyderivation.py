"""Keys derived from the pepper, one per purpose. See docs/27-users-and-sessions.md.

Each purpose gets its own key, ``HMAC-SHA256(pepper, "gateway derived key v1: " + label)``,
so no two purposes ever key an HMAC with the same bytes, and none uses the pepper itself.
The one exception is older than this module: API key HMACs (``gateway.apikeys``) are keyed
with the pepper directly, and changing that would invalidate every key already issued. Their
input is always a key (``gwk_...``), and a derivation's input always starts with
``DERIVATION_PREFIX``, so a derived key can never equal an API key's HMAC.

Rotating the pepper changes every derived key: CSRF tokens of existing sessions stop
matching, and the in-memory login limiter starts again.
"""

from __future__ import annotations

import hashlib
import hmac
from enum import StrEnum

DERIVATION_PREFIX = b"gateway derived key v1: "


class KeyPurpose(StrEnum):
    """Each value is the label of one derived key; no two may be the same."""

    CSRF_TOKEN = "csrf token"
    LOGIN_LIMITER = "login limiter"


def derive_key(secret: bytes, purpose: KeyPurpose) -> bytes:
    """The key for ``purpose``: 32 bytes, from ``secret`` (the pepper) and the purpose's label."""
    return hmac.new(
        secret, DERIVATION_PREFIX + KeyPurpose(purpose).value.encode(), hashlib.sha256
    ).digest()
