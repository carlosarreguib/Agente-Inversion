"""Tests del modelo de costes realista (T2.2).

Test clave: buy-and-hold 1 año con no-costs vs with-costs.
La diferencia debe coincidir con el cálculo manual de comisiones
documentado en el propio test (sin approx() con tolerancia > 0.01 EUR).
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from qtrader.backtesting.engine import (
    ApproveAllRisk,
    BacktestEngine,
    BuyAllStrategy,
    CostAwareSimBroker,
    EqualWeightPortfolio,
    InstantSimBroker,
    SandboxedDataView,
)
from qtrader.core.types import (
    Bar,
    DataQuality,
    Direction,
    Instrument,
    InstrumentCategory,
    InstrumentType,
    Order,
    OrderType,
    Position,
    Side,
    Signal,
    TargetPosition,
    ValidatedBar,
)
from qtrader.costs import (
    CostsConfig,
    OrderTooSmall,
    adjusted_fill_price,
    calculate_costs,
)

_ZERO = Decimal("0")

# ---------------------------------------------------------------------------
# Helpers de fixture
# ---------------------------------------------------------------------------

_EPOCH = date(2022, 1, 3)


def _d(offset: int) -> date:
    return _EPOCH + timedelta(days=offset)


def _dt(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


def _bar(
    symbol: str,
    day: date,
    open_: str = "100",
    close: str = "102",
    volume: str = "500000",
) -> ValidatedBar:
    op = Decimal(open_)
    cl = Decimal(close)
    hi = max(op, cl) + Decimal("1")
    lo = min(op, cl) - Decimal("1")
    return ValidatedBar(
        bar=Bar(
            symbol=symbol,
            timestamp=_dt(day),
            open=op,
            high=hi,
            low=lo,
            close=cl,
            volume=Decimal(volume),
        ),
        quality=DataQuality.OK,
    )


def _instrument(
    symbol: str = "SPY",
    category: InstrumentCategory = InstrumentCategory.REGION,
    spread_bps: int | None = None,
) -> Instrument:
    return Instrument(
        symbol=symbol,
        name=f"{symbol} ETF",
        exchange="XNYS",
        currency="USD",
        category=category,
        ticker_proxy=symbol,
        ticker_ucits=f"{symbol}U",
        declared_on=_EPOCH,
        instrument_type=InstrumentType.ETF,
        spread_bps=spread_bps,
    )


def _order(
    symbol: str = "SPY",
    qty: str = "10",
    side: Side = Side.BUY,
    day: date | None = None,
) -> Order:
    d = day or _EPOCH
    return Order(
        client_order_id=str(uuid.uuid4()),
        symbol=symbol,
        side=side,
        quantity=Decimal(qty),
        order_type=OrderType.MOO,
        timestamp=_dt(d),
        strategy_id="test",
    )


_STD_CONFIG = CostsConfig(
    commission_rate=Decimal("0.0005"),
    min_commission=Decimal("1.25"),
    spread_bps_by_category=dict.fromkeys(InstrumentCategory, 5),
    slippage_factor=Decimal("0.1"),
    slippage_bps_fallback=3,
    max_participation=Decimal("0.10"),
    min_order_eur=Decimal("1000"),
)

# Config igual que _STD_CONFIG pero sin límite mínimo de orden (para tests de componentes).
_STD_NO_MIN = CostsConfig(
    commission_rate=Decimal("0.0005"),
    min_commission=Decimal("1.25"),
    spread_bps_by_category=dict.fromkeys(InstrumentCategory, 5),
    slippage_factor=Decimal("0.1"),
    slippage_bps_fallback=3,
    max_participation=Decimal("0.10"),
    min_order_eur=_ZERO,
)

_ZERO_CONFIG = CostsConfig.zero_costs()


# ---------------------------------------------------------------------------
# 1. Verificación manual exacta de calculate_costs
# ---------------------------------------------------------------------------

class TestCalculateCostsManual:
    """Verificación manual: cada número se calcula a mano en el comentario."""

    def test_commission_rate_case(self) -> None:
        """
        Setup:
          symbol=SPY, qty=100, open=200 USD, ADV=500_000
          notional = 100 × 200 = 20_000
          commission = max(1.25, 0.0005 × 20_000) = max(1.25, 10.0) = 10.0
        """
        order = _order(qty="100")
        bar = _bar("SPY", _EPOCH, open_="200", volume="500000")
        inst = _instrument()

        breakdown = calculate_costs(order, bar, inst, _STD_CONFIG, Decimal("500000"))

        assert breakdown.commission == Decimal("10.0"), (
            f"Expected commission=10.0, got {breakdown.commission}"
        )

    def test_commission_minimum_case(self) -> None:
        """
        Setup:
          qty=5, open=100, notional=500 (< 1000 EUR — usamos _STD_NO_MIN)
          commission = max(1.25, 0.0005 × 500) = max(1.25, 0.25) = 1.25
          (min_commission aplicado)
        """
        order = _order(qty="5")
        bar = _bar("SPY", _EPOCH, open_="100", volume="500000")
        inst = _instrument()

        breakdown = calculate_costs(order, bar, inst, _STD_NO_MIN, Decimal("500000"))

        assert breakdown.commission == Decimal("1.25"), (
            f"Expected commission=1.25 (minimum), got {breakdown.commission}"
        )

    def test_spread_cost_calculation(self) -> None:
        """
        Setup:
          qty=100, open=200, spread_bps=5 (category default)
          notional = 100 × 200 = 20_000
          spread_cost = 0.5 × 5/10_000 × 20_000 = 0.5 × 0.0005 × 20_000 = 5.0
        """
        order = _order(qty="100")
        bar = _bar("SPY", _EPOCH, open_="200", volume="500000")
        inst = _instrument(spread_bps=None)  # usa category default = 5 bps

        breakdown = calculate_costs(order, bar, inst, _STD_CONFIG, Decimal("500000"))

        assert breakdown.spread_cost == Decimal("5.0"), (
            f"Expected spread_cost=5.0, got {breakdown.spread_cost}"
        )

    def test_slippage_with_adv(self) -> None:
        """
        Setup:
          qty=100, open=200, ADV=500_000
          ratio = 100 / 500_000 = 0.0002
          slippage_frac = 0.1 × sqrt(0.0002) = 0.001414213562373095...
          notional = 100 × 200 = 20_000
          slippage = slippage_frac × 20_000 ≈ 28.284...

        El test replica exactamente la aritmética de calculate_costs para
        garantizar igualdad Decimal (sin approx ni tolerancia).
        """
        import math
        order = _order(qty="100")
        bar = _bar("SPY", _EPOCH, open_="200", volume="500000")
        inst = _instrument()
        adv = Decimal("500000")
        qty = Decimal("100")
        notional = qty * Decimal("200")

        breakdown = calculate_costs(order, bar, inst, _STD_NO_MIN, adv)

        # Replica exacta de la fórmula en costs.py:
        # ratio = float(qty / adv)
        # slippage_frac = Decimal(str(float(slippage_factor) * math.sqrt(ratio)))
        ratio = float(qty / adv)
        expected_frac = Decimal(str(float(Decimal("0.1")) * math.sqrt(ratio)))
        expected_slippage = expected_frac * notional

        assert breakdown.slippage == expected_slippage, (
            f"Slippage mismatch: expected {expected_slippage}, got {breakdown.slippage}"
        )
        assert breakdown.slippage > _ZERO, "Slippage debe ser positivo cuando ADV>0"

    def test_slippage_fallback_no_adv(self) -> None:
        """
        Setup:
          qty=100, open=200, ADV=0 (no disponible) → fallback 3 bps
          slippage = 3/10_000 × 20_000 = 6.0
        """
        order = _order(qty="100")
        bar = _bar("SPY", _EPOCH, open_="200", volume="500000")
        inst = _instrument()

        breakdown = calculate_costs(order, bar, inst, _STD_NO_MIN, _ZERO)

        assert breakdown.slippage == Decimal("6.0"), (
            f"Expected slippage fallback=6.0, got {breakdown.slippage}"
        )

    def test_total_equals_sum_of_components(self) -> None:
        """total debe ser siempre commission + spread_cost + slippage."""
        order = _order(qty="50")
        bar = _bar("SPY", _EPOCH, open_="150", volume="300000")
        inst = _instrument()

        breakdown = calculate_costs(order, bar, inst, _STD_CONFIG, Decimal("300000"))

        assert breakdown.total == breakdown.commission + breakdown.spread_cost + breakdown.slippage

    def test_zero_costs_config_yields_zeros(self) -> None:
        """CostsConfig.zero_costs() → todos los costes = 0 (min_order_eur=0, no lanza)."""
        order = _order(qty="100")
        bar = _bar("SPY", _EPOCH, open_="200", volume="500000")
        inst = _instrument()

        breakdown = calculate_costs(order, bar, inst, _ZERO_CONFIG, Decimal("500000"))

        assert breakdown.commission == _ZERO
        assert breakdown.spread_cost == _ZERO
        assert breakdown.slippage == _ZERO
        assert breakdown.total == _ZERO


# ---------------------------------------------------------------------------
# 2. Participation limit
# ---------------------------------------------------------------------------

class TestParticipationLimit:
    _PART_CONFIG = CostsConfig(
        commission_rate=Decimal("0.0005"),
        min_commission=Decimal("1.25"),
        spread_bps_by_category=dict.fromkeys(InstrumentCategory, 5),
        slippage_factor=Decimal("0.1"),
        slippage_bps_fallback=3,
        max_participation=Decimal("0.10"),
        min_order_eur=Decimal("0"),  # sin mínimo para estos tests
    )

    def test_qty_reduced_when_exceeds_participation(self) -> None:
        """
        ADV=1000, max_participation=0.10 → max_qty=100
        order qty=500 → adjusted_qty=100, participation_adjusted=True
        """
        order = _order(qty="500")
        bar = _bar("SPY", _EPOCH, open_="100", volume="1000")
        inst = _instrument()

        breakdown = calculate_costs(order, bar, inst, self._PART_CONFIG, Decimal("1000"))

        assert breakdown.participation_adjusted is True
        assert breakdown.adjusted_qty == Decimal("100"), (
            f"Expected adjusted_qty=100, got {breakdown.adjusted_qty}"
        )

    def test_qty_not_reduced_within_limit(self) -> None:
        """qty=50 ≤ 10% × 1000 = 100 → no ajuste."""
        order = _order(qty="50")
        bar = _bar("SPY", _EPOCH, open_="100", volume="1000")
        inst = _instrument()

        breakdown = calculate_costs(order, bar, inst, self._PART_CONFIG, Decimal("1000"))

        assert breakdown.participation_adjusted is False
        assert breakdown.adjusted_qty == Decimal("50")


# ---------------------------------------------------------------------------
# 3. Tamaño mínimo de orden
# ---------------------------------------------------------------------------

class TestMinOrderSize:
    def test_order_below_min_raises(self) -> None:
        """
        qty=1, open=10 → notional=10 < min_order_eur=1000 → OrderTooSmall
        """
        order = _order(qty="1")
        bar = _bar("SPY", _EPOCH, open_="10", volume="500000")
        inst = _instrument()

        with pytest.raises(OrderTooSmall):
            calculate_costs(order, bar, inst, _STD_CONFIG, Decimal("500000"))

    def test_order_at_exact_minimum_passes(self) -> None:
        """
        qty=10, open=100 → notional=1000 = min_order_eur → OK
        """
        order = _order(qty="10")
        bar = _bar("SPY", _EPOCH, open_="100", volume="500000")
        inst = _instrument()

        breakdown = calculate_costs(order, bar, inst, _STD_CONFIG, Decimal("500000"))

        assert breakdown.adjusted_qty == Decimal("10")


# ---------------------------------------------------------------------------
# 4. Spread por instrumento vs por categoría
# ---------------------------------------------------------------------------

class TestSpreadResolution:
    def test_instrument_spread_overrides_category(self) -> None:
        """
        Instrumento tiene spread_bps=2; categoría tiene 5.
        spread_cost = 0.5 × 2/10_000 × notional (no 5 bps).
        """
        order = _order(qty="100")
        bar = _bar("SPY", _EPOCH, open_="200", volume="500000")
        inst = _instrument(spread_bps=2)  # override

        breakdown = calculate_costs(order, bar, inst, _STD_CONFIG, Decimal("500000"))

        # notional=20000, half-spread = 0.5×2/10000×20000 = 2.0
        assert breakdown.spread_cost == Decimal("2.0"), (
            f"Expected spread_cost=2.0 (instrument override 2bps), got {breakdown.spread_cost}"
        )

    def test_category_spread_used_when_no_instrument_override(self) -> None:
        """category=ALTERNATIVE → 10 bps en config."""
        config = CostsConfig(
            commission_rate=Decimal("0.0005"),
            min_commission=Decimal("1.25"),
            spread_bps_by_category={
                InstrumentCategory.REGION: 5,
                InstrumentCategory.SECTOR: 5,
                InstrumentCategory.FACTOR: 5,
                InstrumentCategory.FIXED_INCOME: 8,
                InstrumentCategory.ALTERNATIVE: 10,
            },
            slippage_factor=Decimal("0"),  # slippage=0 para aislar spread
            slippage_bps_fallback=0,
            max_participation=Decimal("1"),
            min_order_eur=Decimal("0"),
        )
        order = _order(qty="100")
        bar = _bar("SPY", _EPOCH, open_="200", volume="500000")
        inst = _instrument(category=InstrumentCategory.ALTERNATIVE, spread_bps=None)

        breakdown = calculate_costs(order, bar, inst, config, Decimal("500000"))

        # notional=20000, half-spread = 0.5×10/10000×20000 = 10.0
        assert breakdown.spread_cost == Decimal("10.0"), (
            f"Expected spread_cost=10.0 (alternative 10bps), got {breakdown.spread_cost}"
        )

    def test_fixed_income_spread_8bps(self) -> None:
        """category=FIXED_INCOME → 8 bps."""
        config = CostsConfig(
            commission_rate=Decimal("0"),
            min_commission=Decimal("0"),
            spread_bps_by_category=dict.fromkeys(InstrumentCategory, 5)
            | {InstrumentCategory.FIXED_INCOME: 8},
            slippage_factor=Decimal("0"),
            slippage_bps_fallback=0,
            max_participation=Decimal("1"),
            min_order_eur=Decimal("0"),
        )
        order = _order(qty="100")
        bar = _bar("SPY", _EPOCH, open_="200", volume="500000")
        inst = _instrument(category=InstrumentCategory.FIXED_INCOME, spread_bps=None)

        breakdown = calculate_costs(order, bar, inst, config, Decimal("500000"))

        # notional=20000, half-spread = 0.5×8/10000×20000 = 8.0
        assert breakdown.spread_cost == Decimal("8.0")


# ---------------------------------------------------------------------------
# 5. adjusted_fill_price
# ---------------------------------------------------------------------------

class TestAdjustedFillPrice:
    def test_buy_pays_more(self) -> None:
        """BUY: precio neto > open."""
        open_p = Decimal("100")
        adj = Decimal("0.001")  # 0.1%
        price = adjusted_fill_price(open_p, Side.BUY, adj)
        assert price == Decimal("100.1")

    def test_sell_receives_less(self) -> None:
        """SELL: precio neto < open."""
        open_p = Decimal("100")
        adj = Decimal("0.001")
        price = adjusted_fill_price(open_p, Side.SELL, adj)
        assert price == Decimal("99.9")

    def test_zero_adjustment_returns_open(self) -> None:
        """Sin ajuste → precio = open."""
        open_p = Decimal("150")
        assert adjusted_fill_price(open_p, Side.BUY, Decimal("0")) == open_p
        assert adjusted_fill_price(open_p, Side.SELL, Decimal("0")) == open_p


# ---------------------------------------------------------------------------
# 6. CostsConfig.from_yaml
# ---------------------------------------------------------------------------

class TestCostsConfigFromYaml:
    def test_loads_real_config(self) -> None:
        """Carga config/costs.yaml real y verifica valores clave."""
        from pathlib import Path
        config_path = Path(__file__).resolve().parents[2] / "config" / "costs.yaml"
        config = CostsConfig.from_yaml(config_path)

        assert config.commission_rate == Decimal("0.0005")
        assert config.min_commission == Decimal("1.25")
        assert config.max_participation == Decimal("0.10")
        assert config.min_order_eur == Decimal("1000.0")
        assert InstrumentCategory.REGION in config.spread_bps_by_category
        assert config.spread_bps_by_category[InstrumentCategory.REGION] == 5
        assert config.spread_bps_by_category[InstrumentCategory.ALTERNATIVE] == 10


# ---------------------------------------------------------------------------
# 7. TEST CLAVE: Buy-and-hold 1 año no-costs vs with-costs
# ---------------------------------------------------------------------------

class TestBuyAndHoldCostComparison:
    """
    Escenario buy-and-hold 1 año con 1 instrumento:

    Setup:
      - 252 días de trading (aproximado 1 año bursátil).
      - 1 instrumento: SPY.
      - Precio constante: open=close=200 USD todo el año.
      - ADV constante: 500_000 acciones.
      - Estrategia: compra 10 acciones en T0, no opera más.
      - EqualWeightPortfolio: qty=10 acciones.
      - Capital inicial: 50_000 EUR.

    Cálculo manual de costes:
      1 ENTRADA en T1 (fill al open de T1=200):
        notional = 10 × 200 = 2_000
        commission = max(1.25, 0.0005 × 2_000) = max(1.25, 1.0) = 1.25  ← mínimo
        spread_cost = 0.5 × 5/10_000 × 2_000 = 0.5
        slippage_frac = 0.1 × sqrt(10/500_000) = 0.1 × 0.004472... = 0.000447...
        slippage = 0.000447... × 2_000 = 0.8944...
        total_costs_entry = 1.25 + 0.5 + 0.8944... = 2.6444...

      NO HAY SALIDA (buy-and-hold, la estrategia no vende).

      total_costs = 2.6444...

    Diferencia equity(no-costs) - equity(with-costs) debe coincidir con
    commission + spread_cost + slippage  de la única operación de entrada.

    Nota: con CostAwareSimBroker el precio de fill incluye spread+slippage
    (ajuste al precio), y la comisión sale del cash vía fill.commission.
    La diferencia total = cash_pagado_de_más_en_precio + comisión.
    """

    def _make_days(self, n: int) -> list[date]:
        return [_d(i) for i in range(n)]

    def _make_data(
        self, days: list[date], price: str = "200", volume: str = "500000"
    ) -> dict[tuple[str, date], ValidatedBar]:
        return {("SPY", d): _bar("SPY", d, open_=price, close=price, volume=volume)
                for d in days}

    def test_cost_difference_matches_manual_calculation(self) -> None:
        """
        La diferencia equity(no-costs) - equity(with-costs) debe coincidir
        exactamente con el coste de la operación de entrada.
        """
        import math

        n_days = 10  # suficiente para buy en T0, fill en T1, y hold hasta T9
        days = self._make_days(n_days)
        data = self._make_data(days)

        instruments = [_instrument("SPY")]
        initial_equity = Decimal("50000")

        # Config estándar (con costes)
        costs_cfg = _STD_CONFIG

        # --- Motor SIN costes ---
        class NoCostBuyOnceStrategy:
            """Compra 10 acciones solo en el primer día."""
            _done = False

            def on_bar(self, trading_day: date, universe: list[Instrument],
                       data: SandboxedDataView) -> list[Signal]:
                if not self._done:
                    self._done = True
                    return [Signal(
                        symbol="SPY",
                        timestamp=_dt(trading_day),
                        direction=Direction.LONG,
                        strength=Decimal("1"),
                        strategy_id="buy_once",
                    )]
                return []

        class FixedQtyPortfolio:
            _filled = False

            def build(self, signals: list[Signal], positions: dict[str, Position],
                      universe: list[Instrument], equity: Decimal) -> list[TargetPosition]:
                if signals and not self._filled:
                    self._filled = True
                    return [TargetPosition(
                        symbol="SPY",
                        quantity=Decimal("10"),
                        weight=Decimal("1"),
                    )]
                return []

        class _StubProvider:
            def get_bars(self, symbol: str, start: datetime, end: datetime,
                         as_of: datetime) -> list[ValidatedBar]:
                result = []
                d = start.date()
                while d <= end.date():
                    vb = data.get((symbol, d))
                    if vb is not None:
                        result.append(vb)
                    d += timedelta(days=1)
                return result

            def get_corporate_actions(self, symbol: str, start: datetime,
                                      end: datetime) -> list[object]:
                return []

        class _StubUniverseManager:
            def get_universe(self, as_of: date) -> list[Instrument]:
                return instruments

        provider = _StubProvider()
        universe_mgr = _StubUniverseManager()

        def _make_engine(broker: object) -> BacktestEngine:
            return BacktestEngine(
                provider=provider,  # type: ignore[arg-type]
                universe_mgr=universe_mgr,  # type: ignore[arg-type]
                strategy=NoCostBuyOnceStrategy(),
                portfolio=FixedQtyPortfolio(),
                risk=ApproveAllRisk(),
                broker=broker,  # type: ignore[arg-type]
                trading_days=days,
                initial_equity=initial_equity,
                slippage_bps=Decimal("0"),  # el motor no aplica slippage propio
            )

        no_cost_broker = InstantSimBroker()
        result_no_cost = _make_engine(no_cost_broker).run()

        cost_broker = CostAwareSimBroker(
            costs_config=costs_cfg,
            universe={"SPY": _instrument("SPY")},
            adv_map={"SPY": Decimal("500000")},
        )
        result_with_cost = _make_engine(cost_broker).run()

        # --- Cálculo manual ---
        # Entrada: qty=10, open=200, ADV=500_000, spread_bps=5
        qty = Decimal("10")
        open_p = Decimal("200")
        adv = Decimal("500000")
        notional = qty * open_p  # 2_000

        commission = max(Decimal("1.25"), Decimal("0.0005") * notional)
        # max(1.25, 1.0) = 1.25

        half_spread_frac = Decimal("0.5") * Decimal("5") / Decimal("10000")
        spread_cost = half_spread_frac * notional
        # 0.5 × 0.0005 × 2000 = 0.5

        slippage_frac = Decimal("0.1") * Decimal(str(math.sqrt(float(qty / adv))))
        slippage = slippage_frac * notional
        # 0.1 × sqrt(10/500000) × 2000

        price_adj = half_spread_frac + slippage_frac
        # Precio pagado con costes: 200 × (1 + price_adj)
        fill_price_with_costs = open_p * (1 + price_adj)
        # Diferencia en precio por las 10 acciones:
        price_diff = (fill_price_with_costs - open_p) * qty
        # Total diferencia de equity: precio extra + comisión
        expected_diff = price_diff + commission

        actual_diff = result_no_cost.final_equity - result_with_cost.final_equity

        # Comparación exacta (misma aritmética Decimal)
        assert actual_diff == expected_diff, (
            f"\nBuy-and-hold 1 instrumento:\n"
            f"  Equity sin costes:  {result_no_cost.final_equity}\n"
            f"  Equity con costes:  {result_with_cost.final_equity}\n"
            f"  Diferencia actual:  {actual_diff}\n"
            f"  Diferencia esperada:{expected_diff}\n"
            f"  commission={commission}, spread={spread_cost}, slippage={slippage}\n"
            f"  price_diff={price_diff}, commission={commission}"
        )

    def test_no_costs_equity_unchanged_without_fills(self) -> None:
        """Sin fills, equity permanece igual al initial."""
        days = self._make_days(5)

        class NoOpStrategy:
            def on_bar(self, trading_day: date, universe: list[Instrument],
                       data: SandboxedDataView) -> list[Signal]:
                return []

        class NoOpPortfolio:
            def build(self, signals: list[Signal], positions: dict[str, Position],
                      universe: list[Instrument], equity: Decimal) -> list[TargetPosition]:
                return []

        class _SP:
            def get_bars(self, sym: str, start: datetime, end: datetime,
                         as_of: datetime) -> list[ValidatedBar]:
                return []
            def get_corporate_actions(self, sym: str, s: datetime, e: datetime) -> list[object]:
                return []

        class _SU:
            def get_universe(self, as_of: date) -> list[Instrument]:
                return [_instrument()]

        initial = Decimal("10000")
        engine = BacktestEngine(
            provider=_SP(),  # type: ignore[arg-type]
            universe_mgr=_SU(),  # type: ignore[arg-type]
            strategy=NoOpStrategy(),
            portfolio=NoOpPortfolio(),
            risk=ApproveAllRisk(),
            broker=InstantSimBroker(),
            trading_days=days,
            initial_equity=initial,
            slippage_bps=Decimal("0"),
        )
        result = engine.run()
        assert result.final_equity == initial
        assert result.total_fills == 0

    def test_with_costs_equity_lower_than_without(self) -> None:
        """Equity con costes siempre ≤ equity sin costes si hay fills."""
        n_days = 5
        days = self._make_days(n_days)
        data = self._make_data(days)

        instruments = [_instrument("SPY")]

        class _SP:
            def get_bars(self, sym: str, start: datetime, end: datetime,
                         as_of: datetime) -> list[ValidatedBar]:
                d = start.date()
                result = []
                while d <= end.date():
                    vb = data.get((sym, d))
                    if vb:
                        result.append(vb)
                    d += timedelta(days=1)
                return result
            def get_corporate_actions(self, sym: str, s: datetime, e: datetime) -> list[object]:
                return []

        class _SU:
            def get_universe(self, as_of: date) -> list[Instrument]:
                return instruments

        initial = Decimal("50000")

        def _make(broker: object) -> BacktestEngine:
            return BacktestEngine(
                provider=_SP(),  # type: ignore[arg-type]
                universe_mgr=_SU(),  # type: ignore[arg-type]
                strategy=BuyAllStrategy(),
                portfolio=EqualWeightPortfolio(),
                risk=ApproveAllRisk(),
                broker=broker,  # type: ignore[arg-type]
                trading_days=days,
                initial_equity=initial,
                slippage_bps=Decimal("0"),
            )

        r_no = _make(InstantSimBroker()).run()
        r_yes = _make(CostAwareSimBroker(
            costs_config=_STD_CONFIG,
            universe={"SPY": _instrument("SPY")},
            adv_map={"SPY": Decimal("500000")},
        )).run()

        if r_no.total_fills > 0:
            assert r_yes.final_equity <= r_no.final_equity, (
                "Equity con costes debe ser ≤ equity sin costes"
            )
