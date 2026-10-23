"""No password, session id, CSRF token or hash leaks: a scan of everything a login can
leave behind, with a planted control that the scan must find. See
docs/27-users-and-sessions.md."""

from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path

import httpx
import pytest

from accounts_helpers import (
    OTHER_PASSWORD,
    PASSWORD,
    Accounts,
    console,
    cookie,
    http_login,
    make_accounts,
    with_csrf,
)
from gateway.auth import ADMIN_PASSWORD_ENV, ADMIN_USERNAME_ENV, LoginFailedError
from gateway.passwords import MAX_LENGTH, PasswordPolicyError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from manage import main as manage

ADMIN_PASSWORD = "maple quarry eleven hounds"
# What a user types into the username box by mistake: their password.
TYPED_AS_USERNAME = "ochre lighthouse forty"
OVERSIZED = "z" * (MAX_LENGTH + 1)
CLI_PASSWORD = "birch canyon thirteen gulls"
STEP_UP_WRONG = "amber window sixty crows"


def find_leaks(corpus: dict[str, str], secrets: dict[str, str]) -> list[tuple[str, str]]:
    """Every (where, which secret) such that the secret appears in that text."""
    return [
        (where, name)
        for where, text in corpus.items()
        for name, secret in secrets.items()
        if secret and secret in text
    ]


def _table(connection: sqlite3.Connection, table: str, hidden: set[str]) -> str:
    """A table's rows, without the columns that are meant to hold a digest."""
    columns = [r[1] for r in connection.execute(f"PRAGMA table_info({table})")]
    shown = [c for c in columns if c not in hidden]
    rows = connection.execute(f"SELECT {', '.join(shown)} FROM {table}").fetchall()
    return repr(rows)


def _response(response: httpx.Response) -> str:
    headers = [(k, v) for k, v in response.headers.multi_items() if k != "set-cookie"]
    return f"{response.status_code} {headers} {response.text}"


async def _scenario(accounts: Accounts) -> tuple[dict[str, str], dict[str, str], list[object]]:
    """Exercise logins and record what came back. Returns the responses, the secrets used or
    issued, and objects whose repr might be logged."""
    service = accounts.service
    service.create_user("alice", PASSWORD, "north")
    service.bootstrap_admin({ADMIN_USERNAME_ENV: "root", ADMIN_PASSWORD_ENV: ADMIN_PASSWORD})
    responses: dict[str, str] = {}
    secrets: dict[str, str] = {
        "alice's password": PASSWORD,
        "alice's new password": OTHER_PASSWORD,
        "admin password": ADMIN_PASSWORD,
        "password typed as username": TYPED_AS_USERNAME,
        "oversized password": OVERSIZED,
        "wrong step-up password": STEP_UP_WRONG,
    }
    objects: list[object] = [service]
    async with console(accounts) as c:
        response, sid, csrf = await http_login(c, "alice", PASSWORD)
        responses["login"] = _response(response)
        assert sid is not None
        assert csrf is not None
        secrets["session id"], secrets["csrf token"] = sid, csrf
        responses["me"] = _response(await c.client.get("/v1/auth/me", headers=cookie(sid)))
        responses["sessions"] = _response(
            await c.client.get("/v1/auth/sessions", headers=cookie(sid))
        )
        responses["unknown user"] = _response((await http_login(c, TYPED_AS_USERNAME, PASSWORD))[0])
        responses["wrong password"] = _response((await http_login(c, "alice", "x" * 20))[0])
        responses["oversized"] = _response((await http_login(c, "alice", OVERSIZED))[0])
        responses["no csrf"] = _response(
            await c.client.post("/v1/auth/logout", headers=cookie(sid))
        )
        responses["weak new password"] = _response(
            await c.client.post(
                "/v1/auth/change-password",
                json={"current_password": PASSWORD, "new_password": "alice" + PASSWORD},
                headers=with_csrf(sid, csrf),
            )
        )
        responses["changed"] = _response(
            await c.client.post(
                "/v1/auth/change-password",
                json={"current_password": PASSWORD, "new_password": OTHER_PASSWORD},
                headers=with_csrf(sid, csrf),
            )
        )
        # Step-up: a wrong password, then the right one (the new password).
        responses["step-up refused"] = _response(
            await c.client.post(
                "/v1/auth/step-up",
                json={"password": STEP_UP_WRONG},
                headers=with_csrf(sid, csrf),
            )
        )
        responses["step-up granted"] = _response(
            await c.client.post(
                "/v1/auth/step-up",
                json={"password": OTHER_PASSWORD},
                headers=with_csrf(sid, csrf),
            )
        )
        admin, admin_sid, admin_csrf = await http_login(c, "root", ADMIN_PASSWORD)
        responses["admin login"] = _response(admin)
        assert admin_sid is not None
        assert admin_csrf is not None
        secrets["admin session id"], secrets["admin csrf token"] = admin_sid, admin_csrf
        for _ in range(6):  # lock alice
            responses["lockout"] = _response((await http_login(c, "alice", "y" * 20))[0])
        responses["logout"] = _response(
            await c.client.post("/v1/auth/logout", headers=with_csrf(sid, csrf))
        )
    # Exceptions and reprs a log line might carry.
    try:
        await service.login("alice", PASSWORD)
    except LoginFailedError as exc:
        objects.append(exc)
    try:
        service.create_user("dora", "dora is my password", "north")
    except PasswordPolicyError as exc:
        objects.append(exc)
    objects.extend([service.store.find_user("alice"), service.store.list_sessions("x")])
    return responses, secrets, objects


async def test_no_secret_appears_anywhere_and_the_scan_would_see_one(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    accounts = make_accounts()
    responses, secrets, objects = await _scenario(accounts)

    # The CLI, with its prompt answered, on a database of its own.
    environ = {
        "GATEWAY_DATABASE": str(tmp_path / "cli.db"),
        "GATEWAY_ARGON2_MEMORY_KIB": "8",
        "GATEWAY_ARGON2_TIME_COST": "1",
    }
    answers = iter([CLI_PASSWORD, CLI_PASSWORD, CLI_PASSWORD, CLI_PASSWORD])
    assert manage(["create-user", "--username", "erin"], environ, lambda _: next(answers)) == 0
    assert manage(["reset-password", "--username", "erin"], environ, lambda _: next(answers)) == 0
    secrets["cli password"] = CLI_PASSWORD

    # Every digest the database holds is a secret everywhere else.
    databases = [accounts.connection, sqlite3.connect(environ["GATEWAY_DATABASE"])]
    for i, connection in enumerate(databases):
        for (digest,) in connection.execute("SELECT password_hash FROM users"):
            secrets[f"password hash {i}:{len(secrets)}"] = digest
        for id_hash, csrf_hash in connection.execute("SELECT id_hash, csrf_hash FROM sessions"):
            secrets[f"session digest {i}:{len(secrets)}"] = id_hash
            secrets[f"csrf digest {i}:{len(secrets)}"] = csrf_hash

    def corpus() -> dict[str, str]:
        out, err = capsys.readouterr()
        captured["stdout"] += out
        captured["stderr"] += err
        texts = {
            "logs": "\n".join(
                f"{r.name} {r.getMessage()} {r.args!r} {r.exc_text or ''}" for r in caplog.records
            ),
            "stdout": captured["stdout"],
            "stderr": captured["stderr"],
            "reprs": "\n".join(f"{o!r} {o!s}" for o in objects),
        }
        texts.update({f"response: {name}": text for name, text in responses.items()})
        for i, connection in enumerate(databases):
            texts[f"audit_log {i}"] = _table(connection, "audit_log", set())
            texts[f"users {i}"] = _table(connection, "users", {"password_hash"})
            texts[f"sessions {i}"] = _table(connection, "sessions", {"id_hash", "csrf_hash"})
        return texts

    captured = {"stdout": "", "stderr": ""}
    # Login and /me give the CSRF token to the client: that is how it is delivered (as the
    # session id is, in Set-Cookie, which is left out above). Only there.
    for name, token in (
        ("login", "csrf token"),
        ("me", "csrf token"),
        ("admin login", "admin csrf token"),
    ):
        assert secrets[token] in responses[name]
        responses[name] = responses[name].replace(secrets[token], "<delivered>")
    scanned = corpus()
    assert len(secrets) >= 15
    assert find_leaks(scanned, secrets) == []

    # The control: a secret logged by mistake is found, where it was put.
    logging.getLogger("gateway").debug("oops, logged: %s", secrets["session id"])
    print(f"and printed: {secrets['admin password']}")
    found = find_leaks(corpus(), secrets)
    assert found == [("logs", "session id"), ("stdout", "admin password")]
    databases[1].close()


# --- the management endpoints: temporary passwords and issued keys (docs/30) -------------------


async def test_no_temporary_password_or_issued_key_leaks_and_the_scan_would_see_one(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """A temporary password and a new key appear in the one response that delivers each, and
    nowhere else: not in logs, output, other responses, the audit log or any table."""
    from test_management_api import admin_console, make_world

    caplog.set_level(logging.DEBUG)
    world = make_world()
    responses: dict[str, str] = {}
    secrets: dict[str, str] = {}
    delivered: dict[str, str] = {}  # which response delivers which secret
    async with admin_console(world) as admin:
        alice = await admin.login("alice")
        await admin.step_up(alice)
        made = await admin.post(alice, "/north/users", {"username": "newbie"})
        secrets["temporary password, created"] = made.json()["temporary_password"]
        responses["create user"] = _response(made)
        delivered["create user"] = "temporary password, created"
        reset = await admin.post(alice, "/north/users/bob/reset-password")
        secrets["temporary password, reset"] = reset.json()["temporary_password"]
        responses["reset"] = _response(reset)
        delivered["reset"] = "temporary password, reset"
        issued = await admin.post(alice, "/north/applications/north-shop/keys", {"label": "new"})
        secrets["issued key"] = issued.json()["key"]
        responses["issue"] = _response(issued)
        delivered["issue"] = "issued key"
        key_id = issued.json()["key_id"]
        rotated = await admin.post(
            alice, f"/north/applications/north-shop/keys/{key_id}/rotate", {"grace_hours": 1}
        )
        secrets["rotated key"] = rotated.json()["key"]
        responses["rotate"] = _response(rotated)
        delivered["rotate"] = "rotated key"
        generated = await admin.client.post(
            "/v1/generate",
            json={"model": "economy", "prompt": "hello", "max_tokens": 1},
            headers={"Authorization": f"Bearer {secrets['rotated key']}"},
        )
        responses["generate"] = _response(generated)
        newbie = await admin.login("newbie", secrets["temporary password, created"])
        responses["newbie lists"] = _response(await admin.get(newbie, "/north/users"))
        for name, path in (
            ("users", "/north/users"),
            ("keys", "/north/applications/north-shop/keys"),
            ("applications", "/north/applications"),
        ):
            responses[name] = _response(await admin.get(alice, path))
        responses["revoke"] = _response(
            await admin.post(alice, f"/north/applications/north-shop/keys/{key_id}/revoke")
        )
        responses["taken"] = _response(await admin.post(alice, "/north/users", {"username": "sam"}))
    connection = world.accounts.connection
    for (digest,) in connection.execute("SELECT key_hmac FROM api_keys"):
        secrets[f"key hmac {len(secrets)}"] = digest
    for (digest,) in connection.execute("SELECT password_hash FROM users"):
        secrets[f"password hash {len(secrets)}"] = digest
    for name, which in delivered.items():
        assert secrets[which] in responses[name], name
        responses[name] = responses[name].replace(secrets[which], "<delivered>")

    captured = {"stdout": "", "stderr": ""}

    def corpus() -> dict[str, str]:
        out, err = capsys.readouterr()
        captured["stdout"] += out
        captured["stderr"] += err
        texts = {
            "logs": "\n".join(
                f"{r.name} {r.getMessage()} {r.args!r} {r.exc_text or ''}" for r in caplog.records
            ),
            "stdout": captured["stdout"],
            "stderr": captured["stderr"],
            "audit_log": _table(connection, "audit_log", set()),
            "users": _table(connection, "users", {"password_hash"}),
            "api_keys": _table(connection, "api_keys", {"key_hmac"}),
            "applications": _table(connection, "applications", set()),
            "sessions": _table(connection, "sessions", {"id_hash", "csrf_hash"}),
            "reprs": repr(world.keys) + repr(world.accounts.service),
        }
        texts.update({f"response: {name}": text for name, text in responses.items()})
        return texts

    assert len(secrets) >= 15
    assert find_leaks(corpus(), secrets) == []

    # The control: a temporary password logged and a key printed by mistake are found.
    logging.getLogger("gateway").info("made user with %s", secrets["temporary password, reset"])
    print(f"issued {secrets['issued key']}")
    assert find_leaks(corpus(), secrets) == [
        ("logs", "temporary password, reset"),
        ("stdout", "issued key"),
    ]
