"""The provider endpoints under /v1/admin/orgs/{org}/providers: who may do what, consume-on-use,
the key never returned, test-connection through the egress guard, key_unreadable, and the
documented table. See docs/32-provider-settings-and-vault.md. No network: the guard connects
through a scripted network. Test values, not secrets."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpcore
import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from gateway.apikeys import ApplicationAccess
from gateway.app import create_app, declaration_of
from gateway.audit import AuditAction
from gateway.auth import AccountAccess
from gateway.continuous import build_continuous_registry
from gateway.egress import organization_client
from gateway.localonly import LocalOnlyPolicy
from gateway.permissions import PERMISSIONS
from gateway.providers import (
    TESTS_PER_HOUR,
    TESTS_PER_ORGANIZATION_HOUR,
    ProviderConfig,
    ProviderService,
    ProviderStore,
    SqliteProviderStore,
)
from gateway.redaction import Redactor
from gateway.state import run_now
from gateway.vault import Vault, VaultIntegrityError
from mock_network import FakeResolver, Scripted, ScriptedNetwork, http_response, sse_response
from mock_providers import openai_text, openai_tools
from test_management_api import FORBIDDEN, World, make_world

REPO = Path(__file__).resolve().parent.parent
MASTER = bytes(range(32))
OTHER_MASTER = bytes(range(32, 64))
KEY = "sk-planted-provider-key-0123456789abcdefghij"
PUBLIC = "93.184.216.34"
HOST = "api.provider.test"
STEP_UP_REQUIRED = "step_up_required"


def seed_provider(connection: sqlite3.Connection, organization_id: str = "north") -> ProviderConfig:
    """A provider row written directly, for tests of the store and the CLI."""
    if (
        connection.execute(
            "SELECT 1 FROM organizations WHERE id = ?", (organization_id,)
        ).fetchone()
        is None
    ):
        connection.execute(
            "INSERT INTO organizations (id, name, kind, created_at_ms, disabled)"
            " VALUES (?, ?, 'business', 0, 0)",
            (organization_id, organization_id.title()),
        )
    config = ProviderConfig(
        "prv_00000000000000aa", organization_id, "openai_compatible", f"https://{HOST}/v1",
        "model-x", "standard", "fallback", True, True, None, None, {}, None, 0.0,
    )  # fmt: skip
    run_now(SqliteProviderStore(connection).add(config))
    return config


@dataclass
class Rig:
    world: World
    service: ProviderService
    network: ScriptedNetwork
    resolver: FakeResolver
    client: httpx.AsyncClient
    app: FastAPI

    @property
    def store(self) -> ProviderStore:
        return self.service.store

    async def login(self, username: str) -> tuple[str, str]:
        response = await self.client.post(
            "/v1/auth/login", json={"username": username, "password": "plum harbor ninety violet"}
        )
        self.client.cookies.clear()
        assert response.status_code == 200, response.text
        return response.headers["set-cookie"].split(";")[0].split("=", 1)[1], response.json()[
            "csrf_token"
        ]

    async def step_up(self, session: tuple[str, str]) -> None:
        response = await self.client.post(
            "/v1/auth/step-up",
            json={"password": "plum harbor ninety violet"},
            headers=_headers(session),
        )
        assert response.status_code == 200, response.text

    async def call(
        self, session: tuple[str, str], method: str, path: str, body: dict[str, Any] | None = None
    ) -> httpx.Response:
        return await self.client.request(
            method, "/v1/admin/orgs" + path, json=body, headers=_headers(session)
        )


def _headers(session: tuple[str, str]) -> dict[str, str]:
    return {"Cookie": f"gw_session={session[0]}", "X-CSRF-Token": session[1]}


@asynccontextmanager
async def rig(world: World | None = None, **service: Any) -> AsyncIterator[Rig]:
    world = world if world is not None else make_world()
    accounts = world.accounts
    network = ScriptedNetwork()
    resolver = FakeResolver({HOST: [PUBLIC], "inside.provider.test": ["10.0.0.7"]})
    providers = ProviderService(
        SqliteProviderStore(accounts.connection),
        Vault(MASTER),
        accounts.audit,
        accounts.clock,
        accounts.epoch,
        Redactor(),
        lambda: organization_client(resolver, inner=network),
        **service,
    )
    registry = build_continuous_registry(accounts.clock, seed=7)
    app = create_app(
        registry,
        accounts.clock,
        applications=ApplicationAccess(world.keys, world.templates),
        accounts=AccountAccess(accounts.service),
        local_only=LocalOnlyPolicy(),
        epoch=accounts.epoch,
        providers=providers,
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield Rig(world, providers, network, resolver, client, app)
    await registry.aclose()


NEW = {
    "type": "openai_compatible",
    "base_url": f"https://{HOST}/v1",
    "model": "model-x",
    "serves_tier": "standard",
}


async def _created(r: Rig, session: tuple[str, str], org: str = "north", **extra: Any) -> str:
    await r.step_up(session)
    made = await r.call(session, "POST", f"/{org}/providers", {**NEW, **extra})
    assert made.status_code == 201, made.text
    return str(made.json()["provider_id"])


async def _with_key(r: Rig, session: tuple[str, str], org: str = "north", **extra: Any) -> str:
    provider_id = await _created(r, session, org, **extra)
    await r.step_up(session)
    put = await r.call(session, "PUT", f"/{org}/providers/{provider_id}/key", {"key": KEY})
    assert put.status_code == 200, put.text
    return provider_id


# --- the documented table ----------------------------------------------------------------------


def documented() -> list[tuple[str, str, str, str]]:
    text = (REPO / "docs" / "32-provider-settings-and-vault.md").read_text(encoding="utf-8")
    pattern = re.compile(
        r"^\| `(GET|POST|PUT|PATCH|DELETE)` \| `([^`]+)` \| `([a-z_.]+)` \| "
        r"(yes|consume|) ?\| (\d{3}) \|$"
    )
    rows = []
    for line in text.splitlines():
        match = pattern.match(line.strip())
        if match:
            method, path, action, step_up, _ = match.groups()
            rows.append((method, path, action, step_up))
    return rows


async def test_the_routes_are_the_documented_endpoints() -> None:
    rows = documented()
    assert len(rows) == 10
    async with rig() as r:
        app = r.client._transport.app  # type: ignore[attr-defined]
        routes = set()
        for route in app.routes:
            if isinstance(route, APIRoute) and "/providers" in route.path:
                [declaration] = declaration_of(route)
                for method in route.methods or ():
                    routes.add(
                        (
                            method,
                            route.path.removeprefix("/v1/admin/orgs/{org}"),
                            declaration.action,
                        )
                    )
    assert routes == {(m, p, a) for m, p, a, _ in rows}
    for _, _, action, step_up in rows:
        permission = PERMISSIONS[action]
        assert (permission.step_up, permission.consume) == (step_up != "", step_up == "consume"), (
            action
        )


# --- who may do what ----------------------------------------------------------------------------


ENDPOINTS = [
    ("GET", "/north/providers", None),
    ("GET", "/north/providers/status", None),
    ("POST", "/north/providers", NEW),
    ("PATCH", "/north/providers/{id}", {"model": "model-y"}),
    ("POST", "/north/providers/{id}/disable", None),
    ("POST", "/north/providers/{id}/enable", None),
    ("DELETE", "/north/providers/{id}", None),
    ("PUT", "/north/providers/{id}/key", {"key": KEY}),
    ("DELETE", "/north/providers/{id}/key", None),
    ("POST", "/north/providers/{id}/test", None),
]


async def test_app_admins_viewers_and_users_without_a_role_get_nothing() -> None:
    async with rig() as r:
        provider_id = await _with_key(r, await r.login("alice"))
        for user in ("vera", "nina", "bob"):
            session = await r.login(user)
            await r.step_up(session)
            for method, path, body in ENDPOINTS:
                response = await r.call(session, method, path.replace("{id}", provider_id), body)
                assert (response.status_code, response.json()) == (403, FORBIDDEN), (
                    user,
                    method,
                    path,
                )
        assert r.network.connections == []


async def test_platform_admin_sees_existence_and_state_switches_and_never_a_key() -> None:
    async with rig() as r:
        alice = await r.login("alice")
        provider_id = await _with_key(r, alice)
        root = await r.login("root")
        await r.step_up(root)
        status = await r.call(root, "GET", "/north/providers/status")
        # Existence, the switch, and the slow-provider warning (docs/33): nothing else.
        assert status.json() == {
            "providers": [
                {
                    "provider_id": provider_id,
                    "enabled": True,
                    "slow_since_ms": None,
                    "consecutive_misses": 0,
                }
            ]
        }
        for method, path, body in ENDPOINTS:
            if path.endswith(("/status", "/disable", "/enable")):
                continue
            response = await r.call(root, method, path.replace("{id}", provider_id), body)
            assert (response.status_code, response.json()) == (403, FORBIDDEN), (method, path)
        # The platform's switch: org_admin cannot turn it back on.
        off = await r.call(root, "POST", f"/north/providers/{provider_id}/disable")
        assert off.json() == {"provider_id": provider_id, "enabled": False}
        await r.step_up(alice)
        mine = (await r.call(alice, "POST", f"/north/providers/{provider_id}/enable")).json()
        assert (mine["organization_switch"], mine["platform_switch"], mine["enabled"]) == (
            True,
            False,
            False,
        )
        await r.step_up(root)
        on = await r.call(root, "POST", f"/north/providers/{provider_id}/enable")
        assert on.json() == {"provider_id": provider_id, "enabled": True}
    switches = [
        e.event.details["switch"]
        for e in await r.world.accounts.audit.entries()
        if e.event.action in (AuditAction.PROVIDER_DISABLED, AuditAction.PROVIDER_ENABLED)
    ]
    assert switches == ["platform", "organization", "platform"]


async def test_nothing_crosses_organizations_and_missing_looks_forbidden() -> None:
    async with rig() as r:
        south = await _with_key(r, await r.login("sid"), "south")
        alice = await r.login("alice")
        for stepped_up in (False, True):
            if stepped_up:
                await r.step_up(alice)
            for method, path, body in ENDPOINTS:
                if "{id}" not in path:
                    continue
                answers = {
                    _raw(await r.call(alice, method, path.replace("{id}", ident), body))
                    for ident in (south, "prv_0000000000000000", "nonsense")
                }
                answers.add(
                    _raw(
                        await r.call(
                            alice,
                            method,
                            path.replace("/north/", "/south/").replace("{id}", south),
                            body,
                        )
                    )
                )
                assert len(answers) == 1, (method, path, answers)
                assert next(iter(answers))[0] == 403
        stored = await r.store.get_key(south)
        assert stored is not None  # untouched
        assert r.network.connections == []


def _raw(response: httpx.Response) -> tuple[int, bytes]:
    return response.status_code, response.content


# --- creating and changing ---------------------------------------------------------------------


async def test_what_an_organization_may_configure() -> None:
    async with rig() as r:
        alice = await r.login("alice")
        await r.step_up(alice)
        refused = {
            "ollama": {**NEW, "type": "ollama", "base_url": "https://ollama.example.com"},
            "http": {**NEW, "base_url": f"http://{HOST}/v1"},
            "localhost": {**NEW, "base_url": "https://localhost:11434"},
            "loopback": {**NEW, "base_url": "https://127.0.0.1/v1"},
            "private": {**NEW, "base_url": "https://10.1.2.3/v1"},
            "metadata": {**NEW, "base_url": "https://169.254.169.254/latest"},
            "extra field": {**NEW, "api_key": KEY},
            "tier": {**NEW, "serves_tier": "ultra"},
        }
        for name, body in refused.items():
            response = await r.call(alice, "POST", "/north/providers", body)
            assert response.status_code == 422, (name, response.text)
            assert KEY not in response.text
        made = await r.call(alice, "POST", "/north/providers", NEW)
        assert made.status_code == 201
        view = made.json()
        assert view["key"] == {
            "present": False,
            "key_unreadable": False,
            "last4": None,
            "updated_at_ms": None,
        }
        assert re.fullmatch(r"prv_[0-9a-f]{16}", view["provider_id"])
        again = await r.call(alice, "POST", "/north/providers", NEW)
        assert (again.status_code, again.json()["error"]["code"]) == (409, "provider_exists")


async def test_a_new_url_deletes_the_key_and_a_new_model_resets_capabilities() -> None:
    async with rig() as r:
        alice = await r.login("alice")
        provider_id = await _with_key(r, alice)
        config = await r.store.get(provider_id)
        assert config is not None
        await r.store.replace(replace(config, capabilities={"supports_tools": {"value": True}}))
        await r.step_up(alice)
        model = await r.call(
            alice, "PATCH", f"/north/providers/{provider_id}", {"model": "model-y"}
        )
        assert model.json()["key_deleted"] is False
        assert model.json()["capabilities"] == {}
        assert await r.store.get_key(provider_id) is not None
        moved = await r.call(
            alice,
            "PATCH",
            f"/north/providers/{provider_id}",
            {"base_url": "https://other.provider.test/v1"},
        )
        assert moved.json()["key_deleted"] is True
        assert await r.store.get_key(provider_id) is None
        bad = await r.call(
            alice,
            "PATCH",
            f"/north/providers/{provider_id}",
            {"base_url": "https://192.168.0.9/v1"},
        )
        assert bad.status_code == 422


async def test_a_new_url_or_type_deletes_the_key_in_the_same_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The key is deleted inside the transaction that changes the URL: when anything after
    the delete fails, neither the new URL nor the deletion is kept."""
    async with rig() as r:
        alice = await r.login("alice")
        provider_id = await _with_key(r, alice)
        before = await r.store.get(provider_id)
        assert before is not None
        delete_key = r.store.delete_key

        async def delete_then_fail(found: str) -> bool:
            assert await delete_key(found)  # the row is gone inside the transaction ...
            assert await r.store.get_key(found) is None
            raise RuntimeError("the audit write failed")

        monkeypatch.setattr(r.store, "delete_key", delete_then_fail)
        await r.step_up(alice)
        with pytest.raises(RuntimeError, match="audit write failed"):
            await r.call(
                alice,
                "PATCH",
                f"/north/providers/{provider_id}",
                {"base_url": "https://other.provider.test/v1"},
            )
        # ... and back after the rollback, with the old URL.
        assert await r.store.get(provider_id) == before
        assert await r.store.get_key(provider_id) is not None


@pytest.mark.parametrize(
    "change",
    [{"base_url": "https://other.provider.test/v1"}, {"type": "anthropic"}],
    ids=["url", "type"],
)
async def test_the_old_ciphertext_cannot_be_opened_for_the_new_origin_or_type(
    change: dict[str, str],
) -> None:
    async with rig() as r:
        alice = await r.login("alice")
        provider_id = await _with_key(r, alice)
        old = await r.store.get(provider_id)
        stored = await r.store.get_key(provider_id)
        assert old is not None
        assert stored is not None
        await r.step_up(alice)
        moved = await r.call(alice, "PATCH", f"/north/providers/{provider_id}", change)
        assert moved.json()["key_deleted"] is True
        assert await r.store.get_key(provider_id) is None
        new = await r.store.get(provider_id)
        assert new is not None
    vault = Vault(MASTER)
    assert vault.open(stored.sealed, old.binding).reveal() == KEY  # the ciphertext is sound
    with pytest.raises(VaultIntegrityError):
        vault.open(stored.sealed, new.binding)  # but bound to where it was entered


# --- keys and consume-on-use --------------------------------------------------------------------


async def test_the_documented_consume_sequence() -> None:
    """Note 32's table, row by row: step-up actions use the window and leave it open, consume
    actions close it, a refusal before any change gives it back."""
    async with rig() as r:
        alice = await r.login("alice")
        seen: list[tuple[int, str | None]] = []

        def note(response: httpx.Response) -> None:
            body = response.json()
            seen.append((response.status_code, (body.get("error") or {}).get("code")))

        await r.step_up(alice)  # 1
        made = await r.call(alice, "POST", "/north/providers", NEW)  # 2
        note(made)
        provider = f"/north/providers/{made.json()['provider_id']}"
        note(await r.call(alice, "PATCH", provider, {"model": "model-y"}))  # 3
        note(await r.call(alice, "PUT", provider + "/key", {"key": KEY}))  # 4
        note(await r.call(alice, "PUT", provider + "/key", {"key": KEY + "2"}))  # 5
        note(await r.call(alice, "PATCH", provider, {"model": "model-z"}))  # 6
        await r.step_up(alice)  # 7
        note(await r.call(alice, "PUT", provider + "/key", {"key": "abc"}))  # 8
        note(await r.call(alice, "PUT", provider + "/key", {"key": KEY + "3"}))  # 9
        note(await r.call(alice, "DELETE", provider + "/key"))  # 10
    assert seen == [
        (201, None),
        (200, None),
        (200, None),
        (403, STEP_UP_REQUIRED),
        (403, STEP_UP_REQUIRED),
        (422, "invalid_request"),
        (200, None),
        (403, STEP_UP_REQUIRED),
    ]


async def test_a_key_is_never_returned_and_each_change_needs_a_fresh_password() -> None:
    async with rig() as r:
        alice = await r.login("alice")
        provider_id = await _created(r, alice)
        path = f"/north/providers/{provider_id}/key"
        # The window opened to create the provider has passed: none is open.
        await r.world.accounts.clock.sleep_ms(r.world.accounts.service.step_up_ms)
        alice = await r.login("alice")
        refused = await r.call(alice, "PUT", path, {"key": KEY})
        assert refused.json()["error"]["code"] == STEP_UP_REQUIRED
        await r.step_up(alice)
        put = await r.call(alice, "PUT", path, {"key": KEY})
        assert put.status_code == 200
        assert set(put.json()) == {"last4", "updated_at_ms"}
        assert put.json()["last4"] == KEY[-4:]
        assert put.headers["cache-control"] == "no-store"
        # The same window, used up: a second change is refused.
        second = await r.call(alice, "PUT", path, {"key": KEY + "x"})
        assert second.json()["error"]["code"] == STEP_UP_REQUIRED
        listed = await r.call(alice, "GET", "/north/providers")
        assert KEY not in listed.text
        assert listed.json()["providers"][0]["key"]["present"] is True
        await r.step_up(alice)
        assert (await r.call(alice, "DELETE", path)).json() == {
            "provider_id": provider_id,
            "key_deleted": True,
        }
        assert await r.store.get_key(provider_id) is None
        assert (await r.call(alice, "DELETE", path)).json()["error"]["code"] == STEP_UP_REQUIRED
    consumed = [
        e.event.details.get("step_up_consumed")
        for e in await r.world.accounts.audit.entries()
        if e.event.action in (AuditAction.PROVIDER_KEY_SET, AuditAction.PROVIDER_KEY_DELETED)
    ]
    assert consumed == [True, True]


async def test_two_simultaneous_changes_with_one_window_give_exactly_one_success() -> None:
    async with rig() as r:
        alice = await r.login("alice")
        provider_id = await _created(r, alice)
        await r.step_up(alice)
        path = f"/north/providers/{provider_id}/key"
        answers = await asyncio.gather(
            r.call(alice, "PUT", path, {"key": KEY}), r.call(alice, "PUT", path, {"key": KEY + "y"})
        )
        outcomes = sorted((a.status_code, a.json().get("error", {}).get("code")) for a in answers)
        assert outcomes == [(200, None), (403, STEP_UP_REQUIRED)]


async def test_the_window_comes_back_only_if_nothing_was_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with rig() as r:
        alice = await r.login("alice")
        provider_id = await _created(r, alice)
        path = f"/north/providers/{provider_id}/key"
        await r.step_up(alice)
        # Refused before anything changed: the window is given back.
        short = await r.call(alice, "PUT", path, {"key": "short"})
        assert (short.status_code, short.json()["error"]["details"][0]["field"]) == (422, "key")
        no_key = await r.call(alice, "DELETE", path)
        assert (no_key.status_code, no_key.json()["error"]["code"]) == (409, "no_key")
        assert (await r.call(alice, "PUT", path, {"key": KEY})).status_code == 200
        # Failed after the change: the window stays used.
        await r.step_up(alice)

        def broken(_: object) -> object:
            raise RuntimeError("failed after the key was written")

        monkeypatch.setattr(r.service, "key_state", broken)
        with pytest.raises(RuntimeError):
            await r.call(alice, "PUT", path, {"key": KEY + "z"})
        monkeypatch.undo()
        assert await r.store.get_key(provider_id) is not None
        after = await r.call(alice, "PUT", path, {"key": KEY})
        assert after.json()["error"]["code"] == STEP_UP_REQUIRED


async def test_deleting_a_provider_deletes_its_key() -> None:
    async with rig() as r:
        alice = await r.login("alice")
        provider_id = await _with_key(r, alice)
        await r.step_up(alice)
        assert (await r.call(alice, "DELETE", f"/north/providers/{provider_id}")).status_code == 200
        assert await r.store.get(provider_id) is None
        assert await r.store.get_key(provider_id) is None
        count = r.world.accounts.connection.execute("SELECT count(*) FROM provider_keys").fetchone()
        assert count == (0,)


# --- test-connection ---------------------------------------------------------------------------


def _probe_replies(*, tools: bool = True) -> list[Scripted]:
    replies: list[Scripted] = [sse_response(openai_text("OK").chunks)]
    if tools:
        replies.append(sse_response(openai_tools(("call_1", "ping", ["{}"])).chunks))
        replies.append(sse_response(openai_text("pong").chunks))
    return replies


async def test_test_connection_records_capabilities_with_time_and_model() -> None:
    async with rig() as r:
        alice = await r.login("alice")
        provider_id = await _with_key(r, alice)
        r.network.responses = _probe_replies()
        tested = await r.call(alice, "POST", f"/north/providers/{provider_id}/test")
        assert tested.status_code == 200, tested.text
        capabilities = tested.json()["capabilities"]
        assert capabilities["supports_tools"]["value"] is True
        assert capabilities["supports_tools"]["source"] == "probed"
        assert capabilities["supports_tools"]["model"] == "model-x"
        assert capabilities["supports_streaming"]["value"] is True
        assert capabilities["context_window"] == {
            **capabilities["context_window"],
            "value": None,
            "source": "unknown",
        }
        assert KEY not in tested.text
        # Through the guard, to the checked address, with the key only in the request header.
        assert [c.address for c in r.network.connections] == [PUBLIC] * 3
        assert all(f"Bearer {KEY}".encode() in c.written for c in r.network.connections)
        stored = await r.store.get(provider_id)
        assert stored is not None
        assert stored.capabilities == capabilities
        # A new model: the old probe no longer applies.
        await r.step_up(alice)
        changed = await r.call(
            alice, "PATCH", f"/north/providers/{provider_id}", {"model": "model-z"}
        )
        assert changed.json()["capabilities"] == {}


async def test_test_connection_failures_are_mapped_and_never_echo_the_key() -> None:
    async with rig() as r:
        alice = await r.login("alice")
        no_key = await _created(r, alice)
        response = await r.call(alice, "POST", f"/north/providers/{no_key}/test")
        assert (response.status_code, response.json()["error"]["code"]) == (409, "no_key")
        provider_id = await _created(r, alice, model="model-with-key")
        await r.step_up(alice)
        await r.call(alice, "PUT", f"/north/providers/{provider_id}/key", {"key": KEY})
        echo = json.dumps({"error": {"message": f"Incorrect API key provided: {KEY}"}}).encode()
        r.network.responses = [
            http_response(echo, "401 Unauthorized", Content_Type="application/json")
        ]
        failed = await r.call(alice, "POST", f"/north/providers/{provider_id}/test")
        assert (failed.status_code, failed.json()["error"]["code"]) == (502, "provider_auth_failed")
        assert KEY not in failed.text
    outcomes = [
        e.event.details["outcome_code"]
        for e in await r.world.accounts.audit.entries()
        if e.event.action is AuditAction.PROVIDER_TESTED
    ]
    assert outcomes == ["no_key", "provider_auth_failed"]


async def test_test_connection_cannot_reach_inside() -> None:
    async with rig() as r:
        alice = await r.login("alice")
        provider_id = await _created(r, alice)
        await r.step_up(alice)
        await r.call(alice, "PUT", f"/north/providers/{provider_id}/key", {"key": KEY})
        # The name resolves to a private address now (it was public when entered).
        r.resolver.table[HOST] = ["10.0.0.7"]
        r.network.responses = _probe_replies()
        refused = await r.call(alice, "POST", f"/north/providers/{provider_id}/test")
        assert (refused.status_code, refused.json()["error"]["code"]) == (
            502,
            "provider_unreachable",
        )
        assert r.network.connections == []


UNREACHED = (
    b'{"error":{"code":"provider_unreachable","message":"The provider could not be reached."}}'
)


async def test_test_connection_is_not_a_network_scanner() -> None:
    """A blocked address, a refused connection, a timeout and a name that does not resolve
    answer the same status and the same bytes; which it was is in the audit log only, and
    never the key. A host that answered gets its code and fixed message, nothing from its
    body."""
    async with rig() as r:
        alice = await r.login("alice")
        provider_id = await _with_key(r, alice)
        answers = []

        r.resolver.table[HOST] = ["10.0.0.7"]  # blocked: a private address
        answers.append(await r.call(alice, "POST", f"/north/providers/{provider_id}/test"))
        r.resolver.table[HOST] = [PUBLIC]
        r.network.failure = httpcore.ConnectError("[Errno 111] Connection refused")
        answers.append(await r.call(alice, "POST", f"/north/providers/{provider_id}/test"))
        r.network.failure = httpcore.ConnectTimeout("timed out")
        answers.append(await r.call(alice, "POST", f"/north/providers/{provider_id}/test"))
        r.network.failure = None
        r.resolver.missing.add(HOST)  # the name does not resolve
        answers.append(await r.call(alice, "POST", f"/north/providers/{provider_id}/test"))

        assert [a.status_code for a in answers] == [502] * 4
        assert [a.content for a in answers] == [UNREACHED] * 4
        assert [dict(a.headers) for a in answers[1:]] == [dict(answers[0].headers)] * 3

        # A host that answered, with its own words in the body: the code and fixed message.
        r.resolver.missing.clear()
        said = json.dumps(
            {"error": {"type": "invalid_request_error", "message": f"no such model; {KEY}"}}
        ).encode()
        r.network.responses = [
            http_response(said, "400 Bad Request", Content_Type="application/json")
        ]
        answered = await r.call(alice, "POST", f"/north/providers/{provider_id}/test")
    assert answered.status_code == 502
    assert answered.json() == {
        "error": {
            "code": "upstream_rejected",
            "message": "The model provider rejected the request.",
        }
    }
    tested = [
        e.event.details
        for e in await r.world.accounts.audit.entries()
        if e.event.action is AuditAction.PROVIDER_TESTED
    ]
    assert [(d["outcome_code"], d.get("outcome_detail")) for d in tested] == [
        ("egress_refused", "private"),
        ("upstream_unavailable", "connect"),
        ("upstream_timeout", "connect"),
        ("upstream_unavailable", "dns_failure"),
        ("upstream_rejected", None),  # nothing derived from the host's body
    ]
    logged = json.dumps([e.event.details for e in await r.world.accounts.audit.entries()])
    assert KEY not in logged
    assert "no such model" not in logged


async def test_test_connection_is_limited_per_organization_too() -> None:
    """One provider per address would get round the per-provider limit."""
    async with rig(tests_per_hour=2, tests_per_organization_hour=3) as r:
        alice = await r.login("alice")
        first = await _with_key(r, alice)
        second = await _with_key(r, alice, model="model-second")
        statuses = []
        for provider_id in (first, first, second, second):
            r.network.responses = _probe_replies()
            statuses.append(
                (await r.call(alice, "POST", f"/north/providers/{provider_id}/test")).status_code
            )
        refused = await r.call(alice, "POST", f"/north/providers/{second}/test")
    assert statuses == [200, 200, 200, 429]
    assert refused.json()["error"] == {
        "code": "test_rate_limited",
        "message": "at most 3 tests of an organization's providers an hour; try again later",
    }


def test_the_rate_limit_values() -> None:
    assert (TESTS_PER_HOUR, TESTS_PER_ORGANIZATION_HOUR) == (5, 20)


async def test_test_connection_is_rate_limited() -> None:
    async with rig(tests_per_hour=2) as r:
        alice = await r.login("alice")
        provider_id = await _with_key(r, alice)
        statuses = []
        for _ in range(3):
            r.network.responses = _probe_replies()
            statuses.append(
                (await r.call(alice, "POST", f"/north/providers/{provider_id}/test")).status_code
            )
        assert statuses == [200, 200, 429]
        await r.world.accounts.clock.sleep_ms(60 * 60 * 1000.0)
        alice = await r.login("alice")
        r.network.responses = _probe_replies()
        assert (
            await r.call(alice, "POST", f"/north/providers/{provider_id}/test")
        ).status_code == 200


async def test_an_unreadable_key_is_shown_logged_and_never_used(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="gateway.providers")
    async with rig() as r:
        alice = await r.login("alice")
        provider_id = await _created(r, alice)
        config = await r.store.get(provider_id)
        assert config is not None
        # Sealed under a master key the gateway does not have (a lost one).
        await r.store.put_key(provider_id, Vault(OTHER_MASTER).seal(KEY, config.binding), 1.0)
        assert await r.service.warn_unreadable() == 1
        listed = (await r.call(alice, "GET", "/north/providers")).json()["providers"][0]
        assert listed["key"]["key_unreadable"] is True
        for _ in range(2):
            r.network.responses = _probe_replies()
            refused = await r.call(alice, "POST", f"/north/providers/{provider_id}/test")
            assert (refused.status_code, refused.json()["error"]["code"]) == (409, "key_unreadable")
        assert r.network.connections == []
    warnings = [rec.getMessage() for rec in caplog.records]
    assert len(warnings) == 2  # once at startup, once at first use
    assert all(provider_id in w and KEY not in w for w in warnings)


async def test_an_expired_window_cannot_be_used_up() -> None:
    """The same session, its window past (the session itself still live): refused, and the
    stale window is not taken."""
    async with rig() as r:
        alice = await r.login("alice")
        provider_id = await _created(r, alice)
        await r.step_up(alice)
        await r.world.accounts.clock.sleep_ms(r.world.accounts.service.step_up_ms)
        late = await r.call(alice, "PUT", f"/north/providers/{provider_id}/key", {"key": KEY})
        assert late.json()["error"]["code"] == STEP_UP_REQUIRED
        assert await r.store.get_key(provider_id) is None
