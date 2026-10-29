from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from gateway.apikeys import PEPPER_ENV
from gateway.auth import ADMIN_PASSWORD_ENV, ADMIN_USERNAME_ENV
from gateway.passwords import CHEAP, MEMORY_ENV, PARALLELISM_ENV, TIME_COST_ENV
from gateway.vault import VAULT_KEY_ENV
from helpers import TEST_ADMIN_PASSWORD, TEST_ADMIN_USERNAME, TEST_PEPPER, TEST_VAULT_KEY

REAL_MODELS_ENV = "GATEWAY_REAL_MODELS"
# A Valkey for the tests marked valkey (docker-compose.valkey.yml), e.g. redis://127.0.0.1:6380/0.
VALKEY_URL_ENV = "GATEWAY_TEST_VALKEY_URL"
# Live calls to hosted providers (docs/31): never in CI, never by default.
LIVE_PROVIDERS_ENV = "GATEWAY_LIVE_PROVIDERS"


def pytest_report_header(config: pytest.Config) -> str:
    """The SQLite the stores and migrations run on (it is whatever Python was built with)."""
    return f"SQLite {sqlite3.sqlite_version} (Python's sqlite3 module)"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip tests marked ``real_models`` or ``live_provider`` unless they were asked for.

    They need model servers running on this machine (see docs/12-real-models.md), which CI
    does not have. Run them with ``GATEWAY_REAL_MODELS=1 uv run pytest -m real_models``.
    """
    valkey = bool(os.environ.get(VALKEY_URL_ENV))
    skip_valkey = pytest.mark.skip(
        reason=f"needs Valkey (docker-compose.valkey.yml); set {VALKEY_URL_ENV} to run"
    )
    real = os.environ.get(REAL_MODELS_ENV) == "1"
    live = os.environ.get(LIVE_PROVIDERS_ENV) == "1"
    skip_real = pytest.mark.skip(reason=f"needs real model servers; set {REAL_MODELS_ENV}=1 to run")
    skip_live = pytest.mark.skip(
        reason=f"calls a hosted provider and can cost money; set {LIVE_PROVIDERS_ENV}=1 to run"
    )
    for item in items:
        if "valkey" in item.keywords and not valkey:
            item.add_marker(skip_valkey)
        if "real_models" in item.keywords and not real:
            item.add_marker(skip_real)
        if "live_provider" in item.keywords and not live:
            item.add_marker(skip_live)


@pytest.fixture(autouse=True)
def default_server_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test's default server uses a database of its own, the test pepper and a test
    admin.

    So no test writes gateway.db into the working tree, and ``create_default_app`` can start.
    A test of what happens without the pepper removes it with ``monkeypatch.delenv``.
    """
    monkeypatch.setenv("GATEWAY_DATABASE", str(tmp_path / "gateway.db"))
    monkeypatch.setenv(PEPPER_ENV, TEST_PEPPER.decode())
    monkeypatch.setenv(VAULT_KEY_ENV, TEST_VAULT_KEY)
    # Its bootstrap admin, and argon2 at the lowest cost, so starting it stays fast.
    monkeypatch.setenv(ADMIN_USERNAME_ENV, TEST_ADMIN_USERNAME)
    monkeypatch.setenv(ADMIN_PASSWORD_ENV, TEST_ADMIN_PASSWORD)
    monkeypatch.setenv(MEMORY_ENV, str(CHEAP.memory_cost_kib))
    monkeypatch.setenv(TIME_COST_ENV, str(CHEAP.time_cost))
    monkeypatch.setenv(PARALLELISM_ENV, str(CHEAP.parallelism))
