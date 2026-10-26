"""The egress guard: where the gateway may open a connection for an organization's provider.

See docs/32-provider-settings-and-vault.md and docs/provider-plan.md, section 4.

An organization supplies a provider's base URL, so it could point the gateway at this machine,
the network behind it, or a cloud metadata service (SSRF). Every connection to an organization
provider goes through one ``httpcore`` network backend, ``GuardedBackend``, whose
``connect_tcp`` is the single place a socket is opened:

1. **HTTPS only** (``OrganizationTransport`` refuses any other scheme before connecting), with no
   redirects and no proxy from the environment (the adapters' client).
2. The host is **resolved** through an injected resolver, and **every** address it returns is
   checked; one address that is not public refuses the connection.
3. Not public: loopback, private, link-local, carrier-grade NAT, unspecified, multicast,
   reserved and documentation ranges, cloud metadata addresses, and IPv6 forms that embed an
   IPv4 address (IPv4-mapped, NAT64, 6to4, Teredo), judged on the address they embed.
4. The socket connects to **the address that was checked**, never to the name again, so DNS
   rebinding between the check and the connect cannot change it. TLS still verifies the
   certificate for the name in the URL (httpcore starts TLS with the request's host).

There is no allow-list on this path: the operator's ``GATEWAY_PROVIDER_LOCAL_ALLOW`` is for the
operator's own servers (docs/provider-plan.md, section 4) and is never consulted here.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable
from typing import Any
from urllib.parse import urlsplit

import httpcore
import httpx

from gateway.chat import ProviderError, ProviderErrorCode

Resolver = Callable[[str, int], Awaitable[list[str]]]
Address = ipaddress.IPv4Address | ipaddress.IPv6Address

METADATA = frozenset(
    ipaddress.ip_address(a) for a in ("169.254.169.254", "fd00:ec2::254", "100.100.100.200")
)
NAT64 = ipaddress.ip_network("64:ff9b::/96")


class EgressRefused(ProviderError):
    """A connection the guard will not open. ``detail`` says why, in fixed words."""

    def __init__(self, reason: str) -> None:
        super().__init__(ProviderErrorCode.EGRESS_REFUSED, detail=reason)


def embedded_ipv4(address: Address) -> ipaddress.IPv4Address | None:
    """The IPv4 address an IPv6 address carries, if it is one of the forms that do."""
    if not isinstance(address, ipaddress.IPv6Address):
        return None
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address.sixtofour is not None:
        return address.sixtofour
    if address.teredo is not None:
        return address.teredo[1]
    if address in NAT64:
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return None


def refusal(address: Address) -> str | None:
    """Why an address is not one to connect to, or None if it is public."""
    inner = embedded_ipv4(address)
    if inner is not None:
        reason = refusal(inner)
        return f"embedded_{reason}" if reason else None
    if address in METADATA:
        return "metadata"
    if address.is_loopback:
        return "loopback"
    if address.is_link_local:
        return "link_local"
    if address.is_private:
        return "private"
    if address.is_multicast:
        return "multicast"
    if address.is_unspecified or address.is_reserved:
        return "reserved"
    if not address.is_global:
        return "not_global"
    return None


def check_base_url(url: str) -> str:
    """An organization's base URL, checked when it is entered: ``https``, a host, no user, query
    or fragment, and not a literal address or name that is plainly not public. The connect-time
    check is the one that matters; this gives a quick, clear refusal. Returns the URL without a
    trailing slash, or raises ``ValueError``."""
    parts = urlsplit(url.strip())
    if parts.scheme != "https":
        raise ValueError("a provider's base_url must use https")
    if not parts.hostname:
        raise ValueError("a provider's base_url needs a host")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("a provider's base_url has no user, query or fragment")
    host = parts.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("a provider's base_url must be a public host")
    try:
        literal: Address | None = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and refusal(literal):
        raise ValueError("a provider's base_url must be a public address")
    return url.strip().rstrip("/")


async def system_resolver(host: str, port: int) -> list[str]:
    """Every address the system resolver gives for ``host``."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


class GuardedBackend(httpcore.AsyncNetworkBackend):
    """Opens a TCP connection only to a checked, public address, and to that address."""

    def __init__(self, resolver: Resolver, inner: httpcore.AsyncNetworkBackend | None = None):
        self._resolver = resolver
        self._inner = inner if inner is not None else httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 (httpcore passes it by name)
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            literal: Address | None = ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            literal = None
        if literal is not None:
            names = [str(literal)]
        else:
            try:
                names = await self._resolver(host, port)
            except OSError:  # socket.gaierror among them: the name did not resolve
                raise ProviderError(
                    ProviderErrorCode.UPSTREAM_UNAVAILABLE, detail="dns_failure"
                ) from None
        if not names:
            raise EgressRefused("unresolved")
        addresses = []
        for name in names:
            try:
                address = ipaddress.ip_address(name.split("%")[0])
            except ValueError:
                raise EgressRefused("bad_address") from None
            reason = refusal(address)
            if reason is not None:
                raise EgressRefused(reason)
            addresses.append(address)
        # The checked address, not the name: a second resolution could answer differently.
        return await self._inner.connect_tcp(
            str(addresses[0]),
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 (httpcore passes it by name)
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise EgressRefused("unix_socket")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class OrganizationTransport(httpx.AsyncHTTPTransport):
    """httpx's transport, with every connection through ``GuardedBackend`` and only https."""

    def __init__(
        self, resolver: Resolver, inner: httpcore.AsyncNetworkBackend | None = None
    ) -> None:
        super().__init__(trust_env=False)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(),
            network_backend=GuardedBackend(resolver, inner),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.scheme != "https":
            raise EgressRefused("not_https")
        return await super().handle_async_request(request)


def organization_client(
    resolver: Resolver,
    *,
    inner: httpcore.AsyncNetworkBackend | None = None,
    connect_timeout_s: float = 5.0,
) -> httpx.AsyncClient:
    """The client an adapter uses for an organization's provider: guarded, no redirects, no
    proxy from the environment."""
    return httpx.AsyncClient(
        transport=OrganizationTransport(resolver, inner),
        timeout=httpx.Timeout(None, connect=connect_timeout_s),
        trust_env=False,
        follow_redirects=False,
    )
