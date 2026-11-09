"""The pure rules of A4's group 1 (docs/46): the webhook URL when it is stored (the plan's
sections 4.1, 4.2 and 21.2), the webhook ports, the payload allowlist and its size bound
(section 6), the rule-weakening directions (M29), and the vault's channel scope (section 3).

Test values, not secrets: every URL, token and key here is made up for these tests."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from gateway.alertrules import NAME as RULE_NAME
from gateway.alertrules import QuietHours, RuleSpec, validate_rule
from gateway.alerts import payload as payload_module
from gateway.alerts.channels import NAME as CHANNEL_NAME
from gateway.alerts.channels import (
    WebhookUrlError,
    check_webhook_url,
    parse_webhook_ports,
)
from gateway.alerts.payload import (
    MAX_PAYLOAD_BYTES,
    AlertPayload,
    PayloadApplication,
    PayloadDetails,
    PayloadInvalidError,
    PayloadRule,
    PayloadTooLargeError,
)
from gateway.alerts.weakening import weakened_fields
from gateway.vault import (
    Binding,
    ChannelBinding,
    Vault,
    VaultIntegrityError,
    binding,
    channel_binding,
)

MASTER = bytes(range(32))

# --- the webhook URL, when it is stored ---------------------------------------------------------

REFUSED = [
    # Scheme, user info, fragment, length.
    ("http://hooks.example.com/x", "https"),
    ("ftp://hooks.example.com/x", "https"),
    ("https://user:pass@hooks.example.com/x", "user info"),
    ("https://user@hooks.example.com/x", "user info"),
    ("https://hooks.example.com/x#frag", "fragment"),
    ("https://hooks.example.com/" + "a" * 2030, "2048"),
    # Control characters, whitespace and backslashes anywhere (A4-15).
    ("https://hooks.example.com/x\r\nX-Injected: 1", "control"),
    ("https://hooks.example.com/x\ny", "control"),
    ("https://hooks.example.com/x\ty", "control"),
    ("https://hooks.example.com/x y", "control"),
    ("https://hooks.example.com\\@evil.example/", "control"),
    ("https://hooks.example.com/x\x00", "control"),
    ("https://hooks.example.com/x\x7f", "control"),
    ("https://hooks.example.com/x\u200by", "control"),
    ("https://hooks.example.com/x\u2028y", "control"),
    (" https://hooks.example.com/x", "control"),
    # Names that are not public.
    ("https://localhost/x", "public name"),
    ("https://LOCALHOST./x", "public name"),
    ("https://a.localhost/x", "public name"),
    ("https://printer.local/x", "public name"),
    ("https://db.internal/x", "public name"),
    ("https://" + "a" * 64 + ".example/x", "valid name"),
    ("https://a..example/x", "valid name"),
    # Literal addresses that are not public (section 4.2's IPv6 and mapped cases included).
    ("https://127.0.0.1/x", "public address"),
    ("https://10.0.0.1/x", "public address"),
    ("https://192.168.1.1/x", "public address"),
    ("https://169.254.169.254/latest", "public address"),
    ("https://100.100.100.200/x", "public address"),
    ("https://0.0.0.0/x", "public address"),
    ("https://[::1]/x", "public address"),
    ("https://[::]/x", "public address"),
    ("https://[fe80::1]/x", "public address"),
    ("https://[fe80::1%25eth0]/x", "zone id"),
    ("https://[fc00::1]/x", "public address"),
    ("https://[fd00:ec2::254]/x", "public address"),
    ("https://[ff02::1]/x", "public address"),
    ("https://[::ffff:127.0.0.1]/x", "public address"),
    ("https://[::ffff:169.254.169.254]/x", "public address"),
    ("https://[64:ff9b::a9fe:a9fe]/x", "public address"),
    ("https://[2002:7f00:1::]/x", "public address"),
    ("https://[2001:0:4136:e378:8000:63bf:3fff:fdd2]/x", "public address"),
    # Numeric hosts a resolver reads as addresses (section 21.2): decimal and hex whole
    # numbers, octal, short forms.
    ("https://2130706433/x", "dotted quad"),
    ("https://0x7f000001/x", "dotted quad"),
    ("https://0177.0.0.1/x", "dotted quad"),
    ("https://127.1/x", "dotted quad"),
    ("https://0x7f.1/x", "dotted quad"),
    ("https://017700000001/x", "dotted quad"),
    ("https://1.2.3.04/x", "dotted quad"),
    ("https://example.0x10/x", "dotted quad"),
    ("https://3627734734/x", "dotted quad"),  # public as a number: still never stored so
    # Ports (A4-9): only 443 unless the operator allows more.
    ("https://hooks.example.com:80/x", "port"),
    ("https://hooks.example.com:22/x", "port"),
    ("https://hooks.example.com:25/x", "port"),
    ("https://hooks.example.com:6379/x", "port"),
    ("https://hooks.example.com:0/x", "port"),
    ("https://hooks.example.com:65536/x", "port"),
    ("https://hooks.example.com:8443/x", "port"),
    ("https://hooks.example.com:abc/x", "port"),
    # No host at all.
    ("https:///x", "host"),
]


@pytest.mark.parametrize(("url", "why"), REFUSED)
def test_a_webhook_url_is_refused_when_stored(url: str, why: str) -> None:
    with pytest.raises(WebhookUrlError) as caught:
        check_webhook_url(url)
    message = str(caught.value)
    assert why in message, message
    # The refusal names the rule, never the URL or any of it.
    assert "example" not in message
    assert message != "x"


@pytest.mark.parametrize("value", [None, 7, b"https://hooks.example.com", "", ["https://x"]])
def test_a_webhook_url_is_a_non_empty_string(value: Any) -> None:
    with pytest.raises(WebhookUrlError):
        check_webhook_url(value)


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("https://hooks.example.com/services/T0/B0/planted?token=abc", "https://hooks.example.com"),
        ("HTTPS://Hooks.Example.COM/x", "https://hooks.example.com"),
        ("https://hooks.example.com:443/x", "https://hooks.example.com"),
        ("https://hooks.example.com./x", "https://hooks.example.com"),
        ("https://b\u00fccher.example/x", "https://xn--bcher-kva.example"),
        ("https://93.184.216.34/x", "https://93.184.216.34"),
        ("https://[2606:4700:4700::1111]/x", "https://[2606:4700:4700::1111]"),
        ("https://hooks.example.com", "https://hooks.example.com"),
    ],
)
def test_a_public_https_url_is_kept_and_only_its_origin_shown(url: str, origin: str) -> None:
    checked = check_webhook_url(url)
    assert (checked.url, checked.origin) == (url, origin)
    assert "planted" not in repr(checked)
    assert "token" not in repr(checked)


def test_a_port_the_operator_allowed_is_accepted_at_store() -> None:
    ports = parse_webhook_ports("8443, 9443")
    assert ports == frozenset({443, 8443, 9443})
    checked = check_webhook_url("https://hooks.example.com:8443/x", ports)
    assert (checked.origin, checked.port) == ("https://hooks.example.com:8443", 8443)
    with pytest.raises(WebhookUrlError):
        check_webhook_url("https://hooks.example.com:9000/x", ports)


@pytest.mark.parametrize("text", ["1024", "65535", "1024,65535", "", None, " 2048 ,, 3000 "])
def test_the_webhook_ports_at_their_bounds(text: str | None) -> None:
    ports = parse_webhook_ports(text)
    assert 443 in ports
    assert all(p == 443 or 1024 <= p <= 65535 for p in ports)


@pytest.mark.parametrize("text", ["1023", "65536", "0", "80", "443", "22", "-1", "abc", "8443x"])
def test_a_webhook_port_outside_its_range_refuses_the_start(text: str) -> None:
    with pytest.raises(ValueError, match="GATEWAY_ALERT_WEBHOOK_PORTS"):
        parse_webhook_ports(text)


def test_the_channel_name_charset_is_the_rule_name_charset() -> None:
    assert CHANNEL_NAME.pattern == RULE_NAME.pattern


# --- the payload allowlist ----------------------------------------------------------------------

RULE = {
    "name": "Errors",
    "signal": "error_rate",
    "comparison": ">",
    "threshold": 5,
    "window": 5,
    "for_minutes": 5,
    "severity": "critical",
}


def _payload(**changes: Any) -> AlertPayload:
    base: dict[str, Any] = {
        "delivery_id": "ntf_00000000000000a1",
        "kind": "rule_weakened",
        "sent_at_ms": 1.8e12,
        "organization_id": "north",
        "application": PayloadApplication("north-shop", 'Shop "x" \\ } \u00e9'),
        "rule": PayloadRule(
            "rul_00000000000000aa", "Errors", "error_rate", ">", 5.0, "%", 300, 300, "critical"
        ),
        "console_path": "/v1/admin/orgs/north/applications/north-shop/alert-rules/x",
        "details": PayloadDetails(fields=("threshold",), deleted=False),
    }
    return AlertPayload(**{**base, **changes})


def test_a_payload_is_json_v1_from_its_fields_and_ascii_only() -> None:
    body = _payload().body()
    assert body.isascii()
    found = json.loads(body)
    assert found["version"] == 1
    assert found["application"]["name"] == 'Shop "x" \\ } \u00e9'
    assert found["sent_at"] == "2027-01-15T08:00:00Z"
    assert set(found) == {
        "version", "delivery_id", "kind", "sent_at", "organization", "application", "rule",
        "incident", "console_path", "details",
    }  # fmt: skip


@pytest.mark.parametrize(
    "changes",
    [
        {"delivery_id": "ntf_x"},
        {"kind": "anything"},
        {"organization_id": "north org"},
        {"application": PayloadApplication.__new__(PayloadApplication)},
        {"console_path": "https://hooks.example.com/x"},
        {"console_path": "/v1/admin/orgs/north/../../etc"},
        {"details": PayloadDetails.__new__(PayloadDetails)},
    ],
)
def test_a_field_outside_its_form_is_refused(changes: dict[str, Any]) -> None:
    with pytest.raises((PayloadInvalidError, AttributeError, TypeError)):
        _payload(**changes).body()


@pytest.mark.parametrize(
    "build",
    [
        lambda: PayloadApplication("north shop", "Shop"),
        lambda: PayloadApplication("north-shop", "Shop\u200b"),
        lambda: PayloadApplication("north-shop", "Shop\n"),
        lambda: PayloadRule("rul_x", "Errors", "error_rate", ">", 5.0, "%", 300, 300, "critical"),
        lambda: PayloadRule(
            "rul_00000000000000aa", 'Err"', "error_rate", ">", 5.0, "%", 300, 300, "critical"
        ),
        lambda: PayloadRule(
            "rul_00000000000000aa", "Errors", "error_rate", "!=", 5.0, "%", 300, 300, "critical"
        ),
        lambda: PayloadRule(
            "rul_00000000000000aa", "Errors", "error_rate", ">", 5.0, "$", 300, 300, "critical"
        ),
        lambda: PayloadDetails(fields=("password",)),
        lambda: PayloadDetails(channel="https://hooks.example.com"),
    ],
)
def test_each_part_checks_its_own_fields(build: Any) -> None:
    with pytest.raises(PayloadInvalidError):
        build()


def test_the_payload_bound_is_exact_and_nothing_is_cut(monkeypatch: pytest.MonkeyPatch) -> None:
    """At the bound it is kept whole; one byte over it is refused (A4-12). Real payloads are
    about 1 KiB (names are bounded), so the bound is moved to this payload's size."""
    size = len(_payload().body())
    assert size < MAX_PAYLOAD_BYTES
    monkeypatch.setattr(payload_module, "MAX_PAYLOAD_BYTES", size)
    assert len(_payload().body()) == size
    monkeypatch.setattr(payload_module, "MAX_PAYLOAD_BYTES", size - 1)
    with pytest.raises(PayloadTooLargeError):
        _payload().body()


# --- which rule changes weaken it ---------------------------------------------------------------


def _spec(**changes: Any) -> RuleSpec:
    return validate_rule({**RULE, **changes})


QUIET = {"utc_offset": "+00:00", "start": "22:00", "end": "06:00", "days": ["mon"]}


@pytest.mark.parametrize(
    ("before", "after", "fields"),
    [
        ({}, {"threshold": 6}, ["threshold"]),
        ({}, {"threshold": 4}, []),
        ({}, {"comparison": ">="}, []),
        ({"comparison": ">="}, {"comparison": ">"}, ["comparison"]),
        ({"comparison": "<", "threshold": 5}, {"comparison": "<", "threshold": 4}, ["threshold"]),
        ({"comparison": "<", "threshold": 5}, {"comparison": "<", "threshold": 6}, []),
        ({"comparison": "<="}, {"comparison": "<"}, ["comparison"]),
        ({}, {"comparison": "<"}, ["comparison"]),
        ({}, {"window": 15}, ["window"]),
        ({"window": 15}, {"window": 5}, []),
        ({}, {"for_minutes": 10}, ["for_minutes"]),
        ({}, {"for_minutes": 0}, []),
        ({}, {"severity": "warning"}, ["severity"]),
        ({"severity": "info"}, {"severity": "warning"}, []),
        ({}, {"channels": []}, ["channels"]),
        ({"channels": []}, {}, []),
        (
            {"channels": ["chn_00000000000000a1"]},
            {"channels": ["chn_00000000000000a2"]},
            ["channels"],
        ),
        ({}, {"cooldown_minutes": 60}, ["cooldown_minutes"]),
        ({}, {"cooldown_minutes": 10}, []),
        ({"repeat_every_minutes": 60}, {"repeat_every_minutes": None}, ["repeat_every_minutes"]),
        ({"repeat_every_minutes": 60}, {"repeat_every_minutes": 120}, ["repeat_every_minutes"]),
        ({"repeat_every_minutes": 120}, {"repeat_every_minutes": 60}, []),
        ({}, {"quiet_hours": QUIET}, ["quiet_hours"]),
        ({"quiet_hours": QUIET}, {}, []),
        (
            {"quiet_hours": QUIET},
            {"quiet_hours": {**QUIET, "days": ["mon", "tue"]}},
            ["quiet_hours"],
        ),
        ({"quiet_hours": QUIET}, {"quiet_hours": {**QUIET, "end": "07:00"}}, ["quiet_hours"]),
        ({"quiet_hours": QUIET}, {"quiet_hours": {**QUIET, "end": "05:00"}}, []),
        ({}, {"enabled": False}, ["enabled"]),
        ({}, {"name": "Errors renamed"}, []),
        ({}, {"signal": "hosted_share", "window": 60}, ["signal", "window"]),
        ({"enabled": False}, {"threshold": 50, "enabled": False}, []),
    ],
)
def test_each_field_has_a_direction(
    before: dict[str, Any], after: dict[str, Any], fields: list[str]
) -> None:
    assert weakened_fields(_spec(**before), _spec(**{**before, **after})) == fields


def test_deleting_an_enabled_rule_weakens_it_and_a_disabled_one_does_not() -> None:
    assert weakened_fields(_spec(), None) == ["enabled"]
    assert weakened_fields(_spec(enabled=False), None) == []


def test_quiet_hours_are_compared_minute_by_minute_over_a_week() -> None:
    narrow = QuietHours("+00:00", "22:00", "06:00", ("mon",))
    shifted = QuietHours("+01:00", "23:00", "07:00", ("mon",))  # the same UTC minutes
    spec = _spec()
    assert (
        weakened_fields(replace(spec, quiet_hours=narrow), replace(spec, quiet_hours=shifted)) == []
    )


# --- the vault's channel scope ------------------------------------------------------------------


def test_a_channel_secret_never_opens_as_a_provider_key_nor_the_reverse() -> None:
    vault = Vault(MASTER)
    channel = ChannelBinding("north", "chn_00000000000000a1", "webhook_url", "https://h.example")
    provider = Binding("north", "chn_00000000000000a1", "webhook_url", "https://h.example")
    sealed_channel = vault.seal("https://h.example/planted-path?token=planted", channel)
    sealed_provider = vault.seal("sk-planted-provider-key-0123456789", provider)
    with pytest.raises(VaultIntegrityError):
        vault.open(sealed_channel, provider)
    with pytest.raises(VaultIntegrityError):
        vault.open(sealed_provider, channel)
    # The control: each opens with its own binding.
    assert vault.open(sealed_channel, channel).reveal().endswith("token=planted")
    assert vault.open(sealed_provider, provider).reveal().startswith("sk-planted")


def test_the_associated_data_of_the_two_scopes_never_match() -> None:
    """Crafted so every field but the prefix lines up: the bytes still differ (the first
    field names the scope)."""
    fields = ("north", "chn_00000000000000a1", "webhook_url", "https://h.example", "kid")
    assert channel_binding(*fields) != binding(*fields)
    assert channel_binding(*fields).startswith(b"vault-channel-v1|")
    assert binding(*fields).startswith(b"vault-v1|")


@pytest.mark.parametrize(
    "other",
    [
        ChannelBinding("south", "chn_00000000000000a1", "webhook_url", "https://h.example"),
        ChannelBinding("north", "chn_00000000000000a2", "webhook_url", "https://h.example"),
        ChannelBinding("north", "chn_00000000000000a1", "webhook_signing_key", "https://h.example"),
        ChannelBinding("north", "chn_00000000000000a1", "webhook_url", "https://other.example"),
    ],
)
def test_a_channel_secret_opens_only_where_it_was_sealed(other: ChannelBinding) -> None:
    vault = Vault(MASTER)
    where = ChannelBinding("north", "chn_00000000000000a1", "webhook_url", "https://h.example")
    sealed = vault.seal("https://h.example/planted", where)
    with pytest.raises(VaultIntegrityError):
        vault.open(sealed, other)


def test_a_channel_secret_keeps_no_last_four_characters() -> None:
    vault = Vault(MASTER)
    where = ChannelBinding("north", "chn_00000000000000a1", "webhook_url", "https://h.example")
    sealed = vault.seal("https://h.example/services/planted-token-tail", where)
    assert sealed.last4 is None
    assert "tail" not in repr(sealed)


def test_a_channel_binding_names_a_kind_of_channel_secret() -> None:
    with pytest.raises(ValueError, match="kind"):
        ChannelBinding("north", "chn_00000000000000a1", "provider_key", "https://h.example")


def test_rebinding_keeps_the_secret_under_its_new_binding() -> None:
    vault = Vault(MASTER)
    was = ChannelBinding(
        "north", "chn_00000000000000a1", "webhook_signing_key", "https://a.example"
    )
    now = ChannelBinding(
        "north", "chn_00000000000000a1", "webhook_signing_key", "https://b.example"
    )
    sealed = vault.seal("ab" * 32, was)
    moved = vault.rebind(sealed, was, now)
    assert vault.open(moved, now).reveal() == "ab" * 32
    with pytest.raises(VaultIntegrityError):
        vault.open(moved, was)
