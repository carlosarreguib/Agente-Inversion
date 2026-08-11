from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

from qtrader.core.types import Bar, Fill, Order

_MIN_COMMISSION = Decimal("1.25")
_COMMISSION_RATE = Decimal("0.001")  # 0,10 %
_ZERO = Decimal("0")


def fill_at_open(order: Order, fill_bar: Bar) -> Fill:
    """Ejecuta la orden al precio de apertura de fill_bar (la barra T+1).

    Invariante anti-look-ahead: la señal se genera con datos de T;
    el precio de ejecución es siempre fill_bar.open, nunca el cierre de T.
    """
    return _fill(order, price=fill_bar.open, timestamp=fill_bar.timestamp)


def fill_at_price(order: Order, price: Decimal, timestamp: datetime) -> Fill:
    """Ejecuta la orden a un precio explícito (para tests unitarios)."""
    return _fill(order, price=price, timestamp=timestamp)


def _fill(order: Order, price: Decimal, timestamp: datetime) -> Fill:
    nominal = order.quantity * price
    commission = max(_MIN_COMMISSION, nominal * _COMMISSION_RATE)

    return Fill(
        client_order_id=order.client_order_id,
        fill_id=f"fill-{order.client_order_id}",
        symbol=order.symbol,
        side=order.side,
        quantity=order.quantity,
        price=price,
        commission=commission,
        timestamp=timestamp,
    )
