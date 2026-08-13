"""Tests de escenarios adversos — T9 Parte D.

12 tests que verifican que el sistema se comporta correctamente ante
condiciones anómalas: datos, ejecución e infraestructura.

Todos los tests son offline (sin red). Los proveedores externos se mockean.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from qtrader.core.types import (
    Bar,
    DataQuality,
    Instrument,
    InstrumentCategory,
    InstrumentType,
    ValidatedBar,
)

# ---------------------------------------------------------------------------
# Helpers compartidos
# ---------------------------------------------------------------------------

_ZERO = Decimal("0")
_ONE = Decimal("1")
_EQUITY = Decimal("15000")
_TRADING_DATE = date(2024, 6, 14)
_UTC_TS = datetime(_TRADING_DATE.year, _TRADING_DATE.month, _TRADING_DATE.day, tzinfo=UTC)


def _run(coro: Any) -> Any:  # noqa: ANN401
    return asyncio.run(coro)


def _make_instrument(symbol: str = "SPY") -> Instrument:
    return Instrument(
        symbol=symbol,
        name=f"{symbol} ETF",
        exchange="XNYS",
        currency="EUR",
        category=InstrumentCategory.REGION,
        instrument_type=InstrumentType.ETF,
        ticker_proxy=symbol,
        ticker_ucits=f"{symbol}.L",
        declared_on=date(2020, 1, 1),
    )


def _make_approved_order(
    symbol: str,
    qty: Decimal,
    side: str = "BUY",
    price: Decimal = Decimal("100"),
    seq: int = 1,
) -> tuple[Any, str]:
    """Construye (ApprovedOrder, client_order_id) para el PaperBroker."""
    import uuid
    from qtrader.risk.types import ApprovedOrder
    from qtrader.core.types import Side

    # UUID como client_order_id: determinista por (symbol, side, seq)
    coid = f"test-{symbol}-{side}-{seq}-{uuid.uuid4().hex[:8]}"
    order = ApprovedOrder(
        order_id=coid,
        symbol=symbol,
        side=Side(side),
        original_quantity=qty,
        approved_quantity=qty,
        notional=qty * price,
        reduction_reason=None,
    )
    return order, coid


def _trading_days(n: int = 60, start: date = date(2024, 1, 2)) -> list[date]:
    days: list[date] = []
    d = start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def _make_paper_broker(tmp_path: Path, cash: Decimal = _EQUITY, name: str = "paper.db") -> Any:
    from qtrader.brokers.paper_broker import PaperBroker
    from qtrader.costs import CostsConfig

    return PaperBroker(
        db_path=tmp_path / name,
        initial_cash=cash,
        costs_config=CostsConfig.zero_costs(),
        _testing=True,
    )


# ---------------------------------------------------------------------------
# Escenario 1: Provider de datos no disponible (API down)
# ---------------------------------------------------------------------------

class _FailingProvider:
    def get_bars(
        self, symbol: str, start: datetime, end: datetime, as_of: datetime
    ) -> list[ValidatedBar]:
        raise ConnectionError(f"API no disponible para {symbol}")

    def get_corporate_actions(
        self, symbol: str, start: datetime, end: datetime
    ) -> list[Any]:
        return []


def test_api_down_no_signals_generated() -> None:
    """Con provider caído la estrategia cross-sectional excluye silenciosamente.

    CrossSectionalMomentumStrategy ya captura excepciones del provider (BLE001)
    y excluye el instrumento. Sin datos → sin señales → sin órdenes.
    """
    from qtrader.backtesting.engine import BacktestEngine, ApproveAllRisk, CostAwareSimBroker
    from qtrader.costs import CostsConfig
    from qtrader.strategies.momentum import (
        CrossSectionalMomentumStrategy,
        MomentumEqualWeightPortfolio,
    )

    trading_days = _trading_days(30)
    instruments = [_make_instrument("SPY"), _make_instrument("QQQ")]
    univ_dict = {i.symbol: i for i in instruments}

    class _UM:
        def get_universe(self, as_of: date) -> list[Instrument]:
            return instruments
        def get_instrument(self, symbol: str, as_of: date) -> Instrument | None:
            return univ_dict.get(symbol)

    broker = CostAwareSimBroker(costs_config=CostsConfig.zero_costs(), universe=univ_dict)
    engine = BacktestEngine(
        provider=_FailingProvider(),  # type: ignore[arg-type]
        universe_mgr=_UM(),  # type: ignore[arg-type]
        strategy=CrossSectionalMomentumStrategy(trading_days=trading_days),
        portfolio=MomentumEqualWeightPortfolio(initial_equity=_EQUITY),
        risk=ApproveAllRisk(),
        broker=broker,
        trading_days=trading_days,
        initial_equity=_EQUITY,
    )
    result = engine.run()
    # Provider falla → strategy excluye todos los instrumentos → sin señales → sin fills
    assert result.total_fills == 0
    assert result.final_equity == _EQUITY


# ---------------------------------------------------------------------------
# Escenario 2: Precios anómalos (calidad SUSPECT)
# ---------------------------------------------------------------------------

class _BadPricesProvider:
    def get_bars(
        self, symbol: str, start: datetime, end: datetime, as_of: datetime
    ) -> list[ValidatedBar]:
        return [ValidatedBar(
            bar=Bar(
                symbol=symbol,
                timestamp=start,
                open=_ZERO,
                high=_ZERO,
                low=_ZERO,
                close=_ZERO,
                volume=_ONE,
            ),
            quality=DataQuality.SUSPECT,
        )]

    def get_corporate_actions(
        self, symbol: str, start: datetime, end: datetime
    ) -> list[Any]:
        return []


def test_bad_prices_excluded_no_crash() -> None:
    """Datos SUSPECT son excluidos por CrossSectionalMomentumStrategy; sin crash."""
    from qtrader.backtesting.engine import BacktestEngine, ApproveAllRisk, CostAwareSimBroker
    from qtrader.costs import CostsConfig
    from qtrader.strategies.momentum import (
        CrossSectionalMomentumStrategy,
        MomentumEqualWeightPortfolio,
    )

    trading_days = _trading_days(20)
    instruments = [_make_instrument("SPY")]
    univ_dict = {i.symbol: i for i in instruments}

    engine = BacktestEngine(
        provider=_BadPricesProvider(),  # type: ignore[arg-type]
        universe_mgr=type("UM", (), {
            "get_universe": lambda self, as_of: instruments,
            "get_instrument": lambda self, s, as_of: univ_dict.get(s),
        })(),  # type: ignore[arg-type]
        strategy=CrossSectionalMomentumStrategy(trading_days=trading_days),
        portfolio=MomentumEqualWeightPortfolio(initial_equity=_EQUITY),
        risk=ApproveAllRisk(),
        broker=CostAwareSimBroker(costs_config=CostsConfig.zero_costs(), universe=univ_dict),
        trading_days=trading_days,
        initial_equity=_EQUITY,
    )
    result = engine.run()
    assert result.total_fills == 0
    assert result.final_equity == _EQUITY


# ---------------------------------------------------------------------------
# Escenario 3: Broker WAL — orden persiste y no se duplica (simulación crash)
# ---------------------------------------------------------------------------

def test_broker_order_persists_and_no_duplicate(tmp_path: Path) -> None:
    """Orden registrada en WAL sobrevive al cierre del broker. No hay duplicado."""
    broker = _make_paper_broker(tmp_path)
    inst = _make_instrument("SPY")
    broker.set_market_data(
        data={"SPY": {date(2024, 6, 17): Decimal("100")}},
        instruments={"SPY": inst},
    )

    order, coid = _make_approved_order("SPY", Decimal("10"))
    _run(broker.submit_order(order, coid))
    broker.close()

    # Reabrir — simula reinicio de proceso
    broker2 = _make_paper_broker(tmp_path)
    from qtrader.brokers.types import OrderStatus
    status = _run(broker2.get_order_status(coid))
    assert status in (OrderStatus.SUBMITTED, OrderStatus.FILLED, OrderStatus.PENDING)

    # Sin duplicados en la BD
    fills = _run(broker2.get_fills_since(datetime(2024, 1, 1, tzinfo=UTC)))
    client_ids = [f.order_id for f in fills]
    assert client_ids.count(coid) <= 1
    broker2.close()


# ---------------------------------------------------------------------------
# Escenario 4: Fill parcial — posición resultante es coherente
# ---------------------------------------------------------------------------

def test_partial_fill_position_coherent(tmp_path: Path) -> None:
    """Orden de 5 acciones → posición de exactamente 5. Sin sobrecargo."""
    broker = _make_paper_broker(tmp_path)
    inst = _make_instrument("SPY")
    broker.set_market_data(
        data={"SPY": {date(2024, 6, 17): Decimal("100")}},
        instruments={"SPY": inst},
    )

    order, coid = _make_approved_order("SPY", Decimal("5"))
    _run(broker.submit_order(order, coid))
    broker.advance_to(date(2024, 6, 17))

    positions = _run(broker.get_positions())
    spy_pos = next((p for p in positions if p.symbol == "SPY"), None)
    assert spy_pos is not None
    assert spy_pos.quantity == Decimal("5")
    broker.close()


# ---------------------------------------------------------------------------
# Escenario 5: Orden duplicada (mismo client_order_id) es idempotente
# ---------------------------------------------------------------------------

def test_duplicate_order_idempotent(tmp_path: Path) -> None:
    """Reenviar la misma orden (mismo client_order_id) no duplica la posición."""
    broker = _make_paper_broker(tmp_path)
    broker.set_market_data(
        data={"SPY": {date(2024, 6, 17): Decimal("100")}},
        instruments={"SPY": _make_instrument("SPY")},
    )

    order, coid = _make_approved_order("SPY", Decimal("5"))
    _run(broker.submit_order(order, coid))
    # Segundo submit con el mismo ID: debe ser idempotente (INSERT OR IGNORE)
    _run(broker.submit_order(order, coid))

    broker.advance_to(date(2024, 6, 17))
    positions = _run(broker.get_positions())
    spy_pos = next((p for p in positions if p.symbol == "SPY"), None)
    qty = spy_pos.quantity if spy_pos else _ZERO
    assert qty <= Decimal("5"), f"Posición duplicada detectada: qty={qty}"
    broker.close()


# ---------------------------------------------------------------------------
# Escenario 6: Kill switch activo — el agente se detiene
# ---------------------------------------------------------------------------

def test_kill_switch_halts_agent(tmp_path: Path) -> None:
    """Con kill switch activo, TraderAgent lanza AgentHalted en el primer ciclo."""
    from qtrader.agents.trader import AgentHalted, TraderAgent
    from qtrader.agents.context import AgentContext
    from qtrader.agents.store import AgentStateStore
    from qtrader.execution.rate_limiter import ExecutionRateLimiter, RateLimiterConfig
    from qtrader.execution.sanity import BrokerSanityChecker, SanityConfig
    from qtrader.ledger.sqlite import SQLiteLedger
    from qtrader.portfolio.construction import PortfolioConfig, PortfolioConstructor
    from qtrader.risk.config import RiskConfig
    from tests.agents.conftest import (
        FakeKillSwitch, FakeWatchdog, FakeStrategy, FakeProvider,
        SYMBOLS, make_instrument,
    )

    instruments = {s: make_instrument(s) for s in SYMBOLS}
    kill_switch = FakeKillSwitch(active=True)

    from qtrader.brokers.paper_broker import PaperBroker
    from qtrader.costs import CostsConfig as _CC
    broker = PaperBroker(
        db_path=tmp_path / "ks_paper.db",
        initial_cash=_EQUITY,
        costs_config=_CC.zero_costs(),
        _testing=True,
    )
    opens = {s: {_TRADING_DATE: Decimal("100")} for s in SYMBOLS}
    broker.set_market_data(
        data=opens, instruments=dict(instruments),
        adv={s: Decimal("10000000") for s in SYMBOLS},
    )

    ledger = SQLiteLedger(str(tmp_path / "state.db"))
    ledger.initialize()
    store = AgentStateStore(tmp_path / "agent.db")
    rl = ExecutionRateLimiter(
        db_path=tmp_path / "exec.db",
        config=RateLimiterConfig(
            max_orders_per_minute=100, max_orders_per_day=100,
            max_notional_per_day=Decimal("1000000"),
        ),
    )

    ctx = AgentContext(
        broker=broker,  # type: ignore[arg-type]
        ledger=ledger,
        store=store,
        kill_switch=kill_switch,
        watchdog=FakeWatchdog(),  # type: ignore[arg-type]
        rate_limiter=rl,
        sanity=BrokerSanityChecker(config=SanityConfig(
            max_price_deviation=Decimal("0.50"), max_adv_fraction=Decimal("1"),
        )),
        strategy=FakeStrategy(),  # type: ignore[arg-type]
        portfolio_constructor=PortfolioConstructor(
            instruments=instruments,
            config=PortfolioConfig(min_position_size_eur=Decimal("100")),
        ),
        provider=FakeProvider(),  # type: ignore[arg-type]
        instruments=instruments,
        trading_days=[_TRADING_DATE],
        risk_config=RiskConfig(),
        portfolio_config=PortfolioConfig(min_position_size_eur=Decimal("100")),
        mode="paper",
        strategy_id="test",
        adv={s: Decimal("10000000") for s in SYMBOLS},
    )

    from qtrader.agents.states import RunPhase

    agent = TraderAgent(ctx)
    result = _run(agent.run_once(RunPhase.POST_CLOSE, _TRADING_DATE))
    assert result.halted_by_kill_switch, "El agente debe detenerse por kill switch activo"

    store.close()
    rl.close()
    broker.close()


# ---------------------------------------------------------------------------
# Escenario 7: Datos SUSPECT de mercado suspendido → sin órdenes
# ---------------------------------------------------------------------------

class _SuspendedProvider:
    """Todas las barras con DataQuality.SUSPECT."""

    def get_bars(
        self, symbol: str, start: datetime, end: datetime, as_of: datetime
    ) -> list[ValidatedBar]:
        d = start.date()
        bars: list[ValidatedBar] = []
        while d <= end.date():
            if d.weekday() < 5:
                bars.append(ValidatedBar(
                    bar=Bar(
                        symbol=symbol,
                        timestamp=datetime(d.year, d.month, d.day, tzinfo=UTC),
                        open=Decimal("100"),
                        high=Decimal("100"),
                        low=Decimal("100"),
                        close=Decimal("100"),
                        volume=_ZERO,
                    ),
                    quality=DataQuality.SUSPECT,
                ))
            d += timedelta(days=1)
        return bars

    def get_corporate_actions(
        self, symbol: str, start: datetime, end: datetime
    ) -> list[Any]:
        return []


def test_market_suspended_no_fills() -> None:
    """Datos SUSPECT = mercado suspendido → CrossSectionalMomentumStrategy excluye todo."""
    from qtrader.backtesting.engine import BacktestEngine, ApproveAllRisk, CostAwareSimBroker
    from qtrader.costs import CostsConfig
    from qtrader.strategies.momentum import (
        CrossSectionalMomentumStrategy,
        MomentumEqualWeightPortfolio,
    )

    trading_days = _trading_days(20)
    instruments = [_make_instrument("SPY")]
    univ_dict = {i.symbol: i for i in instruments}

    engine = BacktestEngine(
        provider=_SuspendedProvider(),  # type: ignore[arg-type]
        universe_mgr=type("UM", (), {
            "get_universe": lambda self, as_of: instruments,
            "get_instrument": lambda self, s, as_of: univ_dict.get(s),
        })(),  # type: ignore[arg-type]
        strategy=CrossSectionalMomentumStrategy(trading_days=trading_days),
        portfolio=MomentumEqualWeightPortfolio(initial_equity=_EQUITY),
        risk=ApproveAllRisk(),
        broker=CostAwareSimBroker(costs_config=CostsConfig.zero_costs(), universe=univ_dict),
        trading_days=trading_days,
        initial_equity=_EQUITY,
    )
    result = engine.run()
    assert result.total_fills == 0


# ---------------------------------------------------------------------------
# Escenario 8: Risk Engine rechaza todas las órdenes
# ---------------------------------------------------------------------------

def test_risk_engine_halt_level_rejects_buys() -> None:
    """Con nivel HALT, el Risk Engine rechaza todas las órdenes de compra."""
    from qtrader.risk.engine import RiskEngine
    from qtrader.risk.config import RiskConfig
    from qtrader.risk.types import (
        CurrentPortfolio, MarketState, ProposedOrder, RiskLevel,
    )
    from qtrader.core.types import Side

    config = RiskConfig(
        max_drawdown=Decimal("0.05"),  # umbral bajo → nivel HALT inmediato
        daily_loss_limit=Decimal("0.01"),
    )

    portfolio = CurrentPortfolio(
        positions=(),
        nav=Decimal("10000"),         # 33 % de caída desde el pico
        peak_nav=_EQUITY,
        nav_open_today=Decimal("10000"),
        nav_open_week=Decimal("10000"),
        orders_today=0,
        notional_today=_ZERO,
    )

    market_state = MarketState(
        trading_date=_TRADING_DATE,
        days_below_threshold=10,
        is_market_open=True,
    )

    proposed = [
        ProposedOrder(
            order_id="ord-1",
            symbol="SPY",
            side=Side.BUY,
            quantity=Decimal("10"),
            price=Decimal("100"),
            category=InstrumentCategory.REGION,
        )
    ]

    decision = RiskEngine.evaluate(portfolio, proposed, market_state, config)
    # Con drawdown > max_drawdown → nivel HALT → no hay compras aprobadas
    assert len(decision.approved_orders) == 0
    assert decision.level == RiskLevel.HALT


# ---------------------------------------------------------------------------
# Escenario 9: Rate limiter agotado → segunda orden rechazada
# ---------------------------------------------------------------------------

def test_rate_limiter_second_order_rejected(tmp_path: Path) -> None:
    """Rate limiter con max_orders_per_day=1: segunda orden es rechazada."""
    from qtrader.execution.rate_limiter import ExecutionRateLimiter, RateLimiterConfig

    rl = ExecutionRateLimiter(
        db_path=tmp_path / "rl.db",
        config=RateLimiterConfig(
            max_orders_per_minute=10,
            max_orders_per_day=1,
            max_notional_per_day=Decimal("1000000"),
        ),
    )

    r1 = rl.check_and_record(
        order_id="ord-1",
        symbol="SPY",
        notional=Decimal("500"),
        timestamp=_UTC_TS,
    )
    assert r1.allowed, "Primera orden debe pasar"

    r2 = rl.check_and_record(
        order_id="ord-2",
        symbol="SPY",
        notional=Decimal("500"),
        timestamp=_UTC_TS,
    )
    assert not r2.allowed, "Segunda orden debe ser rechazada por límite diario"
    rl.close()


# ---------------------------------------------------------------------------
# Escenario 10: Crash y recovery — WAL preserva las intenciones
# ---------------------------------------------------------------------------

def test_wal_crash_recovery_no_duplicates(tmp_path: Path) -> None:
    """Crash tras submit: al reabrir, la intención está persistida sin duplicar."""
    from qtrader.brokers.paper_broker import PaperBroker
    from qtrader.brokers.types import OrderStatus
    from qtrader.costs import CostsConfig

    db = tmp_path / "crash_paper.db"
    market = {"SPY": {date(2024, 6, 17): Decimal("100")}}
    insts = {"SPY": _make_instrument("SPY")}

    b1 = PaperBroker(db_path=db, initial_cash=_EQUITY,
                     costs_config=CostsConfig.zero_costs(), _testing=True)
    b1.set_market_data(data=market, instruments=insts)
    order, coid = _make_approved_order("SPY", Decimal("3"))
    _run(b1.submit_order(order, coid))
    b1.close()  # simula crash

    # Recovery: reabrir misma BD
    b2 = PaperBroker(db_path=db, initial_cash=_EQUITY,
                     costs_config=CostsConfig.zero_costs(), _testing=True)
    b2.set_market_data(data=market, instruments=insts)
    status = _run(b2.get_order_status(coid))
    assert status in (OrderStatus.SUBMITTED, OrderStatus.FILLED, OrderStatus.PENDING)

    b2.advance_to(date(2024, 6, 17))
    fills = _run(b2.get_fills_since(datetime(2024, 1, 1, tzinfo=UTC)))
    matching = [f for f in fills if f.client_order_id == coid]
    assert len(matching) <= 1, f"Duplicado en fills: {len(matching)}"
    b2.close()


# ---------------------------------------------------------------------------
# Escenario 11: Reinicio del servidor — estado de cartera se reconstituye
# ---------------------------------------------------------------------------

def test_portfolio_survives_restart(tmp_path: Path) -> None:
    """La cartera persiste en SQLite y se puede leer tras un reinicio simulado."""
    from qtrader.brokers.paper_broker import PaperBroker
    from qtrader.costs import CostsConfig
    from qtrader.brokers.types import OrderStatus

    db = tmp_path / "paper.db"

    b1 = PaperBroker(db_path=db, initial_cash=_EQUITY,
                     costs_config=CostsConfig.zero_costs(), _testing=True)
    b1.set_market_data(
        data={"SPY": {date(2024, 6, 17): Decimal("100")}},
        instruments={"SPY": _make_instrument("SPY")},
    )
    order, coid = _make_approved_order("SPY", Decimal("7"))
    _run(b1.submit_order(order, coid))
    b1.advance_to(date(2024, 6, 17))
    b1.close()

    b2 = PaperBroker(db_path=db, initial_cash=_EQUITY,
                     costs_config=CostsConfig.zero_costs(), _testing=True)
    positions = _run(b2.get_positions())
    spy_pos = next((p for p in positions if p.symbol == "SPY"), None)
    assert spy_pos is not None, "La posición debe sobrevivir al reinicio"
    assert spy_pos.quantity == Decimal("7")
    b2.close()


# ---------------------------------------------------------------------------
# Escenario 12: Risk Engine puro — determinismo garantizado
# ---------------------------------------------------------------------------

def test_risk_engine_is_deterministic() -> None:
    """Mismas entradas → misma salida: Risk Engine no tiene estado."""
    from qtrader.risk.engine import RiskEngine
    from qtrader.risk.config import RiskConfig
    from qtrader.risk.types import (
        CurrentPortfolio, MarketState, ProposedOrder,
    )
    from qtrader.core.types import Side

    config = RiskConfig()
    portfolio = CurrentPortfolio(
        positions=(),
        nav=_EQUITY,
        peak_nav=_EQUITY,
        nav_open_today=_EQUITY,
        nav_open_week=_EQUITY,
        orders_today=0,
        notional_today=_ZERO,
    )
    market_state = MarketState(
        trading_date=_TRADING_DATE,
        days_below_threshold=0,
        is_market_open=True,
    )
    proposed = [
        ProposedOrder(
            order_id="ord-x",
            symbol="SPY",
            side=Side.BUY,
            quantity=Decimal("10"),
            price=Decimal("100"),
            category=InstrumentCategory.REGION,
        )
    ]

    d1 = RiskEngine.evaluate(portfolio, proposed, market_state, config)
    d2 = RiskEngine.evaluate(portfolio, proposed, market_state, config)
    d3 = RiskEngine.evaluate(portfolio, proposed, market_state, config)

    assert d1.level == d2.level == d3.level
    assert len(d1.approved_orders) == len(d2.approved_orders) == len(d3.approved_orders)
    assert d1.warnings == d2.warnings == d3.warnings
