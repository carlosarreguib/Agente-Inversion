"""Tests del Risk Engine (T4.2).

Cubre:
  - Calculo de nivel de riesgo por drawdown
  - Calculo de nivel por perdida diaria/semanal (capa independiente)
  - Histeresis de recuperacion (recovery_days)
  - Rate limits (capa independiente): ordenes y notional
  - Reducciones en orden fijo: notional → position_weight → exposure → sector → region
  - Multiplicador de nivel aplicado DESPUES de todos los limites
  - HALT: solo SELL de posiciones existentes
  - RISK_OFF: solo SELL
  - Mercado cerrado: todo rechazado
  - Propiedades P1-P8 con hypothesis
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from qtrader.core.types import InstrumentCategory, Side
from qtrader.risk import (
    CurrentPortfolio,
    MarketState,
    PositionSnapshot,
    ProposedOrder,
    RiskConfig,
    RiskDecision,
    RiskEngine,
    RiskLevel,
    compute_risk_level,
)

# ---------------------------------------------------------------------------
# Fixtures y helpers
# ---------------------------------------------------------------------------

_ZERO = Decimal("0")
_CONFIG = RiskConfig()
_TODAY = date(2024, 1, 15)
_NOW = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)


def _market(
    is_open: bool = True,
    days_below: int = 10,
    trading_date: date = _TODAY,
) -> MarketState:
    return MarketState(
        trading_date=trading_date,
        days_below_threshold=days_below,
        is_market_open=is_open,
    )


def _portfolio(
    nav: Decimal = Decimal("15000"),
    peak_nav: Decimal = Decimal("15000"),
    nav_today: Decimal = Decimal("15000"),
    nav_week: Decimal = Decimal("15000"),
    positions: tuple[PositionSnapshot, ...] = (),
    orders_today: int = 0,
    notional_today: Decimal = _ZERO,
) -> CurrentPortfolio:
    return CurrentPortfolio(
        positions=positions,
        nav=nav,
        peak_nav=peak_nav,
        nav_open_today=nav_today,
        nav_open_week=nav_week,
        orders_today=orders_today,
        notional_today=notional_today,
    )


def _order(
    qty: Decimal = Decimal("10"),
    price: Decimal = Decimal("100"),
    side: Side = Side.BUY,
    symbol: str = "SPY",
    category: InstrumentCategory = InstrumentCategory.REGION,
    order_id: str = "ord-001",
) -> ProposedOrder:
    return ProposedOrder(
        order_id=order_id,
        symbol=symbol,
        side=side,
        quantity=qty,
        price=price,
        category=category,
    )


def _pos(
    symbol: str = "SPY",
    qty: Decimal = Decimal("10"),
    price: Decimal = Decimal("100"),
    category: InstrumentCategory = InstrumentCategory.REGION,
) -> PositionSnapshot:
    return PositionSnapshot(
        symbol=symbol,
        quantity=qty,
        price=price,
        category=category,
        notional=qty * price,
    )


# ---------------------------------------------------------------------------
# Tests de compute_risk_level
# ---------------------------------------------------------------------------

class TestComputeRiskLevel:
    def test_normal_no_drawdown(self) -> None:
        level, warnings = compute_risk_level(
            current_drawdown=_ZERO,
            daily_loss=_ZERO,
            weekly_loss=_ZERO,
            days_below_threshold=0,
            current_level=RiskLevel.NORMAL,
            config=_CONFIG,
        )
        assert level == RiskLevel.NORMAL
        assert warnings == []

    def test_caution_at_threshold(self) -> None:
        level, _ = compute_risk_level(
            current_drawdown=Decimal("-0.05"),
            daily_loss=_ZERO,
            weekly_loss=_ZERO,
            days_below_threshold=0,
            current_level=RiskLevel.NORMAL,
            config=_CONFIG,
        )
        assert level == RiskLevel.CAUTION

    def test_risk_off_at_threshold(self) -> None:
        level, _ = compute_risk_level(
            current_drawdown=Decimal("-0.09"),
            daily_loss=_ZERO,
            weekly_loss=_ZERO,
            days_below_threshold=0,
            current_level=RiskLevel.NORMAL,
            config=_CONFIG,
        )
        assert level == RiskLevel.RISK_OFF

    def test_halt_at_threshold(self) -> None:
        level, warnings = compute_risk_level(
            current_drawdown=Decimal("-0.13"),
            daily_loss=_ZERO,
            weekly_loss=_ZERO,
            days_below_threshold=0,
            current_level=RiskLevel.NORMAL,
            config=_CONFIG,
        )
        assert level == RiskLevel.HALT
        assert any("HALT_DRAWDOWN" in w for w in warnings)

    def test_halt_by_daily_loss(self) -> None:
        level, warnings = compute_risk_level(
            current_drawdown=_ZERO,
            daily_loss=Decimal("-0.025"),  # > max_daily_loss=0.02
            weekly_loss=_ZERO,
            days_below_threshold=10,
            current_level=RiskLevel.NORMAL,
            config=_CONFIG,
        )
        assert level == RiskLevel.HALT
        assert any("HALT_DAILY_LOSS" in w for w in warnings)

    def test_risk_off_by_weekly_loss(self) -> None:
        level, warnings = compute_risk_level(
            current_drawdown=_ZERO,
            daily_loss=_ZERO,
            weekly_loss=Decimal("-0.05"),  # > max_weekly_loss=0.04
            days_below_threshold=10,
            current_level=RiskLevel.NORMAL,
            config=_CONFIG,
        )
        assert level == RiskLevel.RISK_OFF
        assert any("RISK_OFF_WEEKLY_LOSS" in w for w in warnings)

    def test_daily_loss_overrides_drawdown_level(self) -> None:
        # CAUTION por drawdown pero HALT por perdida diaria → HALT gana
        level, _ = compute_risk_level(
            current_drawdown=Decimal("-0.05"),
            daily_loss=Decimal("-0.03"),
            weekly_loss=_ZERO,
            days_below_threshold=10,
            current_level=RiskLevel.CAUTION,
            config=_CONFIG,
        )
        assert level == RiskLevel.HALT

    def test_recovery_requires_enough_days(self) -> None:
        # CAUTION actual, drawdown ya OK, pero solo 3 dias bajo umbral → permanece CAUTION
        level, warnings = compute_risk_level(
            current_drawdown=_ZERO,
            daily_loss=_ZERO,
            weekly_loss=_ZERO,
            days_below_threshold=3,
            current_level=RiskLevel.CAUTION,
            config=_CONFIG,  # recovery_days=5
        )
        assert level == RiskLevel.CAUTION
        assert any("RECOVERY_PENDING" in w for w in warnings)

    def test_recovery_after_enough_days(self) -> None:
        level, _ = compute_risk_level(
            current_drawdown=_ZERO,
            daily_loss=_ZERO,
            weekly_loss=_ZERO,
            days_below_threshold=5,
            current_level=RiskLevel.CAUTION,
            config=_CONFIG,
        )
        assert level == RiskLevel.NORMAL


# ---------------------------------------------------------------------------
# Tests de RiskEngine.evaluate — casos basicos
# ---------------------------------------------------------------------------

class TestEvaluateBasic:
    def test_market_closed_rejects_all(self) -> None:
        portfolio = _portfolio()
        orders = [_order()]
        result = RiskEngine.evaluate(portfolio, orders, _market(is_open=False), _CONFIG)
        assert len(result.approved_orders) == 0
        assert len(result.rejected_orders) == 1
        assert result.rejected_orders[0].reason == "MARKET_CLOSED"

    def test_normal_level_approves_valid_order(self) -> None:
        portfolio = _portfolio()
        orders = [_order(qty=Decimal("15"), price=Decimal("100"))]  # 1500 EUR > min=1000
        result = RiskEngine.evaluate(portfolio, orders, _market(), _CONFIG)
        assert result.level == RiskLevel.NORMAL
        assert len(result.approved_orders) == 1
        assert len(result.rejected_orders) == 0

    def test_halt_rejects_buy(self) -> None:
        portfolio = _portfolio(
            nav=Decimal("13050"),
            peak_nav=Decimal("15000"),  # -13% → HALT
        )
        orders = [_order(side=Side.BUY)]
        result = RiskEngine.evaluate(portfolio, orders, _market(days_below=0), _CONFIG)
        assert result.level == RiskLevel.HALT
        assert result.rejected_orders[0].reason.startswith("HALT")

    def test_halt_allows_sell_of_existing_position(self) -> None:
        pos = _pos(qty=Decimal("20"), price=Decimal("100"))  # 2000 EUR de posicion
        # nav_today=nav_week=nav para que daily/weekly_loss=0 y solo actue el drawdown
        portfolio = _portfolio(
            nav=Decimal("13050"),
            peak_nav=Decimal("15000"),
            nav_today=Decimal("13050"),
            nav_week=Decimal("13050"),
            positions=(pos,),
        )
        sell_order = _order(qty=Decimal("15"), price=Decimal("100"), side=Side.SELL)  # 1500 > min
        result = RiskEngine.evaluate(portfolio, [sell_order], _market(days_below=0), _CONFIG)
        assert result.level == RiskLevel.HALT
        assert len(result.approved_orders) == 1

    def test_halt_rejects_sell_without_position(self) -> None:
        portfolio = _portfolio(
            nav=Decimal("13050"),
            peak_nav=Decimal("15000"),
            nav_today=Decimal("13050"),
            nav_week=Decimal("13050"),
        )
        sell_order = _order(qty=Decimal("15"), price=Decimal("100"), side=Side.SELL)
        result = RiskEngine.evaluate(portfolio, [sell_order], _market(days_below=0), _CONFIG)
        assert result.rejected_orders[0].reason == "HALT: no existing position to close"

    def test_risk_off_rejects_buy(self) -> None:
        # nav_today=nav_week=nav para que daily/weekly_loss=0 y solo actue el drawdown
        portfolio = _portfolio(
            nav=Decimal("13650"),
            peak_nav=Decimal("15000"),  # -9% → RISK_OFF
            nav_today=Decimal("13650"),
            nav_week=Decimal("13650"),
        )
        orders = [_order(side=Side.BUY)]
        result = RiskEngine.evaluate(portfolio, orders, _market(days_below=0), _CONFIG)
        assert result.level == RiskLevel.RISK_OFF
        assert result.rejected_orders[0].reason.startswith("RISK_OFF")

    def test_risk_off_allows_sell(self) -> None:
        pos = _pos(qty=Decimal("20"), price=Decimal("100"))  # 2000 EUR de posicion
        portfolio = _portfolio(
            nav=Decimal("13650"),
            peak_nav=Decimal("15000"),
            nav_today=Decimal("13650"),
            nav_week=Decimal("13650"),
            positions=(pos,),
        )
        sell_order = _order(qty=Decimal("15"), price=Decimal("100"), side=Side.SELL)  # 1500 > min
        result = RiskEngine.evaluate(portfolio, [sell_order], _market(days_below=0), _CONFIG)
        assert len(result.approved_orders) == 1


# ---------------------------------------------------------------------------
# Tests de Rate Limits (capa independiente)
# ---------------------------------------------------------------------------

class TestRateLimits:
    def test_rate_limit_orders_per_day(self) -> None:
        # 20 ordenes ya enviadas hoy
        portfolio = _portfolio(orders_today=20, notional_today=_ZERO)
        order = _order(qty=Decimal("5"), price=Decimal("100"))
        result = RiskEngine.evaluate(portfolio, [order], _market(), _CONFIG)
        assert result.rejected_orders[0].reason.startswith("RATE_LIMIT_ORDERS")

    def test_rate_limit_notional_per_day(self) -> None:
        # 15000 EUR ya enviados hoy
        portfolio = _portfolio(
            orders_today=0,
            notional_today=Decimal("15000"),
        )
        order = _order(qty=Decimal("5"), price=Decimal("100"))
        result = RiskEngine.evaluate(portfolio, [order], _market(), _CONFIG)
        assert result.rejected_orders[0].reason.startswith("RATE_LIMIT_NOTIONAL")

    def test_rate_limit_cumulative_across_orders(self) -> None:
        # Primera orden de 10000, segunda de 6000: la segunda supera el limite
        portfolio = _portfolio()
        cfg2 = RiskConfig(
            max_notional_per_day=Decimal("10000"),
            max_order_notional=Decimal("15000"),
            max_orders_per_day=20,
            min_order_size=Decimal("1"),
        )
        ord_a = _order(qty=Decimal("80"), price=Decimal("100"), order_id="ord-a")  # 8000
        ord_b = _order(qty=Decimal("80"), price=Decimal("100"), order_id="ord-b")  # 8000
        result2 = RiskEngine.evaluate(portfolio, [ord_a, ord_b], _market(), cfg2)
        # ord-a aprobada; ord-b reducida o rechazada por rate limit
        assert len(result2.approved_orders) >= 1
        if len(result2.approved_orders) == 1:
            assert result2.approved_orders[0].order_id == "ord-a"


# ---------------------------------------------------------------------------
# Tests de Reducciones
# ---------------------------------------------------------------------------

class TestReductions:
    def test_max_order_notional_reduces_quantity(self) -> None:
        # max_order_notional=3000; orden de qty=50, price=100 → 5000 EUR → reducida a 30
        portfolio = _portfolio()
        order = _order(qty=Decimal("50"), price=Decimal("100"))
        result = RiskEngine.evaluate(portfolio, [order], _market(), _CONFIG)
        assert len(result.approved_orders) == 1
        approved = result.approved_orders[0]
        assert approved.approved_quantity == Decimal("30")  # floor(3000/100)
        assert approved.reduction_reason is not None
        assert "max_order_notional" in approved.reduction_reason

    def test_min_order_size_rejects_tiny_order(self) -> None:
        portfolio = _portfolio()
        order = _order(qty=Decimal("5"), price=Decimal("100"))  # 500 EUR < min=1000
        result = RiskEngine.evaluate(portfolio, [order], _market(), _CONFIG)
        assert len(result.rejected_orders) == 1
        assert "min_order_size" in result.rejected_orders[0].reason

    def test_level_multiplier_applied_after_limits(self) -> None:
        # CAUTION (x0.6): qty=50, price=100 → max_order_notional deja en 30, luego x0.6 → 18
        # nav_today=nav_week=nav para que daily/weekly_loss=0 y solo actue el drawdown
        portfolio = _portfolio(
            nav=Decimal("14250"),  # -5% → CAUTION
            peak_nav=Decimal("15000"),
            nav_today=Decimal("14250"),
            nav_week=Decimal("14250"),
        )
        cfg = RiskConfig(
            drawdown_caution=Decimal("0.05"),
            max_order_notional=Decimal("3000"),
            min_order_size=Decimal("1000"),
        )
        order = _order(qty=Decimal("50"), price=Decimal("100"))
        result = RiskEngine.evaluate(portfolio, [order], _market(days_below=0), cfg)
        assert result.level == RiskLevel.CAUTION
        # max_order_notional → 30; x0.6 → floor(30*0.6)=18
        approved = result.approved_orders[0]
        assert approved.approved_quantity == Decimal("18")

    def test_max_position_weight_limits_buy(self) -> None:
        # NAV=15000, max_position_weight=0.25 → max notional=3750 por posicion
        # Posicion existente: 2000 EUR → puede anadir solo 1750 EUR → 17 acc a 100
        pos = _pos(qty=Decimal("20"), price=Decimal("100"))  # 2000 EUR
        portfolio = _portfolio(positions=(pos,))
        cfg = RiskConfig(
            max_order_notional=Decimal("10000"),  # sin restriccion de orden
            max_position_weight=Decimal("0.25"),
            min_order_size=Decimal("100"),
            max_total_exposure=Decimal("0.95"),
        )
        order = _order(qty=Decimal("50"), price=Decimal("100"))  # 5000 EUR
        result = RiskEngine.evaluate(portfolio, [order], _market(), cfg)
        approved = result.approved_orders[0]
        # max adicional = 0.25*15000 - 2000 = 1750; floor(1750/100)=17
        assert approved.approved_quantity == Decimal("17")

    def test_max_total_exposure_limits_buy(self) -> None:
        # NAV=15000, max_total_exposure=0.95 → max notional=14250
        # Posicion existente: 13000 EUR → solo puede anadir 1250 EUR
        # Usar ALTERNATIVE para evitar region cap
        pos = _pos(qty=Decimal("130"), price=Decimal("100"),
                   category=InstrumentCategory.ALTERNATIVE)
        portfolio = _portfolio(positions=(pos,))
        cfg = RiskConfig(
            max_order_notional=Decimal("10000"),
            max_position_weight=Decimal("1.0"),  # sin restriccion de peso
            max_single_position=Decimal("1.0"),
            max_total_exposure=Decimal("0.95"),
            min_order_size=Decimal("100"),
        )
        order = _order(
            qty=Decimal("50"), price=Decimal("100"), symbol="NEW",
            category=InstrumentCategory.ALTERNATIVE,
        )
        result = RiskEngine.evaluate(portfolio, [order], _market(), cfg)
        approved = result.approved_orders[0]
        # max adicional = 0.95*15000 - 13000 = 1250; floor(1250/100)=12
        assert approved.approved_quantity == Decimal("12")

    def test_max_sector_exposure_limits_sector_buy(self) -> None:
        # NAV=15000, max_sector_exposure=0.40 → max=6000
        # Sector existente: 5000 EUR → solo puede anadir 1000 EUR
        pos = _pos(symbol="XLC", qty=Decimal("50"), price=Decimal("100"),
                   category=InstrumentCategory.SECTOR)
        portfolio = _portfolio(positions=(pos,))
        cfg = RiskConfig(
            max_order_notional=Decimal("10000"),
            max_position_weight=Decimal("1.0"),
            max_single_position=Decimal("1.0"),
            max_total_exposure=Decimal("1.0"),
            max_sector_exposure=Decimal("0.40"),
            min_order_size=Decimal("100"),
        )
        order = _order(
            qty=Decimal("30"), price=Decimal("100"),
            symbol="XLK", category=InstrumentCategory.SECTOR,
        )
        result = RiskEngine.evaluate(portfolio, [order], _market(), cfg)
        approved = result.approved_orders[0]
        # max adicional = 0.40*15000 - 5000 = 1000; floor(1000/100)=10
        assert approved.approved_quantity == Decimal("10")

    def test_max_region_exposure_limits_region_buy(self) -> None:
        # NAV=15000, max_region_exposure=0.60 → max=9000
        # Region existente: 8000 EUR → solo puede anadir 1000 EUR
        pos = _pos(symbol="VEA", qty=Decimal("80"), price=Decimal("100"),
                   category=InstrumentCategory.REGION)
        portfolio = _portfolio(positions=(pos,))
        cfg = RiskConfig(
            max_order_notional=Decimal("10000"),
            max_position_weight=Decimal("1.0"),
            max_single_position=Decimal("1.0"),
            max_total_exposure=Decimal("1.0"),
            max_region_exposure=Decimal("0.60"),
            min_order_size=Decimal("100"),
        )
        order = _order(
            qty=Decimal("20"), price=Decimal("100"),
            symbol="SPY", category=InstrumentCategory.REGION,
        )
        result = RiskEngine.evaluate(portfolio, [order], _market(), cfg)
        approved = result.approved_orders[0]
        # max adicional = 0.60*15000 - 8000 = 1000; floor(1000/100)=10
        assert approved.approved_quantity == Decimal("10")

    def test_sell_bypasses_exposure_limits(self) -> None:
        # Los limites de exposicion no aplican a SELL
        pos = _pos(qty=Decimal("200"), price=Decimal("100"))  # 20000 EUR > max_total_exposure
        portfolio = _portfolio(positions=(pos,))
        cfg = RiskConfig(
            max_order_notional=Decimal("10000"),
            max_position_weight=Decimal("0.10"),  # muy estricto
            max_total_exposure=Decimal("0.10"),
            min_order_size=Decimal("100"),
        )
        sell = _order(qty=Decimal("10"), price=Decimal("100"), side=Side.SELL)
        result = RiskEngine.evaluate(portfolio, [sell], _market(), cfg)
        assert len(result.approved_orders) == 1


# ---------------------------------------------------------------------------
# Tests de PortfolioRiskMetrics
# ---------------------------------------------------------------------------

class TestPortfolioRiskMetrics:
    def test_drawdown_zero_at_peak(self) -> None:
        portfolio = _portfolio(nav=Decimal("15000"), peak_nav=Decimal("15000"))
        result = RiskEngine.evaluate(portfolio, [], _market(), _CONFIG)
        assert result.portfolio_metrics.current_drawdown == _ZERO

    def test_drawdown_calculated_correctly(self) -> None:
        portfolio = _portfolio(nav=Decimal("13500"), peak_nav=Decimal("15000"))
        result = RiskEngine.evaluate(portfolio, [], _market(), _CONFIG)
        expected = (Decimal("13500") - Decimal("15000")) / Decimal("15000")
        assert result.portfolio_metrics.current_drawdown == expected  # -0.10

    def test_daily_loss_calculated(self) -> None:
        portfolio = _portfolio(nav=Decimal("14700"), nav_today=Decimal("15000"))
        result = RiskEngine.evaluate(portfolio, [], _market(), _CONFIG)
        expected = (Decimal("14700") - Decimal("15000")) / Decimal("15000")
        assert result.portfolio_metrics.daily_loss == expected  # -0.02

    def test_sector_region_exposures_are_tuples(self) -> None:
        pos = _pos(category=InstrumentCategory.SECTOR)
        portfolio = _portfolio(positions=(pos,))
        result = RiskEngine.evaluate(portfolio, [], _market(), _CONFIG)
        metrics = result.portfolio_metrics
        assert isinstance(metrics.sector_exposures, tuple)
        assert isinstance(metrics.region_exposures, tuple)
        # Cada elemento es una tupla (str, Decimal)
        for item in metrics.sector_exposures:
            assert isinstance(item, tuple)
            assert len(item) == 2
            assert isinstance(item[0], str)
            assert isinstance(item[1], Decimal)


# ---------------------------------------------------------------------------
# Tests de RiskDecision — modelo Pydantic
# ---------------------------------------------------------------------------

class TestRiskDecisionModel:
    def test_frozen(self) -> None:
        portfolio = _portfolio()
        result = RiskEngine.evaluate(portfolio, [], _market(), _CONFIG)
        with pytest.raises((ValidationError, AttributeError)):
            result.level = RiskLevel.HALT  # type: ignore[misc]

    def test_timestamp_is_utc(self) -> None:
        result = RiskEngine.evaluate(_portfolio(), [], _market(), _CONFIG)
        assert result.timestamp.tzinfo is not None
        assert result.timestamp.tzinfo == UTC or str(result.timestamp.tzinfo) in ("UTC", "+00:00")


# ---------------------------------------------------------------------------
# Property tests P1-P8 (hypothesis)
# ---------------------------------------------------------------------------

# Strategies
_decimal_pos = st.decimals(
    min_value=Decimal("0.01"), max_value=Decimal("10000"),
    allow_nan=False, allow_infinity=False,
).map(lambda d: d.quantize(Decimal("0.01")))

_nav_strategy = st.decimals(
    min_value=Decimal("1000"), max_value=Decimal("100000"),
    allow_nan=False, allow_infinity=False,
).map(lambda d: d.quantize(Decimal("1")))

_drawdown_strategy = st.decimals(
    min_value=Decimal("-0.50"), max_value=Decimal("0"),
    allow_nan=False, allow_infinity=False,
).map(lambda d: d.quantize(Decimal("0.001")))


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow], deadline=5000)
@given(
    nav=_nav_strategy,
    drawdown=_drawdown_strategy,
    days_below=st.integers(min_value=0, max_value=20),
)
def test_p1_level_monotone_with_drawdown(
    nav: Decimal, drawdown: Decimal, days_below: int,
) -> None:
    """P1: A mayor drawdown, el nivel es igual o mas restrictivo."""
    _LEVELS = [RiskLevel.NORMAL, RiskLevel.CAUTION, RiskLevel.RISK_OFF, RiskLevel.HALT]
    level1, _ = compute_risk_level(
        current_drawdown=drawdown,
        daily_loss=_ZERO,
        weekly_loss=_ZERO,
        days_below_threshold=days_below,
        current_level=RiskLevel.NORMAL,
        config=_CONFIG,
    )
    worse_drawdown = drawdown - Decimal("0.02")
    level2, _ = compute_risk_level(
        current_drawdown=worse_drawdown,
        daily_loss=_ZERO,
        weekly_loss=_ZERO,
        days_below_threshold=days_below,
        current_level=RiskLevel.NORMAL,
        config=_CONFIG,
    )
    assert _LEVELS.index(level2) >= _LEVELS.index(level1)


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow], deadline=5000)
@given(nav=_nav_strategy)
def test_p2_approved_notional_never_exceeds_rate_limit(nav: Decimal) -> None:
    """P2: El notional total aprobado en una sesion <= max_notional_per_day."""
    cfg = RiskConfig(
        max_order_notional=Decimal("5000"),
        min_order_size=Decimal("100"),
    )
    portfolio = _portfolio(nav=nav)
    orders = [
        _order(qty=Decimal("20"), price=Decimal("100"), order_id=f"ord-{i}")
        for i in range(10)
    ]
    result = RiskEngine.evaluate(portfolio, orders, _market(), cfg)
    total_approved = sum(a.notional for a in result.approved_orders)
    assert total_approved <= cfg.max_notional_per_day


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow], deadline=5000)
@given(nav=_nav_strategy)
def test_p3_approved_orders_never_exceed_rate_limit_count(nav: Decimal) -> None:
    """P3: El numero de ordenes aprobadas en una sesion <= max_orders_per_day."""
    cfg = RiskConfig(min_order_size=Decimal("100"))
    portfolio = _portfolio(nav=nav)
    orders = [
        _order(qty=Decimal("5"), price=Decimal("100"), order_id=f"ord-{i}")
        for i in range(30)
    ]
    result = RiskEngine.evaluate(portfolio, orders, _market(), cfg)
    assert len(result.approved_orders) <= cfg.max_orders_per_day


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow], deadline=5000)
@given(nav=_nav_strategy)
def test_p4_halt_no_buy_approved(nav: Decimal) -> None:
    """P4: En nivel HALT nunca se aprueba un BUY."""
    portfolio = _portfolio(
        nav=nav * Decimal("0.85"),
        peak_nav=nav,   # -15% → HALT
    )
    orders = [
        _order(qty=Decimal("5"), price=Decimal("100"), side=Side.BUY, order_id=f"b-{i}")
        for i in range(5)
    ]
    result = RiskEngine.evaluate(portfolio, orders, _market(days_below=0), _CONFIG)
    assert result.level == RiskLevel.HALT
    buys_approved = [a for a in result.approved_orders if a.side == Side.BUY]
    assert buys_approved == []


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow], deadline=5000)
@given(nav=_nav_strategy)
def test_p5_approved_quantity_le_original(nav: Decimal) -> None:
    """P5: La cantidad aprobada nunca supera la cantidad propuesta."""
    portfolio = _portfolio(nav=nav)
    orders = [
        _order(qty=Decimal("50"), price=Decimal("100"), order_id="ord-1"),
    ]
    result = RiskEngine.evaluate(portfolio, orders, _market(), _CONFIG)
    for approved in result.approved_orders:
        # Buscar la orden original
        original = next(o for o in orders if o.order_id == approved.order_id)
        assert approved.approved_quantity <= original.quantity


@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow], deadline=5000)
@given(nav=_nav_strategy)
def test_p6_every_order_classified(nav: Decimal) -> None:
    """P6: Toda orden propuesta termina en approved o rejected, nunca se pierde."""
    portfolio = _portfolio(nav=nav)
    orders = [
        _order(qty=Decimal("5"), price=Decimal("100"), order_id=f"ord-{i}")
        for i in range(5)
    ]
    result = RiskEngine.evaluate(portfolio, orders, _market(), _CONFIG)
    all_order_ids = {o.order_id for o in orders}
    approved_ids = {a.order_id for a in result.approved_orders}
    rejected_ids = {r.order_id for r in result.rejected_orders}
    assert approved_ids | rejected_ids == all_order_ids
    assert approved_ids & rejected_ids == set()  # sin solapamiento


@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow], deadline=5000)
@given(nav=_nav_strategy)
def test_p7_risk_decision_is_pydantic_frozen(nav: Decimal) -> None:
    """P7: RiskDecision es frozen=True — intentar mutarla lanza excepcion."""
    portfolio = _portfolio(nav=nav)
    result = RiskEngine.evaluate(portfolio, [], _market(), _CONFIG)
    assert isinstance(result, RiskDecision)
    with pytest.raises((ValidationError, AttributeError, TypeError)):
        result.level = RiskLevel.HALT  # type: ignore[misc]


@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow], deadline=5000)
@given(nav=_nav_strategy, drawdown=_drawdown_strategy)
def test_p8_determinism(nav: Decimal, drawdown: Decimal) -> None:
    """P8: Mismas entradas → misma salida. Los dos objetos de entrada son distintos en memoria."""
    peak = nav / (Decimal("1") + abs(drawdown)) if drawdown < _ZERO else nav
    order_id = "det-ord"

    # Construir DOS objetos DISTINTOS con los mismos valores
    portfolio_1 = CurrentPortfolio(
        positions=(),
        nav=nav,
        peak_nav=peak,
        nav_open_today=nav,
        nav_open_week=nav,
        orders_today=0,
        notional_today=_ZERO,
    )
    portfolio_2 = CurrentPortfolio(
        positions=(),
        nav=nav,
        peak_nav=peak,
        nav_open_today=nav,
        nav_open_week=nav,
        orders_today=0,
        notional_today=_ZERO,
    )
    assert portfolio_1 is not portfolio_2  # objetos distintos en memoria

    order_1 = ProposedOrder(
        order_id=order_id, symbol="SPY", side=Side.BUY,
        quantity=Decimal("10"), price=Decimal("100"),
        category=InstrumentCategory.REGION,
    )
    order_2 = ProposedOrder(
        order_id=order_id, symbol="SPY", side=Side.BUY,
        quantity=Decimal("10"), price=Decimal("100"),
        category=InstrumentCategory.REGION,
    )
    assert order_1 is not order_2

    market_1 = MarketState(trading_date=_TODAY, days_below_threshold=10, is_market_open=True)
    market_2 = MarketState(trading_date=_TODAY, days_below_threshold=10, is_market_open=True)
    assert market_1 is not market_2

    result_1 = RiskEngine.evaluate(portfolio_1, [order_1], market_1, _CONFIG)
    result_2 = RiskEngine.evaluate(portfolio_2, [order_2], market_2, _CONFIG)

    # Comparacion con == de Pydantic (campo a campo)
    assert result_1.level == result_2.level
    assert result_1.approved_orders == result_2.approved_orders
    assert result_1.rejected_orders == result_2.rejected_orders
    assert result_1.portfolio_metrics == result_2.portfolio_metrics
