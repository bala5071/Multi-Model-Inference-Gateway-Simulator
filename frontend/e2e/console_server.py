"""The admin console's e2e server (docs/47, docs/49): the plain server on a database made for this
run, with the suite's test SMTP server beside it.

Run by Playwright (``playwright.config.ts``), which generates the pepper, the vault key and the
accounts' passwords at the start of the run and passes them in the environment. This script
makes ``frontend/e2e/.console-state/`` afresh (git-ignored; only this script writes it), seeds
it through the operator's CLI (``scripts/manage.py``, each first password handed to its prompt),
and serves it on 127.0.0.1. The CLI's lines (what it created) are printed; no password, pepper
or key is.

The seed: organization ``acme`` with two applications and an organization cap on
``requests_per_minute``; ``admin-e2e`` (org_admin), ``viewer-e2e`` (viewer of the
organization) and ``appadmin-e2e`` (app_admin of ``app-one`` only); and an API key of
``app-one`` that expires in 3 days, so the default rule "Key expires within 14 days" fires on the
evaluator's first cycle: a real incident for the incident screens. The CLI prints a new key
once; that output goes nowhere here, so the key is never printed.

Email (docs/48, docs/49): the suite's test SMTP server (``tests/smtp_helpers.py``) runs on
127.0.0.1 with STARTTLS, its certificate signed by a CA made for this run. The server pins
SMTP's TLS to certifi and reads nothing from the environment for it, so in this process only,
``gateway.app.provider_tls_context`` is replaced by a context that trusts that CA alone (TLS 1.2
at least, the name checked). The tests read the mailbox from a loopback HTTP port
(``CONSOLE_E2E_MAIL_PORT``): each message's recipients and text, from memory, never a file.
"""

from __future__ import annotations

import asyncio
import contextlib
import email
import email.policy
import io
import json
import os
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
STATE = Path(__file__).resolve().parent / ".console-state"

ACCOUNTS = (
    ("admin-e2e", "CONSOLE_E2E_ADMIN_FIRST_PASSWORD", "org_admin", ("--organization", "acme")),
    ("viewer-e2e", "CONSOLE_E2E_VIEWER_FIRST_PASSWORD", "viewer", ("--organization", "acme")),
    (
        "appadmin-e2e",
        "CONSOLE_E2E_APPADMIN_FIRST_PASSWORD",
        "app_admin",
        ("--application", "app-one"),
    ),
)

# Where alerts may be mailed in this run: the reserved example domain only.
MAIL_DOMAIN = "example.com"


def seed(env: dict[str, str]) -> None:
    sys.path.insert(0, str(REPO / "scripts"))
    from manage import main as manage  # the operator's CLI, as an operator runs it

    def run(*argv: str, password: str | None = None, quiet: bool = False) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out) if quiet else contextlib.nullcontext():
            code = manage(list(argv), env, read_password=lambda _prompt: password or "")
        if code != 0:
            raise SystemExit(f"seeding failed at: {argv[0]}")

    run("migrate")
    run("create-organization", "--id", "acme", "--name", "Acme Corp", "--kind", "business")
    for app_id, name, plan in (("app-one", "App one", "enterprise"), ("app-two", "App two", "pro")):
        run("create-application", "--id", app_id, "--name", name, "--plan", plan,
            "--organization", "acme")  # fmt: skip
    for username, variable, role, scope in ACCOUNTS:
        run("create-user", "--username", username, "--organization", "acme",
            password=env[variable])  # fmt: skip
        run("grant-role", "--username", username, "--role", role, *scope)
    run("settings-set", "--organization", "acme", "--expected-version", "0",
        "--set", "requests_per_minute=600")  # fmt: skip
    # Printed once by the CLI: quiet, so it is never printed here.
    run("create-key", "--application", "app-one", "--label", "expiring",
        "--expires-in-days", "3", quiet=True)  # fmt: skip
    print("created key 'expiring' for app-one (expires in 3 days; not printed)")


def start_mail(directory: Path) -> tuple[Any, Any]:
    """The test SMTP server on its own event loop thread, and its certificates."""
    sys.path.insert(0, str(REPO / "tests"))
    from smtp_helpers import SmtpTestServer, make_certificates

    certificates = make_certificates(directory, "127.0.0.1")
    loop = asyncio.new_event_loop()
    started: dict[str, Any] = {}
    ready = threading.Event()

    def run() -> None:
        asyncio.set_event_loop(loop)
        server = SmtpTestServer(certificates)
        loop.run_until_complete(server.start())
        started["server"] = server
        ready.set()
        loop.run_forever()

    threading.Thread(target=run, name="test-smtp", daemon=True).start()
    if not ready.wait(30):
        raise SystemExit("the test SMTP server did not start")
    return started["server"], certificates


def serve_mailbox(server: Any, port: int) -> None:
    """``GET /messages``: every message received, its recipients and its text, from memory."""

    class Mailbox(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/messages":
                self.send_error(404)
                return
            messages = []
            for envelope in list(server.envelopes):
                parsed = email.message_from_bytes(envelope.data, policy=email.policy.default)
                body = parsed.get_body(preferencelist=("plain",))
                text = body.get_content() if body is not None else ""
                messages.append({"to": envelope.recipients, "text": text, "tls": envelope.tls})
            data = json.dumps(messages).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: object) -> None:
            return None  # nothing about the mailbox is printed

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Mailbox)
    threading.Thread(target=httpd.serve_forever, name="mailbox", daemon=True).start()


def main(argv: list[str]) -> int:
    port = argv[0] if argv else "8178"
    if STATE.exists():
        shutil.rmtree(STATE)
    STATE.mkdir()
    env = dict(os.environ)
    env["GATEWAY_DATABASE"] = str(STATE / "gateway.sqlite3")
    os.environ["GATEWAY_DATABASE"] = env["GATEWAY_DATABASE"]
    seed(env)

    mail, certificates = start_mail(STATE / "smtp")
    serve_mailbox(mail, int(os.environ.get("CONSOLE_E2E_MAIL_PORT", "8179")))
    os.environ.update(
        {
            "GATEWAY_SMTP_HOST": "127.0.0.1",
            "GATEWAY_SMTP_PORT": str(mail.port),
            "GATEWAY_SMTP_TLS": "starttls",
            "GATEWAY_SMTP_FROM": f"alerts@{MAIL_DOMAIN}",
            "GATEWAY_ALERT_EMAIL_DOMAINS": MAIL_DOMAIN,
        }
    )
    import gateway.app

    # This process only: the run's CA, and nothing else, is trusted for outgoing TLS.
    gateway.app.provider_tls_context = certificates.client_context

    from gateway.serve import main as serve

    return serve(["--port", port, "--log-level", "warning"])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
