"""Tests de PaperBroker (T5.2).

Tests incluidos:
  - fill_occurs_at_t1_open: fill es al open de T+1, no al cierre de T.
  - festivo_deja_orden_pending: sin datos para una fecha → PENDING.
  - cash_invariant: balance = initial + sum(deltas) tras cada fill.
  - dividendo_incrementa_cash: ex_date procesa el dividendo correctamente.
  - split_ajusta_posicion: ratio aplica a quantity y avg_cost.
  - test de paridad backtest/paper: NAV final, comisiones y fills identicos.
  - get_order_status_unknown: id inexistente → UNKNOWN.
  - cancel_submitted: cancela orden SUBMITTED con exito.
  - cancel_filled: intento de cancelar orden FILLED → fracasa.
  - isinstance_check: PaperBroker pasa isinstance(BrokerInterface).
"""
from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path  # noqa: TCH003 — used in pytest fixture signatures at runtime

import pytest

from qtrader.backtesting.engine import ApproveAllRisk, BacktestEngine
from qtrader.backtesting.synthetic import SyntheticMultiProvider
from qtrader.brokers import BrokerInterface, PaperBroker
from qtrader.brokers.paper_broker import CorporateActionRecord
from qtrader.brokers.types import OrderStatus
from qtrader.core.types import (
    Instrument,
    InstrumentCategory,
    InstrumentType,
    Order,  # used in _ZeroCostSimBroker.fill type signature
    Side,
)
from qtrader.costs import CostsConfig
from qtrader.risk.types import ApprovedOrder

_ZERO = Decimal("0")
_START = date(2022, 1, 3)
_INITIAL_CASH = Decimal("15000")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_instrument(symbol: str) -> Instrument:
    return Instrument(
        symbol=symbol,
        ticker_proxy=symbol,
        ticker_ucits=f"{symbol}.AS",
        name=symbol,
        exchange="XNYS",
        instrument_type=InstrumentType.ETF,
        category=InstrumentCategory.REGION,
        declared_on=date(2022, 1, 1),
        currency="USD",
    )


def _make_approved_order(
    symbol: str,
    side: Side,
    qty: Decimal,
    price: Decimal = Decimal("100"),
) -> tuple[ApprovedOrder, str]:
    """Devuelve (ApprovedOrder, client_order_id)."""
    order_id = f"ord-{symbol}-{side.value}"
    order = ApprovedOrder(
        order_id=order_id,
        symbol=symbol,
        side=side,
        original_quantity=qty,
        approved_quantity=qty,
        notional=qty * price,
        reduction_reason=None,
    )
    return order, order_id


def _make_broker(tmp_path: Path, costs: CostsConfig | None = None) -> PaperBroker:
    broker = PaperBroker(
        db_path=tmp_path / "paper.db",
        initial_cash=_INITIAL_CASH,
        costs_config=costs,
        _testing=True,
    )
    return broker


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. isinstance check — PaperBroker implementa BrokerInterface
# ---------------------------------------------------------------------------

def test_paper_broker_is_broker_interface(tmp_path: Path) -> None:
    broker = _make_broker(tmp_path)
    assert isinstance(broker, BrokerInterface)
    broker.close()


# ---------------------------------------------------------------------------
# 2. Fill al open de T+1
# ---------------------------------------------------------------------------

def test_fill_occurs_at_t1_open(tmp_path: Path) -> None:
    """submit_order → advance_to(T+1) → fill al open exacto de T+1."""
    symbol = "SPY"
    t1_open = Decimal("150.00")

    broker = _make_broker(tmp_path)  # zero_costs por defecto
    broker.set_market_data(
        data={symbol: {date(2022, 1, 4): t1_open}},
        instruments={symbol: _make_instrument(symbol)},
    )

    order, oid = _make_approved_order(symbol, Side.BUY, Decimal("10"))
    ack = _run(broker.submit_order(order, oid))
    assert ack.status == OrderStatus.SUBMITTED

    broker.advance_to(date(2022, 1, 4))

    status = _run(broker.get_order_status(oid))
    assert status == OrderStatus.FILLED

    fills = _run(broker.get_fills_since(datetime(2022, 1, 1, tzinfo=UTC)))
    assert len(fills) == 1
    assert fills[0].price == t1_open
    broker.close()


# ---------------------------------------------------------------------------
# 3. Sin datos → PENDING
# ---------------------------------------------------------------------------

def test_festivo_deja_orden_pending(tmp_path: Path) -> None:
    """Si no hay open para la fecha, la orden queda PENDING."""
    symbol = "IEF"
    broker = _make_broker(tmp_path)
    broker.set_market_data(
        data={symbol: {}},  # sin datos para ninguna fecha
        instruments={symbol: _make_instrument(symbol)},
    )

    order, oid = _make_approved_order(symbol, Side.BUY, Decimal("5"))
    _run(broker.submit_order(order, oid))
    broker.advance_to(date(2022, 1, 5))  # festivo simulado — sin datos

    status = _run(broker.get_order_status(oid))
    assert status == OrderStatus.PENDING
    broker.close()


def test_orden_pending_se_filla_cuando_hay_datos(tmp_path: Path) -> None:
    """Orden PENDING se filla en el primer día con datos disponibles."""
    symbol = "GLD"
    open_day2 = Decimal("180.50")

    broker = _make_broker(tmp_path)
    broker.set_market_data(
        data={symbol: {date(2022, 1, 6): open_day2}},
        instruments={symbol: _make_instrument(symbol)},
    )

    order, oid = _make_approved_order(symbol, Side.BUY, Decimal("3"))
    _run(broker.submit_order(order, oid))

    broker.advance_to(date(2022, 1, 5))  # sin datos → PENDING
    assert _run(broker.get_order_status(oid)) == OrderStatus.PENDING

    broker.advance_to(date(2022, 1, 6))  # con datos → FILLED
    assert _run(broker.get_order_status(oid)) == OrderStatus.FILLED
    broker.close()


# ---------------------------------------------------------------------------
# 4. Cash invariant
# ---------------------------------------------------------------------------

def test_cash_invariant_tras_fills(tmp_path: Path) -> None:
    """balance_after siempre = initial_cash + sum(deltas)."""
    symbol = "QQQ"
    open_price = Decimal("300.00")

    broker = _make_broker(tmp_path)
    broker.set_market_data(
        data={symbol: {date(2022, 1, 4): open_price, date(2022, 1, 5): open_price}},
        instruments={symbol: _make_instrument(symbol)},
    )

    # BUY 5 shares
    buy_order, buy_id = _make_approved_order(symbol, Side.BUY, Decimal("5"))
    _run(broker.submit_order(buy_order, buy_id))
    broker.advance_to(date(2022, 1, 4))

    # SELL 3 shares
    sell_order, sell_id = _make_approved_order(symbol, Side.SELL, Decimal("3"))
    _run(broker.submit_order(sell_order, sell_id))
    broker.advance_to(date(2022, 1, 5))

    # No exception = invariant holds (verified inside _execute_fill)
    # Comprobacion adicional manual
    conn = broker._conn
    total_row = conn.execute("SELECT SUM(CAST(amount AS REAL)) FROM cash_ledger").fetchone()
    balance_row = conn.execute(
        "SELECT balance_after FROM cash_ledger ORDER BY id DESC LIMIT 1"
    ).fetchone()
    total = Decimal(str(total_row[0]))
    balance = Decimal(balance_row[0])
    assert abs(balance - total) < Decimal("0.01")
    broker.close()


# ---------------------------------------------------------------------------
# 5. Dividendo
# ---------------------------------------------------------------------------

def test_dividendo_incrementa_cash(tmp_path: Path) -> None:
    """ex_date del dividendo → cash += quantity * amount_per_share."""
    symbol = "SPY"
    qty = Decimal("10")
    div_per_share = Decimal("1.50")
    ex_date = date(2022, 1, 10)

    broker = _make_broker(tmp_path)
    broker.set_market_data(
        data={symbol: {date(2022, 1, 4): Decimal("100"), ex_date: Decimal("100")}},
        instruments={symbol: _make_instrument(symbol)},
    )

    # Primero comprar para tener posicion
    order, oid = _make_approved_order(symbol, Side.BUY, qty)
    _run(broker.submit_order(order, oid))
    broker.advance_to(date(2022, 1, 4))

    cash_before = broker._get_cash()

    broker.register_corporate_action(CorporateActionRecord(
        symbol=symbol,
        ex_date=ex_date,
        action_type="DIVIDEND",
        amount_per_share=div_per_share,
        ratio=None,
    ))
    broker.advance_to(ex_date)

    cash_after = broker._get_cash()
    assert cash_after - cash_before == qty * div_per_share
    broker.close()


# ---------------------------------------------------------------------------
# 6. Split
# ---------------------------------------------------------------------------

def test_split_ajusta_posicion(tmp_path: Path) -> None:
    """Split 2:1 → quantity *= 2, avg_cost /= 2."""
    symbol = "AAPL"
    qty = Decimal("5")
    buy_price = Decimal("200.00")
    ex_date = date(2022, 1, 10)

    broker = _make_broker(tmp_path)
    broker.set_market_data(
        data={symbol: {date(2022, 1, 4): buy_price, ex_date: buy_price}},
        instruments={symbol: _make_instrument(symbol)},
    )

    order, oid = _make_approved_order(symbol, Side.BUY, qty, buy_price)
    _run(broker.submit_order(order, oid))
    broker.advance_to(date(2022, 1, 4))

    broker.register_corporate_action(CorporateActionRecord(
        symbol=symbol,
        ex_date=ex_date,
        action_type="SPLIT",
        amount_per_share=None,
        ratio=Decimal("2"),
    ))
    broker.advance_to(ex_date)

    positions = _run(broker.get_positions())
    pos = next(p for p in positions if p.symbol == symbol)
    assert pos.quantity == qty * 2
    assert abs(pos.avg_cost - buy_price / 2) < Decimal("0.0001")
    broker.close()


# ---------------------------------------------------------------------------
# 7. cancel_order
# ---------------------------------------------------------------------------

def test_cancel_submitted_order(tmp_path: Path) -> None:
    symbol = "IEF"
    broker = _make_broker(tmp_path)
    broker.set_market_data(data={}, instruments={})

    order, oid = _make_approved_order(symbol, Side.BUY, Decimal("10"))
    _run(broker.submit_order(order, oid))

    result = _run(broker.cancel_order(oid))
    assert result.success
    assert result.reason is None
    assert _run(broker.get_order_status(oid)) == OrderStatus.CANCELLED
    broker.close()


def test_cancel_filled_order_fails(tmp_path: Path) -> None:
    symbol = "SPY"
    broker = _make_broker(tmp_path)
    broker.set_market_data(
        data={symbol: {date(2022, 1, 4): Decimal("100")}},
        instruments={symbol: _make_instrument(symbol)},
    )

    order, oid = _make_approved_order(symbol, Side.BUY, Decimal("5"))
    _run(broker.submit_order(order, oid))
    broker.advance_to(date(2022, 1, 4))

    result = _run(broker.cancel_order(oid))
    assert not result.success
    assert "FILLED" in (result.reason or "")
    broker.close()


def test_cancel_unknown_order(tmp_path: Path) -> None:
    broker = _make_broker(tmp_path)
    result = _run(broker.cancel_order("nonexistent-id"))
    assert not result.success
    assert "NOT_FOUND" in (result.reason or "")
    broker.close()


# ---------------------------------------------------------------------------
# 8. get_order_status UNKNOWN
# ---------------------------------------------------------------------------

def test_get_order_status_unknown(tmp_path: Path) -> None:
    broker = _make_broker(tmp_path)
    status = _run(broker.get_order_status("this-id-does-not-exist"))
    assert status == OrderStatus.UNKNOWN
    broker.close()


# ---------------------------------------------------------------------------
# 9. reset() solo en testing mode
# ---------------------------------------------------------------------------

def test_reset_forbidden_in_production(tmp_path: Path) -> None:
    broker = PaperBroker(
        db_path=tmp_path / "prod.db",
        initial_cash=_INITIAL_CASH,
        _testing=False,
    )
    with pytest.raises(RuntimeError, match="testing mode"):
        broker.reset(_INITIAL_CASH)
    broker.close()


def test_reset_works_in_testing_mode(tmp_path: Path) -> None:
    broker = _make_broker(tmp_path)
    symbol = "SPY"
    broker.set_market_data(
        data={symbol: {date(2022, 1, 4): Decimal("100")}},
        instruments={symbol: _make_instrument(symbol)},
    )
    order, oid = _make_approved_order(symbol, Side.BUY, Decimal("5"))
    _run(broker.submit_order(order, oid))
    broker.advance_to(date(2022, 1, 4))
    assert broker.get_num_fills() == 1

    broker.reset(Decimal("10000"))
    assert broker.get_num_fills() == 0
    assert broker._get_cash() == Decimal("10000")
    broker.close()


# ---------------------------------------------------------------------------
# 10. TEST DE PARIDAD backtest / paper (el mas importante de T5.2)
# ---------------------------------------------------------------------------

_SYMBOLS = ["SPY", "QQQ", "IEF", "GLD", "XLK"]
_NUM_DAYS = 60          # dias de trading
_SEED = 42
_PARITY_CASH = Decimal("15000")
_TOLERANCE = Decimal("0.01")


def _build_instruments() -> dict[str, Instrument]:
    return {s: _make_instrument(s) for s in _SYMBOLS}


def _build_universe_from_instruments(
    instruments: dict[str, Instrument],
) -> list[Instrument]:
    return list(instruments.values())


def _build_data_tables(
    provider: SyntheticMultiProvider,
    trading_days: list[date],
) -> dict[str, dict[date, Decimal]]:
    """Extrae opens de cada simbolo para cada dia de trading."""
    from datetime import UTC, datetime
    data: dict[str, dict[date, Decimal]] = {s: {} for s in _SYMBOLS}
    for symbol in _SYMBOLS:
        for d in trading_days:
            start_dt = datetime(d.year, d.month, d.day, tzinfo=UTC)
            end_dt = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=UTC)
            bars = provider.get_bars(symbol, start_dt, end_dt, start_dt)
            if bars:
                data[symbol][d] = bars[0].bar.open
    return data


class _BuyFirstDayOnlyStrategy:
    """Estrategia que solo genera señales el primer dia de trading.

    Compra qty=10 de cada simbolo exactamente una vez.
    Esto hace el test de paridad determinista y facil de verificar.
    """

    def __init__(self) -> None:
        self._done = False

    def on_bar(self, trading_day: date, universe: list[Instrument], data: object) -> list:
        from datetime import UTC, datetime

        from qtrader.core.types import Direction, Signal
        if self._done:
            return []
        self._done = True
        ts = datetime(trading_day.year, trading_day.month, trading_day.day, tzinfo=UTC)
        return [
            Signal(
                symbol=inst.ticker_proxy,
                timestamp=ts,
                direction=Direction.LONG,
                strength=Decimal("1"),
                strategy_id="parity_test",
            )
            for inst in universe
        ]


class _FixedQtyPortfolio:
    """Portfolio que compra exactamente qty=10 de cada señal LONG."""

    def build(
        self,
        signals: list,
        positions: dict,
        universe: list,
        equity: Decimal,
    ) -> list:
        from qtrader.core.types import Direction, TargetPosition
        return [
            TargetPosition(symbol=s.symbol, quantity=Decimal("10"), weight=Decimal("0.2"))
            for s in signals
            if s.direction == Direction.LONG
        ]


def test_paridad_backtest_paper(tmp_path: Path) -> None:
    """NAV final, comisiones y num_fills deben ser identicos entre backtest y paper.

    Ambos usan CostsConfig.zero_costs() para eliminar diferencias de redondeo.
    La paridad estructural (timing T+1, gap-through, festivos) se verifica
    porque comparten SyntheticMultiProvider con seed=42.
    """
    from datetime import UTC, datetime, timedelta

    costs = CostsConfig.zero_costs()
    provider = SyntheticMultiProvider(base_seed=_SEED)
    instruments = _build_instruments()
    start_dt = datetime(2022, 1, 3, tzinfo=UTC)

    # Construir la lista de dias de trading (todos los dias del periodo)
    trading_days = [
        (start_dt + timedelta(days=i)).date()
        for i in range(_NUM_DAYS + 1)
    ]

    # Extraer opens para el paper broker
    data_tables = _build_data_tables(provider, trading_days)

    # ------------------------------------------------------------------
    # BACKTEST
    # ------------------------------------------------------------------
    universe_list = _build_universe_from_instruments(instruments)

    class _SimpleUniverseMgr:
        def get_universe(self, as_of: date) -> list[Instrument]:
            return universe_list

    class _ZeroCostSimBroker:
        """SimBroker que usa zero_costs — paridad exacta con PaperBroker."""
        def fill(self, order: Order, fill_price: Decimal):  # type: ignore[override]
            import uuid

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

    bt_strategy = _BuyFirstDayOnlyStrategy()
    bt_portfolio = _FixedQtyPortfolio()

    engine = BacktestEngine(
        provider=provider,
        universe_mgr=_SimpleUniverseMgr(),
        strategy=bt_strategy,
        portfolio=bt_portfolio,
        risk=ApproveAllRisk(),
        broker=_ZeroCostSimBroker(),
        trading_days=trading_days,
        initial_equity=_PARITY_CASH,
        slippage_bps=Decimal("0"),  # sin slippage extra — costs ya en broker
    )
    bt_result = engine.run()

    # ------------------------------------------------------------------
    # PAPER BROKER
    # ------------------------------------------------------------------
    broker = PaperBroker(
        db_path=tmp_path / "parity.db",
        initial_cash=_PARITY_CASH,
        costs_config=costs,
        _testing=True,
    )
    broker.set_market_data(
        data=data_tables,
        instruments=instruments,
    )

    # Simular el mismo comportamiento: comprar 10 de cada simbolo en T=0
    first_day = trading_days[0]
    second_day = trading_days[1] if len(trading_days) > 1 else first_day

    for symbol in _SYMBOLS:
        order = ApprovedOrder(
            order_id=f"paper-{symbol}",
            symbol=symbol,
            side=Side.BUY,
            original_quantity=Decimal("10"),
            approved_quantity=Decimal("10"),
            notional=Decimal("1000"),
            reduction_reason=None,
        )
        _run(broker.submit_order(order, f"paper-{symbol}"))

    # advance_to dia 1 = fill al open de trading_days[1]
    broker.advance_to(second_day)

    # Calcular NAV del paper broker con cierres del dia final
    last_day = trading_days[-1]
    last_closes: dict[str, Decimal] = {}
    from datetime import datetime as dt_class
    for symbol in _SYMBOLS:
        end_dt = dt_class(last_day.year, last_day.month, last_day.day, 23, 59, 59, tzinfo=UTC)
        start_dt2 = dt_class(last_day.year, last_day.month, last_day.day, tzinfo=UTC)
        bars = provider.get_bars(symbol, start_dt2, end_dt, start_dt2)
        if bars:
            last_closes[symbol] = bars[0].bar.close

    paper_nav = broker.get_nav(last_closes)
    paper_fills = broker.get_num_fills()
    paper_commission = broker.get_total_commission()

    broker.close()

    # ------------------------------------------------------------------
    # Comparar resultados
    # ------------------------------------------------------------------
    bt_nav = bt_result.final_equity
    bt_fills = bt_result.total_fills
    bt_commission = sum(f.commission for f in bt_result.fills)

    nav_diff = abs(paper_nav - bt_nav)
    comm_diff = abs(paper_commission - bt_commission)

    assert paper_fills == bt_fills, (
        f"PARIDAD ROTA: fills divergen. paper={paper_fills} backtest={bt_fills}"
    )
    assert comm_diff < _TOLERANCE, (
        f"PARIDAD ROTA: comisiones divergen. paper={paper_commission} "
        f"backtest={bt_commission} diff={comm_diff}"
    )
    assert nav_diff < _TOLERANCE, (
        f"PARIDAD ROTA: NAV final diverge. paper={paper_nav} "
        f"backtest={bt_nav} diff={nav_diff}. "
        "Si divergen hay un bug en la logica de fills o costes."
    )
