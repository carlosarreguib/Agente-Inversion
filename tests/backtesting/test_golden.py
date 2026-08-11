"""Golden backtest test - T2.3.

Detecta regresiones silenciosas del motor de backtesting.
Un cambio de 0,01 EUR en final_equity hace fallar este test.

Escenario:
  - 5 ETFs sinteticos: SYNTH_A, SYNTH_B, SYNTH_C, SYNTH_D, SYNTH_E.
  - Semilla maestra: 42. Parametros por instrumento definidos en _SYMBOL_PARAMS.
  - Periodo: 252 dias habiles (lunes a viernes, sin festivos, desde 2022-01-03).
  - Estrategia: momentum SMA20 (close > SMA20 -> LONG, equal weight).
  - Broker: CostAwareSimBroker con config/costs.yaml.
  - Capital inicial: 15.000 EUR.

Para actualizar el JSON de referencia intencionadamente:
    uv run qtrader golden update
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from qtrader.backtesting.engine import (
    ApproveAllRisk,
    BacktestEngine,
    BacktestResult,
    CostAwareSimBroker,
)
from qtrader.backtesting.events import BacktestEvent, BarEvent
from qtrader.backtesting.golden_strategy import GoldenEqualWeightPortfolio, MomentumStrategy
from qtrader.backtesting.metrics import GoldenMetrics, compute_metrics
from qtrader.backtesting.synthetic import SyntheticMultiProvider
from qtrader.core.types import (
    Instrument,
    InstrumentCategory,
    InstrumentType,
)
from qtrader.costs import CostsConfig

# ---------------------------------------------------------------------------
# Parametros fijos del escenario golden (no tocar sin `golden update`)
# ---------------------------------------------------------------------------

_BASE_SEED = 42
_INITIAL_EQUITY = Decimal("15000")
_NUM_TRADING_DAYS = 252
_START_DATE_STR = "2022-01-03"

# Parametros por simbolo: drift diario, vol diaria, precio base
_SYMBOL_PARAMS: dict[str, dict[str, Any]] = {
    "SYNTH_A": {"drift": 0.0005, "vol": 0.010, "base_price": 150.0},
    "SYNTH_B": {"drift": 0.0002, "vol": 0.015, "base_price": 80.0},
    "SYNTH_C": {"drift": -0.0001, "vol": 0.013, "base_price": 200.0},
    "SYNTH_D": {"drift": 0.0008, "vol": 0.020, "base_price": 50.0},
    "SYNTH_E": {"drift": 0.0003, "vol": 0.009, "base_price": 120.0},
}

_GOLDEN_JSON = Path(__file__).parent / "golden_reference.json"

# ---------------------------------------------------------------------------
# Helpers para construir el escenario
# ---------------------------------------------------------------------------

_DECLARED_ON = date(2022, 1, 1)


def _make_instrument(symbol: str) -> Instrument:
    return Instrument(
        symbol=symbol,
        name="Synthetic ETF " + symbol,
        exchange="XNAS",
        currency="USD",
        category=InstrumentCategory.REGION,
        ticker_proxy=symbol,
        ticker_ucits=symbol,
        declared_on=_DECLARED_ON,
        instrument_type=InstrumentType.ETF,
        spread_bps=5,
    )


_INSTRUMENTS = [_make_instrument(sym) for sym in sorted(_SYMBOL_PARAMS)]


class _SyntheticUniverseManager:
    """UniverseManager stub que devuelve siempre los instrumentos sinteticos."""

    def get_universe(self, as_of: date) -> list[Instrument]:
        return [i for i in _INSTRUMENTS if i.declared_on <= as_of]

    def get_instrument(self, symbol: str, as_of: date) -> Instrument | None:
        for inst in _INSTRUMENTS:
            if inst.symbol == symbol and inst.declared_on <= as_of:
                return inst
        return None

    @property
    def all_instruments(self) -> list[Instrument]:
        return list(_INSTRUMENTS)


def _make_trading_days() -> list[date]:
    """Genera 252 dias habiles (lun-vie) desde _START_DATE_STR."""
    start = date.fromisoformat(_START_DATE_STR)
    days: list[date] = []
    current = start
    while len(days) < _NUM_TRADING_DAYS:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


class _PortfolioUpdateObserver:
    """Observer que alimenta al portfolio con el ultimo cierre tras cada BarEvent."""

    def __init__(self, portfolio: GoldenEqualWeightPortfolio) -> None:
        self._portfolio = portfolio

    def on_event(self, event: BacktestEvent) -> None:
        if isinstance(event, BarEvent):
            for vb in event.bars:
                self._portfolio.update_close(vb.bar.symbol, vb.bar.close)


def _run_golden_backtest() -> BacktestResult:
    """Ejecuta el backtest golden y devuelve el resultado.

    Funccion pura: misma semilla -> mismo resultado.
    """
    trading_days = _make_trading_days()
    start_dt = datetime(2022, 1, 3, tzinfo=UTC)

    provider = SyntheticMultiProvider(
        base_seed=_BASE_SEED,
        start_date=start_dt,
        symbol_params=_SYMBOL_PARAMS,
    )

    strategy = MomentumStrategy(sma_period=20)
    portfolio = GoldenEqualWeightPortfolio(
        initial_equity=_INITIAL_EQUITY,
        n_slots=len(_INSTRUMENTS),
        min_order_eur=Decimal("1000"),
    )
    portfolio_observer = _PortfolioUpdateObserver(portfolio)

    universe_dict = {inst.symbol: inst for inst in _INSTRUMENTS}

    config_dir = Path(__file__).resolve().parents[2] / "config"
    costs_config = CostsConfig.from_yaml(config_dir / "costs.yaml")

    broker = CostAwareSimBroker(
        costs_config=costs_config,
        universe=universe_dict,  # type: ignore[arg-type]
    )

    engine = BacktestEngine(
        provider=provider,
        universe_mgr=_SyntheticUniverseManager(),  # type: ignore[arg-type]
        strategy=strategy,
        portfolio=portfolio,
        risk=ApproveAllRisk(),
        broker=broker,
        trading_days=trading_days,
        initial_equity=_INITIAL_EQUITY,
        slippage_bps=Decimal("5"),
        observers=[portfolio_observer],
    )

    return engine.run()


def _load_reference() -> GoldenMetrics | None:
    if not _GOLDEN_JSON.exists():
        return None
    data: dict[str, str | int] = json.loads(_GOLDEN_JSON.read_text(encoding="utf-8"))
    return GoldenMetrics.from_dict(data)


def _save_reference(metrics: GoldenMetrics) -> None:
    _GOLDEN_JSON.write_text(
        json.dumps(metrics.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Fixture de modulo: ejecuta el backtest UNA sola vez para todos los tests
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def golden_result() -> BacktestResult:
    return _run_golden_backtest()


@pytest.fixture(scope="module")
def golden_metrics(golden_result: BacktestResult) -> GoldenMetrics:
    return compute_metrics(golden_result)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestGoldenBacktest:
    """Golden tests: detectan cualquier cambio en el comportamiento del motor."""

    def test_golden_metrics_match_reference(self, golden_metrics: GoldenMetrics) -> None:
        """KEY TEST: las metricas actuales deben coincidir exactamente con el JSON.

        Si este test falla, el motor ha cambiado de comportamiento.
        Revisar si es un bug (corregir el motor) o un cambio legitimo
        (ejecutar: uv run qtrader golden update).
        """
        reference = _load_reference()
        assert reference is not None, (
            "golden_reference.json no existe. "
            "Ejecutalo por primera vez con: uv run qtrader golden update"
        )

        fields = [
            "total_return",
            "sharpe_ratio",
            "max_drawdown",
            "num_trades",
            "total_commission",
            "total_slippage",
            "final_equity",
        ]

        mismatches: list[str] = []
        for field_name in fields:
            expected_val = getattr(reference, field_name)
            actual_val = getattr(golden_metrics, field_name)
            if expected_val != actual_val:
                mismatches.append(
                    f"GOLDEN CHANGED: {field_name} "
                    f"esperado={expected_val} obtenido={actual_val}"
                )

        if mismatches:
            lines = "\n  ".join(mismatches)
            pytest.fail(
                f"El motor ha cambiado de comportamiento:\n  {lines}\n\n"
                "Si el cambio es intencionado, ejecuta:\n"
                "  uv run qtrader golden update"
            )

    def test_golden_result_is_deterministic(self, golden_metrics: GoldenMetrics) -> None:
        """Dos ejecuciones del mismo backtest deben producir exactamente el mismo resultado."""
        result2 = _run_golden_backtest()
        metrics2 = compute_metrics(result2)

        assert golden_metrics == metrics2, (
            "El backtest no es determinista: dos ejecuciones con los mismos "
            "parametros producen resultados distintos."
        )

    def test_golden_trading_days_count(self, golden_result: BacktestResult) -> None:
        """El backtest debe iterar exactamente _NUM_TRADING_DAYS dias."""
        assert golden_result.trading_days == _NUM_TRADING_DAYS, (
            f"Se esperaban {_NUM_TRADING_DAYS} dias de trading, "
            f"se obtuvieron {golden_result.trading_days}"
        )

    def test_golden_equity_curve_has_correct_length(
        self, golden_result: BacktestResult
    ) -> None:
        """La equity curve debe tener exactamente _NUM_TRADING_DAYS entradas."""
        assert len(golden_result.equity_curve) == _NUM_TRADING_DAYS

    def test_golden_fills_have_positive_prices(
        self, golden_result: BacktestResult
    ) -> None:
        """Todo fill debe tener precio > 0 y comision >= 0."""
        for fill in golden_result.fills:
            assert fill.price > Decimal("0"), f"Fill con precio 0: {fill}"
            assert fill.commission >= Decimal("0"), f"Fill con comision negativa: {fill}"

    def test_golden_no_fills_before_sma_warmup(
        self, golden_result: BacktestResult
    ) -> None:
        """No debe haber fills antes del dia 20 de trading (warmup SMA20).

        La primera señal se puede generar el dia 20 (primer dia con SMA completa).
        El timestamp del fill lleva la fecha de la orden (dia T de señal), no T+1.
        """
        trading_days = _make_trading_days()
        first_signal_day = trading_days[19]  # dia 20 (indice 19)

        premature_fills = [
            f for f in golden_result.fills
            if f.timestamp.date() < first_signal_day
        ]
        assert len(premature_fills) == 0, (
            f"Se encontraron {len(premature_fills)} fills antes del primer dia de señal "
            f"posible ({first_signal_day}, dia 20 de trading)"
        )

    def test_golden_reference_json_is_valid(self) -> None:
        """El JSON de referencia debe parsear correctamente a GoldenMetrics."""
        assert _GOLDEN_JSON.exists(), "golden_reference.json no existe"
        data: dict[str, str | int] = json.loads(
            _GOLDEN_JSON.read_text(encoding="utf-8")
        )
        metrics = GoldenMetrics.from_dict(data)
        assert metrics.num_trades >= 0
        assert metrics.final_equity != Decimal("0")
