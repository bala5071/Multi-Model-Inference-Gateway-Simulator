"""Users, login, lockout, sessions, CSRF and the audit of logins. See
docs/27-users-and-sessions.md."""

from __future__ import annotations

import asyncio
import json
import logging
import math
from typing import Any

import httpx
import pytest

from accounts_helpers import (
    ORIGIN_MS,
    OTHER_PASSWORD,
    PASSWORD,
    Accounts,
    console,
    cookie,
    http_login,
    make_accounts,
    ready_user,
    with_csrf,
)
from gateway.app import create_app, create_default_app
from gateway.audit import AuditAction, AuditEvent, Outcome, Reason, TargetType
from gateway.auth import (
    ADMIN_PASSWORD_ENV,
    ADMIN_USERNAME_ENV,
    COOKIE_SECURE_ENV,
    LAST_SEEN_WRITE_MS,
    LOCK_MS,
    LOCK_WINDOW_MS,
    MINUTE_MS,
    SESSION_ABSOLUTE_MS,
    SESSION_IDLE_MS,
    BootstrapError,
    BootstrapOutcome,
    LoginFailedError,
    SessionInvalidError,
    audit_retention_ms,
    session_cookie,
)
from gateway.clock import SimClock
from gateway.continuous import build_continuous_registry
from gateway.passwords import Argon2Parameters, PasswordHasher, PasswordPolicyError
from gateway.permissions import Assignment, ScopeType
from gateway.users import Bootstrap, normalize_username
from helpers import TEST_ADMIN_PASSWORD, TEST_ADMIN_USERNAME, default_server

ADMIN_ENV = {ADMIN_USERNAME_ENV: "root", ADMIN_PASSWORD_ENV: "maple quarry eleven hounds"}


@pytest.fixture
def accounts() -> Accounts:
    return make_accounts()


def _actions(accounts: Accounts) -> list[tuple[str, str, str]]:
    return [
        (e.event.action.value, e.event.outcome.value, e.event.target_type.value)
        for e in accounts.audit.entries()
    ]


# --- usernames and creating users -----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "stored"),
    [("alice", "alice"), ("Alice", "alice"), ("a.b-c_9", "a.b-c_9"), ("x" * 64, "x" * 64)],
)
def test_usernames_are_case_folded_ascii(raw: str, stored: str) -> None:
    assert normalize_username(raw) == stored


@pytest.mark.parametrize(
    "raw",
    ["", "ab", "x" * 65, "al ice", "alice!", "\u00e5lice", "\u212aelvin", "al\u200bice", "a/b"],
)
def test_bad_usernames_are_refused(raw: str) -> None:
    # "\u212a" is the Kelvin sign: it would case-fold to "k", so ASCII is checked first.
    with pytest.raises(ValueError, match="username"):
        normalize_username(raw)


def test_an_individual_organization_refuses_a_second_member(accounts: Accounts) -> None:
    accounts.service.create_user("first", PASSWORD, "solo")
    with pytest.raises(ValueError, match="individual organization"):
        accounts.service.create_user("second", PASSWORD, "solo")
    # A business organization takes any number.
    for name in ("bee1", "bee2", "bee3"):
        accounts.service.create_user(name, PASSWORD, "north")
    assert accounts.store.count_members("north") == 3


def test_creating_a_user_checks_everything_and_never_shows_the_password(
    accounts: Accounts,
) -> None:
    service = accounts.service
    with pytest.raises(ValueError, match="no organization"):
        service.create_user("carol", PASSWORD, "nowhere")
    accounts.organizations.set_organization_disabled("north", True)
    with pytest.raises(ValueError, match="disabled"):
        service.create_user("carol", PASSWORD, "north")
    accounts.organizations.set_organization_disabled("north", False)
    for password, problem in (
        ("short", "too_short"),
        ("carol likes long walks", "contains_username"),
        ("q1w2e3r4t5y6", "too_common"),
    ):
        with pytest.raises(PasswordPolicyError) as excinfo:
            service.create_user("carol", password, "north")
        assert excinfo.value.problem.value == problem
        assert password not in str(excinfo.value)
    service.create_user("carol", PASSWORD, "north")
    with pytest.raises(ValueError, match="taken"):
        service.create_user("CAROL", OTHER_PASSWORD, "north")
    user = accounts.store.find_user("carol")
    assert user is not None
    assert PASSWORD not in user.password_hash
    assert user.password_hash.startswith("$argon2id$")
    assert PASSWORD not in repr(user)
    assert user.password_hash not in repr(user)


# --- the bootstrap admin --------------------------------------------------------------------


def test_the_bootstrap_admin_is_made_once_and_never_overwritten(accounts: Accounts) -> None:
    assert accounts.service.bootstrap_admin(ADMIN_ENV) is BootstrapOutcome.CREATED
    admin = accounts.store.find_user("root")
    assert admin is not None
    assert (admin.must_change_password, admin.bootstrap) == (True, Bootstrap.UNUSED)
    assert accounts.service.roles.assignments_for(admin.id) == (
        Assignment("platform_admin", ScopeType.PLATFORM, "*"),
    )
    assert admin.organization_id == "default"
    other = {ADMIN_USERNAME_ENV: "root", ADMIN_PASSWORD_ENV: "another long passphrase here"}
    assert accounts.service.bootstrap_admin(other) is BootstrapOutcome.EXISTS_WITH_VARIABLES
    assert accounts.service.bootstrap_admin({}) is BootstrapOutcome.EXISTS
    assert accounts.store.find_user("root") == admin  # untouched


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {ADMIN_USERNAME_ENV: "root"},
        {ADMIN_PASSWORD_ENV: "maple quarry eleven hounds"},
    ],
)
def test_without_an_admin_or_both_variables_it_refuses(
    accounts: Accounts, environ: dict[str, str]
) -> None:
    with pytest.raises(BootstrapError, match=f"{ADMIN_USERNAME_ENV} and {ADMIN_PASSWORD_ENV}"):
        accounts.service.bootstrap_admin(environ)


@pytest.mark.parametrize(
    ("password", "rule"),
    [("short", "at least 12"), ("root password is long", "username"), ("q1w2e3r4t5y6", "common")],
)
def test_the_bootstrap_password_must_pass_the_policy(
    accounts: Accounts, password: str, rule: str
) -> None:
    with pytest.raises(BootstrapError, match=rule) as excinfo:
        accounts.service.bootstrap_admin({ADMIN_USERNAME_ENV: "root", ADMIN_PASSWORD_ENV: password})
    assert password not in str(excinfo.value)
    assert not accounts.service.roles.platform_admin_exists()


def test_the_server_refuses_to_start_without_an_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ADMIN_USERNAME_ENV)
    monkeypatch.delenv(ADMIN_PASSWORD_ENV)
    with pytest.raises(BootstrapError, match="no platform_admin exists"):
        create_default_app()
    # It let go of the database: with the variables, the same file starts.
    monkeypatch.setenv(ADMIN_USERNAME_ENV, TEST_ADMIN_USERNAME)
    monkeypatch.setenv(ADMIN_PASSWORD_ENV, TEST_ADMIN_PASSWORD)
    with default_server():
        pass


def test_a_warning_names_the_variables_still_set_never_their_values(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Values that cannot appear in the message by accident.
    username, password = "rootward", "granite meadow ninety owls"
    monkeypatch.setenv(ADMIN_USERNAME_ENV, username)
    monkeypatch.setenv(ADMIN_PASSWORD_ENV, password)
    with default_server():  # creates the admin
        pass
    with caplog.at_level(logging.WARNING, logger="gateway"), default_server():
        pass
    [record] = [r for r in caplog.records if r.name == "gateway"]
    message = record.getMessage()
    assert f"{ADMIN_USERNAME_ENV} and {ADMIN_PASSWORD_ENV} are ignored" in message
    assert password not in message
    assert username not in message
    caplog.clear()
    monkeypatch.delenv(ADMIN_USERNAME_ENV)
    with caplog.at_level(logging.WARNING, logger="gateway"), default_server():
        pass
    [record] = [r for r in caplog.records if r.name == "gateway"]
    assert f"so {ADMIN_PASSWORD_ENV} is ignored" in record.getMessage()


async def test_the_bootstrap_password_works_for_one_login_only(accounts: Accounts) -> None:
    accounts.service.bootstrap_admin(ADMIN_ENV)
    password = ADMIN_ENV[ADMIN_PASSWORD_ENV]
    async with console(accounts) as c:
        first, sid, csrf = await http_login(c, "root", password)
        assert first.status_code == 200
        assert first.json()["user"]["must_change_password"] is True
        # The environment password is now spent, even before it is changed.
        again, *_ = await http_login(c, "root", password)
        assert again.status_code == 401
        # Until it is changed, the session reaches only /me, change-password and logout.
        assert sid is not None
        assert csrf is not None
        assert (await c.client.get("/v1/auth/me", headers=cookie(sid))).status_code == 200
        blocked = await c.client.get("/v1/auth/sessions", headers=cookie(sid))
        assert (blocked.status_code, blocked.json()["error"]["code"]) == (
            403,
            "password_change_required",
        )
        same = await c.client.post(
            "/v1/auth/change-password",
            json={"current_password": password, "new_password": password},
            headers=with_csrf(sid, csrf),
        )
        assert (same.status_code, same.json()["error"]["problem"]) == (400, "same_as_current")
        changed = await c.client.post(
            "/v1/auth/change-password",
            json={"current_password": password, "new_password": OTHER_PASSWORD},
            headers=with_csrf(sid, csrf),
        )
        assert changed.status_code == 200
        assert (await c.client.get("/v1/auth/sessions", headers=cookie(sid))).status_code == 200
        assert (await http_login(c, "root", OTHER_PASSWORD))[0].status_code == 200
        assert (await http_login(c, "root", password))[0].status_code == 401
    admin = accounts.store.find_user("root")
    assert admin is not None
    assert admin.bootstrap is Bootstrap.NONE
    assert not admin.must_change_password


# --- every refusal is the same ------------------------------------------------------------


def _raw(response: httpx.Response) -> tuple[int, list[tuple[str, str]], bytes]:
    return response.status_code, sorted(response.headers.multi_items()), response.content


async def _every_refusal(accounts: Accounts) -> dict[str, tuple[str, str]]:
    """Accounts set up for each way a login can be refused; returns (username, password)."""
    service = accounts.service
    service.create_user("wrongpw", PASSWORD, "north")
    service.create_user("disabled", PASSWORD, "north")
    service.disable_user("disabled")
    service.create_user("locked", PASSWORD, "north")
    for _ in range(5):
        with pytest.raises(LoginFailedError):
            await service.login("locked", "not the password at all")
    service.create_user("limited", PASSWORD, "north")
    for _ in range(5):  # the in-memory limiter alone, with the database lockout untouched
        service.limiter.record_failure(service.limiter.key("limited"))
    service.bootstrap_admin(ADMIN_ENV)
    await service.login("root", ADMIN_ENV[ADMIN_PASSWORD_ENV])
    return {
        "unknown username": ("nobody", PASSWORD),
        "malformed username": ("no such user!", PASSWORD),
        "wrong password": ("wrongpw", "not the password at all"),
        "disabled": ("disabled", PASSWORD),
        "locked, right password": ("locked", PASSWORD),
        "limited, right password": ("limited", PASSWORD),
        "used bootstrap password": ("root", ADMIN_ENV[ADMIN_PASSWORD_ENV]),
    }


async def test_every_refused_login_gets_the_same_bytes(accounts: Accounts) -> None:
    cases = await _every_refusal(accounts)
    async with console(accounts) as c:
        answers = {
            name: _raw((await http_login(c, *credentials))[0])
            for name, credentials in cases.items()
        }
    first = next(iter(answers.values()))
    assert first[0] == 401
    assert b"locked" not in first[2]
    for name, answer in answers.items():
        assert answer == first, name


async def test_every_login_takes_the_same_steps_whatever_is_wrong(
    accounts: Accounts, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Structural, not a timing test: one user lookup, then one verify against an argon2
    hash with the current parameters (the stored one, or the dummy), for every refusal and
    for a success. What the writes after that cost is measured in note 27."""
    cases = await _every_refusal(accounts)
    accounts.service.create_user("good", PASSWORD, "north")
    cases["success"] = ("good", PASSWORD)
    service = accounts.service
    steps: list[tuple[str, object]] = []
    real_find, real_verify = service.store.find_user, service._passwords.hasher.verify

    def find_user(username: str) -> Any:
        steps.append(("lookup", None))
        return real_find(username)

    def verify(stored: str, password: str) -> bool:
        steps.append(("verify", stored.rsplit("$", 2)[0]))  # the algorithm and parameters
        return real_verify(stored, password)

    monkeypatch.setattr(service.store, "find_user", find_user)
    monkeypatch.setattr(service._passwords.hasher, "verify", verify)
    expected = [("lookup", None), ("verify", "$argon2id$v=19$m=8,t=1,p=1")]
    for name, (username, password) in cases.items():
        steps.clear()
        try:
            await service.login(username, password)
        except LoginFailedError:
            assert name != "success"
        assert steps == expected, name


# --- lockout --------------------------------------------------------------------------------


async def _fail(accounts: Accounts, username: str, times: int) -> None:
    for _ in range(times):
        with pytest.raises(LoginFailedError):
            await accounts.service.login(username, "not the password at all")


async def test_five_failures_lock_for_fifteen_minutes(accounts: Accounts) -> None:
    accounts.service.create_user("alice", PASSWORD, "north")
    await _fail(accounts, "alice", 4)
    await accounts.clock.sleep_ms(LOCK_WINDOW_MS - 1)  # four failures, then nearly the window
    await _fail(accounts, "alice", 1)  # the fifth within the window: locked
    user = accounts.store.find_user("alice")
    assert user is not None
    assert user.locked_until_ms == accounts.epoch.epoch_ms() + LOCK_MS
    with pytest.raises(LoginFailedError):
        await accounts.service.login("alice", PASSWORD)  # the right password, still refused
    # It survives a restart: a new service on the same database.
    restarted = accounts.restart()
    with pytest.raises(LoginFailedError):
        await restarted.login("alice", PASSWORD)
    await accounts.clock.sleep_ms(LOCK_MS)
    await restarted.login("alice", PASSWORD)
    user = accounts.store.find_user("alice")
    assert user is not None
    assert (user.failed_logins, user.locked_until_ms) == (0, None)
    assert ("login_locked", "info", "user") in _actions(accounts)


async def test_failures_spread_over_more_than_the_window_do_not_lock(accounts: Accounts) -> None:
    accounts.service.create_user("alice", PASSWORD, "north")
    await _fail(accounts, "alice", 4)
    await accounts.clock.sleep_ms(LOCK_WINDOW_MS)
    await _fail(accounts, "alice", 4)  # a new window
    await accounts.service.login("alice", PASSWORD)


async def test_unlock_user_ends_a_lock_early(accounts: Accounts) -> None:
    accounts.service.create_user("alice", PASSWORD, "north")
    await _fail(accounts, "alice", 5)
    accounts.service.unlock_user("alice")
    # The in-memory limiter still counts those five (it is not in the database).
    accounts.service.limiter.clear(accounts.service.limiter.key("alice"))
    await accounts.service.login("alice", PASSWORD)
    assert ("user_unlocked", "success", "user") in _actions(accounts)


async def test_unknown_usernames_are_limited_like_real_ones(accounts: Accounts) -> None:
    service = accounts.service
    await _fail(accounts, "ghost", 5)
    assert service.limiter.blocked(service.limiter.key("ghost"))
    await accounts.clock.sleep_ms(LOCK_WINDOW_MS)
    assert not service.limiter.blocked(service.limiter.key("ghost"))


# --- re-hashing -----------------------------------------------------------------------------


async def test_a_login_upgrades_a_hash_made_with_other_parameters(accounts: Accounts) -> None:
    accounts.service.create_user("alice", PASSWORD, "north")
    user = accounts.store.find_user("alice")
    assert user is not None
    older = PasswordHasher(Argon2Parameters(time_cost=2, memory_cost_kib=16)).hash(PASSWORD)
    accounts.store.set_password_hash(user.id, older)
    await accounts.service.login("alice", PASSWORD)
    upgraded = accounts.store.find_user("alice")
    assert upgraded is not None
    assert upgraded.password_hash.startswith("$argon2id$v=19$m=8,t=1,p=1$")
    await accounts.service.login("alice", PASSWORD)  # it verifies, and needs no second upgrade
    logins = [e.event for e in accounts.audit.entries() if e.event.action is AuditAction.LOGIN]
    assert [e.details["password_rehashed"] for e in logins] == [True, False]


# --- sessions -------------------------------------------------------------------------------


async def test_sessions_expire_after_thirty_idle_minutes_and_twelve_hours(
    accounts: Accounts,
) -> None:
    accounts.service.create_user("alice", PASSWORD, "north")
    idle = await accounts.service.login("alice", PASSWORD)
    busy = await accounts.service.login("alice", PASSWORD)
    await accounts.clock.sleep_ms(SESSION_IDLE_MS - MINUTE_MS)
    accounts.service.authenticate(busy.session_id)
    await accounts.clock.sleep_ms(MINUTE_MS)
    with pytest.raises(SessionInvalidError):  # idle for 30 minutes
        accounts.service.authenticate(idle.session_id)
    # A session used every 20 minutes lives until its absolute expiry, and no longer.
    while accounts.epoch.epoch_ms() + 20 * MINUTE_MS < ORIGIN_MS + SESSION_ABSOLUTE_MS:
        await accounts.clock.sleep_ms(20 * MINUTE_MS)
        accounts.service.authenticate(busy.session_id)
    await accounts.clock.sleep_ms(ORIGIN_MS + SESSION_ABSOLUTE_MS - accounts.epoch.epoch_ms())
    with pytest.raises(SessionInvalidError):
        accounts.service.authenticate(busy.session_id)


async def test_last_seen_is_written_at_most_once_a_minute(
    accounts: Accounts, monkeypatch: pytest.MonkeyPatch
) -> None:
    accounts.service.create_user("alice", PASSWORD, "north")
    login = await accounts.service.login("alice", PASSWORD)
    writes: list[float] = []
    real_touch = accounts.store.touch_session

    def touch(id_hash: str, at_ms: float) -> None:
        writes.append(at_ms - ORIGIN_MS)
        real_touch(id_hash, at_ms)

    monkeypatch.setattr(accounts.store, "touch_session", touch)
    for _ in range(12):  # a request every 10 seconds for two minutes
        await accounts.clock.sleep_ms(10_000.0)
        accounts.service.authenticate(login.session_id)
    assert writes == [LAST_SEEN_WRITE_MS, 2 * LAST_SEEN_WRITE_MS]


async def test_a_login_never_reuses_a_session_it_was_sent(accounts: Accounts) -> None:
    accounts.service.create_user("alice", PASSWORD, "north")
    planted = "A" * 43  # what an attacker might set in the victim's browser
    async with console(accounts) as c:
        response, sid, _ = await http_login(c, "alice", PASSWORD, Cookie=f"gw_session={planted}")
        second, sid2, _ = await http_login(c, "alice", PASSWORD, Cookie=f"gw_session={sid}")
        assert response.status_code == second.status_code == 200
        assert sid not in (None, planted)
        assert sid2 not in (None, planted, sid)
        assert (await c.client.get("/v1/auth/me", headers=cookie(planted))).status_code == 401


def test_the_set_cookie_header_is_exactly_this() -> None:
    value = "A" * 43
    assert (
        session_cookie(value, secure=False)
        == f"gw_session={value}; HttpOnly; Path=/; SameSite=Strict"
    )
    assert session_cookie(value, secure=True) == (
        f"gw_session={value}; HttpOnly; Path=/; SameSite=Strict; Secure"
    )
    assert session_cookie("", secure=False, clear=True) == (
        "gw_session=; HttpOnly; Path=/; SameSite=Strict; Max-Age=0"
    )


@pytest.mark.parametrize("secure", [False, True])
async def test_login_sets_exactly_that_cookie(accounts: Accounts, secure: bool) -> None:
    accounts.service.create_user("alice", PASSWORD, "north")
    async with console(accounts, cookie_secure=secure) as c:
        response, sid, _ = await http_login(c, "alice", PASSWORD)
    assert sid is not None
    assert response.headers.get_list("set-cookie") == [session_cookie(sid, secure=secure)]
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(("value", "secure"), [(None, False), ("0", False), ("1", True)])
def test_the_default_server_marks_the_cookie_secure_only_when_asked(
    monkeypatch: pytest.MonkeyPatch, value: str | None, secure: bool
) -> None:
    if value is not None:
        monkeypatch.setenv(COOKIE_SECURE_ENV, value)
    with default_server() as app:
        assert app.state.accounts.cookie_secure is secure


# --- CSRF and Origin ------------------------------------------------------------------------


async def test_state_changing_requests_need_the_csrf_token(accounts: Accounts) -> None:
    accounts.service.create_user("alice", PASSWORD, "north")
    async with console(accounts) as c:
        _, sid, csrf = await http_login(c, "alice", PASSWORD)
        assert sid is not None
        assert csrf is not None
        me = (await c.client.get("/v1/auth/me", headers=cookie(sid))).json()
        assert me["csrf_token"] == csrf  # /me gives the same token again
        change = {"current_password": PASSWORD, "new_password": OTHER_PASSWORD}
        for headers in (cookie(sid), with_csrf(sid, "wrong"), with_csrf(sid, csrf[:-1] + "x")):
            for path, body in (("/v1/auth/logout", None), ("/v1/auth/change-password", change)):
                refused = await c.client.post(path, json=body, headers=headers)
                assert (refused.status_code, refused.json()["error"]["code"]) == (
                    403,
                    "csrf_failed",
                ), (path, headers)
        # Another session's token does not do either.
        _, other_sid, other_csrf = await http_login(c, "alice", PASSWORD)
        assert other_csrf is not None
        assert other_csrf != csrf
        refused = await c.client.post("/v1/auth/logout", headers=with_csrf(sid, other_csrf))
        assert refused.status_code == 403
        ok = await c.client.post("/v1/auth/logout", headers=with_csrf(sid, csrf))
        assert ok.status_code == 200
        assert ok.headers["set-cookie"] == session_cookie("", secure=False, clear=True)
        assert (await c.client.get("/v1/auth/me", headers=cookie(sid))).status_code == 401
        assert other_sid is not None
        assert (await c.client.get("/v1/auth/me", headers=cookie(other_sid))).status_code == 200


async def test_a_login_from_another_site_is_refused(accounts: Accounts) -> None:
    accounts.service.create_user("alice", PASSWORD, "north")
    async with console(accounts) as c:
        evil, *_ = await http_login(c, "alice", PASSWORD, Origin="https://evil.example")
        null, *_ = await http_login(c, "alice", PASSWORD, Origin="null")
        own, *_ = await http_login(c, "alice", PASSWORD, Origin="http://localhost:8000")
        form = await c.client.post(
            "/v1/auth/login",
            content="username=alice&password=x",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        rebound = await c.client.post(
            "/v1/auth/login",
            json={"username": "alice", "password": PASSWORD},
            headers={"Host": "attacker.example"},
        )
    assert (evil.status_code, evil.json()["error"]["code"]) == (403, "origin_not_allowed")
    assert null.status_code == 403
    assert own.status_code == 200
    assert form.status_code == 415
    assert (rebound.status_code, rebound.json()["error"]["code"]) == (403, "host_not_allowed")


# --- password change, disabling, the session list ------------------------------------------


async def test_a_password_change_revokes_the_other_sessions(accounts: Accounts) -> None:
    accounts.service.create_user("alice", PASSWORD, "north")
    async with console(accounts) as c:
        _, first, csrf = await http_login(c, "alice", PASSWORD)
        _, second, _ = await http_login(c, "alice", PASSWORD)
        assert first
        assert second
        assert csrf
        wrong = await c.client.post(
            "/v1/auth/change-password",
            json={"current_password": "not the password at all", "new_password": OTHER_PASSWORD},
            headers=with_csrf(first, csrf),
        )
        assert (wrong.status_code, wrong.json()["error"]["code"]) == (
            403,
            "current_password_incorrect",
        )
        weak = await c.client.post(
            "/v1/auth/change-password",
            json={"current_password": PASSWORD, "new_password": "alice123456789"},
            headers=with_csrf(first, csrf),
        )
        assert weak.json()["error"] == {
            "code": "password_rejected",
            "message": "A password may not contain the username.",
            "problem": "contains_username",
        }
        changed = await c.client.post(
            "/v1/auth/change-password",
            json={"current_password": PASSWORD, "new_password": OTHER_PASSWORD},
            headers=with_csrf(first, csrf),
        )
        assert changed.json() == {"changed": True, "sessions_revoked": 1}
        assert (await c.client.get("/v1/auth/me", headers=cookie(first))).status_code == 200
        assert (await c.client.get("/v1/auth/me", headers=cookie(second))).status_code == 401
    actions = _actions(accounts)
    assert ("password_changed", "success", "user") in actions
    assert ("password_change_failed", "failure", "user") in actions
    assert ("sessions_revoked", "success", "user") in actions


async def test_disabling_a_user_revokes_every_session(accounts: Accounts) -> None:
    accounts.service.create_user("alice", PASSWORD, "north")
    logins = [await accounts.service.login("alice", PASSWORD) for _ in range(3)]
    assert accounts.service.disable_user("alice") == 3
    for login in logins:
        with pytest.raises(SessionInvalidError):
            accounts.service.authenticate(login.session_id)
    accounts.service.enable_user("alice")
    for login in logins:  # enabling does not bring revoked sessions back
        with pytest.raises(SessionInvalidError):
            accounts.service.authenticate(login.session_id)


async def test_the_session_list_shows_only_ones_own_live_sessions(accounts: Accounts) -> None:
    ready_user(accounts, "alice", PASSWORD, "north")
    ready_user(accounts, "bob", PASSWORD, "north")
    async with console(accounts) as c:
        _, first, _ = await http_login(c, "alice", PASSWORD)
        _, second, csrf2 = await http_login(c, "alice", PASSWORD)
        await http_login(c, "bob", PASSWORD)
        assert first
        assert second
        assert csrf2
        listed = (await c.client.get("/v1/auth/sessions", headers=cookie(first))).json()
        await c.client.post("/v1/auth/logout", headers=with_csrf(second, csrf2))
        after = (await c.client.get("/v1/auth/sessions", headers=cookie(first))).json()
    assert [s["current"] for s in listed["sessions"]] == [True, False]
    assert set(listed["sessions"][0]) == {
        "handle",
        "created_at_ms",
        "last_seen_at_ms",
        "expires_at_ms",
        "current",
    }
    assert len(after["sessions"]) == 1
    text = json.dumps(listed)
    assert first not in text
    assert second not in text


async def test_nothing_else_takes_a_session_and_no_accounts_means_no_routes() -> None:
    accounts = make_accounts()
    accounts.service.create_user("alice", PASSWORD, "north")
    async with console(accounts) as c:
        _, sid, _ = await http_login(c, "alice", PASSWORD)
        assert sid is not None
        monitor = await c.client.get("/v1/models", headers=cookie(sid))
        assert monitor.status_code == 200  # public anyway; the cookie changes nothing
    clock = SimClock()
    registry = build_continuous_registry(clock, seed=7)
    app = create_app(registry, clock)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        missing = await client.post("/v1/auth/login", json={"username": "a", "password": "b"})
    await registry.aclose()
    assert missing.status_code == 404


# --- hashing does not move simulated time ---------------------------------------------------


async def test_a_login_does_not_let_simulated_time_move(accounts: Accounts) -> None:
    accounts.service.create_user("alice", PASSWORD, "north")
    woke: list[float] = []

    async def sleeper() -> None:
        await accounts.clock.sleep_ms(10.0)
        woke.append(accounts.clock.now_ms())

    pending = asyncio.create_task(sleeper())
    await accounts.clock.sleep_ms(0.0)
    login = await accounts.service.login("alice", PASSWORD)
    with pytest.raises(LoginFailedError):
        await accounts.service.login("alice", "not the password at all")
    with pytest.raises(LoginFailedError):
        await accounts.service.login("nobody-here", PASSWORD)
    context = accounts.service.authenticate(login.session_id)
    await accounts.service.change_password(context, PASSWORD, OTHER_PASSWORD)
    assert accounts.clock.now_ms() == 0.0
    assert woke == []
    await pending
    assert woke == [10.0]


# --- the audit log --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "details",
    [
        {"password": "x"},
        {"session_id": "x"},
        {"reason": "free text"},
        {"failures": "5"},
        {"organization_id": "has spaces in it"},
        {"failures": -1},
    ],
)
def test_audit_details_pass_only_through_the_allowlist(details: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"audit|not a valid Reason"):
        AuditEvent(AuditAction.LOGIN_FAILED, Outcome.FAILURE, TargetType.USER, details=details)


def test_an_audit_entry_names_only_ids() -> None:
    with pytest.raises(ValueError, match="user id"):
        AuditEvent(AuditAction.LOGIN, Outcome.SUCCESS, TargetType.USER, "alice")
    with pytest.raises(ValueError, match="no id"):
        AuditEvent(
            AuditAction.LOGIN_FAILED, Outcome.FAILURE, TargetType.UNKNOWN_USER, "usr_" + "0" * 16
        )


async def test_logins_and_their_failures_are_audited(accounts: Accounts) -> None:
    accounts.service.create_user("alice", PASSWORD, "north")
    await accounts.service.login("alice", PASSWORD)
    await _fail(accounts, "alice", 5)
    await _fail(accounts, "typed-into-the-wrong-box", 1)
    actions = _actions(accounts)
    assert actions[0] == ("user_created", "success", "user")
    assert ("login", "success", "session") in actions
    assert actions.count(("login_failed", "failure", "user")) == 5
    assert ("login_locked", "info", "user") in actions
    unknown = [
        e for e in accounts.audit.entries() if e.event.target_type is TargetType.UNKNOWN_USER
    ]
    assert [(e.event.target_id, e.event.details) for e in unknown] == [
        (None, {"reason": "unknown_user"})
    ]
    raw = accounts.connection.execute("SELECT * FROM audit_log").fetchall()
    assert "typed-into-the-wrong-box" not in repr(raw)


async def test_unknown_user_rows_are_capped_per_minute_and_the_drops_recorded() -> None:
    accounts = make_accounts(unknown_user_rows_per_minute=3)
    await _fail(accounts, "ghost", 7)
    rows = [e for e in accounts.audit.entries() if e.event.target_type is TargetType.UNKNOWN_USER]
    assert len(rows) == 3
    minute_start = math.floor(ORIGIN_MS / MINUTE_MS) * MINUTE_MS
    await accounts.clock.sleep_ms(MINUTE_MS)
    accounts.service.prune()  # the retention pass writes the count once the minute is over
    [dropped] = [e for e in accounts.audit.entries() if e.event.action is AuditAction.ROWS_DROPPED]
    assert dropped.event.details == {
        "reason": "unknown_user_cap",
        "dropped": 4,
        "minute_start_ms": minute_start,
    }
    assert dropped.at_ms == minute_start + MINUTE_MS


async def test_old_audit_entries_and_dead_sessions_are_pruned() -> None:
    accounts = make_accounts(audit_retention_ms=10 * MINUTE_MS)
    service = accounts.service
    service.create_user("alice", PASSWORD, "north")
    revoked = await service.login("alice", PASSWORD)
    service.logout(service.authenticate(revoked.session_id))
    idle = await service.login("alice", PASSWORD)
    await accounts.clock.sleep_ms(SESSION_IDLE_MS)
    live = await service.login("alice", PASSWORD)
    before = len(accounts.audit.entries())
    assert service.prune() == 2 + (before - 1)  # two dead sessions; all but the newest entry
    remaining = {row[0] for row in accounts.connection.execute("SELECT handle FROM sessions")}
    assert remaining == {live.session.handle}
    assert idle.session.handle not in remaining
    assert [e.event.action for e in accounts.audit.entries()] == [AuditAction.LOGIN]


def test_audit_retention_comes_from_the_environment() -> None:
    assert audit_retention_ms({}) == 400 * 24 * 60 * MINUTE_MS
    assert audit_retention_ms({"GATEWAY_AUDIT_RETENTION_DAYS": "30"}) == 30 * 24 * 60 * MINUTE_MS
    for bad in ("0", "-1", "soon"):
        with pytest.raises(ValueError, match="GATEWAY_AUDIT_RETENTION_DAYS"):
            audit_retention_ms({"GATEWAY_AUDIT_RETENTION_DAYS": bad})


def test_session_ids_are_256_bits(accounts: Accounts) -> None:
    login = asyncio.run(_login(accounts))
    assert len(login) == 43  # 32 bytes, base64url without padding
    assert Reason.LOGOUT.value == "logout"


async def _login(accounts: Accounts) -> str:
    accounts.service.create_user("alice", PASSWORD, "north")
    return (await accounts.service.login("alice", PASSWORD)).session_id
