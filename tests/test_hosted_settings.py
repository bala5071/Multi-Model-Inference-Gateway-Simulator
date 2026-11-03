"""Settings S13 to S16, which decide where a prompt may go and how many times (docs/42): their
values and caps, step-up whenever a change loosens one of them for an application it affects
(judged inside the write), the presets, the mode shown in the settings views, and the in-flight
rule at the re-read before each hosted attempt (``hosted_policy``).

The world of ``test_chat_routing``: north's providers on a scripted network behind the real
egress guard, the gateway's tier on another. Test values, not secrets."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from accounts_helpers import PASSWORD
from gateway.adapters import OpenAICompatibleAdapter
from gateway.apikeys import ApplicationAccess
from gateway.app import create_app, default_platform_settings
from gateway.audit import AuditAction, AuditEntry
from gateway.auth import AccountAccess
from gateway.backends import Backend
from gateway.continuous import build_continuous_registry
from gateway.egress import operator_client
from gateway.localonly import LocalOnlyPolicy
from gateway.modes import InMemoryModeStore, Mode, ModeStore, SqliteModeStore
from gateway.providers import ProviderConfig
from gateway.settings import (
    EGRESS_SETTINGS,
    PRESETS,
    EffectiveSettings,
    InMemorySettingsStore,
    Scope,
    SettingsInvalidError,
    SettingsStore,
    SqliteSettingsStore,
    StoredSettings,
    canonical,
    effective_settings,
    loosens_egress,
    refusals,
    validate_document,
)
from gateway.settingsservice import SettingsService
from gateway.usage import SqliteUsageStore
from mock_network import HANG, Gate
from test_chat_routing import (
    BAD_TOOL,
    GOOD_TOOL,
    HOSTS,
    KEY,
    LOCAL,
    OPERATOR,
    PRIMARY,
    TEXT,
    TOOLS,
    A,
    B,
    Routing,
    _headers,
    _source,
    refused,
    routing,
)

REPO = Path(__file__).resolve().parent.parent
SOUTHS = "prv_00000000000000d4"  # a provider of south's
NOWHERE = "prv_00000000000000e5"  # nobody's
FORBIDDEN_BYTES = b'{"error":{"code":"forbidden","message":"Not permitted, or not found."}}'
APP = "/north/applications/north-shop/settings"
Session = tuple[str, str]


# --- the values, unit by unit -------------------------------------------------------------------


def test_each_hosted_setting_is_refused_outside_its_type_and_range() -> None:
    bad: list[tuple[Scope, str, object]] = [
        (Scope.APPLICATION, "allowed_providers", "none"),
        (Scope.APPLICATION, "allowed_providers", [A, A]),
        (Scope.APPLICATION, "allowed_providers", ["provider-a"]),
        (Scope.APPLICATION, "allowed_providers", [f"prv_{i:016x}" for i in range(21)]),
        (Scope.ORGANIZATION, "allowed_providers", [A]),  # an application's only
        (Scope.APPLICATION, "max_hosted_attempts", 0),
        (Scope.APPLICATION, "max_hosted_attempts", 6),
        (Scope.ORGANIZATION, "max_hosted_attempts", True),
        (Scope.APPLICATION, "retry_invalid_tool_json", "no"),
        (Scope.APPLICATION, "hosted_first_token_ms", 999),
        (Scope.ORGANIZATION, "hosted_first_token_ms", 600_001),
    ]
    for scope, name, value in bad:
        with pytest.raises(SettingsInvalidError) as caught:
            validate_document(scope, {name: value})
        assert caught.value.setting == name, (scope, name, value)
    good = validate_document(
        Scope.APPLICATION,
        {
            "allowed_providers": [B, A],
            "max_hosted_attempts": 5,
            "retry_invalid_tool_json": False,
            "hosted_first_token_ms": 1_000,
        },
    )
    assert good["allowed_providers"] == [A, B]  # kept sorted
    assert validate_document(Scope.APPLICATION, {"allowed_providers": "all"}) == {
        "allowed_providers": "all"
    }
    assert validate_document(Scope.APPLICATION, {"allowed_providers": []}) == {
        "allowed_providers": []
    }


async def _shop(r: Routing) -> Any:
    application = await r.world.accounts.organizations.get_application("north-shop")
    assert application is not None
    return application


async def test_effective_values_sources_and_caps_of_s13_to_s16() -> None:
    async with routing() as r:
        shop = await _shop(r)
    platform = default_platform_settings(max_hosted_attempts=3)

    def effective(org: Mapping[str, object], own: Mapping[str, object]) -> EffectiveSettings:
        return effective_settings(platform, shop, dict(org), dict(own))

    default = effective({}, {})
    assert (default.allowed_providers, default.max_hosted_attempts) == (None, 3)
    assert (default.retry_invalid_tool_json, default.hosted_first_token_ms) == (True, None)
    assert effective({}, {"allowed_providers": []}).allowed_providers == frozenset()
    assert effective({}, {"allowed_providers": [A]}).allowed_providers == frozenset({A})
    assert effective({"max_hosted_attempts": 2}, {}).max_hosted_attempts == 2
    own_lower = effective({"max_hosted_attempts": 2}, {"max_hosted_attempts": 1})
    assert own_lower.max_hosted_attempts == 1
    # A cap tightened after the value was set: the stored value is kept, the effective clamped.
    clamped = effective({"max_hosted_attempts": 1}, {"max_hosted_attempts": 2})
    assert clamped.max_hosted_attempts == 1
    assert effective({"retry_invalid_tool_json": False}, {}).retry_invalid_tool_json is False
    assert effective({}, {"retry_invalid_tool_json": False}).retry_invalid_tool_json is False
    assert effective({"hosted_first_token_ms": 2_000}, {}).hosted_first_token_ms == 2_000
    assert (
        effective({"hosted_first_token_ms": 2_000}, {"hosted_first_token_ms": 1_500})
    ).hosted_first_token_ms == 1_500
    explained = effective({"max_hosted_attempts": 2}, {}).explain
    assert explained["max_hosted_attempts"].source.value == "organization"
    # Looser than a cap in force is refused, at each level.
    looser: list[tuple[Scope, dict[str, object], dict[str, object], str]] = [
        (Scope.ORGANIZATION, {"max_hosted_attempts": 4}, {}, "platform"),
        (Scope.APPLICATION, {"max_hosted_attempts": 4}, {}, "platform"),
        (Scope.APPLICATION, {"max_hosted_attempts": 3}, {"max_hosted_attempts": 2}, "organization"),
        (Scope.APPLICATION, {"retry_invalid_tool_json": True},
         {"retry_invalid_tool_json": False}, "organization"),
        (Scope.APPLICATION, {"hosted_first_token_ms": 2_001},
         {"hosted_first_token_ms": 2_000}, "organization"),
    ]  # fmt: skip
    for scope, document, organization, level in looser:
        found = refusals(platform, scope, document, application=shop, organization=organization)
        assert [(f.setting, f.level.value) for f in found] == [(next(iter(document)), level)]


_BASE = EffectiveSettings(
    allowed_tiers=("economy",),
    model_aliases={},
    default_quality=None,
    allow_downgrade_default=False,
    requests_per_minute=None,
    monthly_token_budget=0,
    max_tokens=None,
    default_max_tokens=None,
    generate_timeout_ms=None,
    chat_first_token_ms=30_000.0,
    chat_total_timeout_ms=120_000.0,
)


def _settings(**values: Any) -> EffectiveSettings:
    return dataclasses.replace(_BASE, **values)


def test_loosens_egress_judges_each_of_s13_to_s16_both_ways() -> None:
    """The one place loosening is judged: for each setting, the looser direction is named and
    the stricter one, or no change, is not."""
    some, more = frozenset({A}), frozenset({A, B})
    cases: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
        ("allowed_providers", {"allowed_providers": some}, {"allowed_providers": None}),
        ("allowed_providers", {"allowed_providers": some}, {"allowed_providers": more}),
        ("allowed_providers", {"allowed_providers": frozenset()}, {"allowed_providers": some}),
        ("max_hosted_attempts", {"max_hosted_attempts": 1}, {"max_hosted_attempts": 2}),
        ("retry_invalid_tool_json", {"retry_invalid_tool_json": False},
         {"retry_invalid_tool_json": True}),
        ("hosted_first_token_ms", {"hosted_first_token_ms": 1_000.0},
         {"hosted_first_token_ms": 1_001.0}),
        ("hosted_first_token_ms", {"hosted_first_token_ms": 1_000.0},
         {"hosted_first_token_ms": None}),
    ]  # fmt: skip
    for name, strict, loose in cases:
        assert loosens_egress(_settings(**strict), _settings(**loose)) == (name,), (name, loose)
        assert loosens_egress(_settings(**loose), _settings(**strict)) == (), (name, strict)
        assert loosens_egress(_settings(**strict), _settings(**strict)) == ()
    # Swapping one provider for another loosens: a provider allowed that was not.
    swapped = loosens_egress(
        _settings(allowed_providers=frozenset({A})), _settings(allowed_providers=frozenset({B}))
    )
    assert swapped == ("allowed_providers",)
    # S1 to S11 never do.
    assert loosens_egress(_settings(max_tokens=10), _settings(max_tokens=1000)) == ()
    everything = loosens_egress(
        _settings(
            allowed_providers=frozenset(),
            max_hosted_attempts=1,
            retry_invalid_tool_json=False,
            hosted_first_token_ms=1_000.0,
        ),
        _settings(),
    )
    assert everything == EGRESS_SETTINGS


def _preset_tables(path: Path) -> dict[str, dict[str, object]]:
    found: dict[str, dict[str, object]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\| `(cheap|fast|private)` \| (`[^|]+`) \|", line.strip())
        if match:
            items = re.findall(r"`([^`]+)`", match.group(2))
            found[match.group(1)] = yaml.safe_load("\n".join(items))
    return found


def test_the_presets_are_exactly_the_plans() -> None:
    """The plan's section 4 and note 42 give the presets' exact values; the code has them."""
    plan = _preset_tables(REPO / "docs" / "settings-and-alerts-plan.md")
    note = _preset_tables(REPO / "docs" / "42-hosted-settings-and-presets.md")
    code = {name: dict(values) for name, values in PRESETS.items()}
    assert plan == note == code
    for name, values in PRESETS.items():  # every preset is a valid application document
        assert validate_document(Scope.APPLICATION, values) == dict(values), name


# --- the endpoints ------------------------------------------------------------------------------


async def _south_provider(r: Routing) -> None:
    config = ProviderConfig(
        SOUTHS, "south", "openai_compatible", "https://a.provider.test/v1", "model-x", "economy",
        "primary", True, True, None, None, {}, None, 0.0,
    )  # fmt: skip
    await r.service.store.add(config)


async def _call(
    r: Routing, session: Session, method: str, path: str, body: dict[str, Any] | None = None
) -> httpx.Response:
    url = "/v1/admin/orgs" + path
    if method == "GET":
        return await r.client.get(url, headers=_headers(session))
    return await r.client.request(method, url, json=body or {}, headers=_headers(session))


async def _version(r: Routing, path: str, session: Session) -> int:
    return int((await _call(r, session, "GET", path)).json()["version"])


async def _put(r: Routing, session: Session, path: str, **values: Any) -> httpx.Response:
    version = await _version(r, path, session)
    return await _call(r, session, "PUT", path, {"expected_version": version, "set": values})


async def _audit(r: Routing, action: AuditAction) -> list[AuditEntry]:
    return [e for e in await r.world.accounts.audit.entries() if e.event.action is action]


async def _rows(r: Routing) -> list[tuple[Any, ...]]:
    connection = r.world.accounts.connection
    return connection.execute(
        "SELECT * FROM settings_versions ORDER BY scope_type, scope_id, version"
    ).fetchall()


def _code(response: httpx.Response) -> str:
    return str(response.json()["error"]["code"])


async def test_allowed_providers_names_only_the_organizations_own() -> None:
    """Another organization's provider and one that does not exist are refused alike, byte for
    byte; one of north's is accepted, and restricting needs no step-up."""
    async with routing(settings_routes=True) as r:
        await r.provider(A, "a.provider.test")
        await _south_provider(r)
        alice = await r.login("alice")
        souths = await _put(r, alice, APP, allowed_providers=[SOUTHS])
        nowhere = await _put(r, alice, APP, allowed_providers=[NOWHERE])
        mixed = await _put(r, alice, APP, allowed_providers=[A, SOUTHS])
        before = await _rows(r)
        own = await _put(r, alice, APP, allowed_providers=[A])
    assert souths.status_code == 422
    assert souths.content == nowhere.content == mixed.content
    assert souths.json()["error"]["details"][0]["field"] == "allowed_providers"
    assert before == []
    assert own.status_code == 200, own.text


# (scope, the setting, a stricter value, the looser one): north-shop's own, or north's cap,
# which loosens for its applications, none of which sets its own value.
_LOOSENING = [
    (APP, "allowed_providers", [A], "all"),
    (APP, "max_hosted_attempts", 1, 3),
    (APP, "retry_invalid_tool_json", False, True),
    (APP, "hosted_first_token_ms", 1_000, 2_000),
    ("/north/settings", "max_hosted_attempts", 1, 3),
    ("/north/settings", "retry_invalid_tool_json", False, True),
    ("/north/settings", "hosted_first_token_ms", 1_000, 600_000),
]


@pytest.mark.parametrize(("path", "name", "strict", "loose"), _LOOSENING)
async def test_loosening_each_hosted_setting_needs_a_fresh_password_used_once(
    path: str, name: str, strict: object, loose: object
) -> None:
    """Tightened without step-up and the window untouched; loosened without step-up refused,
    nothing written; with step-up accepted and audited as loosening; tightened again, still no
    step-up needed; loosened a second time on the same window refused (consume-on-use)."""
    async with routing(settings_routes=True) as r:
        await r.provider(A, "a.provider.test")
        alice = await r.login("alice")
        tightened = await _put(r, alice, path, **{name: strict})
        before = await _rows(r)
        refused_ = await _put(r, alice, path, **{name: loose})
        unchanged = await _rows(r)
        await r.step_up(alice)
        loosened = await _put(r, alice, path, **{name: loose})
        again_tightened = await _put(r, alice, path, **{name: strict})
        second = await _put(r, alice, path, **{name: loose})
        changed = await _audit(r, AuditAction.SETTINGS_CHANGED)
    assert tightened.status_code == 200, tightened.text
    assert (refused_.status_code, _code(refused_)) == (403, "step_up_required")
    assert unchanged == before
    assert loosened.status_code == 200, loosened.text
    assert again_tightened.status_code == 200, again_tightened.text
    assert (second.status_code, _code(second)) == (403, "step_up_required")
    assert [e.event.details["loosened_egress"] for e in changed] == [False, True, False]


async def test_an_organization_cap_raised_loosens_only_where_it_brings_a_value_back() -> None:
    """north caps max_hosted_attempts at 1. Raising it to 3 with north-shop's own value 3 and
    north-blog's 1 brings north-shop's 3 back into force: a loosening, step-up. With both
    applications at 1 it brings nothing back: no step-up, though the cap rose."""
    async with routing(settings_routes=True) as r:
        alice = await r.login("alice")
        blog = "/north/applications/north-blog/settings"
        assert (await _put(r, alice, APP, max_hosted_attempts=3)).status_code == 200
        assert (await _put(r, alice, blog, max_hosted_attempts=1)).status_code == 200
        assert (await _put(r, alice, "/north/settings", max_hosted_attempts=1)).status_code == 200
        version = await _version(r, "/north/settings", alice)
        raise_ = {"expected_version": version, "set": {"max_hosted_attempts": 3}}
        dry = (await _call(r, alice, "POST", "/north/settings/validate", raise_)).json()
        brings_back = await _call(r, alice, "PUT", "/north/settings", raise_)
        assert (await _put(r, alice, APP, max_hosted_attempts=1)).status_code == 200
        dry_none = (await _call(r, alice, "POST", "/north/settings/validate", raise_)).json()
        brings_nothing = await _call(r, alice, "PUT", "/north/settings", raise_)
    assert (dry["loosens"], dry["step_up_required"]) == (["max_hosted_attempts"], True)
    assert (brings_back.status_code, _code(brings_back)) == (403, "step_up_required")
    assert (dry_none["loosens"], dry_none["step_up_required"]) == ([], False)
    assert brings_nothing.status_code == 200, brings_nothing.text


async def test_a_revert_and_a_preset_are_judged_by_their_effect_both_ways() -> None:
    """Judged from the change made, never from its name: a revert back to a stricter value
    needs no step-up (the window was used), a revert back to a looser one does; fast over 1
    attempt loosens (to 2), over the default of 3 it does not."""
    async with routing(settings_routes=True) as r:
        alice = await r.login("alice")
        await _put(r, alice, APP, max_hosted_attempts=1)  # v1, tighter than 3
        await r.step_up(alice)
        assert (await _put(r, alice, APP, max_hosted_attempts=2)).status_code == 200  # v2
        # v2 to v1's document: 2 to 1, tightens, and the window is already used.
        revert_tightening = await _call(r, alice, "POST", APP + "/revert", {"expected_version": 2})
        # v3 to v2's document: 1 to 2, loosens.
        revert_loosening = await _call(r, alice, "POST", APP + "/revert", {"expected_version": 3})
        fast_loosening = await _call(
            r, alice, "POST", APP + "/presets/fast", {"expected_version": 3}
        )
        fast_tightening = await _call(
            r,
            alice,
            "POST",
            "/north/applications/north-blog/settings/presets/fast",
            {"expected_version": 0},
        )
        await r.step_up(alice)
        fast_with_password = await _call(
            r, alice, "POST", APP + "/presets/fast", {"expected_version": 3}
        )
        presets = await _audit(r, AuditAction.SETTINGS_PRESET_APPLIED)
        reverted = await _audit(r, AuditAction.SETTINGS_REVERTED)
        listed = (await _call(r, alice, "GET", APP + "/versions")).json()["versions"]
    assert revert_tightening.status_code == 200, revert_tightening.text
    assert (revert_loosening.status_code, _code(revert_loosening)) == (403, "step_up_required")
    assert (fast_loosening.status_code, _code(fast_loosening)) == (403, "step_up_required")
    assert fast_tightening.status_code == 200, fast_tightening.text
    assert fast_with_password.status_code == 200, fast_with_password.text
    assert [e.event.details["loosened_egress"] for e in reverted] == [False]
    assert [
        (
            e.event.target_id,
            e.event.details["settings_version"],
            e.event.details["loosened_egress"],
            e.event.details["preset"],
        )
        for e in presets
    ] == [("north-blog", 1, False, "fast"), ("north-shop", 4, True, "fast")]
    changed_fields = presets[0].event.details["changed_fields"]
    assert isinstance(changed_fields, list)
    assert sorted(changed_fields) == sorted(PRESETS["fast"])
    assert listed[0]["via"] == "preset:fast"
    assert listed[1]["via"] == "revert:1"


async def test_presets_are_listed_with_their_values_and_refused_by_a_cap() -> None:
    """GET lists every preset with its exact values; private tightens (no step-up) and sets
    only its own fields; cheap under a cap of [standard] is refused naming allowed_tiers, never
    clamped, and nothing is written."""
    async with routing(settings_routes=True) as r:
        alice = await r.login("alice")
        listed = (await _call(r, alice, "GET", "/north/settings/presets")).json()
        await _put(r, alice, APP, max_tokens=64)
        version = await _version(r, APP, alice)
        private = await _call(
            r, alice, "POST", APP + "/presets/private", {"expected_version": version}
        )
        await _put(r, alice, "/north/settings", allowed_tiers=["standard"])
        rows = await _rows(r)
        cheap = await _call(
            r, alice, "POST", APP + "/presets/cheap", {"expected_version": version + 1}
        )
        after = await _rows(r)
    assert listed == {"presets": {k: dict(v) for k, v in PRESETS.items()}}
    assert private.status_code == 200, private.text
    assert private.json()["settings"] == {"max_tokens": 64, **PRESETS["private"]}
    assert (cheap.status_code, _code(cheap)) == (409, "setting_looser_than_cap")
    assert "allowed_tiers (capped by the organization)" in cheap.json()["error"]["message"]
    assert after == rows


async def test_the_settings_views_show_the_mode_and_validate_says_what_loosens() -> None:
    async with routing(settings_routes=True) as r:
        await r.mode(PRIMARY, Mode.LOCAL_WITH_HOSTED_FALLBACK)
        alice = await r.login("alice")
        org = (await _call(r, alice, "GET", "/north/settings")).json()
        app = (await _call(r, alice, "GET", APP)).json()
        await _put(r, alice, APP, retry_invalid_tool_json=False)
        rows = await _rows(r)
        dry = await _call(
            r,
            alice,
            "POST",
            APP + "/validate",
            {"expected_version": 1, "set": {"retry_invalid_tool_json": True, "max_tokens": 9}},
        )
        assert await _rows(r) == rows
    assert org["mode"] == "hosted_primary_with_local_fallback"
    assert app["mode"] == {
        "organization": "hosted_primary_with_local_fallback",
        "application": "local_with_hosted_fallback",
        "effective": "local_with_hosted_fallback",
    }
    assert app["effective"]["max_hosted_attempts"]["value"] == 3
    assert app["effective"]["allowed_providers"]["value"] == "all"
    assert dry.json()["loosens"] == ["retry_invalid_tool_json"]
    assert dry.json()["step_up_required"] is True


# --- two instances: a cap moved between the dry run and the write -------------------------------


@asynccontextmanager
async def second_instance(r: Routing) -> AsyncIterator[httpx.AsyncClient]:
    """Another gateway on the same database, its own registry and adapters (a second process,
    as far as the stores can tell)."""
    accounts = r.world.accounts
    registry = build_continuous_registry(accounts.clock, seed=8)
    adapters: dict[str, Backend] = {
        "economy": OpenAICompatibleAdapter(
            name="economy",
            base_url=OPERATOR,
            model="local-model",
            client=operator_client(r.resolver, LOCAL, inner=r.op_net),
        )
    }
    app = create_app(
        registry,
        accounts.clock,
        applications=ApplicationAccess(r.world.keys, r.world.templates),
        accounts=AccountAccess(accounts.service),
        local_only=LocalOnlyPolicy(),
        epoch=accounts.epoch,
        providers=r.service,
        chat_adapters=adapters,
        modes=r.modes,
        local_destinations=LOCAL,
        usage_store=SqliteUsageStore(accounts.connection),
        settings=SqliteSettingsStore(accounts.connection),
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50001))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield client
    await registry.aclose()


def _between(monkeypatch: pytest.MonkeyPatch, then: Any) -> None:
    """Runs ``then`` once, after the next dry run and before its write."""
    original = SettingsService.validate
    pending = [then]

    async def validate(self: SettingsService, *args: Any, **kwargs: Any) -> Any:
        result = await original(self, *args, **kwargs)
        if pending:
            await pending.pop()()
        return result

    monkeypatch.setattr(SettingsService, "validate", validate)


async def test_a_cap_raised_on_another_instance_after_the_dry_run_is_a_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """north-shop at 1 under north's cap of 1; alice sets north-shop to 3: the dry run finds
    nothing loosened (the cap still holds it at 1), so no step-up is claimed. Before the write,
    carol raises the cap to 3 on instance B. Judged again inside the write, the change now
    loosens: refused, 409, nothing written. Read again, it asks for step-up."""
    async with routing(settings_routes=True) as r, second_instance(r) as b:
        alice, carol = await r.login("alice"), await r.login("carol")
        await _put(r, alice, "/north/settings", max_hosted_attempts=1)
        await _put(r, alice, APP, max_hosted_attempts=1)
        await r.step_up(carol)

        async def raise_cap() -> None:
            response = await b.put(
                "/v1/admin/orgs/north/settings",
                json={"expected_version": 1, "set": {"max_hosted_attempts": 3}},
                headers=_headers(carol),
            )
            assert response.status_code == 200, response.text

        _between(monkeypatch, raise_cap)
        rows = await _rows(r)
        raced = await _call(
            r, alice, "PUT", APP, {"expected_version": 1, "set": {"max_hosted_attempts": 3}}
        )
        app_rows = [row for row in await _rows(r) if row[1] == "north-shop"]
        again = await _call(
            r, alice, "PUT", APP, {"expected_version": 1, "set": {"max_hosted_attempts": 3}}
        )
    assert (raced.status_code, _code(raced)) == (409, "settings_version_conflict"), raced.text
    assert app_rows == [row for row in rows if row[1] == "north-shop"]
    assert (again.status_code, _code(again)) == (403, "step_up_required")


async def test_a_cap_lowered_on_another_instance_after_the_dry_run_gives_the_window_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """north-shop's own value 1; alice, stepped up, removes it (back to the default of 3): the
    dry run finds a loosening, and the window is claimed. Before the write carol lowers north's
    cap to 1 on instance B, so the change no longer loosens: refused, 409, nothing written, and
    the window given back: the next loosening alice makes needs no new password."""
    async with routing(settings_routes=True) as r, second_instance(r) as b:
        alice, carol = await r.login("alice"), await r.login("carol")
        await _put(r, alice, APP, max_hosted_attempts=1)
        await _put(r, alice, APP, retry_invalid_tool_json=False)

        async def lower_cap() -> None:
            response = await b.put(
                "/v1/admin/orgs/north/settings",
                json={"expected_version": 0, "set": {"max_hosted_attempts": 1}},
                headers=_headers(carol),
            )
            assert response.status_code == 200, response.text

        await r.step_up(alice)
        _between(monkeypatch, lower_cap)
        raced = await _call(
            r, alice, "PUT", APP, {"expected_version": 2, "unset": ["max_hosted_attempts"]}
        )
        version = await _version(r, APP, alice)
        next_ = await _call(
            r,
            alice,
            "PUT",
            APP,
            {"expected_version": version, "set": {"retry_invalid_tool_json": True}},
        )
    assert (raced.status_code, _code(raced)) == (409, "settings_version_conflict"), raced.text
    assert version == 2
    assert next_.status_code == 200, next_.text


async def test_a_change_on_one_instance_is_seen_by_the_others_next_request() -> None:
    """north-shop made private on instance A: B's next request has no hosted candidate."""
    async with routing(settings_routes=True) as r, second_instance(r) as b:
        await r.mode(PRIMARY)
        await r.provider(A, "a.provider.test")
        alice = await r.login("alice")
        r.org_net.responses = [TEXT]
        r.op_net.responses = [TEXT, TEXT]
        before = await _chat_on(b, r)
        done = await _call(r, alice, "POST", APP + "/presets/private", {"expected_version": 0})
        after = await _chat_on(b, r)
    assert _source(before) == (A, "hosted", None, None)
    assert done.status_code == 200, done.text
    assert _source(after) == ("gateway", "local", None, None)
    assert len(r.org_net.connections) == 1


async def _chat_on(client: httpx.AsyncClient, r: Routing, *, tools: bool = False) -> Any:
    body: dict[str, Any] = {
        "model": "economy",
        "messages": [{"role": "user", "content": "Say hello."}],
    }
    if tools:
        body["tools"] = TOOLS
    return await client.post(
        "/v1/chat/completions",
        json=body,
        headers={"Authorization": f"Bearer {r.world.api_keys['north-shop']}"},
    )


# --- in flight: stricter before the next hosted attempt, looser from the next request ----------


async def _write(r: Routing, scope: Scope, scope_id: str, **values: Any) -> None:
    """A settings change as the operator's CLI makes it, straight to the store."""
    store = SqliteSettingsStore(r.world.accounts.connection)
    current = await store.current(scope, scope_id)
    document = dict(values)
    async with store.write_transaction():
        await store.write(
            scope,
            scope_id,
            expected_version=current.version if current is not None else 0,
            document=canonical(validate_document(scope, document)),
            changed=tuple(document),
            changed_by_user_id=None,
            via="cli",
            at_ms=1.0,
        )


async def _two_primaries(r: Routing, *, tools: bool = False, only_a: bool = False) -> None:
    await r.mode(PRIMARY)
    await r.provider(A, "a.provider.test", role="primary", tools=tools or None)
    if not only_a:
        await r.provider(B, "b.provider.test", role="primary", tools=tools or None)


def _addresses(r: Routing) -> list[str]:
    return [c.address for c in r.org_net.connections]


# Each case: (scope, the settings at the start, the change made while A is being connected to).
_STRICTER: list[tuple[Scope, dict[str, Any], dict[str, Any]]] = [
    (Scope.APPLICATION, {}, {"allowed_providers": [A]}),
    (Scope.APPLICATION, {}, {"max_hosted_attempts": 1}),
    (Scope.ORGANIZATION, {}, {"max_hosted_attempts": 1}),
]


@pytest.mark.parametrize(("scope", "start", "change"), _STRICTER)
async def test_a_stricter_provider_list_or_cap_stops_the_next_hosted_attempt(
    scope: Scope, start: dict[str, Any], change: dict[str, Any]
) -> None:
    """A is refused while B is made not allowed (S13) or the cap lowered to the one attempt
    already made (S14, the application's or the organization's). B, the next primary, is not
    called and its breaker's slot is given back; the gateway's tier answers."""
    async with routing() as r:
        await _two_primaries(r)
        scope_id = "north-shop" if scope is Scope.APPLICATION else "north"
        if start:
            await _write(r, scope, scope_id, **start)
        gate = Gate(refused())
        r.org_net.responses = [gate, TEXT]
        r.op_net.responses = [TEXT]
        request = asyncio.ensure_future(r.chat())
        await gate.reached.wait()
        await _write(r, scope, scope_id, **change)
        gate.opened.set()
        response = await request
        b_admits = await r.router.breakers.admit("north", B)
    assert response.status_code == 200, response.text
    assert _source(response) == ("gateway", "local", A, "connect_error")
    assert _addresses(r) == HOSTS["a.provider.test"]
    assert b_admits  # the slot B was admitted for was given back


_LOOSER: list[tuple[Scope, dict[str, Any], dict[str, Any]]] = [
    (Scope.APPLICATION, {"allowed_providers": [A]}, {"allowed_providers": "all"}),
    (Scope.APPLICATION, {"max_hosted_attempts": 1}, {"max_hosted_attempts": 3}),
]


@pytest.mark.parametrize(("scope", "start", "change"), _LOOSER)
async def test_a_looser_provider_list_or_cap_waits_for_the_next_request(
    scope: Scope, start: dict[str, Any], change: dict[str, Any]
) -> None:
    async with routing() as r:
        await _two_primaries(r)
        await _write(r, scope, "north-shop", **start)
        gate = Gate(refused())
        r.org_net.responses = [gate, refused(), TEXT]
        r.op_net.responses = [TEXT]
        request = asyncio.ensure_future(r.chat())
        await gate.reached.wait()
        await _write(r, scope, "north-shop", **change)
        gate.opened.set()
        during = await request
        during_addresses = _addresses(r)
        next_ = await r.chat()
    assert _source(during)[:3] == ("gateway", "local", A)
    assert during_addresses == HOSTS["a.provider.test"]
    assert _source(next_) == (B, "hosted", A, "connect_error")


@pytest.mark.parametrize(("start", "change", "calls"), [({}, False, 1), ({}, None, 2)])
async def test_the_retry_turned_off_in_flight_is_not_made(
    start: dict[str, Any], change: bool | None, calls: int
) -> None:
    """A answers invalid tool JSON while the retry is turned off (S15): the retry on A is not
    made and the gateway's tier answers. The control, nothing changed: A is retried and
    answers."""
    async with routing() as r:
        await _two_primaries(r, tools=True, only_a=True)
        gate = Gate(BAD_TOOL)
        r.org_net.responses = [gate, GOOD_TOOL]
        r.op_net.responses = [GOOD_TOOL]
        request = asyncio.ensure_future(r.chat(tools=True))
        await gate.reached.wait()
        if change is not None:
            await _write(r, Scope.APPLICATION, "north-shop", retry_invalid_tool_json=change)
        gate.opened.set()
        response = await request
    assert response.status_code == 200, response.text
    assert _addresses(r) == HOSTS["a.provider.test"] * calls
    if calls == 1:
        assert _source(response)[:3] == ("gateway", "local", A)
    else:
        assert _source(response) == (A, "hosted", None, None)


async def test_the_retry_turned_on_in_flight_waits_for_the_next_request() -> None:
    async with routing() as r:
        await _two_primaries(r, tools=True, only_a=True)
        await _write(r, Scope.APPLICATION, "north-shop", retry_invalid_tool_json=False)
        gate = Gate(BAD_TOOL)
        r.org_net.responses = [gate, BAD_TOOL, GOOD_TOOL]
        r.op_net.responses = [GOOD_TOOL]
        request = asyncio.ensure_future(r.chat(tools=True))
        await gate.reached.wait()
        await _write(r, Scope.APPLICATION, "north-shop", retry_invalid_tool_json=True)
        gate.opened.set()
        during = await request
        during_calls = len(r.org_net.connections)
        next_ = await r.chat(tools=True)
    assert _source(during)[:3] == ("gateway", "local", A)
    assert during_calls == 1  # A only, not retried
    assert _source(next_) == (A, "hosted", None, None)  # retried, then answered


async def _timed_b(r: Routing, start: dict[str, Any], change: dict[str, Any]) -> float:
    """A is refused while S16 changes; B hangs. How long B was waited for, in simulated ms."""
    await _two_primaries(r)
    if start:
        await _write(r, Scope.APPLICATION, "north-shop", **start)
    gate = Gate(refused())
    r.org_net.responses = [gate, HANG]
    r.op_net.responses = [TEXT]
    request = asyncio.ensure_future(r.chat())
    await gate.reached.wait()
    await _write(r, Scope.APPLICATION, "north-shop", **change)
    opened = r.clock.now_ms()
    gate.opened.set()
    response = await request
    assert _source(response) == ("gateway", "local", A, "connect_error")
    return float(r.clock.now_ms() - opened)


async def test_a_lower_first_token_budget_applies_to_the_next_hosted_attempt() -> None:
    """S16 lowered to 1,000 ms while A is refused: B is given 1,000 ms, not its own 10,000; the
    control, raised from 1,000 to 600,000 in flight, keeps 1,000."""
    async with routing() as r:
        stricter = await _timed_b(r, {}, {"hosted_first_token_ms": 1_000})
    async with routing() as r:
        looser = await _timed_b(
            r, {"hosted_first_token_ms": 1_000}, {"hosted_first_token_ms": 600_000}
        )
    async with routing() as r:
        unchanged = await _timed_b(r, {}, {"max_tokens": 64})
    assert 1_000 <= stricter < 2_000, stricter
    assert 1_000 <= looser < 2_000, looser
    assert unchanged >= 10_000, unchanged


async def test_s13_keeps_a_provider_not_allowed_from_ever_being_contacted() -> None:
    """Allowed only A: B is not a candidate at all, and private (no provider) contacts none."""
    async with routing() as r:
        await _two_primaries(r)
        await _write(r, Scope.APPLICATION, "north-shop", allowed_providers=[B])
        r.org_net.responses = [TEXT]
        r.op_net.responses = [TEXT]
        only_b = await r.chat()
        await _write(r, Scope.APPLICATION, "north-shop", **dict(PRESETS["private"]))
        none = await r.chat()
        names = r.hosted_contacted()
    assert _source(only_b) == (B, "hosted", None, None)
    assert _source(none) == ("gateway", "local", None, None)
    assert names == HOSTS["b.provider.test"] + ["b.provider.test"]


# --- hosted_policy, on each store ---------------------------------------------------------------


async def test_hosted_policy_reads_both_modes_and_both_rows_alike_in_memory_and_on_sqlite() -> None:
    """The same writes on the in-memory stores and on SQLite give the same policy: both modes
    (the default where none is set) and both settings rows (None where a scope was never set).
    Postgres is compared with SQLite in ``test_postgres``'s scenario."""
    async with routing() as r:
        connection = r.world.accounts.connection
        memory_settings = InMemorySettingsStore()
        stores: list[tuple[ModeStore, SettingsStore]] = [
            (InMemoryModeStore(memory_settings), memory_settings),
            (SqliteModeStore(connection), SqliteSettingsStore(connection)),
        ]
        results = []
        for modes, settings in stores:
            seen = [await modes.hosted_policy("north", "north-shop")]
            await modes.set_organization_mode("north", PRIMARY, 1.0)
            await modes.set_application_mode("north-shop", Mode.LOCAL_WITH_HOSTED_FALLBACK, 1.0)
            for scope, scope_id, document in (
                (Scope.ORGANIZATION, "north", {"max_hosted_attempts": 2}),
                (Scope.APPLICATION, "north-shop", {"allowed_providers": [A]}),
            ):
                async with settings.write_transaction():
                    await settings.write(
                        scope, scope_id, expected_version=0, document=canonical(document),
                        changed=tuple(document), changed_by_user_id=None, via="cli", at_ms=2.0,
                    )  # fmt: skip
            seen += [
                await modes.hosted_policy("north", "north-shop"),
                await modes.hosted_policy("north", "north-blog"),
            ]
            results.append(seen)
    assert results[0] == results[1]
    empty, both, blog = results[1]
    assert (empty.organization_mode, empty.settings) == (
        Mode.LOCAL_ONLY,
        StoredSettings(None, None),
    )
    assert (both.organization_mode, both.application_mode) == (
        PRIMARY,
        Mode.LOCAL_WITH_HOSTED_FALLBACK,
    )
    assert both.settings.organization is not None
    assert both.settings.application is not None
    assert both.settings.application.document == canonical({"allowed_providers": [A]})
    assert blog.settings.application is None
    assert blog.application_mode is Mode.LOCAL_ONLY


# --- the leak scan ------------------------------------------------------------------------------


async def test_no_key_session_token_or_password_reaches_rows_logs_or_answers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every endpoint S3 adds or changes is called with alice's session and CSRF token, after a
    login and a step-up with her password: the preset list and each preset, a loosening and a
    tightening of each of S13 to S16, a dry run and a revert; then chat requests with a hosted
    attempt and its re-read. The provider's key is planted in the vault. Every settings row and
    version, every audit row, every usage row, every log record and every answer is scanned. The
    controls: the session, the token and the password are found in what was sent to the gateway,
    and the provider's key in what was sent to the provider, so a scan that found nothing could
    have."""
    sent: list[bytes] = []

    async def record(request: httpx.Request) -> None:
        sent.append(await request.aread() + repr(request.headers.raw).encode())

    answers: list[httpx.Response] = []
    with caplog.at_level(logging.DEBUG):
        async with routing(settings_routes=True) as r:
            r.client.event_hooks["request"].append(record)
            await r.mode(PRIMARY)
            await r.provider(A, "a.provider.test", role="primary", tools=True)
            alice = await r.login("alice")
            await r.step_up(alice)
            answers.append(await _call(r, alice, "GET", "/north/settings/presets"))
            for name in PRESETS:
                version = await _version(r, APP, alice)
                await r.step_up(alice)
                answers.append(
                    await _call(
                        r, alice, "POST", APP + f"/presets/{name}", {"expected_version": version}
                    )
                )
            for path, setting, strict, loose in _LOOSENING:
                await r.step_up(alice)
                answers.append(await _put(r, alice, path, **{setting: loose}))
                answers.append(await _put(r, alice, path, **{setting: strict}))
            version = await _version(r, APP, alice)
            answers.append(
                await _call(
                    r,
                    alice,
                    "POST",
                    APP + "/validate",
                    {"expected_version": version, "set": {"max_hosted_attempts": 3}},
                )
            )
            await r.step_up(alice)
            org_version = await _version(r, "/north/settings", alice)
            answers.append(
                await _call(
                    r, alice, "POST", "/north/settings/revert", {"expected_version": org_version}
                )
            )
            await _write(r, Scope.APPLICATION, "north-shop", allowed_providers="all")
            await _write(r, Scope.ORGANIZATION, "north", max_hosted_attempts=3)
            r.org_net.responses = [refused(), TEXT, BAD_TOOL, GOOD_TOOL]
            r.op_net.responses = [TEXT, GOOD_TOOL]
            answers.append(await r.chat())
            answers.append(await r.chat())
            answers.append(await r.chat(tools=True))
            to_provider = b"".join(bytes(c.written) for c in r.org_net.connections)
            connection = r.world.accounts.connection
            rows = repr(
                connection.execute("SELECT * FROM settings_current").fetchall()
                + connection.execute("SELECT * FROM settings_versions").fetchall()
            )
            audit = repr(connection.execute("SELECT * FROM audit_log").fetchall())
            usage = repr(connection.execute("SELECT * FROM usage_events").fetchall())
    assert [a.status_code for a in answers].count(200) == len(answers), [
        (a.status_code, a.text) for a in answers if a.status_code != 200
    ]
    logged = "\n".join(rec.getMessage() for rec in caplog.records)
    answered = "\n".join(a.text + repr(sorted(a.headers.items())) for a in answers)
    planted = {
        "session": alice[0],
        "csrf token": alice[1],
        "password": PASSWORD,
        "api key": r.world.api_keys["north-shop"],
        "provider key": KEY,
    }
    for name in ("session", "csrf token", "password"):
        assert any(planted[name].encode() in body for body in sent), name  # the controls
    assert KEY.encode() in to_provider  # the control for the provider's key
    for name, value in planted.items():
        for where, text in (("rows", rows), ("audit", audit), ("usage", usage),
                            ("logs", logged), ("answers", answered)):  # fmt: skip
            assert value not in text, (name, where)
