"""Tests de portfolio construction (T4.1).

Incluye:
  - Tests unitarios para cada restriccion individualmente.
  - Tests de renormalizacion exacta (|sum-1| < 1e-10).
  - Tests de la banda de no-rebalanceo.
  - Tests de fallback de volatilidad.
  - Property tests con hypothesis para invariantes de restricciones.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from qtrader.core.types import (
    Direction,
    Instrument,
    InstrumentCategory,
    InstrumentType,
    Signal,
)
from qtrader.portfolio.construction import (
    PortfolioConfig,
    PortfolioConstructor,
    PortfolioTarget,
    _ZERO,
    _compute_vol,
    _normalize,
)

# ---------------------------------------------------------------------------
# Helpers de fixtures
# ---------------------------------------------------------------------------

_TS = datetime(2023, 1, 2, tzinfo=UTC)
_DECLARED = datetime(2022, 1, 1).date()


def _inst(
    symbol: str,
    category: InstrumentCategory = InstrumentCategory.REGION,
) -> Instrument:
    return Instrument(
        symbol=symbol,
        name=symbol,
        exchange="XNYS",
        currency="USD",
        category=category,
        ticker_proxy=symbol,
        ticker_ucits=symbol,
        declared_on=_DECLARED,
        instrument_type=InstrumentType.ETF,
        spread_bps=5,
    )


def _signal(symbol: str) -> Signal:
    return Signal(
        symbol=symbol,
        timestamp=_TS,
        direction=Direction.LONG,
        strength=Decimal("1"),
        strategy_id="test",
    )


def _closes(n: int = 100, base: float = 100.0) -> list[Decimal]:
    """Serie de cierres plana (vol=0, usa fallback en esa condicion)."""
    return [Decimal(str(base))] * n


def _volatile_closes(n: int = 100, base: float = 100.0, pct: float = 0.01) -> list[Decimal]:
    """Serie con variacion alternada +pct/-pct para dar vol > 0."""
    result = []
    price = base
    for i in range(n):
        result.append(Decimal(str(round(price, 6))))
        price = price * (1 + pct) if i % 2 == 0 else price * (1 - pct)
    return result


def _make_constructor(
    symbols: list[str],
    categories: dict[str, InstrumentCategory] | None = None,
    config: PortfolioConfig | None = None,
) -> PortfolioConstructor:
    instruments: dict[str, Instrument] = {}
    for sym in symbols:
        cat = (categories or {}).get(sym, InstrumentCategory.REGION)
        instruments[sym] = _inst(sym, cat)
    return PortfolioConstructor(instruments=instruments, config=config)


def _build(
    symbols: list[str],
    nav: Decimal = Decimal("100000"),
    categories: dict[str, InstrumentCategory] | None = None,
    config: PortfolioConfig | None = None,
    current_weights: dict[str, Decimal] | None = None,
    closes_override: dict[str, list[Decimal]] | None = None,
) -> PortfolioTarget:
    constructor = _make_constructor(symbols, categories, config)
    signals = [_signal(s) for s in symbols]
    closes: dict[str, list[Decimal]] = {
        s: _volatile_closes(100) for s in symbols
    }
    if closes_override:
        closes.update(closes_override)
    return constructor.build(
        signals=signals,
        closes_history=closes,
        current_weights=current_weights or {},
        nav=nav,
        timestamp=_TS,
    )


# ---------------------------------------------------------------------------
# 1. Normalizacion
# ---------------------------------------------------------------------------

class TestNormalize:
    def test_sums_to_one(self) -> None:
        w = {"A": Decimal("0.3"), "B": Decimal("0.5"), "C": Decimal("0.2")}
        n = _normalize(w)
        assert abs(sum(n.values()) - Decimal("1")) < Decimal("1e-10")

    def test_already_normalized(self) -> None:
        w = {"A": Decimal("0.5"), "B": Decimal("0.5")}
        n = _normalize(w)
        assert n["A"] == Decimal("0.5")
        assert n["B"] == Decimal("0.5")

    def test_zero_total_unchanged(self) -> None:
        w: dict[str, Decimal] = {}
        n = _normalize(w)
        assert n == {}


# ---------------------------------------------------------------------------
# 2. Volatilidad
# ---------------------------------------------------------------------------

class TestComputeVol:
    def test_not_enough_bars_uses_fallback(self) -> None:
        closes = _closes(30)
        vol = _compute_vol(closes, lookback=63, fallback=Decimal("0.15"))
        assert vol == Decimal("0.15")

    def test_flat_series_uses_fallback(self) -> None:
        closes = _closes(100)
        vol = _compute_vol(closes, lookback=63, fallback=Decimal("0.15"))
        # std=0 -> fallback
        assert vol == Decimal("0.15")

    def test_volatile_series_positive(self) -> None:
        closes = _volatile_closes(100)
        vol = _compute_vol(closes, lookback=63, fallback=Decimal("0.15"))
        assert vol > Decimal("0")

    def test_annualized(self) -> None:
        closes = _volatile_closes(100, pct=0.01)
        vol = _compute_vol(closes, lookback=63, fallback=Decimal("0.15"))
        daily_vol_approx = Decimal("0.01")
        expected_range = (daily_vol_approx * Decimal("10"), daily_vol_approx * Decimal("25"))
        assert expected_range[0] <= vol <= expected_range[1]


# ---------------------------------------------------------------------------
# 3. Restriccion: max_position_weight
# ---------------------------------------------------------------------------

class TestMaxPositionWeight:
    def test_no_position_exceeds_cap(self) -> None:
        cfg = PortfolioConfig(max_position_weight=Decimal("0.25"))
        result = _build(
            symbols=["A", "B", "C", "D", "E"],
            config=cfg,
        )
        if result.rebalance_needed:
            for t in result.targets:
                assert t.weight <= Decimal("0.25") + Decimal("1e-9"), (
                    f"{t.symbol}: weight {t.weight} > 0.25"
                )

    def test_cap_applied_with_many_symbols(self) -> None:
        # Con 6 instrumentos donde uno tiene vol muy baja → weight sin cap sería > 0.25
        cfg = PortfolioConfig(
            max_position_weight=Decimal("0.25"),
            max_sector_exposure=Decimal("1.0"),
            max_region_exposure=Decimal("1.0"),
            min_position_size_eur=Decimal("0"),
            max_positions=10,
        )
        syms = [f"X{i}" for i in range(6)]
        closes_override = {
            "X0": _volatile_closes(100, pct=0.0001),  # vol muy baja → alto inv-vol
        }
        for s in syms[1:]:
            closes_override[s] = _volatile_closes(100, pct=0.02)
        result = _build(symbols=syms, config=cfg, closes_override=closes_override)
        if result.rebalance_needed:
            for t in result.targets:
                assert t.weight <= Decimal("0.25") + Decimal("1e-9"), (
                    f"{t.symbol}: weight {t.weight} > 0.25"
                )


# ---------------------------------------------------------------------------
# 4. Restriccion: max_sector_exposure
# ---------------------------------------------------------------------------

class TestMaxSectorExposure:
    def test_sector_exposure_capped(self) -> None:
        # 6 sectores + 4 regiones; sectores tendrian >40% sin cap
        syms = [f"S{i}" for i in range(6)] + [f"R{i}" for i in range(4)]
        cats = {s: InstrumentCategory.SECTOR for s in syms if s.startswith("S")}
        cats.update({s: InstrumentCategory.REGION for s in syms if s.startswith("R")})

        cfg = PortfolioConfig(
            max_position_weight=Decimal("1.0"),  # sin cap por posicion
            max_sector_exposure=Decimal("0.40"),
            max_region_exposure=Decimal("1.0"),
            min_position_size_eur=Decimal("0"),
            max_positions=20,
        )
        result = _build(symbols=syms, categories=cats, config=cfg)
        if result.rebalance_needed:
            sector_total = sum(
                t.weight for t in result.targets
                if cats.get(t.symbol) == InstrumentCategory.SECTOR
            )
            assert sector_total <= Decimal("0.40") + Decimal("1e-9"), (
                f"sector exposure {sector_total} > 0.40"
            )

    def test_single_sector_not_capped_below_limit(self) -> None:
        # Un solo sector: peso < 40% → no se toca
        syms = ["S1", "R1", "R1b", "R1c"]
        cats = {"S1": InstrumentCategory.SECTOR}

        cfg = PortfolioConfig(
            max_position_weight=Decimal("1.0"),
            max_sector_exposure=Decimal("0.40"),
            max_region_exposure=Decimal("1.0"),
            min_position_size_eur=Decimal("0"),
            max_positions=20,
        )
        result = _build(symbols=syms, categories=cats, config=cfg, nav=Decimal("1000000"))
        # Con 4 instrumentos de igual vol: S1 tiene peso ~0.25 < 0.40 → no se toca
        if result.rebalance_needed:
            sector_total = sum(
                t.weight for t in result.targets if cats.get(t.symbol) == InstrumentCategory.SECTOR
            )
            assert sector_total <= Decimal("0.40") + Decimal("1e-9")


# ---------------------------------------------------------------------------
# 5. Restriccion: max_region_exposure
# ---------------------------------------------------------------------------

class TestMaxRegionExposure:
    def test_region_exposure_capped_with_mixed_categories(self) -> None:
        # 5 regiones + 3 alternativas; regiones sin cap tendrian ~5/8 > 60%
        syms = [f"REG{i}" for i in range(5)] + [f"ALT{i}" for i in range(3)]
        cats = {s: InstrumentCategory.REGION for s in syms if s.startswith("REG")}
        cats.update({s: InstrumentCategory.ALTERNATIVE for s in syms if s.startswith("ALT")})

        cfg = PortfolioConfig(
            max_position_weight=Decimal("1.0"),
            max_sector_exposure=Decimal("1.0"),
            max_region_exposure=Decimal("0.60"),
            min_position_size_eur=Decimal("0"),
            max_positions=20,
        )
        result = _build(symbols=syms, categories=cats, config=cfg)
        if result.rebalance_needed:
            region_total = sum(
                t.weight for t in result.targets
                if cats.get(t.symbol) == InstrumentCategory.REGION
            )
            assert region_total <= Decimal("0.60") + Decimal("1e-9"), (
                f"region total {region_total} > 0.60"
            )

    def test_all_regions_best_effort(self) -> None:
        # Cuando todos los instrumentos son del mismo grupo y el cap no puede
        # satisfacerse (no hay otros grupos), el constructor hace "best effort":
        # elimina hasta que queda 1 instrumento (mínimo posible).
        # El property test documenta este comportamiento esperado.
        syms = [f"REG{i}" for i in range(5)]
        cats = {s: InstrumentCategory.REGION for s in syms}

        cfg = PortfolioConfig(
            max_position_weight=Decimal("1.0"),
            max_sector_exposure=Decimal("1.0"),
            max_region_exposure=Decimal("0.60"),
            min_position_size_eur=Decimal("0"),
            max_positions=20,
        )
        result = _build(symbols=syms, categories=cats, config=cfg)
        # Debe terminar: no bucle infinito
        assert isinstance(result, PortfolioTarget)
        # Los pesos deben sumar 1 si hay targets
        if result.rebalance_needed and result.targets:
            total = sum(t.weight for t in result.targets)
            assert abs(total - Decimal("1")) < Decimal("1e-10")


# ---------------------------------------------------------------------------
# 6. Restriccion: min_position_size
# ---------------------------------------------------------------------------

class TestMinPositionSize:
    def test_small_nav_removes_positions(self) -> None:
        # Con nav=5000 y min_size=1000, 10 posiciones → algunas se eliminan
        cfg = PortfolioConfig(
            max_position_weight=Decimal("1.0"),
            max_sector_exposure=Decimal("1.0"),
            max_region_exposure=Decimal("1.0"),
            min_position_size_eur=Decimal("1000"),
            max_positions=20,
        )
        result = _build(
            symbols=[f"S{i}" for i in range(10)],
            nav=Decimal("5000"),
            config=cfg,
        )
        if result.rebalance_needed and result.targets:
            for t in result.targets:
                notional = t.weight * Decimal("5000")
                assert notional >= Decimal("1000") - Decimal("0.01"), (
                    f"{t.symbol}: notional {notional} < 1000"
                )

    def test_large_nav_keeps_all_positions(self) -> None:
        cfg = PortfolioConfig(
            min_position_size_eur=Decimal("1000"),
            max_positions=20,
        )
        result = _build(
            symbols=[f"S{i}" for i in range(5)],
            nav=Decimal("100000"),
            config=cfg,
        )
        if result.rebalance_needed:
            assert len(result.targets) == 5


# ---------------------------------------------------------------------------
# 7. Restriccion: max_positions
# ---------------------------------------------------------------------------

class TestMaxPositions:
    def test_max_positions_respected(self) -> None:
        cfg = PortfolioConfig(
            max_position_weight=Decimal("1.0"),
            max_sector_exposure=Decimal("1.0"),
            max_region_exposure=Decimal("1.0"),
            min_position_size_eur=Decimal("0"),
            max_positions=3,
        )
        result = _build(symbols=[f"S{i}" for i in range(10)], config=cfg)
        if result.rebalance_needed:
            assert len(result.targets) <= 3


# ---------------------------------------------------------------------------
# 8. Suma de pesos = 1 exactamente
# ---------------------------------------------------------------------------

class TestWeightSum:
    def test_weights_sum_to_one(self) -> None:
        result = _build(symbols=["A", "B", "C", "D"])
        if result.rebalance_needed and result.targets:
            total = sum(t.weight for t in result.targets)
            assert abs(total - Decimal("1")) < Decimal("1e-10"), (
                f"sum(weights) = {total}, expected 1"
            )

    def test_weights_sum_after_all_constraints(self) -> None:
        cfg = PortfolioConfig(
            max_position_weight=Decimal("0.20"),
            max_sector_exposure=Decimal("0.30"),
            max_region_exposure=Decimal("0.50"),
            min_position_size_eur=Decimal("1000"),
            max_positions=5,
        )
        syms = [f"SECT{i}" for i in range(4)] + [f"REG{i}" for i in range(4)]
        cats = {s: InstrumentCategory.SECTOR for s in syms if s.startswith("SECT")}
        cats.update({s: InstrumentCategory.REGION for s in syms if s.startswith("REG")})
        result = _build(
            symbols=syms,
            categories=cats,
            config=cfg,
            nav=Decimal("50000"),
        )
        if result.rebalance_needed and result.targets:
            total = sum(t.weight for t in result.targets)
            assert abs(total - Decimal("1")) < Decimal("1e-10")


# ---------------------------------------------------------------------------
# 9. Banda de no-rebalanceo
# ---------------------------------------------------------------------------

class TestRebalanceBand:
    def test_no_rebalance_when_within_threshold(self) -> None:
        # Pesos actuales muy cercanos al objetivo → no rebalancear
        symbols = ["A", "B", "C", "D"]
        cfg = PortfolioConfig(
            rebalance_threshold=Decimal("0.05"),
            min_position_size_eur=Decimal("0"),
        )
        # Con 4 instrumentos de vol igual, pesos objetivo ≈ 0.25 cada uno
        current = {"A": Decimal("0.24"), "B": Decimal("0.26"),
                   "C": Decimal("0.25"), "D": Decimal("0.25")}
        result = _build(
            symbols=symbols,
            config=cfg,
            current_weights=current,
        )
        assert result.rebalance_needed is False

    def test_rebalance_when_outside_threshold(self) -> None:
        symbols = ["A", "B"]
        cfg = PortfolioConfig(
            rebalance_threshold=Decimal("0.05"),
            min_position_size_eur=Decimal("0"),
            max_positions=10,
            max_position_weight=Decimal("1.0"),
            max_sector_exposure=Decimal("1.0"),
            max_region_exposure=Decimal("1.0"),
        )
        # Pesos actuales muy distintos del objetivo
        current = {"A": Decimal("0.10"), "B": Decimal("0.90")}
        result = _build(symbols=symbols, config=cfg, current_weights=current)
        assert result.rebalance_needed is True

    def test_new_symbol_triggers_rebalance(self) -> None:
        # Si el objetivo incluye un simbolo no en el portfolio actual → rebalancear.
        # Usamos ALTERNATIVE para evitar las restricciones de sector/region.
        cfg = PortfolioConfig(
            rebalance_threshold=Decimal("0.01"),
            min_position_size_eur=Decimal("0"),
            max_position_weight=Decimal("1.0"),  # sin cap de posicion
            max_sector_exposure=Decimal("1.0"),
            max_region_exposure=Decimal("1.0"),
        )
        cats = {"A": InstrumentCategory.ALTERNATIVE, "B": InstrumentCategory.ALTERNATIVE}
        current = {"A": Decimal("1.0")}
        result = _build(
            symbols=["A", "B"],
            categories=cats,
            config=cfg,
            current_weights=current,
        )
        assert result.rebalance_needed is True


# ---------------------------------------------------------------------------
# 10. Sin senales → cartera vacia
# ---------------------------------------------------------------------------

class TestEmptySignals:
    def test_no_signals_returns_empty(self) -> None:
        constructor = _make_constructor(["A", "B"])
        result = constructor.build(
            signals=[],
            closes_history={},
            current_weights={},
            nav=Decimal("100000"),
            timestamp=_TS,
        )
        assert result.targets == ()
        assert result.total_weight == _ZERO
        assert result.rebalance_needed is False

    def test_flat_signals_only_returns_empty(self) -> None:
        from qtrader.core.types import Direction
        constructor = _make_constructor(["A", "B"])
        signals = [
            Signal(symbol="A", timestamp=_TS, direction=Direction.FLAT,
                   strength=Decimal("0"), strategy_id="test"),
        ]
        result = constructor.build(
            signals=signals,
            closes_history={},
            current_weights={},
            nav=Decimal("100000"),
            timestamp=_TS,
        )
        assert result.targets == ()


# ---------------------------------------------------------------------------
# 11. PortfolioTarget es Pydantic frozen
# ---------------------------------------------------------------------------

class TestPortfolioTargetFrozen:
    def test_frozen(self) -> None:
        result = _build(symbols=["A"])
        from pydantic import ValidationError
        with pytest.raises((ValidationError, TypeError, AttributeError)):
            result.rebalance_needed = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 12. Property tests con hypothesis
# ---------------------------------------------------------------------------

_CATEGORIES = [
    InstrumentCategory.REGION,
    InstrumentCategory.SECTOR,
    InstrumentCategory.FACTOR,
    InstrumentCategory.FIXED_INCOME,
    InstrumentCategory.ALTERNATIVE,
]

# Estrategia hypothesis: lista de simbolos (2..15) con categorias aleatorias
@st.composite
def _universe_strategy(draw: Any) -> tuple[list[str], dict[str, InstrumentCategory]]:
    n = draw(st.integers(min_value=2, max_value=15))
    symbols = [f"INST{i}" for i in range(n)]
    cats = {s: draw(st.sampled_from(_CATEGORIES)) for s in symbols}
    return symbols, cats


@st.composite
def _nav_strategy(draw: Any) -> Decimal:
    # NAV entre 1000 y 1000000 EUR
    raw = draw(st.integers(min_value=1000, max_value=1_000_000))
    return Decimal(str(raw))


@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.too_slow],
    deadline=5000,
)
@given(universe=_universe_strategy(), nav=_nav_strategy())
def test_property_all_constraints_respected(
    universe: tuple[list[str], dict[str, InstrumentCategory]],
    nav: Decimal,
) -> None:
    """Para cualquier universo y NAV aleatorio, ninguna restriccion se viola."""
    symbols, cats = universe

    cfg = PortfolioConfig(
        max_position_weight=Decimal("0.25"),
        max_sector_exposure=Decimal("0.40"),
        max_region_exposure=Decimal("0.60"),
        min_position_size_eur=Decimal("1000"),
        max_positions=10,
        rebalance_threshold=Decimal("0.05"),
    )
    result = _build(
        symbols=symbols,
        categories=cats,
        config=cfg,
        nav=nav,
    )

    if not result.rebalance_needed or not result.targets:
        return

    targets = result.targets

    # 1. sum(weights) ∈ [0.999999, 1.000001]
    total = sum(t.weight for t in targets)
    assert Decimal("0.999999") <= total <= Decimal("1.000001"), (
        f"sum(weights)={total} fuera de [0.999999, 1.000001]"
    )

    # 2. Ningún peso > max_position_weight
    for t in targets:
        assert t.weight <= cfg.max_position_weight + Decimal("1e-9"), (
            f"{t.symbol}: weight={t.weight} > {cfg.max_position_weight}"
        )

    # 3. Ningún sector > max_sector_exposure
    # (solo aplica cuando hay instrumentos fuera del grupo sector)
    all_cats = [cats.get(t.symbol) for t in targets]
    has_non_sector = any(c != InstrumentCategory.SECTOR for c in all_cats)
    sector_total = sum(
        t.weight for t in targets
        if cats.get(t.symbol) == InstrumentCategory.SECTOR
    )
    if has_non_sector:
        assert sector_total <= cfg.max_sector_exposure + Decimal("1e-9"), (
            f"sector total {sector_total} > {cfg.max_sector_exposure}"
        )

    # 4. Ninguna región > max_region_exposure
    # (solo aplica cuando hay instrumentos fuera del grupo region)
    has_non_region = any(c != InstrumentCategory.REGION for c in all_cats)
    region_total = sum(
        t.weight for t in targets
        if cats.get(t.symbol) == InstrumentCategory.REGION
    )
    if has_non_region:
        assert region_total <= cfg.max_region_exposure + Decimal("1e-9"), (
            f"region total {region_total} > {cfg.max_region_exposure}"
        )

    # 5. Ninguna position_eur < min_position_size
    for t in targets:
        notional = t.weight * nav
        assert notional >= cfg.min_position_size_eur - Decimal("0.01"), (
            f"{t.symbol}: notional={notional} < {cfg.min_position_size_eur}"
        )

    # 6. len(targets) ≤ max_positions
    assert len(targets) <= cfg.max_positions, (
        f"len(targets)={len(targets)} > {cfg.max_positions}"
    )


@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.too_slow],
    deadline=5000,
)
@given(universe=_universe_strategy(), nav=_nav_strategy())
def test_property_weights_always_nonnegative(
    universe: tuple[list[str], dict[str, InstrumentCategory]],
    nav: Decimal,
) -> None:
    """Ningun peso es negativo."""
    symbols, cats = universe
    result = _build(symbols=symbols, categories=cats, nav=nav)
    for t in result.targets:
        assert t.weight >= Decimal("0"), f"{t.symbol}: weight={t.weight} < 0"
