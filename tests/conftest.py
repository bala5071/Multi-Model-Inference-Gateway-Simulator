from __future__ import annotations

import os
from pathlib import Path

import pytest

from gateway.apikeys import PEPPER_ENV
from helpers import TEST_PEPPER

REAL_MODELS_ENV = "GATEWAY_REAL_MODELS"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip tests marked ``real_models`` unless they were asked for.

    They need model servers running on this machine (see docs/12-real-models.md), which CI
    does not have. Run them with ``GATEWAY_REAL_MODELS=1 uv run pytest -m real_models``.
    """
    if os.environ.get(REAL_MODELS_ENV) == "1":
        return
    skip = pytest.mark.skip(reason=f"needs real model servers; set {REAL_MODELS_ENV}=1 to run")
    for item in items:
        if "real_models" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def default_server_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test's default server uses a database of its own and the test pepper.

    So no test writes gateway.db into the working tree, and ``create_default_app`` can start.
    A test of what happens without the pepper removes it with ``monkeypatch.delenv``.
    """
    monkeypatch.setenv("GATEWAY_DATABASE", str(tmp_path / "gateway.db"))
    monkeypatch.setenv(PEPPER_ENV, TEST_PEPPER.decode())
