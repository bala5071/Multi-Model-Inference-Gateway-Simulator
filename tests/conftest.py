from __future__ import annotations

import os

import pytest

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
