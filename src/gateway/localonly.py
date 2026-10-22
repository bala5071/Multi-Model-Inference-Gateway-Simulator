"""Keep the unauthenticated endpoints local until A8: an ASGI middleware.

See docs/25-local-only.md and docs/identity-plan.md, section 10.

Monitoring, Prometheus metrics, the overload view, load control, the Playground and the
dashboard have no user authentication yet. Until the A8 role-based monitoring step they are
served only to this machine, and the checks are made inside the app, so they hold however it
is served (``python -m gateway.serve``, or plain ``uvicorn``):

- **Peer address only.** The socket peer must be loopback (``127.0.0.0/8``, ``::1``, or an
  IPv4-mapped ``::ffff:127.x``), or in a network listed in ``GATEWAY_LOCAL_NETWORKS``.
- **No forwarded headers.** A request carrying ``Forwarded``, ``X-Forwarded-For`` or
  ``X-Real-IP`` is refused: a proxy may have rewritten its client address (uvicorn itself does
  so by default for a peer of ``127.0.0.1``).
- **Host allowlist.** ``localhost``, ``127.0.0.1``, ``[::1]`` (any port) and the names in
  ``GATEWAY_ALLOWED_HOSTS``; anything else, such as a DNS-rebound name, is refused.
- **Origin.** A state-changing request that carries ``Origin`` must come from an allowed host.
- **No simple-request bodies.** A state-changing request may not be ``text/plain``,
  ``application/x-www-form-urlencoded`` or ``multipart/form-data``: the content types a
  cross-site page can send without a CORS preflight (415).

Every other endpoint (generate, models, health, an application's own usage) is untouched.
"""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Awaitable, Callable, Iterable, Mapping, MutableMapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

# Paths served only locally: each one and everything under it.
LOCAL_ONLY_PATHS = (
    "/v1/monitor",
    "/metrics",
    "/v1/overload",
    "/v1/control",
    "/v1/playground",
    "/dashboard",
    # Console login (docs/27): a session is only ever made on this machine until A8.
    "/v1/auth",
)
FORWARDING_HEADERS = (b"forwarded", b"x-forwarded-for", b"x-real-ip")
DEFAULT_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]"})
STATE_CHANGING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# The content types a page on another site can POST without a CORS preflight.
SIMPLE_REQUEST_TYPES = frozenset(
    {"text/plain", "application/x-www-form-urlencoded", "multipart/form-data"}
)
ALLOWED_HOSTS_ENV = "GATEWAY_ALLOWED_HOSTS"
LOCAL_NETWORKS_ENV = "GATEWAY_LOCAL_NETWORKS"

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


@dataclass(frozen=True, slots=True)
class LocalOnlyPolicy:
    """What counts as local: allowed Host names (lower-case, IPv6 in brackets) and extra
    networks counted as local peers (for Docker; they trust the published binding)."""

    allowed_hosts: frozenset[str] = DEFAULT_HOSTS
    local_networks: tuple[Network, ...] = ()


def policy_from(environ: Mapping[str, str]) -> LocalOnlyPolicy:
    """The defaults plus ``GATEWAY_ALLOWED_HOSTS`` and ``GATEWAY_LOCAL_NETWORKS`` (each a
    comma-separated list). The wiring reads the environment; this only parses it."""
    names = {n.strip().lower() for n in environ.get(ALLOWED_HOSTS_ENV, "").split(",")}
    try:
        networks = tuple(
            ipaddress.ip_network(n.strip(), strict=False)
            for n in environ.get(LOCAL_NETWORKS_ENV, "").split(",")
            if n.strip()
        )
    except ValueError as exc:
        raise ValueError(
            f"{LOCAL_NETWORKS_ENV} must be a comma-separated list of networks: {exc}"
        ) from None
    return LocalOnlyPolicy(DEFAULT_HOSTS | {n for n in names if n}, networks)


def is_local_only(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in LOCAL_ONLY_PATHS)


def is_local_peer(host: str | None, networks: Iterable[Network] = ()) -> bool:
    """True for a loopback address (IPv4-mapped IPv6 included) or one in ``networks``."""
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback or any(address in network for network in networks)


def host_name(header: str) -> str:
    """The name in a Host header, without the port: ``[::1]:8000`` -> ``[::1]``."""
    header = header.strip().lower()
    if header.startswith("["):
        end = header.find("]")
        return header[: end + 1] if end != -1 else header
    return header.rsplit(":", 1)[0] if header.count(":") == 1 else header


def _headers(scope: Scope) -> dict[bytes, bytes]:
    return {name.lower(): value for name, value in scope.get("headers", [])}


def refusal(scope: Scope, policy: LocalOnlyPolicy) -> tuple[int, str, str] | None:
    """Why this request must be refused, as (status, code, message), or None."""
    headers = _headers(scope)
    if any(name in headers for name in FORWARDING_HEADERS):
        return (
            403,
            "local_only",
            "This endpoint is served only to this machine, and a request through a proxy "
            "(Forwarded, X-Forwarded-For or X-Real-IP) cannot show that it is local.",
        )
    client = scope.get("client")
    if not is_local_peer(client[0] if client else None, policy.local_networks):
        return (403, "local_only", "This endpoint is served only to this machine.")
    if host_name(headers.get(b"host", b"").decode("latin-1")) not in policy.allowed_hosts:
        return (
            403,
            "host_not_allowed",
            "This endpoint answers only to localhost, 127.0.0.1, [::1] and the names in "
            f"{ALLOWED_HOSTS_ENV}.",
        )
    if scope.get("method", "GET").upper() in STATE_CHANGING:
        origin = headers.get(b"origin")
        if origin is not None:
            parsed = urlsplit(origin.decode("latin-1"))
            if not parsed.netloc or host_name(parsed.netloc) not in policy.allowed_hosts:
                return (403, "origin_not_allowed", "Requests from other sites are refused.")
        content_type = headers.get(b"content-type", b"").decode("latin-1")
        if content_type.split(";", 1)[0].strip().lower() in SIMPLE_REQUEST_TYPES:
            return (
                415,
                "unsupported_media_type",
                "Send application/json: plain-text and form bodies are refused here.",
            )
    return None


class LocalOnlyMiddleware:
    """Refuses requests to the local-only endpoints that fail ``refusal``; passes the rest."""

    def __init__(self, app: ASGIApp, policy: LocalOnlyPolicy) -> None:
        self.app = app
        self.policy = policy

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket") and is_local_only(scope.get("path", "")):
            refused = refusal(scope, self.policy)
            if refused is not None:
                status, code, message = refused
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                body = json.dumps({"error": {"code": code, "message": message}}).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": status,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)
