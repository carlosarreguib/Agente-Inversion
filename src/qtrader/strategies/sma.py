from __future__ import annotations

from decimal import Decimal

from qtrader.core.types import Bar, Direction, Signal

_SMA_PERIOD = 20
_STRATEGY_ID = "sma20"


def generate_signal(bars: list[Bar]) -> Signal | None:
    """Genera señal SMA20 a partir del cierre de la última barra.

    Devuelve None si hay menos de `_SMA_PERIOD` barras (no hay suficiente historia).
    La señal se genera con datos de bars[-1].timestamp — NUNCA se usa la siguiente barra.
    """
    if len(bars) < _SMA_PERIOD:
        return None

    closes = [b.close for b in bars[-_SMA_PERIOD:]]
    sma20: Decimal = sum(closes, Decimal("0")) / Decimal(str(_SMA_PERIOD))

    last_bar = bars[-1]
    direction = Direction.LONG if last_bar.close > sma20 else Direction.FLAT

    return Signal(
        symbol=last_bar.symbol,
        timestamp=last_bar.timestamp,
        direction=direction,
        strength=Decimal("1"),
        strategy_id=_STRATEGY_ID,
    )
