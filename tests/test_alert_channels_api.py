"""The alert channel endpoints of A4's group 1 (docs/46): every route and its action as
documented; step-up on creating, changing, re-enabling and rotating, used up once and given back
when nothing changed; disabling with none; only the URL's origin ever shown; the cap of 20,
unique names and versions; nothing across organizations and missing equal to forbidden on every
endpoint; only org_admins; a rule's channels of its own organization (M15); the
``rule_weakened`` (M29) and ``channel_removed`` notices queued in their write's transaction; and
the leak scan of the URL and the signing keys with its planted controls.

The world of ``test_management_api``: north (alice and carol org_admins, vera viewer, nina
app_admin of north-shop, bob no role) and south (sid org_admin). Test values, not secrets."""

from __future__ import annotations

import json
import logging
import random
import re
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.routing import APIRoute

from accounts_helpers import PASSWORD
from gateway.alertrules import SqliteRuleStore
from gateway.alerts.channels import (
    KEY_KIND,
    PREVIOUS_KIND,
    URL_KIND,
    ChannelService,
    SqliteChannelStore,
    binding_of,
)
from gateway.alerts.notices import Notices
from gateway.alerts.outbox import SqliteOutboxStore
from gateway.apikeys import ApplicationAccess
from gateway.app import create_app, declaration_of
from gateway.auth import AccountAccess
from gateway.continuous import build_continuous_registry
from gateway.incidents import SqliteIncidentStore
from gateway.localonly import LocalOnlyPolicy
from gateway.permissions import PERMISSIONS
from gateway.vault import Vault
from test_management_api import World, make_world

REPO = Path(__file__).resolve().parent.parent
FORBIDDEN_BYTES = b'{"error":{"code":"forbidden","message":"Not permitted, or not found."}}'
STEP_UP_REQUIRED = "step_up_required"
MASTER = bytes(range(32))
TOKEN = "planted-path-token-7c1e40"
QUERY = "planted-query-token-5b2a91"
URL = f"https://hooks.example.com/services/{TOKEN}?token={QUERY}"
OTHER_TOKEN = "planted-second-token-a83f"
OTHER_URL = f"https://other.example.net/hook/{OTHER_TOKEN}"
RULE = {
    "name": "Errors",
    "signal": "error_rate",
    "comparison": ">",
    "threshold": 5,
    "window": 5,
    "for_minutes": 0,
    "severity": "critical",
}
Session = tuple[str, str]


@dataclass(frozen=True)
class Endpoint:
    method: str
    path: str
    action: str
    success: int


def documented_endpoints() -> list[Endpoint]:
    text = (REPO / "docs" / "46-alert-channels.md").read_text(encoding="utf-8")
    pattern = re.compile(
        r"^\| `(GET|POST|PATCH|DELETE)` \| `([^`]+)` \| `([a-z_.]+)` \| (\d{3}) \|$"
    )
    return [
        Endpoint(m.group(1), "/{org}" + m.group(2), m.group(3), int(m.group(4)))
        for m in (pattern.match(line.strip()) for line in text.splitlines())
        if m
    ]


ENDPOINTS = documented_endpoints()


@dataclass
class Console:
    client: httpx.AsyncClient
    world: World
    app: Any
    service: ChannelService

    @property
    def db(self) -> sqlite3.Connection:
        return self.world.accounts.connection

    async def login(self, username: str) -> Session:
        response = await self.client.post(
            "/v1/auth/login", json={"username": username, "password": PASSWORD}
        )
        self.client.cookies.clear()
        assert response.status_code == 200, response.text
        sid = response.headers["set-cookie"].split(";")[0].split("=", 1)[1]
        return sid, response.json()["csrf_token"]

    async def step_up(self, session: Session) -> None:
        answer = await self.call(session, "POST", "/v1/auth/step-up", {"password": PASSWORD})
        assert answer.status_code == 200, answer.text

    async def call(
        self, session: Session, method: str, path: str, body: dict[str, Any] | None = None
    ) -> httpx.Response:
        headers = {"Cookie": f"gw_session={session[0]}", "X-CSRF-Token": session[1]}
        url = path if path.startswith("/v1/auth") else "/v1/admin/orgs" + path
        if method in ("GET", "DELETE") and body is None:
            return await self.client.request(method, url, headers=headers)
        return await self.client.request(method, url, json=body or {}, headers=headers)

    async def create(
        self, session: Session, name: str = "Ops", url: str = URL, default: bool = True,
        org: str = "north",
    ) -> dict[str, Any]:  # fmt: skip
        await self.step_up(session)
        made = await self.call(
            session, "POST", f"/{org}/alert-channels",
            {"type": "webhook", "name": name, "url": url, "default": default},
        )  # fmt: skip
        assert made.status_code == 201, made.text
        return dict(made.json())

    def deliveries(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT channel_id, state, last_code, payload FROM notification_deliveries"
            " ORDER BY rowid"  # the order they were queued in
        ).fetchall()
        return [
            {"channel": c, "state": s, "code": code, "payload": json.loads(p) if p else None}
            for c, s, code, p in rows
        ]


@asynccontextmanager
async def console(seed: int = 5) -> AsyncIterator[Console]:
    world = make_world()
    accounts = world.accounts
    connection = accounts.connection
    store = SqliteChannelStore(connection)
    notices = Notices(
        store, SqliteOutboxStore(connection), accounts.organizations, accounts.epoch,
        random.Random(seed + 1).randbytes,
    )  # fmt: skip
    service = ChannelService(
        store, Vault(MASTER), accounts.service.audit, accounts.epoch,
        random.Random(seed).randbytes, notices=notices,
    )  # fmt: skip
    registry = build_continuous_registry(accounts.clock, seed=7)
    app = create_app(
        registry,
        accounts.clock,
        applications=ApplicationAccess(world.keys, world.templates),
        accounts=AccountAccess(accounts.service),
        local_only=LocalOnlyPolicy(),
        epoch=accounts.epoch,
        rules=SqliteRuleStore(connection),
        incidents=SqliteIncidentStore(connection, accounts.epoch),
        channels=service,
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield Console(client, world, app, service)
    await registry.aclose()


def _path(endpoint: Endpoint, org: str, channel: str = "chn_00000000000000ff") -> str:
    return (
        endpoint.path.replace("/{org}", f"/{org}")
        .replace("{channel}", channel)
        .replace("{app}", "north-shop" if org == "north" else "south-shop")
    )


# --- the routes ---------------------------------------------------------------------------------


async def test_each_channel_route_is_documented_and_declares_its_action() -> None:
    assert len(ENDPOINTS) == 9
    async with console() as c:
        declared = {}
        for route in c.app.routes:
            if isinstance(route, APIRoute) and "alert-channels" in route.path:
                assert route.methods is not None
                [method] = route.methods
                actions = {d.action for d in declaration_of(route) if d.kind == "requires"}
                declared[(method, route.path)] = actions
    documented = {
        (e.method, "/v1/admin/orgs" + e.path): {e.action} for e in ENDPOINTS
    }  # fmt: skip
    assert declared == documented
    assert all(e.action in PERMISSIONS for e in ENDPOINTS)


# --- creating, changing, disabling, re-enabling, rotating ---------------------------------------


async def test_creating_needs_a_step_up_used_once_and_shows_the_key_once() -> None:
    async with console() as c:
        alice = await c.login("alice")
        body = {"type": "webhook", "name": "Ops", "url": URL, "default": True}
        refused = await c.call(alice, "POST", "/north/alert-channels", body)
        assert (refused.status_code, refused.json()["error"]["code"]) == (403, STEP_UP_REQUIRED)
        await c.step_up(alice)
        made = await c.call(alice, "POST", "/north/alert-channels", body)
        assert made.status_code == 201, made.text
        assert made.headers["cache-control"] == "no-store"
        found = made.json()
        assert re.fullmatch(r"[0-9a-f]{64}", found["signing_key"])
        assert found["url_origin"] == "https://hooks.example.com"
        assert (found["enabled"], found["default"], found["version"]) == (True, True, 1)
        assert TOKEN not in made.text
        assert QUERY not in made.text
        # Used up: a second change needs a new password.
        again = await c.call(alice, "POST", "/north/alert-channels", {**body, "name": "Two"})
        assert (again.status_code, again.json()["error"]["code"]) == (403, STEP_UP_REQUIRED)
        # Never shown again.
        shown = [
            await c.call(alice, "GET", "/north/alert-channels"),
            await c.call(alice, "GET", f"/north/alert-channels/{found['id']}"),
        ]
        for answer in shown:
            assert answer.status_code == 200
            assert "signing_key" not in answer.text
            assert found["signing_key"] not in answer.text
            assert TOKEN not in answer.text
            assert QUERY not in answer.text


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"url": "http://hooks.example.com/x"}, "url"),
        ({"url": "https://127.0.0.1/x"}, "url"),
        ({"url": f"https://hooks.example.com/{TOKEN}\r\nX-Injected: 1"}, "url"),
        ({"url": f"https://hooks.example.com:6379/{TOKEN}"}, "url"),
        ({"url": "https://2130706433/x"}, "url"),
        ({"name": " Ops"}, "name"),
        ({"name": 'Ops"'}, "name"),
        ({"type": "email"}, "type"),
    ],
)
async def test_a_refused_body_names_its_field_and_gives_the_window_back(
    body: dict[str, Any], field: str
) -> None:
    async with console() as c:
        alice = await c.login("alice")
        await c.step_up(alice)
        full = {"type": "webhook", "name": "Ops", "url": URL, "default": True, **body}
        refused = await c.call(alice, "POST", "/north/alert-channels", full)
        assert refused.status_code == 422, refused.text
        assert refused.json()["error"]["details"][0]["field"] == field
        assert TOKEN not in refused.text
        assert "127.0.0.1" not in refused.text
        # Nothing was changed, so the window is still open for the next change.
        made = await c.call(
            alice, "POST", "/north/alert-channels",
            {"type": "webhook", "name": "Ops", "url": URL, "default": True},
        )  # fmt: skip
        assert made.status_code == 201, made.text


async def test_changes_are_versioned_and_one_that_changes_nothing_gives_the_window_back() -> None:
    async with console() as c:
        alice = await c.login("alice")
        made = await c.create(alice)
        path = f"/north/alert-channels/{made['id']}"
        await c.step_up(alice)
        renamed = await c.call(alice, "PATCH", path, {"expected_version": 1, "name": "Ops 2"})
        assert (renamed.status_code, renamed.json()["version"]) == (200, 2), renamed.text
        assert renamed.json()["changed"] is True
        await c.step_up(alice)
        same = await c.call(alice, "PATCH", path, {"expected_version": 2, "name": "Ops 2"})
        assert (same.status_code, same.json()["changed"], same.json()["version"]) == (200, False, 2)
        # Given back: the next change needs no new password.
        moved = await c.call(alice, "PATCH", path, {"expected_version": 2, "url": OTHER_URL})
        assert moved.status_code == 200, moved.text
        assert moved.json()["url_origin"] == "https://other.example.net"
        assert OTHER_TOKEN not in moved.text
        await c.step_up(alice)
        stale = await c.call(alice, "PATCH", path, {"expected_version": 2, "name": "Ops 3"})
        assert (stale.status_code, stale.json()["error"]["code"]) == (
            409, "channel_version_conflict",
        )  # fmt: skip
        await c.create(alice, name="Pager", default=False)
        await c.step_up(alice)
        taken = await c.call(alice, "PATCH", path, {"expected_version": 3, "name": "Pager"})
        assert (taken.status_code, taken.json()["error"]["code"]) == (409, "channel_name_taken")
        await c.step_up(alice)
        duplicate = await c.call(
            alice, "POST", "/north/alert-channels",
            {"type": "webhook", "name": "Pager", "url": URL},
        )  # fmt: skip
        assert (duplicate.status_code, duplicate.json()["error"]["code"]) == (
            409, "channel_name_taken",
        )  # fmt: skip


async def test_a_new_origin_keeps_the_signing_keys_rebound_to_it() -> None:
    async with console() as c:
        alice = await c.login("alice")
        made = await c.create(alice)
        await c.step_up(alice)
        path = f"/north/alert-channels/{made['id']}"
        assert (
            await c.call(alice, "PATCH", path, {"expected_version": 1, "url": OTHER_URL})
        ).is_success
        channel = await c.service.store.get_channel(made["id"])
        assert channel is not None
        secrets = await c.service.store.secrets(made["id"])
        vault = c.service.vault
        key = vault.open(secrets[KEY_KIND].sealed, binding_of(channel, KEY_KIND)).reveal()
        assert key == made["signing_key"]
        url = vault.open(secrets[URL_KIND].sealed, binding_of(channel, URL_KIND)).reveal()
        assert url == OTHER_URL


async def test_disabling_needs_no_step_up_and_re_enabling_needs_it() -> None:
    async with console() as c:
        alice = await c.login("alice")
        made = await c.create(alice)
        path = f"/north/alert-channels/{made['id']}"
        off = await c.call(alice, "POST", path + "/disable")
        assert (off.status_code, off.json()["enabled"]) == (200, False), off.text
        # Disabling through PATCH is refused (A4-16): the window it claimed is given back.
        await c.step_up(alice)
        refused = await c.call(alice, "PATCH", path, {"expected_version": 2, "enabled": False})
        assert refused.status_code == 422
        assert refused.json()["error"]["details"][0]["field"] == "enabled"
        carol = await c.login("carol")  # a session with no window
        needs = await c.call(carol, "PATCH", path, {"expected_version": 2, "enabled": True})
        assert (needs.status_code, needs.json()["error"]["code"]) == (403, STEP_UP_REQUIRED)
        await c.step_up(carol)
        on = await c.call(carol, "PATCH", path, {"expected_version": 2, "enabled": True})
        assert (on.status_code, on.json()["enabled"], on.json()["version"]) == (200, True, 3)
        actions = [
            r[0]
            for r in c.db.execute(
                "SELECT action FROM audit_log WHERE target_id = ? ORDER BY seq", (made["id"],)
            )
        ]
        assert actions == [
            "alert_channel_created", "alert_channel_disabled", "alert_channel_enabled",
        ]  # fmt: skip


async def test_rotation_keeps_the_previous_key_for_24_hours() -> None:
    async with console() as c:
        alice = await c.login("alice")
        made = await c.create(alice)
        path = f"/north/alert-channels/{made['id']}/rotate-key"
        refused = await c.call(alice, "POST", path)
        assert (refused.status_code, refused.json()["error"]["code"]) == (403, STEP_UP_REQUIRED)
        await c.step_up(alice)
        now = c.world.accounts.epoch.epoch_ms()
        rotated = await c.call(alice, "POST", path)
        assert rotated.status_code == 200, rotated.text
        new_key = rotated.json()["signing_key"]
        assert re.fullmatch(r"[0-9a-f]{64}", new_key)
        assert new_key != made["signing_key"]
        channel = await c.service.store.get_channel(made["id"])
        assert channel is not None
        secrets = await c.service.store.secrets(made["id"])
        vault = c.service.vault
        current = vault.open(secrets[KEY_KIND].sealed, binding_of(channel, KEY_KIND)).reveal()
        previous = secrets[PREVIOUS_KIND]
        old = vault.open(previous.sealed, binding_of(channel, PREVIOUS_KIND)).reveal()
        assert (current, old) == (new_key, made["signing_key"])
        assert previous.expires_at_ms == now + 24 * 3_600_000.0


async def test_an_organization_has_at_most_twenty_channels() -> None:
    async with console() as c:
        alice = await c.login("alice")
        for n in range(20):
            await c.create(alice, name=f"Hook {n}", default=False)
        await c.step_up(alice)
        body = {"type": "webhook", "name": "Hook 20", "url": URL}
        full = await c.call(alice, "POST", "/north/alert-channels", body)
        assert (full.status_code, full.json()["error"]["code"]) == (409, "channel_limit")
        # South has its own twenty.
        sid = await c.login("sid")
        await c.create(sid, org="south")
        # The refusal gave the window back.
        await c.call(alice, "DELETE", "/north/alert-channels/" + (
            await c.call(alice, "GET", "/north/alert-channels?limit=1")
        ).json()["channels"][0]["id"])  # fmt: skip
        assert (await c.call(alice, "POST", "/north/alert-channels", body)).status_code == 201


# --- who may, and nothing across organizations ---------------------------------------------------


async def test_nothing_crosses_organizations_and_missing_looks_forbidden() -> None:
    async with console() as c:
        alice, sid = await c.login("alice"), await c.login("sid")
        north = (await c.create(alice))["id"]
        south = (await c.create(sid, org="south"))["id"]
        checked = 0
        for endpoint in ENDPOINTS:
            body = {"expected_version": 1, "name": "X"} if endpoint.method == "PATCH" else None
            if endpoint.method == "POST" and "{channel}" not in endpoint.path:
                body = {"type": "webhook", "name": "X", "url": URL}
            tries = [
                (sid, _path(endpoint, "north", north)),  # another organization's
                (alice, _path(endpoint, "south", south)),
                (alice, _path(endpoint, "north", south)),  # in the wrong organization's path
            ]
            if "{channel}" in endpoint.path:
                tries += [
                    (alice, _path(endpoint, "north", "chn_00000000000000ff")),  # missing
                    (alice, _path(endpoint, "north", "not-a-channel-id")),
                ]
            for who, path in tries:
                if "{channel}" not in endpoint.path and who is alice and "/north" in path:
                    continue  # alice's own organization-level routes are hers
                answer = await c.call(who, endpoint.method, path, body)
                assert (answer.status_code, answer.content) == (403, FORBIDDEN_BYTES), (
                    endpoint, path, answer.text,
                )  # fmt: skip
                checked += 1
        assert checked == 36  # 6 channel routes x 5 tries, 3 organization-level x 2
        # And both channels are as they were.
        assert (await c.service.store.get_channel(north)) is not None
        assert (await c.service.store.get_channel(south)) is not None


async def test_only_org_admins_reach_the_channel_endpoints() -> None:
    async with console() as c:
        alice = await c.login("alice")
        made = (await c.create(alice))["id"]
        for username in ("vera", "nina", "bob", "root"):
            session = await c.login(username)
            for endpoint in ENDPOINTS:
                if endpoint.action == "alert_rules.read":
                    continue
                answer = await c.call(session, endpoint.method, _path(endpoint, "north", made))
                assert (answer.status_code, answer.content) == (403, FORBIDDEN_BYTES), (
                    username, endpoint,
                )  # fmt: skip


async def test_an_applications_readers_see_names_and_never_a_target() -> None:
    async with console() as c:
        alice = await c.login("alice")
        made = await c.create(alice)
        for username in ("nina", "vera", "alice"):
            listed = await c.call(
                await c.login(username), "GET", "/north/applications/north-shop/alert-channels"
            )
            assert listed.status_code == 200, (username, listed.text)
            assert listed.json() == {
                "channels": [
                    {"id": made["id"], "name": "Ops", "type": "webhook", "default": True,
                     "enabled": True},
                ]
            }  # fmt: skip
            assert "hooks.example.com" not in listed.text
        # nina's application role reaches only her own application.
        other = await c.call(
            await c.login("nina"), "GET", "/north/applications/north-blog/alert-channels"
        )
        assert (other.status_code, other.content) == (403, FORBIDDEN_BYTES)


# --- a rule's channels, and the notices --------------------------------------------------------

RULES = "/north/applications/north-shop/alert-rules"


async def test_a_rule_names_only_its_own_organizations_channels() -> None:
    """M15: an id of another organization's channel is refused exactly as one that does not
    exist, at create, at change and in the dry run."""
    async with console() as c:
        alice, sid = await c.login("alice"), await c.login("sid")
        north = (await c.create(alice))["id"]
        south = (await c.create(sid, org="south"))["id"]
        made = await c.call(alice, "POST", RULES, {**RULE, "channels": [north]})
        assert made.status_code == 201, made.text
        assert made.json()["channels"] == [north]
        theirs = await c.call(alice, "POST", RULES, {**RULE, "channels": [south]})
        missing = await c.call(alice, "POST", RULES, {**RULE, "channels": ["chn_00000000000000ff"]})
        assert theirs.status_code == 422
        assert theirs.json()["error"]["details"][0]["field"] == "channels"
        assert theirs.content == missing.content
        rule = made.json()
        changed = await c.call(
            alice, "PUT", f"{RULES}/{rule['id']}",
            {"expected_version": 1, "rule": {**RULE, "channels": [north, south]}},
        )  # fmt: skip
        assert changed.status_code == 422
        assert changed.content == missing.content
        dry = [
            (await c.call(alice, "POST", RULES + "/validate", {**RULE, "channels": [x]})).json()
            for x in (south, "chn_00000000000000ff", north)
        ]
        assert [d["valid"] for d in dry] == [False, False, True]
        assert dry[0] == dry[1]
        # Nothing was written by the refusals.
        assert (await c.call(alice, "GET", f"{RULES}/{rule['id']}")).json()["version"] == 1


async def _rule(c: Console, session: Session, **changes: Any) -> dict[str, Any]:
    made = await c.call(session, "POST", RULES, {**RULE, **changes})
    assert made.status_code == 201, made.text
    return dict(made.json())


async def test_a_weakened_rule_tells_its_channels_as_they_were() -> None:
    """M29: raising the threshold of an enabled rule queues one rule_weakened notice to its
    channels before the change, naming the rule and the field, never the user."""
    async with console() as c:
        alice = await c.login("alice")
        first = (await c.create(alice, name="First", default=False))["id"]
        second = (await c.create(alice, name="Second", default=False))["id"]
        rule = await _rule(c, alice, channels=[first])
        path = f"{RULES}/{rule['id']}"
        stronger = await c.call(
            alice, "PUT", path,
            {"expected_version": 1, "rule": {**RULE, "threshold": 3, "channels": [first]}},
        )  # fmt: skip
        assert stronger.status_code == 200
        assert c.deliveries() == []  # a stricter rule tells nobody
        weaker = await c.call(
            alice, "PUT", path,
            {"expected_version": 2, "rule": {**RULE, "threshold": 9, "channels": [second]}},
        )  # fmt: skip
        assert weaker.status_code == 200, weaker.text
        [notice] = c.deliveries()
        assert (notice["channel"], notice["state"]) == (first, "queued")  # as it was
        body = notice["payload"]
        assert body["kind"] == "rule_weakened"
        assert body["details"]["fields"] == ["threshold", "channels"]
        assert body["details"]["deleted"] is False
        assert body["rule"]["id"] == rule["id"]
        assert body["rule"]["threshold"] == 3.0
        assert body["application"] == {"id": "north-shop", "name": "Shop"}
        text = json.dumps(body)
        assert "alice" not in text
        assert "usr_" not in text
        assert "hooks.example.com" not in text
        assert TOKEN not in text


async def test_disabling_and_deleting_a_rule_tell_its_channels() -> None:
    async with console() as c:
        alice = await c.login("alice")
        on = (await c.create(alice, name="Defaults", default=True))["id"]
        off = (await c.create(alice, name="Off", default=True))["id"]
        await c.call(alice, "POST", f"/north/alert-channels/{off}/disable")
        before = len(c.deliveries())
        rule = await _rule(c, alice)  # to the organization's defaults
        path = f"{RULES}/{rule['id']}"
        await c.call(alice, "POST", path + "/disable", {"expected_version": 1})
        await c.call(alice, "POST", path + "/enable", {"expected_version": 2})
        await c.call(alice, "DELETE", path, {"expected_version": 3})
        notices = [d for d in c.deliveries()[before:] if d["payload"]["kind"] == "rule_weakened"]
        assert [
            (d["channel"], d["payload"]["details"]["fields"], d["payload"]["details"]["deleted"])
            for d in notices
        ] == [(on, ["enabled"], False), (on, ["enabled"], True)]
        # A disabled rule weakened further tells nobody.
        disabled = await _rule(c, alice, enabled=False)
        count = len(c.deliveries())
        await c.call(
            alice, "PUT", f"{RULES}/{disabled['id']}",
            {"expected_version": 1, "rule": {**RULE, "threshold": 50, "enabled": False}},
        )  # fmt: skip
        assert len(c.deliveries()) == count


async def test_restoring_the_defaults_is_judged_as_any_write() -> None:
    async with console() as c:
        alice = await c.login("alice")
        on = (await c.create(alice, name="Defaults", default=True))["id"]
        # This world's applications were made without their defaults: restored first.
        assert (await c.call(alice, "POST", RULES + "/restore-defaults")).status_code == 200
        listed = (await c.call(alice, "GET", RULES + "?limit=50")).json()["rules"]
        [errors] = [r for r in listed if r["default_key"] == "error_rate"]
        # Made stricter by hand; restoring it makes it fire less again: told.
        stricter = {**{k: errors[k] for k in RULE}, "threshold": 1, "for_minutes": 0,
                    "channels": ["organization defaults"]}  # fmt: skip
        put = await c.call(
            alice, "PUT", f"{RULES}/{errors['id']}", {"expected_version": 1, "rule": stricter}
        )
        assert put.status_code == 200, put.text
        assert c.deliveries() == []
        assert (await c.call(alice, "POST", RULES + "/restore-defaults")).status_code == 200
        [notice] = c.deliveries()
        assert notice["channel"] == on
        assert notice["payload"]["details"]["fields"] == ["threshold", "for_minutes"]


async def test_a_rolled_back_rule_write_queues_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The notice is in the rule write's transaction: a write that fails after it was queued
    leaves no notice behind."""
    async with console() as c:
        alice = await c.login("alice")
        channel = (await c.create(alice))["id"]
        rule = await _rule(c, alice, channels=[channel])

        async def fails(*_: Any) -> None:
            raise RuntimeError("the write failed")

        monkeypatch.setattr(SqliteRuleStore, "put_rule", fails)
        with pytest.raises(RuntimeError):
            await c.call(
                alice, "PUT", f"{RULES}/{rule['id']}",
                {"expected_version": 1, "rule": {**RULE, "threshold": 9, "channels": [channel]}},
            )  # fmt: skip
        assert c.deliveries() == []
        monkeypatch.undo()
        found = await SqliteRuleStore(c.db).get_rule("north-shop", rule["id"])
        assert found is not None
        assert found.version == 1


async def test_removing_a_channel_tells_the_other_default_channels() -> None:
    async with console() as c:
        alice = await c.login("alice")
        a = (await c.create(alice, name="A", default=True))["id"]
        b = (await c.create(alice, name="B", default=True))["id"]
        d = (await c.create(alice, name="D", default=True))["id"]
        n = (await c.create(alice, name="N", default=False))["id"]
        await c.call(alice, "POST", f"/north/alert-channels/{a}/disable")
        told = c.deliveries()
        assert sorted(x["channel"] for x in told) == sorted([b, d])
        assert all(x["payload"]["kind"] == "channel_removed" for x in told)
        assert all(x["payload"]["details"]["channel"] == a for x in told)
        # Disabling it again changes nothing and tells nobody.
        again = await c.call(alice, "POST", f"/north/alert-channels/{a}/disable")
        assert again.json()["version"] == 2
        assert len(c.deliveries()) == 2
        # Deleting an enabled one tells the rest; its own queued deliveries fail as gone.
        assert (await c.call(alice, "DELETE", f"/north/alert-channels/{b}")).status_code == 200
        rows = c.deliveries()
        assert [(x["channel"], x["state"], x["code"]) for x in rows if x["channel"] == b] == [
            (b, "failed", "channel_gone")
        ]
        assert [x["channel"] for x in rows[2:]] == [d]
        # Deleting the disabled one was told when it was disabled: no more.
        await c.call(alice, "DELETE", f"/north/alert-channels/{a}")
        assert len(c.deliveries()) == 3
        assert n not in {x["channel"] for x in c.deliveries()}
        # Its secrets went with it.
        assert c.db.execute(
            "SELECT count(*) FROM channel_secrets WHERE channel_id IN (?, ?)", (a, b)
        ).fetchone() == (0,)


# --- the leak scan ------------------------------------------------------------------------------


async def test_the_url_and_signing_keys_appear_nowhere_but_where_they_are_given(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The planted URL (a token in its path and its query), a second URL, the signing key (the
    generator injected), the key it rotated into (and so the previous key): through create,
    reading, a change of URL, rotation, disable, re-enable and delete, and a rule notice. Every
    stored row, every audit row, every log record, every event, the metrics, /healthz and
    every answer but the two that return a key are scanned; the controls find each planted
    value in its unredacted copy."""
    with caplog.at_level(logging.DEBUG):
        async with console() as c:
            alice = await c.login("alice")
            await c.step_up(alice)
            body = {"type": "webhook", "name": "Ops", "url": URL, "default": True}
            created = await c.call(alice, "POST", "/north/alert-channels", body)
            made = created.json()
            path = f"/north/alert-channels/{made['id']}"
            await c.step_up(alice)
            rotated = await c.call(alice, "POST", path + "/rotate-key")
            answers = [
                await c.call(alice, "GET", "/north/alert-channels"),
                await c.call(alice, "GET", path),
                await c.call(alice, "GET", path + "/deliveries"),
                await c.call(alice, "GET", "/north/applications/north-shop/alert-channels"),
            ]
            await c.step_up(alice)
            answers.append(
                await c.call(alice, "PATCH", path, {"expected_version": 1, "url": OTHER_URL})
            )
            answers.append(await c.call(alice, "POST", RULES, {**RULE, "channels": [made["id"]]}))
            rule = answers[-1].json()
            answers.append(
                await c.call(
                    alice, "PUT", f"{RULES}/{rule['id']}",
                    {"expected_version": 1,
                     "rule": {**RULE, "threshold": 9, "channels": [made["id"]]}},
                )
            )  # fmt: skip
            answers.append(await c.call(alice, "POST", path + "/disable"))
            await c.step_up(alice)
            answers.append(
                await c.call(alice, "PATCH", path, {"expected_version": 3, "enabled": True})
            )
            answers.append(await c.call(alice, "DELETE", path))
            answers += [
                await c.client.get("/healthz"),
                await c.client.get("/v1/monitor/snapshot?events=200"),
                await c.client.get("/metrics"),
            ]
            tables = [
                r[0]
                for r in c.db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                    " AND name NOT LIKE 'sqlite_%'"
                )
            ]
            stored = "\n".join(
                repr(c.db.execute(f"SELECT * FROM {table}").fetchall()) for table in tables
            )
            audit = repr(c.db.execute("SELECT * FROM audit_log").fetchall())
            payloads = repr(c.db.execute("SELECT payload FROM notification_deliveries").fetchall())
    assert all(a.status_code < 500 for a in answers)
    assert any("rule_weakened" in p for p in [payloads])  # the notice was queued and scanned
    logged = "\n".join(f"{r.getMessage()} {r.args!r}" for r in caplog.records)
    answered = "\n".join(a.text for a in answers)
    planted = {
        "path token": TOKEN,
        "query token": QUERY,
        "second url's token": OTHER_TOKEN,
        "signing key": made["signing_key"],
        "rotated key": rotated.json()["signing_key"],
    }
    # The controls: each planted value is found in its unredacted copy.
    control = "\n".join([URL, OTHER_URL, created.text, rotated.text])
    for name, value in planted.items():
        assert value in control, name
    for name, value in planted.items():
        for where, text in (("rows", stored), ("audit", audit), ("logs", logged),
                            ("answers", answered)):  # fmt: skip
            assert value not in text, (name, where)
    # Not even the origins in an audit row or a notice body.
    assert "hooks.example.com" not in audit
    assert "other.example.net" not in audit
    assert "example.com" not in payloads
    assert "example.net" not in payloads


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.example.com/x",
        "https://10.0.0.1/x",
        f"https://hooks.example.com/{TOKEN}\r\nX-Injected: 1",
        "https://hooks.example.com:22/x",
        "https://0x7f000001/x",
    ],
)
async def test_a_change_of_url_is_checked_as_a_new_one_is(url: str) -> None:
    """M16 at change: the same check as at create, and the window given back."""
    async with console() as c:
        alice = await c.login("alice")
        made = await c.create(alice)
        path = f"/north/alert-channels/{made['id']}"
        await c.step_up(alice)
        refused = await c.call(alice, "PATCH", path, {"expected_version": 1, "url": url})
        assert refused.status_code == 422, refused.text
        assert refused.json()["error"]["details"][0]["field"] == "url"
        assert TOKEN not in refused.text
        found = await c.service.store.get_channel(made["id"])
        assert found is not None
        assert (found.version, found.url_origin) == (1, "https://hooks.example.com")
        renamed = await c.call(alice, "PATCH", path, {"expected_version": 1, "name": "Ops 2"})
        assert renamed.status_code == 200, renamed.text
