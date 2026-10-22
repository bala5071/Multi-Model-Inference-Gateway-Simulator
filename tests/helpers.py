"""Shared test fixtures that are plain values, not pytest fixtures."""

from __future__ import annotations

import os
import random
import zlib
from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType
from typing import Any

from gateway.apikeys import ApiKeyService, ApplicationAccess
from gateway.applications import (
    InMemoryApplicationStore,
    SqliteApplicationStore,
    application_from_template,
    seed_demo_applications,
)
from gateway.backends import Backend, BackendTokenCallback, Generation
from gateway.clock import Clock, ClockEpoch, SystemEpoch
from gateway.database import open_database
from gateway.migrations import MIGRATIONS, MigrationRunner
from gateway.profiles import ModelProfile, Tier
from gateway.routing import PlansConfig, load_plans
from gateway.schema import GenerateRequest

# Every value is a power of two (or a small multiple), so expected times computed by hand
# are exact in floating point and can be compared with ==.
EXACT = ModelProfile(
    name="exact-test-model",
    tier=Tier.STANDARD,
    price_per_1k_input_tokens=0.5,
    price_per_1k_output_tokens=1.0,
    prefill_ms_per_token=0.5,
    decode_base_ms=4.0,
    decode_ms_per_extra_seq=0.25,
    max_batch_size=4,
    kv_capacity_tokens=1024,
)

# TEST_PROFILES: hand-made fixtures, not data.
#
# Three round-numbered profiles that tests use when they need exact, hand-checkable timings
# on three tiers with clearly different shapes. They are the invented numbers of the first
# drafts of this project and were never measured. Nothing outside tests/ may import them
# (tests/test_profiles.py enforces that) and no document may quote a result from them.
TEST_ECONOMY = ModelProfile(
    name="llama-3.2-1b",
    tier=Tier.ECONOMY,
    price_per_1k_input_tokens=0.0001,
    price_per_1k_output_tokens=0.0002,
    prefill_ms_per_token=0.05,
    decode_base_ms=6.0,
    decode_ms_per_extra_seq=0.2,
    max_batch_size=32,
    kv_capacity_tokens=65_536,
)
TEST_STANDARD = ModelProfile(
    name="llama-3.2-3b",
    tier=Tier.STANDARD,
    price_per_1k_input_tokens=0.0003,
    price_per_1k_output_tokens=0.0006,
    prefill_ms_per_token=0.15,
    decode_base_ms=12.0,
    decode_ms_per_extra_seq=0.6,
    max_batch_size=16,
    kv_capacity_tokens=32_768,
)
TEST_PREMIUM = ModelProfile(
    name="llama-3.1-8b",
    tier=Tier.PREMIUM,
    price_per_1k_input_tokens=0.0010,
    price_per_1k_output_tokens=0.0020,
    prefill_ms_per_token=0.60,
    decode_base_ms=30.0,
    decode_ms_per_extra_seq=2.5,
    max_batch_size=8,
    kv_capacity_tokens=16_384,
)
TEST_PROFILES: Mapping[Tier, ModelProfile] = MappingProxyType(
    {p.tier: p for p in (TEST_ECONOMY, TEST_STANDARD, TEST_PREMIUM)}
)


class SizedBackend(Backend):
    """Token counts looked up by request id, so tests control the workload exactly.

    An id that is not in ``sizes`` makes ``generate`` raise ``KeyError``.
    """

    def __init__(self, sizes: Mapping[str, tuple[int, int]]) -> None:
        self._sizes = dict(sizes)

    async def generate(
        self, request: GenerateRequest, *, on_token: BackendTokenCallback | None = None
    ) -> Generation:
        tokens_in, tokens_out = self._sizes[request.request_id]
        return Generation(
            prompt_tokens=unique_tokens(request.request_id, tokens_in), tokens_out=tokens_out
        )


def unique_tokens(request_id: str, count: int) -> tuple[int, ...]:
    """``count`` token ids that no other request id shares, so nothing is a cache hit."""
    base = zlib.crc32(request_id.encode()) * 1_000_000
    return tuple(base + i for i in range(count))


# --- applications and keys -----------------------------------------------------------------

# A test pepper: not a secret, and never used outside tests.
TEST_PEPPER = b"test-pepper-not-a-secret-0123456789abcdef"


def application_access(
    clock: Clock,
    templates: PlansConfig | None = None,
    *,
    apps: Mapping[str, str] | None = None,
    limits: Mapping[str, tuple[int | None, int | None]] | None = None,
    seed: int = 0,
) -> tuple[ApplicationAccess, dict[str, str]]:
    """In-memory applications with one key each, as ``create_app`` takes them.

    ``apps`` maps application id to template plan; without it the templates' demo
    applications are created. ``limits`` gives (max_tokens, requests_per_minute) per
    application. Returns the access and each application's key. Key material comes from a
    seeded ``random.Random``, so tests are repeatable.
    """
    templates = templates if templates is not None else load_plans()
    store = InMemoryApplicationStore()
    epoch = ClockEpoch(clock)
    if apps is None:
        seed_demo_applications(store, templates, created_at_ms=epoch.epoch_ms())
    else:
        for app_id, plan in apps.items():
            application = application_from_template(
                app_id, app_id, templates, plan, created_at_ms=epoch.epoch_ms()
            )
            if limits and app_id in limits:
                max_tokens, rpm = limits[app_id]
                application = replace(application, max_tokens=max_tokens, requests_per_minute=rpm)
            store.add_application(application)
    rng = random.Random(seed)
    keys = ApiKeyService(store, TEST_PEPPER, epoch, random_bytes=rng.randbytes)
    issued = {a.id: keys.issue(a.id, "test").key for a in store.list_applications()}
    return ApplicationAccess(keys, templates), issued


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


# Well formed, but matches no record: what a test sends for an application with no key.
UNKNOWN_KEY = "gwk_" + "0" * 16 + "_" + "A" * 43


def tenant_headers(body: dict[str, Any], keys: Mapping[str, str]) -> dict[str, str]:
    """Remove a test's ``tenant_id`` from a request body and return the Authorization header
    of that application's key instead (``UNKNOWN_KEY`` for an application that has none)."""
    tenant = body.pop("tenant_id", None)
    if tenant is None:
        return {}
    return bearer(keys.get(tenant, UNKNOWN_KEY))


def default_server_key(application_id: str = "acme") -> str:
    """Create the demo applications in the default server's database (GATEWAY_DATABASE,
    set by conftest) and issue a key for one of them, as scripts/manage.py would."""
    connection = open_database(os.environ["GATEWAY_DATABASE"])
    epoch = SystemEpoch()
    MigrationRunner(connection, MIGRATIONS, epoch).migrate()
    store = SqliteApplicationStore(connection)
    seed_demo_applications(store, load_plans(), created_at_ms=epoch.epoch_ms())
    key = ApiKeyService(store, TEST_PEPPER, epoch).issue(application_id, "test").key
    connection.close()
    return key
