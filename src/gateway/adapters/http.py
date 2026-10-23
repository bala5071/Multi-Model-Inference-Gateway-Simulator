"""What every provider adapter shares: its HTTP client, the credential header, mapping what a
provider answers to a ``ProviderError``, and reading a stream.

See docs/31-chat-and-adapters.md.

- **The client** takes no proxy from the environment (``trust_env=False``) and follows no
  redirect: the gateway talks to the URL it was configured with, and nothing else.
- **``credential_headers`` is the one place a ``Secret`` is revealed**: into the headers of one
  request. A test fails on any other call of ``reveal()`` in the package.
- **A provider's error body is never passed on.** At most its error *type* is kept, and only if
  it is on a fixed list; providers echo parts of a bad key in their messages.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any, Literal

import httpx

from gateway.chat import ProviderError, ProviderErrorCode
from gateway.secret import Secret

DEFAULT_CONNECT_TIMEOUT_S = 5.0
# The most of an error body read, only to find the provider's error type.
ERROR_BODY_LIMIT = 4096
MAX_RETRY_AFTER_S = 3600
# Provider error types worth naming in a refusal: they say what kind of problem it was and
# carry nothing the client sent or the provider holds.
KNOWN_ERROR_TYPES = frozenset(
    {
        "invalid_request_error",
        "not_found_error",
        "request_too_large",
        "billing_error",
        "permission_error",
        "authentication_error",
        "rate_limit_error",
        "api_error",
        "overloaded_error",
        "timeout_error",
        "invalid_api_key",
        "insufficient_quota",
        "model_not_found",
        "context_length_exceeded",
    }
)

AuthScheme = Literal["bearer", "x-api-key"]


def make_client(connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S) -> httpx.AsyncClient:
    """No overall timeout here: the first-token and total timeouts are on the gateway's
    clock (``gateway.chatservice``); only connecting is bounded by httpx."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(None, connect=connect_timeout_s),
        trust_env=False,
        follow_redirects=False,
    )


def credential_headers(credential: Secret | None, scheme: AuthScheme) -> dict[str, str]:
    """The header that carries ``credential``, for one request. The only ``reveal()``."""
    if credential is None:
        return {}
    if scheme == "bearer":
        return {"Authorization": "Bearer " + credential.reveal()}
    return {"x-api-key": credential.reveal()}


def retry_after(headers: httpx.Headers) -> int | None:
    """``Retry-After`` when it is a whole number of seconds from 1 to an hour, else None."""
    raw = headers.get("retry-after", "").strip()
    if raw.isdecimal() and 1 <= int(raw) <= MAX_RETRY_AFTER_S:
        return int(raw)
    return None


def error_type(body: bytes) -> str | None:
    """The provider's error type, if its body names one on the fixed list."""
    try:
        parsed = json.loads(body[:ERROR_BODY_LIMIT])
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    error = parsed.get("error") if isinstance(parsed, dict) else None
    kind = error.get("type") if isinstance(error, dict) else None
    if not isinstance(kind, str) and isinstance(error, dict):
        kind = error.get("code")
    return kind if isinstance(kind, str) and kind in KNOWN_ERROR_TYPES else None


def status_error(status: int, headers: httpx.Headers, body: bytes) -> ProviderError:
    """What a non-200 answer means."""
    if status in (401, 403):
        return ProviderError(ProviderErrorCode.PROVIDER_AUTH_FAILED)
    if status == 429:
        return ProviderError(
            ProviderErrorCode.UPSTREAM_RATE_LIMITED, retry_after_s=retry_after(headers)
        )
    if status >= 500:
        return ProviderError(ProviderErrorCode.UPSTREAM_UNAVAILABLE, detail="server_error")
    if 300 <= status < 400:
        return ProviderError(ProviderErrorCode.UPSTREAM_REJECTED, detail="redirect")
    return ProviderError(ProviderErrorCode.UPSTREAM_REJECTED, detail=error_type(body))


@asynccontextmanager
async def open_stream(
    client: httpx.AsyncClient, url: str, payload: Mapping[str, Any], headers: Mapping[str, str]
) -> AsyncIterator[httpx.Response]:
    """POST and yield the streaming response once it answered 200; map every failure. Leaving
    the block (also by cancellation) closes the upstream response."""
    try:
        async with client.stream("POST", url, json=payload, headers=dict(headers)) as response:
            if response.status_code != 200:
                body = b""
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) >= ERROR_BODY_LIMIT:
                        break
                raise status_error(response.status_code, response.headers, body)
            yield response
    except httpx.ConnectTimeout as exc:
        raise ProviderError(ProviderErrorCode.UPSTREAM_TIMEOUT, detail="connect") from exc
    except (httpx.ConnectError, httpx.UnsupportedProtocol) as exc:
        raise ProviderError(ProviderErrorCode.UPSTREAM_UNAVAILABLE, detail="connect") from exc
    except httpx.TransportError as exc:
        raise ProviderError(
            ProviderErrorCode.UPSTREAM_UNAVAILABLE, detail="connection_lost"
        ) from exc


def malformed(detail: str) -> ProviderError:
    return ProviderError(ProviderErrorCode.UPSTREAM_MALFORMED, detail=detail)


async def ndjson(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    """Each line of a newline-delimited JSON stream, as an object."""
    async for line in response.aiter_lines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            raise malformed("not_json") from None
        if not isinstance(value, dict):
            raise malformed("not_an_object")
        yield value


async def server_sent_events(response: httpx.Response) -> AsyncIterator[tuple[str, str]]:
    """Each event of a server-sent event stream: (event name, or "" for none; its data)."""
    event, data = "", list[str]()
    async for line in response.aiter_lines():
        if not line:
            if data:
                yield event, "\n".join(data)
            event, data = "", []
        elif line.startswith(":"):
            continue  # a comment, such as a keep-alive
        elif line.startswith("event:"):
            event = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            data.append(line.removeprefix("data:").removeprefix(" "))
    if data:
        yield event, "\n".join(data)


def sse_object(data: str) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except json.JSONDecodeError:
        raise malformed("not_json") from None
    if not isinstance(value, dict):
        raise malformed("not_an_object")
    return value
