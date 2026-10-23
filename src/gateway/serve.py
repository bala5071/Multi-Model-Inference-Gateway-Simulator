"""Start the gateway: ``python -m gateway.serve``.

See docs/25-local-only.md. A thin launcher around uvicorn that:

- turns uvicorn's proxy headers off, so the client address is always the socket peer (the
  app's local-only guard refuses forwarded headers anyway, so plain ``uvicorn`` is safe too);
- refuses to bind a non-loopback address while monitoring, load control and the dashboard are
  unauthenticated (and console login and management are local only), unless
  ``GATEWAY_ALLOW_NETWORK_BIND=1`` (the Docker image sets it, because it must bind 0.0.0.0
  inside the container and is published on 127.0.0.1 only); then it starts and prints a
  warning naming the local-only endpoints.

    uv run python -m gateway.serve                     # 127.0.0.1:8000, the default server
    uv run python -m gateway.serve --port 8123
    GATEWAY_ALLOW_NETWORK_BIND=1 uv run python -m gateway.serve --host 0.0.0.0
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import uvicorn

from gateway.localonly import LOCAL_ONLY_PATHS

ALLOW_NETWORK_BIND_ENV = "GATEWAY_ALLOW_NETWORK_BIND"
DEFAULT_APP = "gateway.app:create_default_app"


def is_loopback_bind(host: str) -> bool:
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip().strip("[]")).is_loopback
    except ValueError:
        return False  # a name that could resolve to anything


def bind_decision(host: str, environ: Mapping[str, str]) -> tuple[bool, str | None]:
    """(may start, message to print). A loopback bind starts silently; any other bind is
    refused unless opted in, and then starts with a warning."""
    if is_loopback_bind(host):
        return True, None
    endpoints = ", ".join(LOCAL_ONLY_PATHS)
    if environ.get(ALLOW_NETWORK_BIND_ENV) == "1":
        return True, (
            f"WARNING: binding {host}, which is not loopback, because "
            f"{ALLOW_NETWORK_BIND_ENV}=1. These endpoints are local only until A8, and most "
            f"have no user authentication: {endpoints}. The app still serves them only to "
            "loopback peers and to GATEWAY_LOCAL_NETWORKS, but make sure nothing outside this "
            "machine can reach this port (in Docker: publish it on 127.0.0.1 only)."
        )
    return False, (
        f"Refusing to bind {host}: it is not a loopback address, and these endpoints are local "
        f"only until A8 (most have no user authentication): {endpoints}. Bind 127.0.0.1 "
        f"(the default), or set {ALLOW_NETWORK_BIND_ENV}=1 if something else (such as Docker "
        "publishing on 127.0.0.1 only) keeps the port local. See docs/25-local-only.md."
    )


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    run: Callable[..., Any] = uvicorn.run,
) -> int:
    env = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(prog="python -m gateway.serve", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--app", default=DEFAULT_APP, help="an app factory, module:function")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)

    allowed, message = bind_decision(args.host, env)
    if message is not None:
        print(message, file=sys.stderr, flush=True)
    if not allowed:
        return 2
    run(
        args.app,
        factory=True,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        proxy_headers=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
