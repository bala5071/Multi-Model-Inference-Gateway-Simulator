"""The admin console's e2e server (docs/47): the plain server on a database made for this run.

Run by Playwright (``playwright.config.ts``), which generates the pepper, the vault key and the
accounts' passwords at the start of the run and passes them in the environment. This script
makes ``frontend/e2e/.console-state/`` afresh (git-ignored; only this script writes it), seeds
it through the operator's CLI (``scripts/manage.py``, each first password handed to its prompt),
and serves it on 127.0.0.1. The CLI's lines (what it created) are printed; no password, pepper
or key is.

The seed: organization ``acme`` with two applications and an organization cap on
``requests_per_minute``; ``admin-e2e`` (org_admin), ``viewer-e2e`` (viewer of the
organization) and ``appadmin-e2e`` (app_admin of ``app-one`` only).
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

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


def seed(env: dict[str, str]) -> None:
    sys.path.insert(0, str(REPO / "scripts"))
    from manage import main as manage  # the operator's CLI, as an operator runs it

    def run(*argv: str, password: str | None = None) -> None:
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


def main(argv: list[str]) -> int:
    port = argv[0] if argv else "8178"
    if STATE.exists():
        shutil.rmtree(STATE)
    STATE.mkdir()
    env = dict(os.environ)
    env["GATEWAY_DATABASE"] = str(STATE / "gateway.sqlite3")
    os.environ["GATEWAY_DATABASE"] = env["GATEWAY_DATABASE"]
    seed(env)

    from gateway.serve import main as serve

    return serve(["--port", port, "--log-level", "warning"])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
