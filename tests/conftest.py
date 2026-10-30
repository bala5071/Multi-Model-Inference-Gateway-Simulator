from __future__ import annotations

import asyncio
import faulthandler
import functools
import inspect
import os
import sqlite3
import sys
import threading
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from gateway.apikeys import PEPPER_ENV
from gateway.auth import ADMIN_PASSWORD_ENV, ADMIN_USERNAME_ENV
from gateway.passwords import CHEAP, MEMORY_ENV, PARALLELISM_ENV, TIME_COST_ENV
from gateway.vault import VAULT_KEY_ENV
from helpers import TEST_ADMIN_PASSWORD, TEST_ADMIN_USERNAME, TEST_PEPPER, TEST_VAULT_KEY

REAL_MODELS_ENV = "GATEWAY_REAL_MODELS"
# A Valkey for the tests marked valkey (docker-compose.valkey.yml), e.g. redis://127.0.0.1:6380/0.
VALKEY_URL_ENV = "GATEWAY_TEST_VALKEY_URL"
# A Postgres for the tests marked postgres (docker-compose.postgres.yml), e.g.
# postgresql://postgres@127.0.0.1:5433/postgres. Each test works in a schema of its own.
POSTGRES_URL_ENV = "GATEWAY_TEST_POSTGRES_URL"
# Live calls to hosted providers (docs/31): never in CI, never by default.
LIVE_PROVIDERS_ENV = "GATEWAY_LIVE_PROVIDERS"


# The suite-wide limit on one test, in seconds (``gateway_test_timeout_s`` in pyproject.toml; a
# run may change it with ``-o gateway_test_timeout_s=N``). No test can hang on a lock or on
# another instance: an async test past it is cancelled and fails; anything still running at
# twice it, a sync test or a wait cancelling cannot reach, ends the run with every thread's
# traceback.
TIMEOUT_INI = "gateway_test_timeout_s"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addini(TIMEOUT_INI, "the limit on one test, in seconds", default="60")


def _limit_s(config: pytest.Config) -> float:
    return float(config.getini(TIMEOUT_INI))


def _end_the_run(item: pytest.Item, after_s: float) -> None:
    """Out of pytest's capture, so it is seen: which test, and where every thread waits."""
    capture = item.config.pluginmanager.getplugin("capturemanager")
    if capture is not None:
        capture.suspend_global_capture(in_=True)
    sys.stderr.write(
        f"\n{item.nodeid}: still running after {after_s:g} s, twice the limit per test "
        f"({TIMEOUT_INI}); ending the run\n"
    )
    sys.stderr.flush()
    faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
    sys.stderr.flush()
    os._exit(1)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item: pytest.Item) -> Generator[None, object, object]:
    """The hard limit: setup, call and teardown of one test, whatever they wait on."""
    after_s = 2 * _limit_s(item.config)
    watchdog = threading.Timer(after_s, _end_the_run, (item, after_s))
    watchdog.daemon = True
    watchdog.start()
    try:
        return (yield)
    finally:
        watchdog.cancel()


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, object, object]:
    """The limit on an async test: cancelled at it, and failed with the limit named."""
    if not isinstance(item, pytest.Function) or not inspect.iscoroutinefunction(item.obj):
        return (yield)
    test, limit = item.obj, _limit_s(item.config)

    @functools.wraps(test)
    async def bounded(*args: Any, **kwargs: Any) -> Any:
        deadline = asyncio.timeout(limit)
        try:
            async with deadline:
                return await test(*args, **kwargs)
        except TimeoutError:
            if deadline.expired():
                pytest.fail(f"over the suite's limit of {limit:g} s per test ({TIMEOUT_INI})")
            raise

    item.obj = bounded
    try:
        return (yield)
    finally:
        item.obj = test


def pytest_report_header(config: pytest.Config) -> str:
    """The SQLite the stores and migrations run on (it is whatever Python was built with)."""
    return (
        f"SQLite {sqlite3.sqlite_version} (Python's sqlite3 module); "
        f"limit per test {_limit_s(config):g} s, the run ends at {2 * _limit_s(config):g} s"
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip tests marked ``real_models`` or ``live_provider`` unless they were asked for.

    They need model servers running on this machine (see docs/12-real-models.md), which CI
    does not have. Run them with ``GATEWAY_REAL_MODELS=1 uv run pytest -m real_models``.
    """
    valkey = bool(os.environ.get(VALKEY_URL_ENV))
    skip_valkey = pytest.mark.skip(
        reason=f"needs Valkey (docker-compose.valkey.yml); set {VALKEY_URL_ENV} to run"
    )
    postgres = bool(os.environ.get(POSTGRES_URL_ENV))
    skip_postgres = pytest.mark.skip(
        reason=f"needs Postgres (docker-compose.postgres.yml); set {POSTGRES_URL_ENV} to run"
    )
    real = os.environ.get(REAL_MODELS_ENV) == "1"
    live = os.environ.get(LIVE_PROVIDERS_ENV) == "1"
    skip_real = pytest.mark.skip(reason=f"needs real model servers; set {REAL_MODELS_ENV}=1 to run")
    skip_live = pytest.mark.skip(
        reason=f"calls a hosted provider and can cost money; set {LIVE_PROVIDERS_ENV}=1 to run"
    )
    for item in items:
        if "postgres" in item.keywords and not postgres:
            item.add_marker(skip_postgres)
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
