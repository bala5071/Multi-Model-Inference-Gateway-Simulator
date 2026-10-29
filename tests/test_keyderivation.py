"""One key per purpose, derived from the pepper; and what rotating the pepper does to CSRF
tokens. See gateway.keyderivation and docs/27-users-and-sessions.md."""

from __future__ import annotations

import base64
import hashlib
import hmac
import itertools

import pytest

from accounts_helpers import PASSWORD, make_accounts, make_service
from gateway.apikeys import KEY_PREFIX
from gateway.auth import CsrfError, csrf_token_for
from gateway.keyderivation import DERIVATION_PREFIX, KeyPurpose, derive_key
from helpers import TEST_PEPPER

OTHER_PEPPER = b"a different test pepper, long enough to be one"


def test_every_purpose_has_its_own_label() -> None:
    labels = [purpose.value for purpose in KeyPurpose]
    assert len(set(labels)) == len(labels)
    assert set(labels) == {"csrf token", "login limiter"}


def test_derived_keys_differ_from_each_other_and_from_the_pepper() -> None:
    keys = {purpose: derive_key(TEST_PEPPER, purpose) for purpose in KeyPurpose}
    for a, b in itertools.combinations(keys.values(), 2):
        assert a != b
    for key in keys.values():
        assert len(key) == 32
        assert key != TEST_PEPPER
    # The same pepper and purpose always give the same key; another pepper, another key.
    assert derive_key(TEST_PEPPER, KeyPurpose.CSRF_TOKEN) == keys[KeyPurpose.CSRF_TOKEN]
    assert derive_key(OTHER_PEPPER, KeyPurpose.CSRF_TOKEN) != keys[KeyPurpose.CSRF_TOKEN]


def test_a_derivation_can_never_be_an_api_key_hmac() -> None:
    # API key HMACs are keyed with the pepper itself, over a key, which starts with "gwk_";
    # every derivation's input starts with the derivation prefix instead.
    assert not DERIVATION_PREFIX.startswith(KEY_PREFIX.encode())
    for purpose in KeyPurpose:
        message = DERIVATION_PREFIX + purpose.value.encode()
        expected = hmac.new(TEST_PEPPER, message, hashlib.sha256).digest()
        assert derive_key(TEST_PEPPER, purpose) == expected


async def test_the_csrf_token_uses_the_csrf_key_and_rotating_the_pepper_invalidates_it() -> None:
    accounts = make_accounts()
    await accounts.service.create_user("alice", PASSWORD, "north")
    login = await accounts.service.login("alice", PASSWORD)
    context = await accounts.service.authenticate(login.session_id)
    csrf_key = derive_key(TEST_PEPPER, KeyPurpose.CSRF_TOKEN)
    assert login.csrf_token == csrf_token_for(csrf_key, login.session_id)
    # Not keyed by the pepper itself, nor by the session id alone.
    for wrong_key in (TEST_PEPPER, login.session_id.encode()):
        unkeyed = hmac.new(wrong_key, login.session_id.encode(), hashlib.sha256).digest()
        assert login.csrf_token != base64.urlsafe_b64encode(unkeyed).rstrip(b"=").decode()
    accounts.service.check_csrf(context, login.csrf_token)

    # The same database, served with another pepper: the session still identifies its user,
    # but its CSRF token no longer matches, and the token given now is a different one.
    rotated = make_service(
        accounts.clock,
        accounts.epoch,
        accounts.connection,
        accounts.organizations,
        secret=OTHER_PEPPER,
    )
    context = await rotated.authenticate(login.session_id)
    assert rotated.csrf_token(context) != login.csrf_token
    with pytest.raises(CsrfError):
        rotated.check_csrf(context, rotated.csrf_token(context))
    assert rotated.limiter.key("alice") != accounts.service.limiter.key("alice")
