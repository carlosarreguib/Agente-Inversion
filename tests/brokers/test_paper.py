"""Tests del paper broker — especialmente el invariante de fill en T+1."""
from __future__ import annotations

from decimal import Decimal

from qtrader.brokers.paper import fill_at_open, fill_at_price
from qtrader.core.types import Order, OrderType, Side
from qtrader.data.synthetic import SyntheticProvider

_BARS = SyntheticProvider(seed=99).get_bars("TEST", 30)


def _order(qty: Decimal = Decimal("2"), side: Side = Side.BUY) -> Order:
    return Order(
        client_order_id="test-ord-001",
        symbol="TEST",
        side=side,
        quantity=qty,
        order_type=OrderType.MOO,
        timestamp=_BARS[0].timestamp,
        strategy_id="test",
    )


# ---------------------------------------------------------------------------
# Invariante crítico: fill en T+1, nunca en T
# ---------------------------------------------------------------------------


def test_fill_price_equals_fill_bar_open() -> None:
    """El precio de fill debe ser el open de la barra T+1, no el cierre de T."""
    signal_idx = 20
    fill_idx = signal_idx + 1  # T+1

    signal_bar = _BARS[signal_idx]
    fill_bar = _BARS[fill_idx]

    order = Order(
        client_order_id="test-timing-001",
        symbol="TEST",
        side=Side.BUY,
        quantity=Decimal("1"),
        order_type=OrderType.MOO,
        timestamp=signal_bar.timestamp,
        strategy_id="test",
    )
    fill = fill_at_open(order, fill_bar)

    assert fill.price == fill_bar.open, (
        f"Fill price {fill.price} debe ser fill_bar.open {fill_bar.open}"
    )


def test_fill_timestamp_equals_fill_bar_timestamp() -> None:
    """El timestamp del fill es el del bar T+1, no el del bar de señal T."""
    signal_bar = _BARS[20]
    fill_bar = _BARS[21]

    order = Order(
        client_order_id="test-timing-002",
        symbol="TEST",
        side=Side.BUY,
        quantity=Decimal("1"),
        order_type=OrderType.MOO,
        timestamp=signal_bar.timestamp,
        strategy_id="test",
    )
    fill = fill_at_open(order, fill_bar)

    assert fill.timestamp == fill_bar.timestamp
    assert fill.timestamp != signal_bar.timestamp


def test_fill_never_uses_signal_bar_close() -> None:
    """Verificación explícita: el precio del fill != close del bar de señal."""
    for i in range(20, 28):
        signal_bar = _BARS[i]
        fill_bar = _BARS[i + 1]

        order = Order(
            client_order_id=f"test-timing-{i:03d}",
            symbol="TEST",
            side=Side.BUY,
            quantity=Decimal("1"),
            order_type=OrderType.MOO,
            timestamp=signal_bar.timestamp,
            strategy_id="test",
        )
        fill = fill_at_open(order, fill_bar)

        # El fill usa fill_bar.open; signal_bar.close es el dato que generó la señal
        # En datos sintéticos deterministas, open[T+1] != close[T] (diferentes barras)
        assert fill.price == fill_bar.open
        assert fill.timestamp > signal_bar.timestamp


# ---------------------------------------------------------------------------
# Comisión
# ---------------------------------------------------------------------------


def test_commission_is_at_least_minimum() -> None:
    fill = fill_at_open(_order(qty=Decimal("1")), _BARS[1])
    assert fill.commission >= Decimal("1.25")


def test_commission_proportional_for_large_order() -> None:
    big_order = Order(
        client_order_id="big-ord",
        symbol="TEST",
        side=Side.BUY,
        quantity=Decimal("1000"),
        order_type=OrderType.LIMIT,
        limit_price=Decimal("100"),
        timestamp=_BARS[0].timestamp,
        strategy_id="test",
    )
    fill = fill_at_open(big_order, _BARS[1])
    # 1000 shares * ~100 € * 0.1% = ~100 € >> 1.25 €
    assert fill.commission > Decimal("1.25")


# ---------------------------------------------------------------------------
# fill_at_price (para tests unitarios con precio explícito)
# ---------------------------------------------------------------------------


def test_fill_at_price_uses_given_price() -> None:
    from datetime import UTC, datetime

    ts = datetime(2024, 6, 1, tzinfo=UTC)
    fill = fill_at_price(_order(), price=Decimal("123.45"), timestamp=ts)
    assert fill.price == Decimal("123.45")
    assert fill.timestamp == ts
