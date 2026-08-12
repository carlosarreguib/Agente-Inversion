"""Momentum cross-sectional (12-1) — T3.1.

Señal: retorno de 12 meses excluyendo el ultimo mes.
  momentum_score(t) = close(t-21) / close(t-252) - 1

Ranking transversal ese dia:
  Top 20 %  → Direction.LONG  (señal positiva)
  Resto     → Direction.FLAT  (sin posición, no short)

Rebalanceo semanal (viernes habiles). Entre rebalanceos se mantienen las señales
del ultimo rebalanceo (el motor las aplica cada dia pero portfolio constructor
solo genera targets en dias de rebalanceo).

Invariantes:
  - Un instrumento necesita >= 252 barras para generar señal (§3.1 no look-ahead).
  - Datos sospechosos (DataQuality.SUSPECT) → excluidos silenciosamente ese dia.
  - La señal se genera con close(T); la ejecucion es contra open(T+1) (§3.1).
"""
from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

from qtrader.core.types import Direction, Instrument, Position, Signal, TargetPosition

if TYPE_CHECKING:
    from qtrader.backtesting.engine import SandboxedDataView

_STRATEGY_ID = "momentum"
_LOOKBACK_LONG = 252    # 12 meses habiles (~1 año)
_LOOKBACK_SHORT = 21    # 1 mes habiles
_MIN_BARS = 252         # minimo de historia requerida para señal
_TOP_QUINTILE = 0.20    # top 20 % → LONG
_ZERO = Decimal("0")
_ONE = Decimal("1")


# ---------------------------------------------------------------------------
# Calculo de la señal (pure function — sin I/O, sin estado)
# ---------------------------------------------------------------------------

def compute_momentum_score(closes: list[Decimal]) -> Decimal | None:
    """Calcula el score de momentum 12-1.

    closes: lista de cierres ordenada cronologicamente (mas antiguo primero).
    Indices usados (closes[-1] = close(T)):
      closes[-22]  = close(T-21)  → numerador   (excluye el mes reciente)
      closes[-253] = close(T-252) → denominador (inicio del periodo 12M)
    Requiere len >= 253 para acceder a closes[-253].
    Devuelve None si la historia es insuficiente o los precios son cero.
    """
    if len(closes) < _MIN_BARS + 1:   # necesitamos index [-253] → len >= 253
        return None
    price_recent = closes[-(_LOOKBACK_SHORT + 1)]   # close(T-21)
    price_old = closes[-(_LOOKBACK_LONG + 1)]        # close(T-252)
    if price_old <= _ZERO or price_recent <= _ZERO:
        return None
    return price_recent / price_old - _ONE


def _is_rebalance_day(trading_day: date, trading_days: list[date]) -> bool:
    """True si trading_day es viernes habiles o el ultimo dia habil antes del fin de semana."""
    if trading_day.weekday() == 4:  # viernes
        return True
    # Si el proximo dia habiles es lunes o mas (saltamos fin de semana), este dia es
    # el "ultimo antes del fin de semana" — caso de festivo en viernes.
    try:
        idx = trading_days.index(trading_day)
    except ValueError:
        return False
    if idx + 1 >= len(trading_days):
        return True  # ultimo dia del backtest → rebalancear
    next_day = trading_days[idx + 1]
    return (next_day - trading_day).days > 2  # gap > 2 dias → saltamos fin de semana/festivo


# ---------------------------------------------------------------------------
# Estrategia (Protocol Strategy)
# ---------------------------------------------------------------------------

class CrossSectionalMomentumStrategy:
    """Momentum cross-sectional 12-1 con rebalanceo semanal.

    Args:
        trading_days: lista completa de dias habiles del backtest (necesaria
                      para detectar el rebalanceo correcto en festivos).
        strategy_id:  identificador de la estrategia.
    """

    def __init__(
        self,
        trading_days: list[date],
        strategy_id: str = _STRATEGY_ID,
    ) -> None:
        self._trading_days = trading_days
        self._strategy_id = strategy_id
        # Ultima señal generada en el ultimo rebalanceo (para mantener entre rebalanceos)
        self._last_signals: list[Signal] = []

    def on_bar(
        self,
        trading_day: date,
        universe: list[Instrument],
        data: SandboxedDataView,
    ) -> list[Signal]:
        """Genera señales. En dias sin rebalanceo devuelve las señales del ultimo rebalanceo."""
        if not _is_rebalance_day(trading_day, self._trading_days):
            return list(self._last_signals)

        signals = self._compute_signals(trading_day, universe, data)
        self._last_signals = signals
        return signals

    def _compute_signals(
        self,
        trading_day: date,
        universe: list[Instrument],
        data: SandboxedDataView,
    ) -> list[Signal]:
        ts = datetime(
            trading_day.year, trading_day.month, trading_day.day, tzinfo=UTC
        )
        # Necesitamos 253 barras: 252 de lookback + 1 de hoy
        bars_needed = _MIN_BARS + 1
        start_dt = trading_day - timedelta(days=bars_needed * 2)  # margen amplio (fines de semana)

        scores: dict[str, Decimal] = {}
        for inst in universe:
            try:
                validated_bars = data.get_bars(
                    inst.ticker_proxy, start_dt, trading_day
                )
            except Exception:  # noqa: BLE001 — provider falla → excluir silenciosamente
                continue

            # Excluir barras SUSPECT (invariante §3.4)
            from qtrader.core.types import DataQuality
            ok_bars = [vb for vb in validated_bars if vb.quality == DataQuality.OK]
            if len(ok_bars) < bars_needed:
                continue  # historia insuficiente → excluir

            closes = [vb.bar.close for vb in ok_bars]
            score = compute_momentum_score(closes)
            if score is None:
                continue
            scores[inst.ticker_proxy] = score

        if not scores:
            return []

        # Ranking transversal
        n_universe = len(scores)
        top_n = max(1, math.ceil(n_universe * _TOP_QUINTILE))

        # Ordenar descendente; empates por symbol (determinista)
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        top_symbols = {sym for sym, _ in ranked[:top_n]}

        signals: list[Signal] = []
        for sym, _score in scores.items():
            direction = Direction.LONG if sym in top_symbols else Direction.FLAT
            strength_val = (
                _ONE if direction == Direction.LONG
                else _ZERO
            )
            signals.append(Signal(
                symbol=sym,
                timestamp=ts,
                direction=direction,
                strength=strength_val,
                strategy_id=self._strategy_id,
            ))

        return signals


# ---------------------------------------------------------------------------
# Portfolio constructor — equal weight dentro del quintil superior
# ---------------------------------------------------------------------------

class MomentumEqualWeightPortfolio:
    """Equal-weight entre los instrumentos con señal LONG.

    Calcula la cantidad de acciones para cada instrumento en el quintil superior
    usando: qty = floor((equity * weight) / price_open).
    Requiere que el caller le actualice los precios de apertura antes de build().

    El motor llama a on_bar() con close de T, luego a build() con esas señales.
    Los precios de open de T+1 no se conocen en build() — usamos close(T) como
    proxy del precio de ejecucion (conservador, el motor aplica slippage aparte).
    """

    def __init__(
        self,
        initial_equity: Decimal,
        min_order_eur: Decimal = Decimal("1000"),
    ) -> None:
        self._initial_equity = initial_equity
        self._min_order_eur = min_order_eur
        self._last_close: dict[str, Decimal] = {}

    def update_close(self, symbol: str, close: Decimal) -> None:
        """Registra el cierre del dia actual (llamado por el observer del motor)."""
        self._last_close[symbol] = close

    def build(
        self,
        signals: list[Signal],
        positions: dict[str, Position],
        universe: list[Instrument],
        equity: Decimal,
    ) -> list[TargetPosition]:
        # NAV = cash + valor de mercado de posiciones actuales a cierres actuales.
        # Lo usamos solo para calcular pesos; el presupuesto de compra
        # esta limitado al cash disponible (equity) para no descubiertos.
        nav = equity
        for sym, pos in positions.items():
            price = self._last_close.get(sym)
            if price is not None and price > _ZERO:
                nav += pos.quantity * price

        long_symbols = {
            s.symbol for s in signals if s.direction == Direction.LONG
        }

        # Posiciones que deben cerrarse (ya no estan en el top quintil)
        targets: list[TargetPosition] = []
        for sym, pos in positions.items():
            if sym not in long_symbols and pos.quantity > _ZERO:
                targets.append(TargetPosition(symbol=sym, quantity=_ZERO, weight=_ZERO))

        if not long_symbols:
            return targets

        # Presupuesto: distribuimos NAV en equal weight, pero limitamos la compra
        # nueva al cash disponible. Las posiciones existentes se mantienen o ajustan.
        n = len(long_symbols)
        weight = _ONE / Decimal(n)

        for sym in sorted(long_symbols):  # orden determinista
            price = self._last_close.get(sym)
            if price is None or price <= _ZERO:
                continue
            target_notional = nav * weight
            if target_notional < self._min_order_eur:
                continue
            qty_raw = target_notional / price
            qty = qty_raw.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            if qty <= _ZERO:
                continue
            # Verificar que la compra adicional no excede el cash disponible.
            # Si la posicion actual ya cubre o supera el target, no compramos mas.
            current_qty = positions.get(sym)
            current = current_qty.quantity if current_qty else _ZERO
            delta = qty - current
            if delta > _ZERO:
                cost_estimate = delta * price
                if cost_estimate > equity:
                    # Ajustamos qty a lo que podemos comprar con el cash disponible
                    affordable_delta = equity / price
                    qty = (current + affordable_delta).quantize(
                        Decimal("1"), rounding=ROUND_HALF_UP
                    )
                    if qty <= current:
                        qty = current  # mantener posicion actual
            if qty < _ZERO:
                qty = _ZERO
            actual_notional = qty * price
            actual_weight = actual_notional / nav if nav > _ZERO else _ZERO
            actual_weight = min(_ONE, max(_ZERO, actual_weight))
            targets.append(TargetPosition(
                symbol=sym,
                quantity=qty,
                weight=actual_weight,
            ))

        return targets
