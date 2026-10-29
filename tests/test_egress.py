"""The egress guard for organization providers: every blocked range, by literal address, by
name and embedded in IPv6; DNS rebinding; redirects; https only; no proxy from the environment;
the base-URL check at entry. See docs/32-provider-settings-and-vault.md. And the locality rule
(docs/33): a hosted destination only under a scope that allows it now, the operator's local
destinations as configured. No network.

The tests of the hosted rules run under a scope that allows hosted (``_get``), as a chat
request in a hosted mode or a test-connection would."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path

import httpx
import pytest

from gateway.audit import FIXED_VALUES
from gateway.chat import ProviderErrorCode
from gateway.egress import (
    EgressRefused,
    GuardedBackend,
    LocalDestinations,
    check_base_url,
    egress_scope,
    hosted_allowed,
    operator_client,
    organization_client,
    refusal,
)
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


def answer(value: Callable[[], bool]) -> Callable[[], Awaitable[bool]]:
    """A scope's answer, asked afresh each time, as the router's reads the mode store."""

    async def ask() -> bool:
        return value()

    return ask


async def _get(client: httpx.AsyncClient, url: str) -> httpx.Response:
    """A request in a scope that may reach hosted destinations."""
    with egress_scope(answer(lambda: True)):
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


def test_every_reason_the_guard_gives_can_be_audited() -> None:
    """Test-connection records why it did not reach a host; the audit takes only fixed words,
    so each reason in gateway.egress (and the transport's) must be one of them."""
    source = (Path(__file__).resolve().parent.parent / "src" / "gateway" / "egress.py").read_text(
        encoding="utf-8"
    )
    given = set(re.findall(r'EgressRefused\("([a-z_]+)"\)', source))
    given |= set(re.findall(r'detail="([a-z_]+)"', source))
    addressed = set(re.findall(r'return "([a-z_]+)"', source))
    assert addressed == {
        "metadata", "loopback", "link_local", "private", "multicast", "reserved", "not_global",
    }  # fmt: skip
    allowed = FIXED_VALUES["outcome_detail"]
    assert given | addressed | {f"embedded_{a}" for a in addressed} <= allowed
    assert {"connect", "connection_lost", "probe"} <= allowed


# --- locality (docs/33) -------------------------------------------------------------------


async def test_with_no_scope_a_hosted_destination_is_refused_before_resolving() -> None:
    network = ScriptedNetwork([http_response(b"{}")])
    resolver = FakeResolver({"api.example.test": [PUBLIC]})
    assert not await hosted_allowed()
    async with organization_client(resolver, inner=network) as client:
        with pytest.raises(EgressRefused) as refused:
            await client.get("https://api.example.test/")
    assert refused.value.detail == "local_only"
    assert resolver.calls == []  # not even the name left the machine
    assert network.connections == []


async def test_a_scope_that_says_no_refuses_and_is_asked_at_every_request() -> None:
    """The scope's answer is asked again for each request, so a pooled connection opened while
    hosted was allowed carries nothing once the answer changes (a mode made stricter)."""
    network = ScriptedNetwork([http_response(b"{}"), http_response(b"{}")])
    resolver = FakeResolver({"api.example.test": [PUBLIC]})
    allowed = [True]
    async with organization_client(resolver, inner=network) as client:
        with egress_scope(answer(lambda: allowed[0])):
            first = await client.get("https://api.example.test/a")
            allowed[0] = False
            with pytest.raises(EgressRefused) as refused:
                await client.get("https://api.example.test/b")
    assert first.status_code == 200
    assert refused.value.detail == "local_only"
    assert len(network.connections) == 1
    assert b"GET /b" not in bytes(network.connections[0].written)


async def test_a_scope_whose_answer_cannot_be_read_refuses_the_connection() -> None:
    """The answer is read from the mode store (docs/34): a store that cannot answer raises, and
    nothing leaves the machine, not even the name."""
    network = ScriptedNetwork([http_response(b"{}")])
    resolver = FakeResolver({"api.example.test": [PUBLIC]})

    async def unreadable() -> bool:
        raise ConnectionError("the mode store is unavailable")

    async with organization_client(resolver, inner=network) as client:
        with egress_scope(unreadable), pytest.raises(ConnectionError):
            await client.get("https://api.example.test/")
    assert resolver.calls == []
    assert network.connections == []


async def test_the_connect_check_refuses_without_the_transport() -> None:
    """The backend refuses on its own, for any caller that reaches it another way."""
    network = ScriptedNetwork([http_response(b"{}")])
    backend = GuardedBackend(FakeResolver({"api.example.test": [PUBLIC]}), network)
    with egress_scope(answer(lambda: False)), pytest.raises(EgressRefused) as refused:
        await backend.connect_tcp("api.example.test", 443)
    assert refused.value.detail == "local_only"
    assert network.connections == []


async def test_an_organization_client_never_treats_anything_as_local() -> None:
    """Even a destination on the operator's list is hosted for an organization: under a scope
    that allows hosted, loopback is still refused by the address check."""
    network = ScriptedNetwork([http_response(b"{}")])
    resolver = FakeResolver({"localhost": ["127.0.0.1"]})
    async with organization_client(resolver, inner=network) as client:
        with pytest.raises(EgressRefused) as refused:
            await _get(client, "https://localhost:11434/")
    assert refused.value.detail == "loopback"
    assert network.connections == []


async def test_the_operator_client_reaches_its_local_destinations_without_a_scope() -> None:
    network = ScriptedNetwork([http_response(b"{}")])
    local = LocalDestinations.parse("127.0.0.1:11434")
    async with operator_client(FakeResolver({}), local, inner=network) as client:
        response = await client.get("http://127.0.0.1:11434/api/chat")  # plain http: its own
    assert response.status_code == 200
    assert [(c.address, c.port) for c in network.connections] == [("127.0.0.1", 11434)]


async def test_the_operator_client_holds_anything_else_to_the_hosted_rules() -> None:
    local = LocalDestinations.parse("127.0.0.1:11434")
    resolver = FakeResolver({"api.example.test": [PUBLIC], "gpu.lan": ["10.0.0.9"]})
    network = ScriptedNetwork([http_response(b"{}")])
    async with operator_client(resolver, local, inner=network) as client:
        cases = [
            ("http://127.0.0.1:8080/", None, "local_only"),  # another port: not on the list
            ("https://api.example.test/", None, "local_only"),  # hosted, no scope
            ("http://api.example.test/", True, "not_https"),
            ("https://gpu.lan/", True, "private"),  # not on the list, so not reachable
        ]
        for url, scope, reason in cases:
            with (
                egress_scope(answer(partial(bool, scope))),
                pytest.raises(EgressRefused) as refused,
            ):
                await client.get(url)
            assert refused.value.detail == reason, url
        with egress_scope(answer(lambda: True)):
            hosted = await client.get("https://api.example.test/")
    assert hosted.status_code == 200
    assert [c.address for c in network.connections] == [PUBLIC]


def test_local_destinations_parse_and_loopback_defaults() -> None:
    parsed = LocalDestinations.parse(" localhost:11434, [::1]:8080 ,gpu.lan:9000,")
    assert parsed.destinations == {("localhost", 11434), ("::1", 8080), ("gpu.lan", 9000)}
    assert parsed.contains("LOCALHOST.", 11434)
    assert not parsed.contains("localhost", 11435)
    for bad in ("localhost", "localhost:", ":80", "host:port"):
        with pytest.raises(ValueError, match="host:port"):
            LocalDestinations.parse(bad)
    found = LocalDestinations.loopback_of(
        [
            "http://127.0.0.1:11434",
            "http://localhost:8080/v1",
            "https://[::1]",
            "http://10.0.0.9:8000",
            "https://api.example.com",
        ]
    )
    assert found.destinations == {("127.0.0.1", 11434), ("localhost", 8080), ("::1", 443)}
    assert found.contains_url("http://localhost:8080/v1/chat")
    assert not found.contains_url("https://api.example.com/v1")
