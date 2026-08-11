"""Estrategia momentum simple para el golden backtest (T2.3).

Regla: si close[T] > SMA(close, 20)[T] -> señal LONG con fuerza=1.
       en caso contrario -> sin señal (FLAT implícito).

La estrategia es stateful (acumula historial de cierres) pero determinista:
dado el mismo proveedor con la misma semilla, produce exactamente las mismas
señales en el mismo orden. Sin I/O, sin red, sin estado global.

También contiene GoldenEqualWeightPortfolio: convierte señales LONG en
posiciones de tamaño fijo. Solo opera cuando la señal cambia (entrada/salida),
evitando rebalanceos diarios que generarían cientos de trades pequeños.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import ROUND_DOWN, Decimal

from qtrader.backtesting.engine import SandboxedDataView  # noqa: TCH001 — usado en firma de metodo
from qtrader.core.types import (  # noqa: TCH001 — usados en firmas de método
    Direction,
    Instrument,
    Position,
    Signal,
    TargetPosition,
)

_ZERO = Decimal("0")
_ONE = Decimal("1")
_SMA_PERIOD = 20


class MomentumStrategy:
    """Momentum simple: close > SMA20 -> LONG, peso igual.

    Implementa el protocolo Strategy de BacktestEngine.
    Mantiene un historial de cierres por simbolo para calcular la SMA.
    """

    def __init__(self, sma_period: int = _SMA_PERIOD) -> None:
        self._period = sma_period
        self._close_history: dict[str, list[Decimal]] = {}

    def on_bar(
        self,
        trading_day: date,
        universe: list[Instrument],
        data: SandboxedDataView,
    ) -> list[Signal]:
        ts = datetime(trading_day.year, trading_day.month, trading_day.day, tzinfo=UTC)
        signals: list[Signal] = []

        for inst in universe:
            symbol = inst.ticker_proxy
            bars = data.get_bars(symbol, trading_day, trading_day)
            if not bars:
                continue

            close = bars[0].bar.close
            history = self._close_history.setdefault(symbol, [])
            history.append(close)
            if len(history) > self._period + 1:
                history.pop(0)

            if len(history) < self._period:
                continue

            sma = sum(history[-self._period:], _ZERO) / Decimal(self._period)
            if close > sma:
                signals.append(Signal(
                    symbol=symbol,
                    timestamp=ts,
                    direction=Direction.LONG,
                    strength=_ONE,
                    strategy_id="momentum_sma20",
                ))

        return signals


class GoldenEqualWeightPortfolio:
    """Portfolio constructor: equal weight entre señales LONG.

    Opera SOLO cuando la señal cambia (entrada o salida del universo momentum).
    Si un simbolo ya esta en posicion y sigue con señal LONG, no genera orden.
    Si un simbolo nuevo entra con señal LONG, compra con la asignacion inicial.
    Si un simbolo pierde la señal LONG, vende toda la posicion.

    El sizing usa floor(initial_equity / n_slots / close[T]) al entrar.
    Esto evita rebalanceos diarios causados por variaciones del cash.
    """

    def __init__(
        self,
        initial_equity: Decimal,
        n_slots: int,
        min_order_eur: Decimal = Decimal("1000"),
    ) -> None:
        self._initial_equity = initial_equity
        self._n_slots = n_slots
        self._min_order_eur = min_order_eur
        self._last_close: dict[str, Decimal] = {}

    def update_close(self, symbol: str, close: Decimal) -> None:
        """Llamado por el harness del test despues de cada BarEvent."""
        self._last_close[symbol] = close

    def build(
        self,
        signals: list[Signal],
        positions: dict[str, Position],
        universe: list[Instrument],  # noqa: ARG002
        equity: Decimal,  # noqa: ARG002 — no se usa (evita rebalanceos por cash)
    ) -> list[TargetPosition]:
        long_signals = [s for s in signals if s.direction == Direction.LONG]
        current_long_set = {s.symbol for s in long_signals}
        targets: list[TargetPosition] = []

        weight = _ONE / Decimal(self._n_slots)

        # Nuevas entradas: señal LONG sin posicion abierta
        for symbol in current_long_set - set(positions.keys()):
            price = self._last_close.get(symbol)
            if price is None or price <= _ZERO:
                continue
            alloc = self._initial_equity * weight
            if alloc < self._min_order_eur:
                continue
            qty = (alloc / price).to_integral_value(rounding=ROUND_DOWN)
            if qty <= _ZERO:
                continue
            targets.append(TargetPosition(symbol=symbol, quantity=qty, weight=weight))

        # Salidas: posicion abierta sin señal LONG
        for symbol in set(positions.keys()) - current_long_set:
            targets.append(TargetPosition(symbol=symbol, quantity=_ZERO, weight=_ZERO))

        return targets
