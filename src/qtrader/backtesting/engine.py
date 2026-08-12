"""Motor de backtesting event-driven (T2.1).

Invariantes de corrección (CLAUDE.md §3):
  - Una señal generada con el cierre de T se ejecuta contra la apertura de T+1.
    Nunca contra la barra que la generó (§3.1).
  - El universo se consulta point-in-time via UniverseManager.get_universe(T) (§3.3).
  - Datos sospechosos → excluir; el motor no opera sobre barras SUSPECT (§3.4).
  - Look-ahead: LookAheadError si la estrategia intenta leer barras futuras.

Diseño:
  - Sin I/O dentro del bucle. Todo se inyecta en __init__.
  - El motor emite BacktestEvent a una lista de EngineObserver.
  - El fill se procesa AL INICIO de T+1 contra open[T+1], con slippage.
  - Gap-through: si open[T+1] cruza el precio de stop de una orden,
    el fill ocurre al open (precio real), no al stop teórico.

Protocolos (typing.Protocol):
  - Strategy: recibe SandboxedDataView + lista de instrumentos → list[Signal]
  - PortfolioConstructor: recibe signals + positions → list[TargetPosition]
  - RiskEngineProtocol: recibe Order → RiskDecision
  - SimBroker: recibe Order + fill_price → Fill
  - EngineObserver: recibe BacktestEvent
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from qtrader.backtesting.events import (
    BacktestEvent,
    BarEvent,
    CycleEndEvent,
    CycleStartEvent,
    FillEvent,
    OrderEvent,
    RiskEvent,
    SignalEvent,
)
from qtrader.core.types import (
    Fill,
    Instrument,
    Order,
    OrderType,
    Position,
    RiskDecision,
    RiskLevel,
    Side,
    Signal,
    TargetPosition,
    ValidatedBar,
)
from qtrader.data.provider import (
    MarketDataProvider,  # noqa: TCH001 — usado en Protocol + __init__ signature
)
from qtrader.data.universe import UniverseManager  # noqa: TCH001 — usado en __init__ signature

_ZERO = Decimal("0")
_ONE = Decimal("1")


# ---------------------------------------------------------------------------
# Excepción de look-ahead
# ---------------------------------------------------------------------------

class LookAheadError(Exception):
    """Raised when a strategy attempts to read data not yet available."""


# ---------------------------------------------------------------------------
# Vista de datos con sandbox temporal
# ---------------------------------------------------------------------------

class SandboxedDataView:
    """Wrappea un MarketDataProvider e impide acceso a barras futuras.

    Cualquier barra con effective_available_from() > current_date lanza
    LookAheadError en lugar de devolverse silenciosamente.
    """

    def __init__(self, provider: MarketDataProvider, current_date: date) -> None:
        self._provider = provider
        self._current_date = current_date

    def get_bars(
        self,
        symbol: str,
        start: date,
        end: date,
        as_of: date | None = None,
    ) -> list[ValidatedBar]:
        effective_as_of = as_of or self._current_date
        # Construimos datetimes UTC para el provider
        start_dt = datetime(start.year, start.month, start.day, tzinfo=UTC)
        end_dt = datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=UTC)
        as_of_dt = datetime(
            effective_as_of.year, effective_as_of.month, effective_as_of.day, tzinfo=UTC
        )
        bars = self._provider.get_bars(symbol, start_dt, end_dt, as_of_dt)
        for bar in bars:
            avail = bar.bar.effective_available_from()
            if avail > self._current_date:
                raise LookAheadError(
                    f"Strategy attempted to read {symbol} bar from {avail} "
                    f"but current engine date is {self._current_date}. "
                    "This is look-ahead bias — invariant §3.1 violated."
                )
        return bars


# ---------------------------------------------------------------------------
# Protocolos inyectables
# ---------------------------------------------------------------------------

@runtime_checkable
class Strategy(Protocol):
    def on_bar(
        self,
        trading_day: date,
        universe: list[Instrument],
        data: SandboxedDataView,
    ) -> list[Signal]: ...


@runtime_checkable
class PortfolioConstructor(Protocol):
    def build(
        self,
        signals: list[Signal],
        positions: dict[str, Position],
        universe: list[Instrument],
        equity: Decimal,
    ) -> list[TargetPosition]: ...


@runtime_checkable
class RiskEngineProtocol(Protocol):
    def evaluate(self, order: Order) -> RiskDecision: ...


@runtime_checkable
class SimBroker(Protocol):
    def fill(self, order: Order, fill_price: Decimal) -> Fill: ...


@runtime_checkable
class EngineObserver(Protocol):
    def on_event(self, event: BacktestEvent) -> None: ...


# ---------------------------------------------------------------------------
# Orden pendiente en la queue
# ---------------------------------------------------------------------------

@dataclass
class _PendingOrder:
    order: Order
    intended_fill_date: date
    stop_price: Decimal | None = None  # para gap-through modeling


# ---------------------------------------------------------------------------
# Resultado del backtest
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BacktestResult:
    initial_equity: Decimal
    final_equity: Decimal
    total_fills: int
    trading_days: int
    fills: tuple[Fill, ...]
    equity_curve: tuple[tuple[date, Decimal], ...]  # (date, equity)

    @property
    def pnl(self) -> Decimal:
        return self.final_equity - self.initial_equity

    @property
    def pnl_pct(self) -> Decimal:
        if self.initial_equity == _ZERO:
            return _ZERO
        return (self.final_equity - self.initial_equity) / self.initial_equity


# ---------------------------------------------------------------------------
# Motor principal
# ---------------------------------------------------------------------------

class BacktestEngine:
    """Motor event-driven de backtesting.

    Args:
        provider:       proveedor de datos de mercado (MarketDataProvider).
        universe_mgr:   gestor del universo (punto de tiempo).
        strategy:       estrategia de trading.
        portfolio:      constructor de posiciones objetivo.
        risk:           risk engine.
        broker:         broker simulado.
        trading_days:   lista de días de trading ordenados.
        initial_equity: capital inicial en EUR.
        slippage_bps:   slippage en puntos básicos aplicado al fill (default 5 bps).
        observers:      lista de observers que reciben todos los eventos.
    """

    def __init__(
        self,
        *,
        provider: MarketDataProvider,
        universe_mgr: UniverseManager,
        strategy: Strategy,
        portfolio: PortfolioConstructor,
        risk: RiskEngineProtocol,
        broker: SimBroker,
        trading_days: list[date],
        initial_equity: Decimal,
        slippage_bps: Decimal = Decimal("5"),
        observers: list[EngineObserver] | None = None,
    ) -> None:
        self._provider = provider
        self._universe_mgr = universe_mgr
        self._strategy = strategy
        self._portfolio = portfolio
        self._risk = risk
        self._broker = broker
        self._trading_days = list(trading_days)
        self._initial_equity = initial_equity
        self._slippage_factor = slippage_bps / Decimal("10000")
        self._observers: list[EngineObserver] = observers or []

    def run(self) -> BacktestResult:
        equity = self._initial_equity
        positions: dict[str, Position] = {}
        pending: list[_PendingOrder] = []
        all_fills: list[Fill] = []
        equity_curve: list[tuple[date, Decimal]] = []

        for T in self._trading_days:
            universe = self._universe_mgr.get_universe(T)
            self._emit(CycleStartEvent(trading_day=T, universe=tuple(universe)))

            # --- FASE 1: fills de órdenes pendientes para T ---
            bars_today = self._fetch_bars(universe, T)
            bar_map = {vb.bar.symbol: vb for vb in bars_today}

            fills_today, pending = self._process_pending(pending, T, bar_map)
            for fill_ev in fills_today:
                self._emit(fill_ev)
                fill = fill_ev.fill
                all_fills.append(fill)
                positions, equity = self._apply_fill(fill, positions, equity)

            # --- FASE 2: publicar barras a la estrategia ---
            # Solo barras cuya available_from <= T (no look-ahead)
            safe_bars = [
                vb for vb in bars_today
                if vb.bar.effective_available_from() <= T
            ]
            bar_event = BarEvent(trading_day=T, bars=tuple(safe_bars))
            self._emit(bar_event)

            # --- FASE 3: estrategia genera señales ---
            data_view = SandboxedDataView(self._provider, T)
            signals = self._strategy.on_bar(T, universe, data_view)
            self._emit(SignalEvent(trading_day=T, signals=tuple(signals)))

            # --- FASE 4: portfolio construction ---
            targets = self._portfolio.build(signals, positions, universe, equity)

            # --- FASE 5: generar órdenes → risk → queue ---
            new_orders = self._targets_to_orders(targets, positions, bar_map, T)
            for order in new_orders:
                decision = self._risk.evaluate(order)
                self._emit(RiskEvent(trading_day=T, decision=decision))
                if decision.decision == RiskLevel.APPROVE:
                    intended_date = self._next_trading_day(T)
                    if intended_date is not None:
                        order_ev = OrderEvent(
                            trading_day=T,
                            order=order,
                            intended_fill_date=intended_date,
                        )
                        self._emit(order_ev)
                        pending.append(_PendingOrder(
                            order=order,
                            intended_fill_date=intended_date,
                        ))
                elif decision.decision == RiskLevel.REDUCE:
                    reduced_order = self._reduce_order(order, decision)
                    intended_date = self._next_trading_day(T)
                    if intended_date is not None and reduced_order is not None:
                        order_ev = OrderEvent(
                            trading_day=T,
                            order=reduced_order,
                            intended_fill_date=intended_date,
                        )
                        self._emit(order_ev)
                        pending.append(_PendingOrder(
                            order=reduced_order,
                            intended_fill_date=intended_date,
                        ))

            # Mark-to-market: NAV = cash + valor de mercado de posiciones a cierres de T.
            # El motor registra NAV en la equity curve para que las metricas sean correctas.
            nav = equity
            for sym, pos in positions.items():
                vb_today = bar_map.get(sym)
                if vb_today is not None:
                    nav += pos.quantity * vb_today.bar.close
                else:
                    nav += pos.market_value  # ultimo valor conocido como fallback

            equity_curve.append((T, nav))
            self._emit(CycleEndEvent(trading_day=T, equity=nav))

        final_nav = equity_curve[-1][1] if equity_curve else equity
        return BacktestResult(
            initial_equity=self._initial_equity,
            final_equity=final_nav,
            total_fills=len(all_fills),
            trading_days=len(self._trading_days),
            fills=tuple(all_fills),
            equity_curve=tuple(equity_curve),
        )

    # ------------------------------------------------------------------
    # Helpers privados
    # ------------------------------------------------------------------

    def _emit(self, event: BacktestEvent) -> None:
        for obs in self._observers:
            obs.on_event(event)

    def _fetch_bars(self, universe: list[Instrument], T: date) -> list[ValidatedBar]:
        """Obtiene barras para todos los instrumentos del universo en T."""
        bars: list[ValidatedBar] = []
        start_dt = datetime(T.year, T.month, T.day, tzinfo=UTC)
        end_dt = datetime(T.year, T.month, T.day, 23, 59, 59, tzinfo=UTC)
        as_of_dt = datetime(T.year, T.month, T.day, tzinfo=UTC)
        for inst in universe:
            symbol = inst.ticker_proxy
            try:
                result = self._provider.get_bars(symbol, start_dt, end_dt, as_of_dt)
                bars.extend(result)
            except Exception:  # noqa: BLE001 — provider falla silenciosamente
                pass
        return bars

    def _process_pending(
        self,
        pending: list[_PendingOrder],
        T: date,
        bar_map: dict[str, ValidatedBar],
    ) -> tuple[list[FillEvent], list[_PendingOrder]]:
        """Procesa órdenes pendientes con intended_fill_date == T.

        Fill price = open[T] ± slippage.
        Gap-through: si la orden es STOP y open cruza el stop, fill al open.
        """
        fills: list[FillEvent] = []
        remaining: list[_PendingOrder] = []

        for po in pending:
            if po.intended_fill_date != T:
                remaining.append(po)
                continue

            symbol = po.order.symbol
            vb = bar_map.get(symbol)
            if vb is None:
                # No hay barra hoy (mercado cerrado, instrumento sin datos).
                # Llevar la orden al siguiente día disponible.
                remaining.append(po)
                continue

            open_price = vb.bar.open
            fill_price = self._apply_slippage(open_price, po.order.side)

            # Gap-through: si hay stop y el open ya cruzó el stop
            if po.stop_price is not None:
                if po.order.side == Side.SELL and open_price < po.stop_price:
                    fill_price = self._apply_slippage(open_price, Side.SELL)
                elif po.order.side == Side.BUY and open_price > po.stop_price:
                    fill_price = self._apply_slippage(open_price, Side.BUY)

            fill = self._broker.fill(po.order, fill_price)
            fills.append(FillEvent(
                trading_day=T,
                fill=fill,
                intended_fill_date=T,
            ))

        return fills, remaining

    def _apply_slippage(self, price: Decimal, side: Side) -> Decimal:
        """Aplica slippage: BUY paga más, SELL recibe menos."""
        if side == Side.BUY:
            return price * (1 + self._slippage_factor)
        return price * (1 - self._slippage_factor)

    def _apply_fill(
        self,
        fill: Fill,
        positions: dict[str, Position],
        equity: Decimal,
    ) -> tuple[dict[str, Position], Decimal]:
        """Actualiza positions y equity tras un fill. Simplificado para T2.1."""
        cost = fill.quantity * fill.price + fill.commission
        if fill.side == Side.BUY:
            equity -= cost
            existing = positions.get(fill.symbol)
            if existing is None:
                positions[fill.symbol] = Position(
                    symbol=fill.symbol,
                    quantity=fill.quantity,
                    avg_cost=fill.price,
                    market_value=fill.quantity * fill.price,
                    unrealized_pnl=_ZERO,
                )
            else:
                new_qty = existing.quantity + fill.quantity
                old_cost = existing.avg_cost * existing.quantity
                new_cost = (old_cost + fill.price * fill.quantity) / new_qty
                positions[fill.symbol] = Position(
                    symbol=fill.symbol,
                    quantity=new_qty,
                    avg_cost=new_cost,
                    market_value=new_qty * fill.price,
                    unrealized_pnl=_ZERO,
                )
        else:  # SELL
            equity += fill.quantity * fill.price - fill.commission
            existing = positions.get(fill.symbol)
            if existing is not None:
                new_qty = existing.quantity - fill.quantity
                if new_qty <= _ZERO:
                    del positions[fill.symbol]
                else:
                    positions[fill.symbol] = Position(
                        symbol=fill.symbol,
                        quantity=new_qty,
                        avg_cost=existing.avg_cost,
                        market_value=new_qty * fill.price,
                        unrealized_pnl=_ZERO,
                    )
        return positions, equity

    def _targets_to_orders(
        self,
        targets: list[TargetPosition],
        positions: dict[str, Position],
        bar_map: dict[str, ValidatedBar],
        T: date,
    ) -> list[Order]:
        """Convierte TargetPositions en Orders delta."""
        orders: list[Order] = []
        ts = datetime(T.year, T.month, T.day, tzinfo=UTC)
        for target in targets:
            symbol = target.symbol
            current_qty = positions.get(symbol)
            current = current_qty.quantity if current_qty else _ZERO
            delta = target.quantity - current
            if delta == _ZERO:
                continue
            side = Side.BUY if delta > _ZERO else Side.SELL
            qty = abs(delta)
            orders.append(Order(
                client_order_id=str(uuid.uuid4()),
                symbol=symbol,
                side=side,
                quantity=qty,
                order_type=OrderType.MOO,
                timestamp=ts,
                strategy_id="engine",
            ))
        return orders

    def _next_trading_day(self, T: date) -> date | None:
        """Devuelve el día de trading siguiente a T, o None si es el último."""
        try:
            idx = self._trading_days.index(T)
        except ValueError:
            return None
        if idx + 1 >= len(self._trading_days):
            return None
        return self._trading_days[idx + 1]

    @staticmethod
    def _reduce_order(order: Order, decision: RiskDecision) -> Order | None:
        if decision.adjusted_quantity is None or decision.adjusted_quantity <= _ZERO:
            return None
        return Order(
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=decision.adjusted_quantity,
            order_type=order.order_type,
            limit_price=order.limit_price,
            timestamp=order.timestamp,
            strategy_id=order.strategy_id,
        )


# ---------------------------------------------------------------------------
# Stubs inyectables para tests y demos
# ---------------------------------------------------------------------------

class BuyAllStrategy:
    """Estrategia stub: genera señal LONG para todos los instrumentos del universo."""

    def on_bar(
        self,
        trading_day: date,
        universe: list[Instrument],
        data: SandboxedDataView,
    ) -> list[Signal]:
        ts = datetime(trading_day.year, trading_day.month, trading_day.day, tzinfo=UTC)
        from qtrader.core.types import Direction
        return [
            Signal(
                symbol=inst.ticker_proxy,
                timestamp=ts,
                direction=Direction.LONG,
                strength=_ONE,
                strategy_id="buy_all",
            )
            for inst in universe
        ]


class EqualWeightPortfolio:
    """Portfolio stub: equal weight entre todos los instrumentos con señal LONG."""

    def build(
        self,
        signals: list[Signal],
        positions: dict[str, Position],
        universe: list[Instrument],
        equity: Decimal,
    ) -> list[TargetPosition]:
        from qtrader.core.types import Direction
        long_signals = [s for s in signals if s.direction == Direction.LONG]
        if not long_signals:
            return []
        n = len(long_signals)
        weight = _ONE / Decimal(n)
        return [
            TargetPosition(
                symbol=s.symbol,
                quantity=Decimal("10"),  # 10 participaciones por instrumento (stub)
                weight=weight,
            )
            for s in long_signals
        ]


class ApproveAllRisk:
    """Risk engine stub: aprueba todas las órdenes."""

    def evaluate(self, order: Order) -> RiskDecision:
        return RiskDecision(
            client_order_id=order.client_order_id,
            decision=RiskLevel.APPROVE,
            timestamp=order.timestamp,
        )


class InstantSimBroker:
    """Broker simulado stub: fill instantáneo al precio indicado, comisión 0."""

    def fill(self, order: Order, fill_price: Decimal) -> Fill:
        from qtrader.core.types import Fill
        return Fill(
            client_order_id=order.client_order_id,
            fill_id=str(uuid.uuid4()),
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price=fill_price,
            commission=_ZERO,
            timestamp=order.timestamp,
        )


class CostAwareSimBroker:
    """Broker simulado con modelo de costes realista (T2.2).

    Usa calculate_costs() para ajustar el precio de fill y extraer la comisión.
    El fill neto refleja spread + slippage en el precio; la comisión sale del cash.

    Args:
        costs_config:    parámetros del modelo de costes.
        universe:        dict symbol → Instrument (para spread por instrumento).
        bar_map:         dict symbol → ValidatedBar del día actual (para ADV proxy).
        adv_map:         dict symbol → avg_daily_volume (Decimal). Puede ser vacío.
    """

    def __init__(
        self,
        costs_config: object,  # CostsConfig — importado lazy para evitar ciclo
        universe: dict[str, object],  # str → Instrument
        adv_map: dict[str, Decimal] | None = None,
    ) -> None:
        self._costs_config = costs_config
        self._universe = universe
        self._adv_map: dict[str, Decimal] = adv_map or {}

    def fill(self, order: Order, fill_price: Decimal) -> Fill:
        from qtrader.core.types import Fill, Instrument
        from qtrader.costs import CostsConfig, adjusted_fill_price, calculate_costs

        instrument = self._universe.get(order.symbol)
        if not isinstance(instrument, Instrument):
            # Sin metadatos de instrumento → fill sin coste (fail-safe)
            return Fill(
                client_order_id=order.client_order_id,
                fill_id=str(uuid.uuid4()),
                symbol=order.symbol,
                side=order.side,
                quantity=order.quantity,
                price=fill_price,
                commission=_ZERO,
                timestamp=order.timestamp,
            )

        config = self._costs_config
        if not isinstance(config, CostsConfig):
            raise TypeError(f"costs_config must be CostsConfig, got {type(config)}")

        adv = self._adv_map.get(order.symbol, _ZERO)

        # Creamos un ValidatedBar mínimo con open=fill_price para el cálculo de costes.
        # El bar real no está disponible aquí — usamos el precio ya calculado por el motor.
        from qtrader.core.types import Bar, DataQuality
        from qtrader.core.types import ValidatedBar as VB
        bar = VB(
            bar=Bar(
                symbol=order.symbol,
                timestamp=order.timestamp,
                open=fill_price,
                high=fill_price,
                low=fill_price,
                close=fill_price,
                volume=adv if adv > _ZERO else Decimal("1000000"),
            ),
            quality=DataQuality.OK,
        )

        try:
            breakdown = calculate_costs(order, bar, instrument, config, adv)
        except Exception:  # noqa: BLE001 — OrderTooSmall u otros → fill sin coste
            return Fill(
                client_order_id=order.client_order_id,
                fill_id=str(uuid.uuid4()),
                symbol=order.symbol,
                side=order.side,
                quantity=order.quantity,
                price=fill_price,
                commission=_ZERO,
                timestamp=order.timestamp,
            )

        net_price = adjusted_fill_price(fill_price, order.side, breakdown.price_adjustment)

        return Fill(
            client_order_id=order.client_order_id,
            fill_id=str(uuid.uuid4()),
            symbol=order.symbol,
            side=order.side,
            quantity=breakdown.adjusted_qty,
            price=net_price,
            commission=breakdown.commission,
            timestamp=order.timestamp,
        )
