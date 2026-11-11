"""What the admin console's alert screens rely on in the gateway (docs/49), pinned here.

The console draws a write control only when the gateway has said the write is allowed (note 47).
Where a write has a dry run it asks with one: a body the route's own model refuses, sent after
``requires`` has run. Someone allowed gets 422 ``invalid_request``; anyone else 403
``forbidden``, the same bytes as a missing resource; nothing is written and nothing is audited.
Two such probes: an alert rule's (``alert_rules.write``) and a recipient's code
(``alert_channels.confirm_recipient``), whose probes count no wrong entry.

Where a write has none, its control follows an answer of the same role row (note 49):

- ``incidents.acknowledge`` takes no body: it follows the rule probe on the same application;
- ``alert_channels.disable`` and ``.test`` take no body, and a dry run of
  ``alert_channels.write`` is no dry run: without an open window it is refused
  ``step_up_required``, and every step-up refusal is audited
  (``test_a_channel_write_probe_would_be_audited``). So all three follow the channel list's own
  answer (``alert_channels.read``);
- ``application_keys.issue``, ``.rotate`` and ``.revoke`` (docs/51) take step-up, so a probe of
  any of them would be an audited ``step_up_required`` refusal, and Revoke has no body to
  refuse: all three follow the key list's own answer (``application_keys.list``).

``test_sibling_rows_are_equal`` pins each pair's rows equal, so a change to the matrix fails here
instead of showing a control the gateway would refuse.

The world of ``test_management_api``: north (alice org_admin, vera viewer, nina app_admin of
north-shop, bob no role) and south (sid org_admin). Test values, not secrets."""

from __future__ import annotations

import json

import pytest

from gateway.permissions import PERMISSIONS
from test_alert_channels_api import FORBIDDEN_BYTES, STEP_UP_REQUIRED
from test_recipients_api import ADDRESS, EmailConsole, console

# The body the console's probes send: a JSON array, which these routes' models refuse.
PROBE_BODY = b"[]"

# (the answer the console asks for, the action whose control follows it): note 49.
SIBLINGS = (
    ("alert_rules.write", "incidents.acknowledge"),
    ("alert_channels.read", "alert_channels.write"),
    ("alert_channels.read", "alert_channels.disable"),
    ("alert_channels.read", "alert_channels.test"),
    # An application's keys (docs/51): Issue, Rotate and Revoke follow the key list's answer.
    ("application_keys.list", "application_keys.issue"),
    ("application_keys.list", "application_keys.rotate"),
    ("application_keys.list", "application_keys.revoke"),
)

Session = tuple[str, str]


def headers(session: Session) -> dict[str, str]:
    return {"Cookie": f"gw_session={session[0]}", "X-CSRF-Token": session[1]}


async def probe(c: EmailConsole, session: Session, path: str) -> tuple[int, bytes]:
    answer = await c.client.request(
        "POST",
        "/v1/admin/orgs" + path,
        content=PROBE_BODY,
        headers=headers(session) | {"Content-Type": "application/json"},
    )
    return answer.status_code, answer.content


def counts(c: EmailConsole) -> dict[str, int]:
    """Every table a write or an audit entry could touch."""
    tables = (
        "alert_rules", "alert_channels", "channel_secrets", "alert_recipients", "audit_log",
        "confirmation_caps", "notification_deliveries",
    )  # fmt: skip
    return {t: c.db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


def code(content: bytes) -> str:
    return str(json.loads(content)["error"]["code"])


async def logins(c: EmailConsole, *names: str) -> dict[str, Session]:
    # Logged in before the counts are taken: a login is audited, a probe must not be.
    return {name: await c.login(name) for name in names}


async def test_the_rule_write_probe_answers_422_to_writers_and_forbidden_to_everyone_else() -> None:
    async with console() as c:
        s = await logins(c, "alice", "nina", "vera", "bob", "sid")
        before = counts(c)
        path = "/north/applications/north-shop/alert-rules"
        for who in ("alice", "nina"):
            status, content = await probe(c, s[who], path)
            assert (status, code(content)) == (422, "invalid_request"), who
        for who in ("vera", "bob", "sid"):
            assert await probe(c, s[who], path) == (403, FORBIDDEN_BYTES), who
        # Another application of the organization, and one that does not exist: the same bytes.
        for other in ("north-blog", "missing"):
            assert await probe(c, s["nina"], f"/north/applications/{other}/alert-rules") == (
                403,
                FORBIDDEN_BYTES,
            )
        assert counts(c) == before


async def test_the_channel_list_answers_org_admins_only_and_writes_nothing() -> None:
    async with console() as c:
        s = await logins(c, "alice", "vera", "nina", "bob", "sid")
        before = counts(c)
        listed = await c.client.get(
            "/v1/admin/orgs/north/alert-channels", headers=headers(s["alice"])
        )
        assert listed.status_code == 200
        for who in ("vera", "nina", "bob", "sid"):
            refused = await c.client.get(
                "/v1/admin/orgs/north/alert-channels", headers=headers(s[who])
            )
            assert (refused.status_code, refused.content) == (403, FORBIDDEN_BYTES), who
        assert counts(c) == before


async def test_a_channel_write_probe_would_be_audited() -> None:
    """Why channel writes follow the list rather than a dry run: an admin without an open window
    is refused ``step_up_required``, and that refusal is an audit row."""
    async with console() as c:
        s = await logins(c, "alice")
        before = counts(c)
        status, content = await probe(c, s["alice"], "/north/alert-channels")
        assert (status, code(content)) == (403, STEP_UP_REQUIRED)
        assert counts(c)["audit_log"] == before["audit_log"] + 1


async def test_the_confirm_probe_counts_no_wrong_entry_and_writes_nothing() -> None:
    async with console() as c:
        alice = await c.login("alice")
        made = await c.create_email(alice, [ADDRESS])
        channel, recipient = made["id"], made["recipients"][0]["id"]
        others = await logins(c, "vera", "nina", "bob", "sid")
        before = counts(c)
        path = f"/north/alert-channels/{channel}/recipients/{recipient}/confirm"
        # More probes than the five wrong entries that would void the code.
        for _ in range(7):
            status, content = await probe(c, alice, path)
            assert (status, code(content)) == (422, "invalid_request")
        for who, session in others.items():
            assert await probe(c, session, path) == (403, FORBIDDEN_BYTES), who
        assert counts(c) == before
        # The code still verifies: none of the probes was a wrong entry.
        right = await c.call(alice, "POST", path, {"code": c.mailer.sent[-1].shown_code})
        assert right.status_code == 200, right.text
        assert right.json()["state"] == "verified"


@pytest.mark.parametrize(("asked", "follower"), SIBLINGS)
def test_sibling_rows_are_equal(asked: str, follower: str) -> None:
    """The console shows ``follower``'s control on ``asked``'s answer (note 49): the same level
    and the same roles at the same relations, or this fails."""
    a, b = PERMISSIONS[asked], PERMISSIONS[follower]
    assert a.level == b.level
    assert a.allowed == b.allowed
