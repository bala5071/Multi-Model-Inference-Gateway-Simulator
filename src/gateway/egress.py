"""The egress guard: where the gateway may open a connection to a model provider.

See docs/32-provider-settings-and-vault.md, docs/33-fallback-modes.md and docs/provider-plan.md,
sections 4 and 5.

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

**Locality** (docs/33). A destination is *local* when its host and port are on the operator's
list (``LocalDestinations``); everything else is *hosted*, every organization provider
included. The same transport and backend refuse a hosted destination unless the current
request's scope allows it (``egress_scope``): the router opens a scope for each chat request
whose answer is asked at every request sent and every connection opened. A chat request's scope
answers from the mode the request read once (``gateway.modes.effective_mode``); the router
reads the mode again before each hosted attempt and refuses it if the mode was made stricter
(docs/33, docs/38). With no scope the guard refuses every hosted
destination (fail closed). The operator's own adapters go
through ``operator_client``: local destinations as configured (any scheme, no address check:
they are the operator's own), hosted ones under every rule above.

**Webhooks** (docs/46) go through ``webhook_client`` only: the same guard with no local
destination ever, under ``egress_scope(always_hosted)``, plus a port rule checked at connect
(443, or a port the operator allows), and a bound on each stage on the injected clock (name
resolution, connect, the TLS handshake, each write and each read: ``StageTimeout``). A failed
handshake is ``TlsFailure``. Neither carries anything but the stage's name.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
from collections.abc import Awaitable, Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpcore
import httpx

from gateway.chat import ProviderError, ProviderErrorCode
from gateway.clock import Clock
from gateway.overload import RequestTimeoutError, run_with_timeout

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


# EgressRefused's detail for a port not allowed (webhooks: 443, or the operator's list).
PORT_REFUSED = "port"


class StageTimeout(Exception):
    """A webhook stage (``resolve``, ``connect``, ``tls``, ``write``, ``read``) took longer than
    its bound. Its message is the stage's name alone."""

    def __init__(self, stage: str) -> None:
        super().__init__(stage)
        self.stage = stage


class TlsFailure(Exception):
    """A webhook's TLS handshake failed (a certificate not verified, a name not matching): a
    permanent failure. It carries nothing of the cause."""


@dataclass(frozen=True, slots=True)
class EgressScope:
    """Whether the request in progress may reach a hosted destination now. Asked again at every
    request and every connection, never remembered. A coroutine: the answer is read from the
    mode store (docs/34-state-interfaces.md), and a store that cannot answer raises, which
    refuses the connection."""

    may_reach_hosted: Callable[[], Awaitable[bool]]


_SCOPE: ContextVar[EgressScope | None] = ContextVar("gateway_egress_scope", default=None)


@contextmanager
def egress_scope(may_reach_hosted: Callable[[], Awaitable[bool]]) -> Iterator[None]:
    """Run the block (and the tasks it starts) under this answer to "may it go hosted?"."""
    token = _SCOPE.set(EgressScope(may_reach_hosted))
    try:
        yield
    finally:
        _SCOPE.reset(token)


async def hosted_allowed() -> bool:
    """Whether the current scope lets a request reach a hosted destination. No scope: no."""
    scope = _SCOPE.get()
    return scope is not None and await scope.may_reach_hosted()


async def always_hosted() -> bool:
    """A scope's answer for a probe an org_admin asked for (docs/33): yes, whatever the mode."""
    return True


def _host(name: str) -> str:
    return name.strip("[]").lower().rstrip(".")


@dataclass(frozen=True, slots=True)
class LocalDestinations:
    """The operator's local destinations, as (host, port). Never an organization's."""

    destinations: frozenset[tuple[str, int]] = frozenset()

    def contains(self, host: str, port: int) -> bool:
        return (_host(host), port) in self.destinations

    def contains_url(self, url: str) -> bool:
        parts = urlsplit(url)
        if not parts.hostname:
            return False
        port = parts.port or (443 if parts.scheme == "https" else 80)
        return self.contains(parts.hostname, port)

    @classmethod
    def parse(cls, text: str) -> LocalDestinations:
        """``host:port`` entries, comma-separated (``GATEWAY_PROVIDER_LOCAL_ALLOW``)."""
        found: set[tuple[str, int]] = set()
        for entry in (e.strip() for e in text.split(",")):
            if not entry:
                continue
            host, sep, port = entry.rpartition(":")
            if not sep or not host or not port.isdigit():
                raise ValueError(f"a local destination is host:port, not {entry!r}")
            found.add((_host(host), int(port)))
        return cls(frozenset(found))

    @classmethod
    def loopback_of(cls, urls: Iterable[str]) -> LocalDestinations:
        """The destinations among ``urls`` that are this machine (loopback)."""
        found: set[tuple[str, int]] = set()
        for url in urls:
            parts = urlsplit(url)
            host = _host(parts.hostname or "")
            try:
                loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = False
            if loopback:
                found.add((host, parts.port or (443 if parts.scheme == "https" else 80)))
        return cls(frozenset(found))

    def __or__(self, other: LocalDestinations) -> LocalDestinations:
        return LocalDestinations(self.destinations | other.destinations)


NO_LOCAL_DESTINATIONS = LocalDestinations()
# The operator's own local destinations, host:port, comma-separated (docs/33).
LOCAL_ALLOW_ENV = "GATEWAY_PROVIDER_LOCAL_ALLOW"


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
    """Opens a TCP connection to a local destination of the operator's, or, when the request's
    scope allows hosted, only to a checked, public address, and to that address."""

    def __init__(
        self,
        resolver: Resolver,
        inner: httpcore.AsyncNetworkBackend | None = None,
        local: LocalDestinations = NO_LOCAL_DESTINATIONS,
        ports: frozenset[int] | None = None,
    ):
        """``ports``: the only ports a hosted connection may use (webhooks); None, any."""
        self._resolver = resolver
        self._inner = inner if inner is not None else httpcore.AnyIOBackend()
        self._local = local
        self._ports = ports

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 (httpcore passes it by name)
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if self._local.contains(host, port):
            # The operator's own destination, as configured.
            return await self._inner.connect_tcp(
                host,
                port,
                timeout=timeout,
                local_address=local_address,
                socket_options=socket_options,
            )
        # Hosted: refused before the name is even resolved unless the scope allows it now.
        if not await hosted_allowed():
            raise EgressRefused("local_only")
        if self._ports is not None and port not in self._ports:
            # The effective port of the URL being connected to, checked again here (A4-9).
            raise EgressRefused(PORT_REFUSED)
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
        # Asked again now: resolving takes time, and the mode may have changed meanwhile.
        if not await hosted_allowed():
            raise EgressRefused("local_only")
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


def provider_tls_context() -> ssl.SSLContext:
    """The TLS context provider calls are made with: certificates verified, host names checked,
    against httpx's own trust store (``certifi``). ``trust_env=False``, as for every guarded
    client: ``SSL_CERT_FILE`` and ``SSL_CERT_DIR`` cannot replace it. TLS 1.2 at least, set
    here rather than left to the platform's OpenSSL defaults, which differ (docs/46). Nothing in
    it depends on the organization; the server makes one and shares it (docs/33, tested)."""
    context = httpx.create_ssl_context(trust_env=False)
    if context.minimum_version < ssl.TLSVersion.TLSv1_2:
        context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


class StagedStream(httpcore.AsyncNetworkStream):
    """A connection whose every write, read and TLS handshake is bounded on ``clock``."""

    def __init__(self, inner: httpcore.AsyncNetworkStream, clock: Clock, bound_ms: float) -> None:
        self._inner = inner
        self._clock = clock
        self._bound_ms = bound_ms

    async def _bounded(self, stage: str, work: Awaitable[Any]) -> Any:
        try:
            return await run_with_timeout(self._clock, work, self._bound_ms)
        except RequestTimeoutError:
            raise StageTimeout(stage) from None

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:  # noqa: ASYNC109
        found: bytes = await self._bounded("read", self._inner.read(max_bytes, None))
        return found

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:  # noqa: ASYNC109
        await self._bounded("write", self._inner.write(buffer, None))

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> httpcore.AsyncNetworkStream:
        try:
            done = await self._bounded(
                "tls", self._inner.start_tls(ssl_context, server_hostname, None)
            )
        except StageTimeout:
            raise
        except Exception:
            # Whatever the handshake raised (httpcore maps ssl errors to ConnectError): a
            # permanent ``tls``, its cause not carried.
            raise TlsFailure() from None
        return StagedStream(done, self._clock, self._bound_ms)

    def get_extra_info(self, info: str) -> Any:
        return self._inner.get_extra_info(info)


class StagedBackend(httpcore.AsyncNetworkBackend):
    """Connects through ``inner`` with the connect bounded on ``clock``, and bounds every stage
    of the connection it returns (``StagedStream``)."""

    def __init__(self, inner: httpcore.AsyncNetworkBackend, clock: Clock, bound_ms: float) -> None:
        self._inner = inner
        self._clock = clock
        self._bound_ms = bound_ms

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 (httpcore passes it by name)
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            stream = await run_with_timeout(
                self._clock,
                self._inner.connect_tcp(
                    host, port, timeout=None, local_address=local_address,
                    socket_options=socket_options,
                ),
                self._bound_ms,
            )  # fmt: skip
        except RequestTimeoutError:
            raise StageTimeout("connect") from None
        return StagedStream(stream, self._clock, self._bound_ms)

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 (httpcore passes it by name)
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise EgressRefused("unix_socket")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def staged_resolver(resolver: Resolver, clock: Clock, bound_ms: float) -> Resolver:
    """``resolver`` bounded on ``clock``: past the bound, ``StageTimeout("resolve")``."""

    async def resolve(host: str, port: int) -> list[str]:
        try:
            return await run_with_timeout(clock, resolver(host, port), bound_ms)
        except RequestTimeoutError:
            raise StageTimeout("resolve") from None

    return resolve


class GuardedTransport(httpx.AsyncHTTPTransport):
    """httpx's transport, with every connection through ``GuardedBackend``. Each request is
    checked too, so a pooled connection never carries one past the locality rule: a hosted
    destination needs a scope that allows it now, and https."""

    def __init__(
        self,
        resolver: Resolver,
        inner: httpcore.AsyncNetworkBackend | None = None,
        local: LocalDestinations = NO_LOCAL_DESTINATIONS,
        ssl_context: ssl.SSLContext | None = None,
        network: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        """``network`` replaces the guarded backend built from the rest (``webhook_client``
        builds its own, with the port rule and the stage bounds)."""
        # One TLS context, given to httpx's own setup too, which would otherwise build another:
        # building one loads the certificate bundle, which costs more than a whole request.
        tls = ssl_context if ssl_context is not None else provider_tls_context()
        super().__init__(verify=tls, trust_env=False)
        self._local = local
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=tls,
            network_backend=(
                network if network is not None else GuardedBackend(resolver, inner, local)
            ),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        port = url.port or (443 if url.scheme == "https" else 80)
        if not self._local.contains(url.host, port):
            if not await hosted_allowed():
                raise EgressRefused("local_only")
            if url.scheme != "https":
                raise EgressRefused("not_https")
        return await super().handle_async_request(request)


class OrganizationTransport(GuardedTransport):
    """The transport for an organization's provider: no local destination, ever."""

    def __init__(
        self,
        resolver: Resolver,
        inner: httpcore.AsyncNetworkBackend | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        super().__init__(resolver, inner, NO_LOCAL_DESTINATIONS, ssl_context)


def organization_client(
    resolver: Resolver,
    *,
    inner: httpcore.AsyncNetworkBackend | None = None,
    connect_timeout_s: float = 5.0,
    ssl_context: ssl.SSLContext | None = None,
) -> httpx.AsyncClient:
    """The client an adapter uses for an organization's provider: guarded, no redirects, no
    proxy from the environment. A new one is made for every call (docs/33), so the server
    passes one ``ssl_context`` for all of them (``provider_tls_context()``, made once)."""
    return httpx.AsyncClient(
        transport=OrganizationTransport(resolver, inner, ssl_context),
        timeout=httpx.Timeout(None, connect=connect_timeout_s),
        trust_env=False,
        follow_redirects=False,
    )


def operator_client(
    resolver: Resolver,
    local: LocalDestinations,
    *,
    inner: httpcore.AsyncNetworkBackend | None = None,
    connect_timeout_s: float = 5.0,
) -> httpx.AsyncClient:
    """The client for the operator's own adapters: its local destinations as configured, and
    anything else under the hosted rules (the request's scope, https, public addresses)."""
    return httpx.AsyncClient(
        transport=GuardedTransport(resolver, inner, local),
        timeout=httpx.Timeout(None, connect=connect_timeout_s),
        trust_env=False,
        follow_redirects=False,
    )


# Each webhook stage's bound (name resolution, connect, TLS handshake, each write and each read),
# and the whole attempt's, on the injected clock (the A4 plan's section 4.2, decision 13).
WEBHOOK_STAGE_MS = 5_000.0
WEBHOOK_ATTEMPT_MS = 10_000.0


def webhook_client(
    resolver: Resolver,
    clock: Clock,
    ports: frozenset[int],
    *,
    inner: httpcore.AsyncNetworkBackend | None = None,
    ssl_context: ssl.SSLContext | None = None,
) -> httpx.AsyncClient:
    """The only client a webhook is sent with (an AST test). Every connection resolved and
    checked by ``GuardedBackend`` (no local destination, every address public, the socket to
    the checked address), on a port in ``ports``; each stage bounded on ``clock``; TLS with the
    pinned, verifying context (``provider_tls_context()``); no redirects; no proxy from the
    environment. A new one for every delivery, so every delivery resolves and checks again
    (M45): it keeps no connection."""
    tls = ssl_context if ssl_context is not None else provider_tls_context()
    network = GuardedBackend(
        staged_resolver(resolver, clock, WEBHOOK_STAGE_MS),
        StagedBackend(
            inner if inner is not None else httpcore.AnyIOBackend(), clock, WEBHOOK_STAGE_MS
        ),
        NO_LOCAL_DESTINATIONS,
        ports,
    )
    return httpx.AsyncClient(
        transport=GuardedTransport(
            resolver, ssl_context=tls, local=NO_LOCAL_DESTINATIONS, network=network
        ),
        timeout=httpx.Timeout(None),
        trust_env=False,
        follow_redirects=False,
    )
