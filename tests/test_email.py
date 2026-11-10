"""Email channels' pure rules (docs/48): the operator's settings, a recipient address's checks
and the exact-domain allowlist (A5-22), and the confirmation code's alphabet, length, entropy,
display and entry. Test values, not secrets."""

from __future__ import annotations

import math
import random

import pytest

from gateway.alerts.email import (
    CODE_ALPHABET,
    CODE_BITS,
    CODE_LENGTH,
    AddressError,
    address_digest,
    check_address,
    check_syntax,
    code_digest,
    domains_from,
    email_settings_from,
    entered,
    new_code,
    normalised,
    shown,
)

DOMAINS = frozenset({"example.com", "xn--bcher-kva.example"})
BASE = {
    "GATEWAY_SMTP_HOST": "smtp.example.com",
    "GATEWAY_SMTP_FROM": "alerts@example.com",
    "GATEWAY_ALERT_EMAIL_DOMAINS": "example.com",
}


# --- addresses ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "stored"),
    [
        ("ops@example.com", "ops@example.com"),
        ("Ops.Team+alerts@EXAMPLE.com", "Ops.Team+alerts@example.com"),  # domain lowercased
        ("a!#$%&'*+/=?^_`{|}~-z@example.com", "a!#$%&'*+/=?^_`{|}~-z@example.com"),
        ("ops@b\u00fccher.example", "ops@xn--bcher-kva.example"),  # IDNA, stored in ASCII
        ("x" * 64 + "@example.com", "x" * 64 + "@example.com"),
    ],
)
def test_an_address_is_stored_as_entered_with_its_domain_lowercased_in_ascii(
    address: str, stored: str
) -> None:
    assert check_address(address, DOMAINS).address == stored


@pytest.mark.parametrize(
    ("address", "rule"),
    [
        ("", "non-empty"),
        ("opsexample.com", "exactly one @"),
        ("ops@one@example.com", "exactly one @"),
        ("Ops <ops@example.com>", "control character, whitespace"),  # a display name: a space
        ('"ops"@example.com', "local part"),  # a quoted string
        ("ops(comment)@example.com", "local part"),
        (".ops@example.com", "no dot at an end"),
        ("ops.@example.com", "no dot at an end"),
        ("o..ps@example.com", "no dot at an end"),
        ("x" * 65 + "@example.com", "local part"),
        ("ops@example.com\r\nBcc: x@example.com", "control character"),  # header injection
        ("ops@example.com\n", "control character"),
        ("ops\\@example.com", "backslash"),
        ("ops\u200b@example.com", "control character"),  # a zero-width space (Cf)
        ("ops@-bad-.example.com", "valid name"),
        ("ops@localhost", "valid name"),  # one label
        ("ops@" + "a" * 64 + ".com", "valid name"),
        ("o" * 60 + "@" + ("d" * 60 + ".") * 3 + "example.com", "at most 254"),
    ],
)
def test_each_address_rule_refuses_and_says_which_never_the_address(
    address: str, rule: str
) -> None:
    with pytest.raises(AddressError) as refused:
        check_address(address, DOMAINS | {("d" * 60 + ".") * 3 + "example.com"})
    assert rule in str(refused.value)
    if address:
        assert address not in str(refused.value)


@pytest.mark.parametrize(
    "address",
    [
        "ops@eu.example.com",  # a subdomain of an allowed domain (A5-22: exact only)
        "ops@example.com.evil.net",
        "ops@notexample.com",  # a suffix match would take it
        "ops@example.org",
    ],
)
def test_the_allowlist_is_exact_domains_only(address: str) -> None:
    with pytest.raises(AddressError, match="not one of the allowed domains"):
        check_address(address, DOMAINS)
    assert check_address("ops@example.com", DOMAINS).domain == "example.com"  # the control


def test_the_senders_address_is_checked_for_form_without_the_allowlist() -> None:
    assert check_syntax("alerts@operator.example.net").address == "alerts@operator.example.net"
    with pytest.raises(AddressError):
        check_syntax("Alerts <alerts@operator.example.net>")


def test_the_cap_counts_by_a_stricter_normalised_form_and_never_holds_the_address() -> None:
    assert normalised(" Ops@Example.COM ") == "ops@example.com"
    key = b"k" * 32
    one, two = address_digest(key, "Ops@example.com"), address_digest(key, "ops@EXAMPLE.com")
    assert one == two  # two spellings of perhaps one mailbox count together
    assert "ops" not in one
    assert len(one) == 64
    assert address_digest(b"j" * 32, "ops@example.com") != one  # keyed


# --- the operator's settings ---------------------------------------------------------------------


def test_settings_from_the_environment_with_their_defaults() -> None:
    settings = email_settings_from(BASE)
    assert settings is not None
    assert (settings.host, settings.port, settings.tls, settings.username) == (
        "smtp.example.com", 587, "starttls", None,
    )  # fmt: skip
    assert settings.sender == "alerts@example.com"
    assert settings.domains == frozenset({"example.com"})
    implicit = email_settings_from({**BASE, "GATEWAY_SMTP_TLS": "implicit"})
    assert implicit is not None
    assert implicit.port == 465
    relay = email_settings_from(
        {**BASE, "GATEWAY_SMTP_HOST": "mailrelay", "GATEWAY_SMTP_PORT": "2525",
         "GATEWAY_SMTP_USERNAME": "gateway"}
    )  # fmt: skip
    assert relay is not None
    assert (relay.host, relay.port, relay.username) == ("mailrelay", 2525, "gateway")


def test_no_host_or_no_domain_means_no_email_and_the_start_does_not_fail() -> None:
    assert email_settings_from({}) is None
    assert email_settings_from({**BASE, "GATEWAY_SMTP_HOST": ""}) is None
    assert email_settings_from({**BASE, "GATEWAY_ALERT_EMAIL_DOMAINS": ""}) is None


@pytest.mark.parametrize(
    ("change", "variable"),
    [
        ({"GATEWAY_SMTP_TLS": "cleartext"}, "GATEWAY_SMTP_TLS"),  # there is no plain option
        ({"GATEWAY_SMTP_TLS": "none"}, "GATEWAY_SMTP_TLS"),
        ({"GATEWAY_SMTP_PORT": "0"}, "GATEWAY_SMTP_PORT"),
        ({"GATEWAY_SMTP_PORT": "65536"}, "GATEWAY_SMTP_PORT"),
        ({"GATEWAY_SMTP_PORT": "smtp"}, "GATEWAY_SMTP_PORT"),
        ({"GATEWAY_SMTP_HOST": "smtp example.com"}, "GATEWAY_SMTP_HOST"),
        ({"GATEWAY_SMTP_HOST": "bad_host!"}, "GATEWAY_SMTP_HOST"),
        ({"GATEWAY_SMTP_FROM": ""}, "GATEWAY_SMTP_FROM"),
        ({"GATEWAY_SMTP_FROM": "Alerts <a@example.com>"}, "GATEWAY_SMTP_FROM"),
        ({"GATEWAY_SMTP_USERNAME": "gate\nway"}, "GATEWAY_SMTP_USERNAME"),
        ({"GATEWAY_ALERT_EMAIL_DOMAINS": "*.example.com"}, "GATEWAY_ALERT_EMAIL_DOMAINS"),
        ({"GATEWAY_ALERT_EMAIL_DOMAINS": "example.com,*"}, "GATEWAY_ALERT_EMAIL_DOMAINS"),
        ({"GATEWAY_ALERT_EMAIL_DOMAINS": "not a domain"}, "GATEWAY_ALERT_EMAIL_DOMAINS"),
    ],
)
def test_a_setting_that_is_not_valid_refuses_the_start_naming_it_never_its_value(
    change: dict[str, str], variable: str
) -> None:
    with pytest.raises(ValueError, match=variable) as refused:
        email_settings_from({**BASE, **change})
    assert variable in str(refused.value)
    for value in change.values():
        if value:
            assert value not in str(refused.value)


def test_the_domains_are_exact_ascii_names() -> None:
    assert domains_from(" Example.COM , b\u00fccher.example,, ") == frozenset(
        {"example.com", "xn--bcher-kva.example"}
    )


# --- the confirmation code ---------------------------------------------------------------------


def test_the_code_is_ten_base32_characters_fifty_bits() -> None:
    assert CODE_ALPHABET == "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
    assert len(set(CODE_ALPHABET)) == 32
    assert not set("0189") & set(CODE_ALPHABET)  # no 0/O or 1/I confusion
    assert CODE_LENGTH == 10
    assert CODE_BITS == CODE_LENGTH * int(math.log2(len(CODE_ALPHABET))) == 50
    rng = random.Random(3)
    codes = [new_code(rng) for _ in range(2000)]
    assert all(len(c) == 10 and set(c) <= set(CODE_ALPHABET) for c in codes)
    assert len(set(codes)) == len(codes)
    # Every symbol is drawn: the whole alphabet appears.
    assert set("".join(codes)) == set(CODE_ALPHABET)


def test_the_code_is_drawn_from_the_random_source_given() -> None:
    assert new_code(random.Random(9)) == new_code(random.Random(9))
    assert new_code(random.Random(9)) != new_code(random.Random(10))


def test_the_code_is_shown_in_two_groups_and_entered_in_any_case() -> None:
    assert shown("ABCDEFGH23") == "ABCDE-FGH23"
    for typed in ("ABCDE-FGH23", "abcde fgh23", " abcdefgh23 ", "Abc-De-Fgh-23"):
        assert entered(typed) == "ABCDEFGH23"
    # Anything else is passed on as it is, to be compared and fail, after the same steps.
    assert entered("ABCDEFGH2") == "ABCDEFGH2"
    assert entered("ABCDEFGH23!") == "ABCDEFGH23!"


def test_a_codes_digest_binds_the_channel_and_the_address() -> None:
    key = b"c" * 32
    base = code_digest(key, "chn_00000000000000a1", "ops@example.com", "ABCDEFGH23")
    assert base == code_digest(key, "chn_00000000000000a1", "OPS@example.com", "ABCDEFGH23")
    assert base != code_digest(key, "chn_00000000000000a2", "ops@example.com", "ABCDEFGH23")
    assert base != code_digest(key, "chn_00000000000000a1", "other@example.com", "ABCDEFGH23")
    assert base != code_digest(key, "chn_00000000000000a1", "ops@example.com", "ABCDEFGH24")
    assert base != code_digest(b"d" * 32, "chn_00000000000000a1", "ops@example.com", "ABCDEFGH23")
    assert b"ABCDEFGH23" not in base
