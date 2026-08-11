"""Tests de la estrategia SMA20."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from qtrader.core.types import Bar, Direction
from qtrader.strategies.sma import generate_signal

_BASE_TS = datetime(2024, 1, 2, tzinfo=UTC)


def _bar(close: Decimal, day: int = 0) -> Bar:
    return Bar(
        symbol="SPY",
        timestamp=_BASE_TS + timedelta(days=day),
        open=close,
        high=close + Decimal("1"),
        low=close - Decimal("1"),
        close=close,
        volume=Decimal("1000000"),
    )


def _flat_bars(price: Decimal, n: int) -> list[Bar]:
    return [_bar(price, i) for i in range(n)]


def test_returns_none_for_fewer_than_20_bars() -> None:
    bars = _flat_bars(Decimal("100"), 19)
    assert generate_signal(bars) is None


def test_returns_none_for_empty_list() -> None:
    assert generate_signal([]) is None


def test_returns_signal_with_exactly_20_bars() -> None:
    bars = _flat_bars(Decimal("100"), 20)
    signal = generate_signal(bars)
    assert signal is not None


def test_flat_bars_return_flat_direction() -> None:
    """Con close == SMA20, la señal es FLAT (no LONG)."""
    bars = _flat_bars(Decimal("100"), 25)
    signal = generate_signal(bars)
    assert signal is not None
    assert signal.direction == Direction.FLAT


def test_uptrend_returns_long() -> None:
    """Últimas barras por encima de la SMA → LONG."""
    bars = _flat_bars(Decimal("100"), 19)
    # Añadir una barra con close muy por encima de la media de las 20 anteriores
    bars.append(_bar(Decimal("200"), 19))
    signal = generate_signal(bars)
    assert signal is not None
    assert signal.direction == Direction.LONG


def test_downtrend_returns_flat() -> None:
    """Última barra por debajo de la SMA → FLAT."""
    bars = _flat_bars(Decimal("100"), 19)
    bars.append(_bar(Decimal("50"), 19))
    signal = generate_signal(bars)
    assert signal is not None
    assert signal.direction == Direction.FLAT


def test_signal_timestamp_is_last_bar_timestamp() -> None:
    bars = _flat_bars(Decimal("100"), 25)
    signal = generate_signal(bars)
    assert signal is not None
    assert signal.timestamp == bars[-1].timestamp


def test_signal_symbol_matches_bars() -> None:
    bars = _flat_bars(Decimal("100"), 20)
    signal = generate_signal(bars)
    assert signal is not None
    assert signal.symbol == "SPY"
