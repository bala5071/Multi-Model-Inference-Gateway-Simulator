"""One webhook attempt below the outbox (docs/46, the A4 plan's section 4.2): every connection
resolved and checked again, to the checked address, on an allowed port, with the verifying TLS
context, no redirect, no proxy; each stage bounded on the injected clock; at most 4 KiB of the
answer read; every failure one of the fixed codes and nothing of its message; the request's
headers fixed and its body as stored; the signature over the timestamp and the body, verified
by a verifier written here from the note, not by the gateway's code.

Every test injects the resolver and the network (``FakeResolver``, ``ScriptedNetwork``): no
production guard is relaxed and no socket is opened. Test values, not secrets."""

from __future__ import annotations

import ast
import hashlib
import hmac
import json
import ssl
from pathlib import Path

import httpcore
import pytest

from gateway.alerts.payload import AlertPayload, PayloadApplication, PayloadRule
from gateway.alerts.webhook import (
    CODES,
    MAX_ANSWER_BYTES,
    Attempt,
    build_request,
    send,
)
from gateway.clock import SimClock
from gateway.egress import (
    WEBHOOK_ATTEMPT_MS,
    WEBHOOK_STAGE_MS,
    EgressRefused,
    GuardedBackend,
    always_hosted,
    egress_scope,
    provider_tls_context,
    webhook_client,
)
from gateway.monitoring import ALERT_DELIVERY_RESULTS
from gateway.secret import Secret
from mock_network import (
    CONNECT_HANGS,
    HANG,
    TLS_HANGS,
    WRITE_HANGS,
    FakeResolver,
    Pauses,
    ScriptedNetwork,
    TlsFails,
    Trickles,
    http_response,
)

REPO = Path(__file__).resolve().parent.parent
HOST = "hooks.example.com"
TOKEN = "planted-webhook-token-91d3"
URL = f"https://{HOST}/services/{TOKEN}?token=planted-query-4c1"
PUBLIC = "93.184.216.34"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"
KEY = "ab" * 32
PREVIOUS = "cd" * 32
DELIVERY = "ntf_00000000000000d1"
BODY = AlertPayload(DELIVERY, "test", 1.8e12, "north").body()
TIMESTAMP = 1_800_000_000
OK = http_response(b"{}", Content_Length="2")
# One pinned context for the tests, as the server makes one (building it loads the bundle).
TLS = provider_tls_context()


def _resolver(*addresses: str, then: list[str] | None = None) -> FakeResolver:
    return FakeResolver(
        {HOST: list(addresses or [PUBLIC])}, then={HOST: then} if then is not None else {}
    )


async def _attempt(
    network: ScriptedNetwork,
    resolver: FakeResolver | None = None,
    *,
    url: str = URL,
    clock: SimClock | None = None,
    ports: frozenset[int] = frozenset({443}),
    keys: tuple[str, ...] = (KEY,),
    body: str = BODY,
    tls: ssl.SSLContext | None = TLS,
) -> Attempt:
    clock = clock if clock is not None else SimClock()
    client = webhook_client(
        resolver if resolver is not None else _resolver(),
        clock,
        ports,
        inner=network,
        ssl_context=tls,
    )
    request = build_request(Secret(url), [Secret(k) for k in keys], body, DELIVERY, TIMESTAMP)
    with egress_scope(always_hosted):
        return await send(client, request, clock)


def verify(key_hex: str, headers: dict[str, str], body: bytes) -> bool:
    """The receiver's check, from note 46's words alone: any ``v1=`` value equal to the HMAC of
    the timestamp, a dot and the body."""
    expected = hmac.new(
        bytes.fromhex(key_hex),
        headers["x-gateway-timestamp"].encode() + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    found = [
        v.split("=", 1)[1] for v in headers["x-gateway-signature"].split(",") if v.startswith("v1=")
    ]
    return any(hmac.compare_digest(expected, v) for v in found)


def parsed(written: bytes) -> tuple[str, dict[str, str], bytes]:
    head, _, body = written.partition(b"\r\n\r\n")
    lines = head.decode("ascii").split("\r\n")
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return lines[0], headers, body


# --- the address: checked on every connection, the socket to the checked address --------------


async def test_a_delivery_connects_to_the_checked_address_with_the_name_for_tls() -> None:
    """M18: the socket goes to the address the guard checked, never to the name again."""
    network = ScriptedNetwork([OK])
    done = await _attempt(network)
    assert done == Attempt("delivered", False)
    [connection] = network.connections
    assert (connection.address, connection.port, connection.tls_host) == (PUBLIC, 443, HOST)


async def test_rebinding_public_when_stored_private_when_delivered_opens_no_socket() -> None:
    """M17: the guard resolves at delivery; a name now private is refused before any socket."""
    resolver = _resolver(PUBLIC, then=["10.0.0.7"])
    network = ScriptedNetwork([OK, OK])
    assert (await _attempt(network, resolver)).code == "delivered"
    assert (await _attempt(network, resolver)) == Attempt("egress_refused", False)
    assert len(network.connections) == 1  # the second opened nothing
    assert resolver.calls == [HOST, HOST]  # resolved again for the second (M45's half here)


@pytest.mark.parametrize(
    "addresses",
    [
        [PUBLIC, "10.0.0.7"],
        ["10.0.0.7", PUBLIC],
        [PUBLIC, "169.254.169.254"],
        [PUBLIC_V6, "::1"],
    ],
)
async def test_one_address_that_is_not_public_refuses_the_connection(addresses: list[str]) -> None:
    """M44: every address the name resolves to is checked, not only the first."""
    network = ScriptedNetwork([OK])
    assert (await _attempt(network, _resolver(*addresses))) == Attempt("egress_refused", False)
    assert network.connections == []


@pytest.mark.parametrize(
    "address",
    [
        "::1",
        "::",
        "fe80::1",
        "fe80::1%eth0",
        "fc00::1",
        "fd12:3456::1",
        "ff02::1",
        "::ffff:127.0.0.1",
        "::ffff:169.254.169.254",
        "64:ff9b::a9fe:a9fe",
        "2002:7f00:1::",
        "2001:0:4136:e378:8000:63bf:80ff:fffe",
        "fd00:ec2::254",
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "100.64.0.1",
        "169.254.169.254",
        "0.0.0.0",
        "224.0.0.1",
        "192.0.2.1",
    ],
)
async def test_each_address_case_is_refused_at_connect(address: str) -> None:
    """The table of section 4.2 through the webhook path's guard: resolved to each, no socket."""
    network = ScriptedNetwork([OK])
    assert (await _attempt(network, _resolver(address))).code == "egress_refused"
    assert network.connections == []


async def test_an_aaaa_record_private_while_the_a_record_is_public_is_refused() -> None:
    network = ScriptedNetwork([OK])
    assert (await _attempt(network, _resolver(PUBLIC, "fd00::5"))).code == "egress_refused"
    assert network.connections == []


async def test_a_literal_address_in_the_url_is_checked_as_a_resolved_one() -> None:
    network = ScriptedNetwork([OK, OK])
    found = await _attempt(network, url="https://[::ffff:10.0.0.1]/hook")
    assert found.code == "egress_refused"
    assert (await _attempt(network, url=f"https://{PUBLIC}/hook")).code == "delivered"
    assert [c.address for c in network.connections] == [PUBLIC]


async def test_a_name_that_does_not_resolve_is_dns_failure_and_retried() -> None:
    resolver = FakeResolver({}, missing={HOST})
    network = ScriptedNetwork([OK])
    assert (await _attempt(network, resolver)) == Attempt("dns_failure", True)
    assert network.connections == []


# --- ports, redirects, proxies, TLS --------------------------------------------------------------


@pytest.mark.parametrize("port", [80, 22, 25, 6379, 8443])
async def test_a_port_not_allowed_is_refused_at_connect(port: int) -> None:
    """A4-9 at connect: the parsed URL's effective port, before resolving."""
    resolver = _resolver()
    network = ScriptedNetwork([OK])
    found = await _attempt(network, resolver, url=f"https://{HOST}:{port}/hook")
    assert found == Attempt("port_refused", False)
    assert network.connections == []
    assert resolver.calls == []


async def test_a_port_the_operator_allowed_is_accepted() -> None:
    network = ScriptedNetwork([OK])
    found = await _attempt(network, url=f"https://{HOST}:8443/hook", ports=frozenset({443, 8443}))
    assert found.code == "delivered"
    assert network.connections[0].port == 8443


@pytest.mark.parametrize("port", [0, 65536, 22])
async def test_the_guard_refuses_a_port_not_allowed_whatever_asks(port: int) -> None:
    backend = GuardedBackend(_resolver(), ScriptedNetwork([OK]), ports=frozenset({443}))
    with egress_scope(always_hosted), pytest.raises(EgressRefused) as refused:
        await backend.connect_tcp(HOST, port)
    assert refused.value.detail == "port"


@pytest.mark.parametrize(
    "status",
    ["301 Moved Permanently", "302 Found", "307 Temporary Redirect", "308 Permanent Redirect"],
)
@pytest.mark.parametrize("location", ["https://10.0.0.1/x", f"https://{HOST}/elsewhere"])
async def test_a_redirect_is_permanent_and_never_followed(status: str, location: str) -> None:
    """M19: a 3xx is ``http_3xx``; its Location is not fetched (one connection only)."""
    network = ScriptedNetwork([http_response(b"", status, Location=location), OK])
    assert (await _attempt(network)) == Attempt("http_3xx", False)
    assert len(network.connections) == 1


@pytest.mark.parametrize("variable", ["HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy"])
async def test_the_environment_neither_proxies_nor_adds_credentials(
    variable: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """M46: ``trust_env=False``. The request goes through the guard, not to a proxy, and a
    ``.netrc`` entry for the receiver's host (what httpx reads from the environment when it
    trusts it, as it ignores proxies once given a transport) adds no Authorization header."""
    monkeypatch.setenv(variable, "http://proxy.invalid:3128")
    netrc = tmp_path / "netrc"
    netrc.write_text(f"machine {HOST}\nlogin planted-netrc-user\npassword planted-pw\n",
                     encoding="utf-8")  # fmt: skip
    monkeypatch.setenv("NETRC", str(netrc))
    network = ScriptedNetwork([OK])
    assert (await _attempt(network)).code == "delivered"
    assert network.connections[0].address == PUBLIC
    _, headers, _ = parsed(bytes(network.connections[0].written))
    assert "authorization" not in headers


async def test_tls_is_the_verifying_context_whatever_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A4-10: certificates required, names checked, TLS 1.2 at least; SSL_CERT_FILE ignored."""
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "nothing.pem"))
    network = ScriptedNetwork([OK])
    assert (await _attempt(network, tls=None)).code == "delivered"  # the client's own
    context = network.connections[0].tls_context
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2


async def test_a_failed_handshake_is_permanent_tls_and_keeps_nothing_of_its_message() -> None:
    network = ScriptedNetwork([TlsFails(f"certificate verify failed for {URL}")])
    found = await _attempt(network)
    assert found == Attempt("tls", False)
    assert TOKEN not in repr(found)


def test_the_pinned_context_sets_tls_1_2_whatever_the_platform_allows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The floor is the gateway's, not the platform's: on a platform whose OpenSSL allows any
    version (as a CI runner's did), the pinned context still requires TLS 1.2."""
    import httpx

    def permissive(**_: object) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
        return context

    monkeypatch.setattr(httpx, "create_ssl_context", permissive)
    assert provider_tls_context().minimum_version == ssl.TLSVersion.TLSv1_2


def test_the_webhook_client_takes_only_the_pinned_context() -> None:
    """No alert module, nor ``webhook_client``, makes a TLS context of its own or turns
    verification off; the only client construction in the alert modules is in egress."""
    banned_calls = {
        "create_default_context",
        "_create_unverified_context",
        "create_ssl_context",
        "SSLContext",
        "AsyncClient",
        "Client",
        "AsyncHTTPTransport",
    }
    sources = sorted((REPO / "src" / "gateway" / "alerts").glob("*.py"))
    found = []
    for path in sources:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", getattr(node.func, "id", None))
                if name in banned_calls:
                    found.append((path.name, name))
                found += [(path.name, "verify") for k in node.keywords if k.arg == "verify"]
    assert found == []
    egress = ast.parse((REPO / "src" / "gateway" / "egress.py").read_text(encoding="utf-8"))
    [function] = [
        n for n in ast.walk(egress) if isinstance(n, ast.FunctionDef) and n.name == "webhook_client"
    ]
    calls = {
        getattr(n.func, "attr", getattr(n.func, "id", None))
        for n in ast.walk(function)
        if isinstance(n, ast.Call)
    }
    assert "provider_tls_context" in calls
    assert not calls & {"create_default_context", "_create_unverified_context", "SSLContext"}
    keywords = {
        (k.arg, ast.unparse(k.value))
        for n in ast.walk(function)
        if isinstance(n, ast.Call)
        for k in n.keywords
    }
    assert ("trust_env", "False") in keywords
    assert ("follow_redirects", "False") in keywords
    assert not any(k == "verify" for k, _ in keywords)


# --- each stage bounded on the injected clock ---------------------------------------------------


async def _hangs_forever(host: str, port: int) -> list[str]:
    import asyncio

    await asyncio.get_running_loop().create_future()
    return []


@pytest.mark.parametrize(
    ("scripted", "resolver_hangs"),
    [
        (OK, True),  # name resolution
        (CONNECT_HANGS, False),  # connect
        (TLS_HANGS, False),  # the handshake
        (WRITE_HANGS, False),  # the request's write
        (HANG, False),  # the first byte
        (Pauses(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nhalf", b"rest"), False),
    ],
    ids=["resolve", "connect", "handshake", "write", "first-byte", "mid-body"],
)
async def test_each_stage_ends_at_its_own_bound(scripted: object, resolver_hangs: bool) -> None:
    """A4-11: each stage at 5 s on the clock, never the 10 s of the whole attempt."""
    clock = SimClock()
    network = ScriptedNetwork([scripted])
    resolver = _resolver()
    if resolver_hangs:
        resolver = FakeResolver({})
        resolver.__class__ = type(
            "Hanging",
            (FakeResolver,),
            {"__call__": staticmethod(lambda host, port: _hangs_forever(host, port))},
        )
    found = await _attempt(network, resolver, clock=clock)
    assert found == Attempt("timeout", True)
    assert clock.now_ms() == WEBHOOK_STAGE_MS


async def test_the_whole_attempt_ends_at_ten_seconds() -> None:
    """Each read inside its bound, the attempt over 10 s: it ends at 10 s."""
    clock = SimClock()
    head = b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n"
    network = ScriptedNetwork([Trickles((head, *([b"x"] * 10)), clock, 4_000.0)])
    found = await _attempt(network, clock=clock)
    assert found == Attempt("timeout", True)
    assert clock.now_ms() == WEBHOOK_ATTEMPT_MS


# --- the answer: at most 4 KiB read, only its status kept ---------------------------------------


async def test_an_answer_of_a_mebibyte_is_read_only_to_four_kibibytes() -> None:
    """A4-12's answer half."""
    big = b"m" * (1024 * 1024)
    network = ScriptedNetwork([http_response(big, Content_Length=str(len(big)))])
    assert (await _attempt(network)).code == "delivered"
    assert network.connections[0].read < MAX_ANSWER_BYTES + 2 * 512


@pytest.mark.parametrize(
    ("status", "code", "retry"),
    [
        ("200 OK", "delivered", False),
        ("204 No Content", "delivered", False),
        ("400 Bad Request", "http_4xx", False),
        ("401 Unauthorized", "http_4xx", False),
        ("404 Not Found", "http_4xx", False),
        ("408 Request Timeout", "http_4xx", True),
        ("429 Too Many Requests", "http_4xx", True),
        ("500 Internal Server Error", "http_5xx", True),
        ("503 Service Unavailable", "http_5xx", True),
    ],
)
async def test_each_status_is_its_code(status: str, code: str, retry: bool) -> None:
    network = ScriptedNetwork([http_response(b"", status, Content_Length="0")])
    assert (await _attempt(network)) == Attempt(code, retry)


@pytest.mark.parametrize(("given", "honoured"), [("120", 120.0), ("7200", 3600.0), ("soon", None)])
async def test_retry_after_is_honoured_up_to_an_hour(given: str, honoured: float | None) -> None:
    network = ScriptedNetwork(
        [http_response(b"", "429 Too Many Requests", Retry_After=given, Content_Length="0")]
    )
    assert (await _attempt(network)).retry_after_s == honoured


@pytest.mark.parametrize(
    "scripted",
    [
        httpcore.ConnectError(f"could not reach {URL}"),
        httpcore.ReadError(f"reset while reading {URL}"),
        OSError(f"refused {URL}"),
        TlsFails(f"no certificate for {URL}"),
    ],
)
async def test_every_failure_is_a_fixed_code_never_its_message(scripted: object) -> None:
    """A4-13: the code by the kind of failure; the exception's words (here, the URL) kept
    nowhere."""
    found = await _attempt(ScriptedNetwork([scripted]))
    assert found.code in CODES
    assert TOKEN not in found.code
    assert TOKEN not in repr(found)


def test_the_metric_results_are_the_codes_an_attempt_can_have() -> None:
    assert set(ALERT_DELIVERY_RESULTS) == CODES - {
        "payload_too_large",
        "rule_cap",
        "organization_cap",
    }


# --- the request: fixed headers, the stored body, the signature ---------------------------------


async def test_the_request_is_the_stored_body_with_fixed_headers_and_a_valid_signature() -> None:
    network = ScriptedNetwork([OK])
    assert (await _attempt(network)).code == "delivered"
    line, headers, body = parsed(bytes(network.connections[0].written))
    assert line == f"POST /services/{TOKEN}?token=planted-query-4c1 HTTP/1.1"
    assert body == BODY.encode()
    assert headers["content-type"] == "application/json"
    assert headers["x-gateway-delivery"] == DELIVERY
    assert headers["x-gateway-timestamp"] == str(TIMESTAMP)
    assert verify(KEY, headers, body)
    # A changed timestamp or body fails (M32: the timestamp is signed).
    assert not verify(KEY, {**headers, "x-gateway-timestamp": str(TIMESTAMP + 1)}, body)
    assert not verify(KEY, headers, body + b" ")
    assert not verify(PREVIOUS, headers, body)
    gateway_set = {
        "host",
        "accept",
        "accept-encoding",
        "connection",
        "user-agent",
        "content-length",
        "content-type",
        "x-gateway-delivery",
        "x-gateway-timestamp",
        "x-gateway-signature",
    }
    assert set(headers) <= gateway_set


async def test_two_keys_sign_during_a_rotation() -> None:
    network = ScriptedNetwork([OK])
    assert (await _attempt(network, keys=(KEY, PREVIOUS))).code == "delivered"
    _, headers, body = parsed(bytes(network.connections[0].written))
    assert headers["x-gateway-signature"].count("v1=") == 2
    assert verify(KEY, headers, body)
    assert verify(PREVIOUS, headers, body)


async def test_names_with_quotes_backslashes_braces_and_unicode_stay_in_the_json_body() -> None:
    """A4-15's body half: user text reaches the request only inside ``json.dumps``; the
    headers are the gateway's own."""
    name = 'Sh"op\\ } \u00e9t\u00e9 \u2603 "x": 1'
    payload = AlertPayload(
        DELIVERY,
        "firing",
        1.8e12,
        "north",
        PayloadApplication("north-shop", name),
        PayloadRule(
            "rul_00000000000000a1", "Errors 5", "error_rate", ">", 5.0, "%", 300, 0, "critical"
        ),
    )
    body = payload.body()
    network = ScriptedNetwork([OK])
    assert (await _attempt(network, body=body)).code == "delivered"
    _, headers, sent = parsed(bytes(network.connections[0].written))
    assert json.loads(sent)["application"]["name"] == name
    assert sent.isascii()
    assert headers["x-gateway-delivery"] == DELIVERY
    assert verify(KEY, headers, sent)


def test_build_request_is_the_one_reveal_of_the_alert_modules() -> None:
    """M36: one ``reveal()`` call in the alert modules, in ``build_request``."""
    found = []
    for path in sorted((REPO / "src" / "gateway" / "alerts").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            for node in ast.walk(function):
                if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "reveal":
                    found.append((path.name, function.name))
    assert found == [("webhook.py", "build_request")]
