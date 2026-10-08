from __future__ import annotations

from importlib.metadata import version

import gateway


def test_version_matches_package_metadata() -> None:
    assert gateway.__version__ == version("gateway")
