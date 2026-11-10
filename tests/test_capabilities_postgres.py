"""``GET /v1/auth/capabilities`` on two instances over one Postgres (docs/50): the answer reads the
roles, the organization and its applications on each request, so a change made through one
instance is in the next answer of the other. Marked ``postgres``: run when
``GATEWAY_TEST_POSTGRES_URL`` is set (CI's ``postgres`` job). Test values, not secrets."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import httpx
import pytest

from accounts_helpers import PASSWORD
from gateway.apikeys import ApiKeyService, ApplicationAccess
from gateway.app import CAPABILITIES_PATH, create_app
from gateway.auth import AccountAccess, AccountService
from gateway.clock import ClockEpoch, SimClock
from gateway.continuous import build_continuous_registry
from gateway.localonly import LocalOnlyPolicy
from gateway.management import ManagementService
from gateway.organizations import new_organization
from gateway.passwords import CHEAP, PasswordHasher
from gateway.postgres import PostgresApplicationStore, PostgresDatabase, postgres_state
from gateway.routing import load_plans
from gateway.users import Bootstrap
from helpers import TEST_PEPPER
from postgres_helpers import pg_accounts, pg_world

pytestmark = pytest.mark.postgres


class Instance:
    """One gateway instance: its account service and an HTTP client to its app."""

    def __init__(self, service: AccountService, client: httpx.AsyncClient) -> None:
        self.service = service
        self.client = client

    async def login(self, username: str) -> str:
        answer = await self.client.post(
            "/v1/auth/login", json={"username": username, "password": PASSWORD}
        )
        self.client.cookies.clear()
        assert answer.status_code == 200, answer.text
        return str(answer.headers["set-cookie"].split(";")[0].split("=", 1)[1])

    async def capabilities(self, session: str) -> Any:
        answer = await self.client.get(
            CAPABILITIES_PATH, headers={"Cookie": f"gw_session={session}"}
        )
        assert answer.status_code == 200, answer.text
        return answer.json()


@asynccontextmanager
async def instance(db: PostgresDatabase, seed: int) -> AsyncIterator[Instance]:
    clock = SimClock()
    state = postgres_state(db)
    keys = ApiKeyService(state.applications, TEST_PEPPER, ClockEpoch(clock), store_clock=db)
    service = pg_accounts(db, seed=seed)
    registry = build_continuous_registry(clock, seed=7)
    app = create_app(
        registry,
        clock,
        applications=ApplicationAccess(keys, load_plans()),
        accounts=AccountAccess(service),
        usage_store=state.usage,
        epoch=ClockEpoch(clock),
        modes=state.modes,
        local_only=LocalOnlyPolicy(),
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield Instance(service, client)
    await registry.aclose()


async def ready(service: AccountService, username: str, organization: str) -> None:
    """A user who has already chosen their own password."""
    user = await service.create_user(username, PASSWORD, organization)
    await service.store.set_password(
        user.id, PasswordHasher(CHEAP).hash(PASSWORD), must_change=False, bootstrap=Bootstrap.NONE
    )


def allowed(actions: dict[str, bool]) -> set[str]:
    return {a for a, yes in actions.items() if yes}


async def test_a_role_and_an_organization_changed_on_one_instance_are_seen_by_the_other() -> None:
    async with pg_world(instances=2) as world, AsyncExitStack() as stack:
        a, b = [
            await stack.enter_async_context(instance(db, seed=i + 1))
            for i, db in enumerate(world.instances)
        ]
        await PostgresApplicationStore(world.instances[0]).add_organization(
            new_organization("north", "North", "business", created_at_ms=1.0)
        )
        await ready(a.service, "alice", "north")
        await ready(a.service, "keeper", "north")
        # Revoking alice's role must not leave north without an org_admin.
        await a.service.grant_role("keeper", "org_admin", "organization", "north")
        operator = ManagementService(
            ApiKeyService(
                PostgresApplicationStore(world.instances[0]),
                TEST_PEPPER,
                ClockEpoch(SimClock()),
                store_clock=world.instances[0],
            ),
            load_plans(),
            a.service.audit,
            ClockEpoch(SimClock()),
        )
        await operator.create_application("north", "Shop", "pro", application_id="north-shop")
        session = await b.login("alice")
        assert allowed((await b.capabilities(session))["organization"]) == set()

        # Granted on A: B's next answer has it, and names the application.
        await a.service.grant_role("alice", "org_admin", "organization", "north")
        answer = await b.capabilities(session)
        assert "org_settings.read" in allowed(answer["organization"])
        assert [x["application_id"] for x in answer["applications"]] == ["north-shop"]

        # The organization disabled on A: B answers nothing allowed.
        await PostgresApplicationStore(world.instances[0]).set_organization_disabled("north", True)
        answer = await b.capabilities(session)
        assert allowed(answer["organization"]) == set()
        assert answer["applications"] == []
        await PostgresApplicationStore(world.instances[0]).set_organization_disabled("north", False)
        assert "org_settings.read" in allowed((await b.capabilities(session))["organization"])

        # Revoked on A: gone from B's next answer.
        await a.service.revoke_role("alice", "org_admin", "organization", "north")
        answer = await b.capabilities(session)
        assert allowed(answer["organization"]) == set()
        assert answer["applications"] == []
        assert answer["roles"] == []
