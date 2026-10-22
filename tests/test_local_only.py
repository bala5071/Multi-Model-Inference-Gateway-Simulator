"""The local-only guard and the launcher. See docs/25-local-only.md.

Monitoring, metrics, the overload view, load control, the Playground and the dashboard are
served only to this machine until A8. These tests cover the guard on its own, through the app,
through uvicorn's proxy-headers middleware (what plain ``uvicorn`` adds), through a real
``uvicorn`` process started directly, and the launcher's bind check.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import subprocess
import sys
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from gateway.app import create_simulated_live_app
from gateway.localonly import (
    DEFAULT_HOSTS,
    LOCAL_ONLY_PATHS,
    LocalOnlyPolicy,
    host_name,
    is_local_only,
    is_local_peer,
    policy_from,
)
from gateway.serve import ALLOW_NETWORK_BIND_ENV, bind_decision, is_loopback_bind
from gateway.serve import main as serve

REPO = Path(__file__).resolve().parent.parent
DOCKER_BRIDGE = ipaddress.ip_network("172.16.0.0/12")


async def _never(_: float) -> None:
    return None


# --- the pieces ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("peer", "local"),
    [
        ("127.0.0.1", True),
        ("127.5.6.7", True),
        ("::1", True),
        ("::ffff:127.0.0.1", True),  # IPv4-mapped IPv6 loopback
        ("::ffff:10.0.0.1", False),
        ("10.0.0.5", False),
        ("192.168.1.20", False),
        ("172.18.0.1", False),  # the Docker bridge, unless opted in
        ("203.0.113.9", False),
        ("localhost", False),  # a name, not an address
        ("", False),
        (None, False),
    ],
)
def test_only_loopback_peers_are_local(peer: str | None, local: bool) -> None:
    assert is_local_peer(peer) is local


def test_local_networks_are_an_opt_in() -> None:
    assert is_local_peer("172.18.0.1", [DOCKER_BRIDGE])
    assert is_local_peer("::ffff:172.18.0.1", [DOCKER_BRIDGE])
    assert not is_local_peer("10.0.0.5", [DOCKER_BRIDGE])
    policy = policy_from({"GATEWAY_LOCAL_NETWORKS": "172.16.0.0/12, 192.168.65.0/24"})
    assert policy.local_networks == (DOCKER_BRIDGE, ipaddress.ip_network("192.168.65.0/24"))
    with pytest.raises(ValueError, match="GATEWAY_LOCAL_NETWORKS"):
        policy_from({"GATEWAY_LOCAL_NETWORKS": "not-a-network"})


@pytest.mark.parametrize(
    ("header", "name"),
    [
        ("localhost", "localhost"),
        ("localhost:8000", "localhost"),
        ("LOCALHOST:8000", "localhost"),
        ("127.0.0.1:5173", "127.0.0.1"),
        ("[::1]:8000", "[::1]"),
        ("[::1]", "[::1]"),
        ("attacker.example:8000", "attacker.example"),
    ],
)
def test_host_names_are_read_without_the_port(header: str, name: str) -> None:
    assert host_name(header) == name


def test_allowed_hosts_are_the_defaults_plus_the_configured_ones() -> None:
    assert policy_from({}).allowed_hosts == DEFAULT_HOSTS
    configured = policy_from({"GATEWAY_ALLOWED_HOSTS": "Gateway.Internal, monitor.local"})
    assert configured.allowed_hosts == DEFAULT_HOSTS | {"gateway.internal", "monitor.local"}


def test_which_paths_are_local_only() -> None:
    for path in LOCAL_ONLY_PATHS:
        assert is_local_only(path)
        assert is_local_only(path + "/anything")
    for path in ("/v1/generate", "/v1/models", "/healthz", "/", "/v1/applications/me/usage"):
        assert not is_local_only(path)
    assert not is_local_only("/metricsx")  # whole path segments only


# --- through the app -----------------------------------------------------------------------


@asynccontextmanager
async def _client(
    peer: str = "127.0.0.1", policy: LocalOnlyPolicy | None = None, wrap: bool = False
) -> AsyncIterator[httpx.AsyncClient]:
    app: Any = create_simulated_live_app(wait=_never, dashboard_dir=None, local_only=policy)
    if wrap:  # what plain uvicorn adds by default: proxy headers trusted from 127.0.0.1
        app = ProxyHeadersMiddleware(app, trusted_hosts="127.0.0.1")
    transport = httpx.ASGITransport(app=app, client=(peer, 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as c:
        yield c


async def _status(response: httpx.Response) -> tuple[int, str | None]:
    if response.status_code < 400:
        return response.status_code, None
    return response.status_code, response.json().get("error", {}).get("code")


@pytest.mark.parametrize("peer", ["127.0.0.1", "::1", "::ffff:127.0.0.1"])
async def test_the_local_only_endpoints_answer_loopback(peer: str) -> None:
    async with _client(peer) as client:
        for path in ("/v1/control", "/v1/monitor/snapshot", "/metrics", "/v1/overload"):
            assert (await client.get(path)).status_code == 200, path


@pytest.mark.parametrize("peer", ["10.0.0.5", "203.0.113.9", "::ffff:10.0.0.1", "172.18.0.1"])
async def test_a_peer_that_is_not_local_is_refused(peer: str) -> None:
    async with _client(peer) as client:
        for path in ("/v1/control", "/v1/monitor/snapshot", "/metrics", "/dashboard/"):
            assert await _status(await client.get(path)) == (403, "local_only"), path
        # The endpoints that do not need it are unaffected.
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.get("/v1/models")).status_code == 200


async def test_the_docker_bridge_is_local_only_when_opted_in() -> None:
    policy = policy_from({"GATEWAY_LOCAL_NETWORKS": "172.16.0.0/12"})
    async with _client("172.18.0.1", policy) as client:
        assert (await client.get("/v1/control")).status_code == 200


@pytest.mark.parametrize(
    "header",
    [
        {"X-Forwarded-For": "127.0.0.1"},
        {"Forwarded": "for=127.0.0.1"},
        {"X-Real-IP": "127.0.0.1"},
    ],
    ids=["x-forwarded-for", "forwarded", "x-real-ip"],
)
async def test_each_forwarding_header_is_refused(header: dict[str, str]) -> None:
    async with _client() as client:
        refused = await client.get("/v1/control", headers=header)
        allowed = await client.get("/healthz", headers=header)
    assert await _status(refused) == (403, "local_only")
    assert "proxy" in refused.json()["error"]["message"]
    assert allowed.status_code == 200


@pytest.mark.parametrize(
    "host", ["attacker.example", "attacker.example:8000", "127.0.0.1.nip.io", "localhost.evil"]
)
async def test_dns_rebinding_is_refused_by_the_host_allowlist(host: str) -> None:
    # The page's name was made to resolve to 127.0.0.1: the peer is loopback, the Host is not.
    async with _client() as client:
        response = await client.get("/v1/monitor/snapshot", headers={"Host": host})
    assert await _status(response) == (403, "host_not_allowed")


async def test_a_configured_host_name_is_allowed() -> None:
    policy = policy_from({"GATEWAY_ALLOWED_HOSTS": "gateway.internal"})
    async with _client(policy=policy) as client:
        ok = await client.get("/v1/control", headers={"Host": "gateway.internal:8000"})
    assert ok.status_code == 200


async def test_a_cross_site_request_is_refused() -> None:
    async with _client() as client:
        # What a page on another site can send without a CORS preflight.
        simple = await client.post(
            "/v1/control/load/stop",
            headers={"Origin": "https://attacker.example", "Content-Type": "text/plain"},
            content=b"{}",
        )
        no_body = await client.post(
            "/v1/control/load/stop", headers={"Origin": "https://attacker.example"}
        )
        null_origin = await client.post("/v1/control/load/stop", headers={"Origin": "null"})
    assert await _status(simple) == (403, "origin_not_allowed")
    assert await _status(no_body) == (403, "origin_not_allowed")
    assert await _status(null_origin) == (403, "origin_not_allowed")


@pytest.mark.parametrize(
    "content_type",
    [
        "text/plain",
        "text/plain; charset=utf-8",
        "application/x-www-form-urlencoded",
        "multipart/form-data; boundary=x",
    ],
)
async def test_simple_request_bodies_are_refused(content_type: str) -> None:
    async with _client() as client:
        response = await client.post(
            "/v1/control/speed", headers={"Content-Type": content_type}, content=b'{"speed": 5}'
        )
    assert await _status(response) == (415, "unsupported_media_type")


async def test_the_dashboards_own_requests_are_accepted() -> None:
    origin = {"Origin": "http://localhost:8000"}
    async with _client() as client:
        speed = await client.post("/v1/control/speed", json={"speed": 5}, headers=origin)
        stop = await client.post("/v1/control/load/stop", headers=origin)
        playground = await client.post(
            "/v1/playground/generate",
            json={"application_id": "acme", "model": "economy", "prompt": "hi", "max_tokens": 1},
            headers=origin,
        )
        # The Vite dev server proxies with the browser's Host and Origin.
        dev = await client.post(
            "/v1/control/speed",
            json={"speed": 1},
            headers={"Host": "localhost:5173", "Origin": "http://localhost:5173"},
        )
    assert [r.status_code for r in (speed, stop, playground, dev)] == [200, 200, 200, 200]


async def test_plain_uvicorns_proxy_headers_cannot_make_a_request_local() -> None:
    # uvicorn by default trusts X-Forwarded-For from 127.0.0.1 and rewrites the client
    # address. Whatever it rewrites it to, the header itself gets the request refused.
    async with _client(wrap=True) as client:
        spoofed = await client.get("/v1/control", headers={"X-Forwarded-For": "127.0.0.1"})
        outside = await client.get("/v1/control", headers={"X-Forwarded-For": "203.0.113.9"})
        plain = await client.get("/v1/control")
    assert await _status(spoofed) == (403, "local_only")
    assert await _status(outside) == (403, "local_only")
    assert plain.status_code == 200


# --- real servers -------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextmanager
def _server(command: list[str], port: int) -> Iterator[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GATEWAY_")}
    env.update(GATEWAY_SIMULATED_LIVE="1", GATEWAY_SIM_SPEED="20")
    process = subprocess.Popen(
        command, cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    base = f"http://127.0.0.1:{port}"
    pause = threading.Event()
    try:
        for _ in range(300):
            try:
                if httpx.get(f"{base}/healthz", timeout=1.0).status_code == 200:
                    break
            except httpx.TransportError:
                pause.wait(0.1)
        else:
            raise AssertionError(f"the server did not start: {command}")
        yield base
    finally:
        process.terminate()
        process.wait(timeout=30)


def _checks(base: str) -> list[int]:
    return [
        httpx.get(f"{base}/v1/control").status_code,
        httpx.get(f"{base}/v1/control", headers={"X-Forwarded-For": "127.0.0.1"}).status_code,
        httpx.get(f"{base}/v1/control", headers={"X-Forwarded-For": "203.0.113.9"}).status_code,
        httpx.get(f"{base}/v1/control", headers={"Forwarded": "for=127.0.0.1"}).status_code,
        httpx.get(f"{base}/v1/control", headers={"X-Real-IP": "127.0.0.1"}).status_code,
        httpx.get(f"{base}/v1/control", headers={"Host": "attacker.example"}).status_code,
    ]


def test_plain_uvicorn_started_directly_is_still_safe() -> None:
    port = _free_port()
    command = [sys.executable, "-m", "uvicorn", "gateway.app:create_default_app", "--factory"]
    with _server([*command, "--port", str(port), "--log-level", "warning"], port) as base:
        assert _checks(base) == [200, 403, 403, 403, 403, 403]


def test_the_launcher_serves_with_the_same_checks() -> None:
    port = _free_port()
    command = [sys.executable, "-m", "gateway.serve", "--port", str(port)]
    with _server([*command, "--log-level", "warning"], port) as base:
        assert _checks(base) == [200, 403, 403, 403, 403, 403]


# --- the launcher's bind check ------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "loopback"),
    [
        ("127.0.0.1", True),
        ("127.0.0.2", True),
        ("::1", True),
        ("[::1]", True),
        ("localhost", True),
        ("0.0.0.0", False),
        ("::", False),
        ("192.168.1.20", False),
        ("gateway.example", False),
    ],
)
def test_which_binds_are_loopback(host: str, loopback: bool) -> None:
    assert is_loopback_bind(host) is loopback


def test_the_launcher_refuses_a_network_bind_without_the_opt_in(
    capsys: pytest.CaptureFixture[str],
) -> None:
    started: list[dict[str, Any]] = []
    code = serve(["--host", "0.0.0.0"], {}, run=lambda *a, **k: started.append(k))
    err = capsys.readouterr().err
    assert (code, started) == (2, [])
    assert "Refusing to bind 0.0.0.0" in err
    assert "/v1/monitor" in err
    assert ALLOW_NETWORK_BIND_ENV in err


def test_with_the_opt_in_it_starts_and_warns_naming_the_open_endpoints(
    capsys: pytest.CaptureFixture[str],
) -> None:
    started: list[dict[str, Any]] = []
    code = serve(
        ["--host", "0.0.0.0", "--port", "9000"],
        {ALLOW_NETWORK_BIND_ENV: "1"},
        run=lambda *a, **k: started.append(k),
    )
    err = capsys.readouterr().err
    assert code == 0
    assert started[0]["host"] == "0.0.0.0"
    assert started[0]["proxy_headers"] is False
    assert err.startswith("WARNING: binding 0.0.0.0")
    for path in LOCAL_ONLY_PATHS:
        assert path in err


def test_a_loopback_bind_starts_silently_with_proxy_headers_off(
    capsys: pytest.CaptureFixture[str],
) -> None:
    started: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    assert serve([], {}, run=lambda *a, **k: started.append((a, k))) == 0
    (args, kwargs) = started[0]
    assert args == ("gateway.app:create_default_app",)
    assert (kwargs["host"], kwargs["port"], kwargs["factory"], kwargs["proxy_headers"]) == (
        "127.0.0.1",
        8000,
        True,
        False,
    )
    assert capsys.readouterr().err == ""
    assert bind_decision("127.0.0.1", {}) == (True, None)
