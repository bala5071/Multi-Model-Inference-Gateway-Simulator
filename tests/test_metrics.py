from __future__ import annotations

import pytest

from gateway.metrics import percentile


def test_percentile_uses_nearest_rank() -> None:
    values = [float(v) for v in range(1, 101)]
    assert percentile(values, 50) == 50.0
    assert percentile(values, 99) == 99.0
    assert percentile(values, 100) == 100.0
    assert percentile(values, 0.5) == 1.0


def test_percentile_returns_an_input_value_and_ignores_order() -> None:
    assert percentile([30.0, 10.0, 20.0], 50) == 20.0
    assert percentile([30.0, 10.0, 20.0], 99) == 30.0
    assert percentile([10.0, 20.0, 30.0, 40.0], 50) == 20.0
    assert percentile([7.0], 99) == 7.0


def test_percentile_accepts_any_iterable() -> None:
    assert percentile((float(v) for v in (3, 1, 2)), 100) == 3.0


@pytest.mark.parametrize("q", [0, -1, 100.1])
def test_percentile_rejects_q_out_of_range(q: float) -> None:
    with pytest.raises(ValueError, match="q must be"):
        percentile([1.0], q)


def test_percentile_of_nothing_is_an_error() -> None:
    with pytest.raises(ValueError, match="no values"):
        percentile([], 50)
