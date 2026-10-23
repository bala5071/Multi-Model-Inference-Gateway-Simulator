"""The egress guard for organization providers: every blocked range, by literal address, by
name and embedded in IPv6; DNS rebinding; redirects; https only; no proxy from the environment;
the base-URL check at entry. See docs/32-provider-settings-and-vault.md. No network."""

from __future__ import annotations

import ipaddress

import httpx
import pytest

from gateway.chat import ProviderErrorCode
from gateway.egress import EgressRefused, check_base_url, organization_client, refusal
from mock_network import FakeResolver, ScriptedNetwork, http_response

PUBLIC = "93.184.216.34"

BLOCKED = [
    ("127.0.0.1", "loopback"),
    ("127.8.9.10", "loopback"),
    ("::1", "loopback"),
    ("10.1.2.3", "private"),
    ("172.16.0.1", "private"),
    ("172.31.255.254", "private"),
    ("192.168.1.1", "private"),
    ("fd12:3456::1", "private"),
    ("169.254.10.10", "link_local"),
    ("fe80::1", "link_local"),
    ("169.254.169.254", "metadata"),
    ("fd00:ec2::254", "metadata"),
    ("100.100.100.200", "metadata"),
    ("100.64.0.1", "not_global"),
    ("0.0.0.0", "private"),  # ipaddress counts 0.0.0.0/8 and 240.0.0.0/4 as private
    ("224.0.0.1", "multicast"),
    ("240.0.0.1", "private"),
    ("192.0.2.10", "private"),
    ("198.51.100.1", "private"),
    ("::ffff:127.0.0.1", "embedded_loopback"),
    ("::ffff:169.254.169.254", "embedded_metadata"),
    ("64:ff9b::a00:1", "embedded_private"),
    ("2002:c0a8:0101::1", "embedded_private"),
]


@pytest.mark.parametrize(("address", "reason"), BLOCKED)
def test_every_blocked_range_is_refused(address: str, reason: str) -> None:
    assert refusal(ipaddress.ip_address(address)) == reason


@pytest.mark.parametrize("address", [PUBLIC, "8.8.8.8", "2606:4700::6810:84e5", "::ffff:8.8.8.8"])
def test_public_addresses_are_allowed(address: str) -> None:
    assert refusal(ipaddress.ip_address(address)) is None


async def _get(client: httpx.AsyncClient, url: str) -> httpx.Response:
    return await client.get(url)


@pytest.mark.parametrize(("address", "reason"), BLOCKED)
async def test_a_name_that_resolves_to_a_blocked_address_never_connects(
    address: str, reason: str
) -> None:
    network = ScriptedNetwork([http_response(b"{}")])
    resolver = FakeResolver({"api.example.test": [address]})
    async with organization_client(resolver, inner=network) as client:
        with pytest.raises(EgressRefused) as refused:
            await _get(client, "https://api.example.test/v1/models")
    assert refused.value.code is ProviderErrorCode.EGRESS_REFUSED
    assert refused.value.detail == reason
    assert network.connections == []


async def test_one_bad_address_among_good_ones_refuses() -> None:
    network = ScriptedNetwork([http_response(b"{}")])
    resolver = FakeResolver({"api.example.test": [PUBLIC, "10.0.0.5"]})
    async with organization_client(resolver, inner=network) as client:
        with pytest.raises(EgressRefused):
            await _get(client, "https://api.example.test/")
    assert network.connections == []


async def test_a_literal_address_in_the_url_is_checked_without_resolving() -> None:
    network = ScriptedNetwork([http_response(b"{}")])
    resolver = FakeResolver({})
    async with organization_client(resolver, inner=network) as client:
        with pytest.raises(EgressRefused):
            await _get(client, "https://169.254.169.254/latest/meta-data/")
    assert resolver.calls == []
    assert network.connections == []


async def test_dns_rebinding_cannot_change_the_address_after_the_check() -> None:
    """The resolver answers a public address first and a private one afterwards: the guard
    connects to the address it checked, and TLS is for the name in the URL."""
    network = ScriptedNetwork([http_response(b"{}")])
    resolver = FakeResolver(
        {"rebind.example.test": [PUBLIC]}, then={"rebind.example.test": ["127.0.0.1"]}
    )
    async with organization_client(resolver, inner=network) as client:
        response = await _get(client, "https://rebind.example.test/v1/x")
    assert response.status_code == 200
    [connection] = network.connections
    assert connection.address == PUBLIC
    assert connection.tls_host == "rebind.example.test"
    assert resolver.calls == ["rebind.example.test"]  # one resolution, the one checked


async def test_a_redirect_is_not_followed() -> None:
    network = ScriptedNetwork(
        [
            http_response(b"", "302 Found", Location="https://169.254.169.254/latest/"),
            http_response(b"{}"),
        ]
    )
    resolver = FakeResolver({"api.example.test": [PUBLIC]})
    async with organization_client(resolver, inner=network) as client:
        response = await _get(client, "https://api.example.test/")
    assert response.status_code == 302
    assert [c.address for c in network.connections] == [PUBLIC]


async def test_only_https() -> None:
    network = ScriptedNetwork([http_response(b"{}")])
    resolver = FakeResolver({"api.example.test": [PUBLIC]})
    async with organization_client(resolver, inner=network) as client:
        with pytest.raises(EgressRefused) as refused:
            await _get(client, "http://api.example.test/")
    assert refused.value.detail == "not_https"
    assert network.connections == []


async def test_a_proxy_in_the_environment_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://10.9.9.9:3128")
    monkeypatch.setenv("ALL_PROXY", "http://10.9.9.9:3128")
    network = ScriptedNetwork([http_response(b"{}")])
    resolver = FakeResolver({"api.example.test": [PUBLIC]})
    async with organization_client(resolver, inner=network) as client:
        await _get(client, "https://api.example.test/")
    assert [c.address for c in network.connections] == [PUBLIC]


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.com/v1",
        "https://localhost:11434",
        "https://ollama.local/v1",
        "https://127.0.0.1/v1",
        "https://[::1]/v1",
        "https://169.254.169.254/latest",
        "https://10.0.0.8/v1",
        "https://user:pass@api.example.com/v1",
        "https://api.example.com/v1?x=1",
        "https://api.example.com/v1#frag",
        "https:///v1",
        "ftp://api.example.com",
    ],
)
def test_a_base_url_an_organization_cannot_enter(url: str) -> None:
    with pytest.raises(ValueError, match="base_url"):
        check_base_url(url)


def test_a_public_https_base_url_is_kept_without_its_trailing_slash() -> None:
    assert check_base_url("https://api.example.com/v1/") == "https://api.example.com/v1"
