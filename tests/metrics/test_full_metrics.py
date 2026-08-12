"""Tests del framework de metricas completo (T3.2).

Verificacion manual con tolerancia < 1e-10 donde aplica.
Tests de bootstrap: CI contiene el estimador puntual para series largas.
"""
from __future__ import annotations

import math
from datetime import UTC, date
from decimal import Decimal

import pytest

from qtrader.backtesting.full_metrics import (
    BacktestReport,
    BootstrapCI,
    _block_bootstrap_sharpe,
    _daily_returns,
    _mean,
    _percentile,
    _std,
    compute_bootstrap_ci,
    compute_cagr,
    compute_calmar,
    compute_full_report,
    compute_max_drawdown,
    compute_max_drawdown_duration,
    compute_sharpe,
    compute_sortino,
    compute_total_return,
    compute_turnover,
    compute_volatility,
)
from qtrader.core.types import Fill, Side

# ---------------------------------------------------------------------------
# Fixtures helpers
# ---------------------------------------------------------------------------

def _dt(d: date):
    from datetime import datetime
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


def _make_fill(
    symbol: str,
    side: Side,
    qty: str,
    price: str,
    commission: str = "1.00",
    d: date | None = None,
) -> Fill:
    if d is None:
        d = date(2020, 1, 2)
    return Fill(
        client_order_id=f"cid-{symbol}-{side}",
        fill_id=f"fid-{symbol}-{side}",
        symbol=symbol,
        side=side,
        quantity=Decimal(qty),
        price=Decimal(price),
        commission=Decimal(commission),
        timestamp=_dt(d),
    )


def _linear_curve(
    n: int = 252,
    start_equity: Decimal = Decimal("10000"),
    end_equity: Decimal = Decimal("11000"),
) -> list[tuple[date, Decimal]]:
    """Equity curve con crecimiento lineal."""
    from datetime import timedelta
    curve = []
    d = date(2020, 1, 2)
    step = (end_equity - start_equity) / Decimal(n - 1) if n > 1 else Decimal("0")
    for i in range(n):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        curve.append((d, start_equity + step * Decimal(i)))
        d += timedelta(days=1)
    return curve


def _flat_curve(n: int = 252, equity: Decimal = Decimal("10000")) -> list[tuple[date, Decimal]]:
    return _linear_curve(n, equity, equity)


# ---------------------------------------------------------------------------
# 1. Funciones basicas
# ---------------------------------------------------------------------------

class TestMean:
    def test_empty(self) -> None:
        assert _mean([]) == Decimal("0")

    def test_single(self) -> None:
        assert _mean([Decimal("5")]) == Decimal("5")

    def test_symmetric(self) -> None:
        values = [Decimal("1"), Decimal("3")]
        assert _mean(values) == Decimal("2")


class TestStd:
    def test_empty(self) -> None:
        assert _std([]) == Decimal("0")

    def test_one_element(self) -> None:
        assert _std([Decimal("42")]) == Decimal("0")

    def test_two_equal(self) -> None:
        values = [Decimal("5"), Decimal("5")]
        assert _std(values) == Decimal("0")

    def test_manual_two_values(self) -> None:
        # std([0, 2], ddof=1) = sqrt(2)
        values = [Decimal("0"), Decimal("2")]
        result = _std(values, ddof=1)
        expected = Decimal(str(math.sqrt(2.0)))
        # tolerancia < 1e-10
        assert abs(result - expected) < Decimal("1e-10")


class TestDailyReturns:
    def test_empty(self) -> None:
        assert _daily_returns([]) == []

    def test_single(self) -> None:
        assert _daily_returns([(date(2020, 1, 2), Decimal("100"))]) == []

    def test_flat(self) -> None:
        curve = [(date(2020, 1, i), Decimal("100")) for i in range(2, 6)]
        returns = _daily_returns(curve)
        assert len(returns) == 3
        assert all(r == Decimal("0") for r in returns)

    def test_manual_two_bars(self) -> None:
        curve = [
            (date(2020, 1, 2), Decimal("100")),
            (date(2020, 1, 3), Decimal("110")),
        ]
        returns = _daily_returns(curve)
        assert len(returns) == 1
        # r = (110 - 100) / 100 = 0.1 exacto
        expected = Decimal("110") / Decimal("100") - Decimal("1")
        assert returns[0] == expected  # assertEqual exacto


class TestPercentile:
    def test_empty(self) -> None:
        assert _percentile([], 50) == Decimal("0")

    def test_p50_odd(self) -> None:
        vals = [Decimal(str(i)) for i in [1, 3, 5]]
        assert _percentile(vals, 50) == Decimal("3")

    def test_p0(self) -> None:
        vals = [Decimal(str(i)) for i in [10, 20, 30]]
        assert _percentile(vals, 0) == Decimal("10")

    def test_p100(self) -> None:
        vals = [Decimal(str(i)) for i in [10, 20, 30]]
        assert _percentile(vals, 100) == Decimal("30")


# ---------------------------------------------------------------------------
# 2. Metricas de retorno
# ---------------------------------------------------------------------------

class TestTotalReturn:
    def test_zero_initial(self) -> None:
        assert compute_total_return(Decimal("0"), Decimal("100")) == Decimal("0")

    def test_no_change(self) -> None:
        assert compute_total_return(Decimal("1000"), Decimal("1000")) == Decimal("0")

    def test_manual_positive(self) -> None:
        # (1100 - 1000) / 1000 = 0.1 exacto
        result = compute_total_return(Decimal("1000"), Decimal("1100"))
        expected = Decimal("1100") / Decimal("1000") - Decimal("1")
        assert result == expected  # assertEqual exacto

    def test_manual_negative(self) -> None:
        result = compute_total_return(Decimal("1000"), Decimal("900"))
        expected = Decimal("900") / Decimal("1000") - Decimal("1")
        assert result == expected  # assertEqual exacto


class TestCagr:
    def test_zero_initial(self) -> None:
        assert compute_cagr(Decimal("0"), Decimal("100"), 252) == Decimal("0")

    def test_zero_days(self) -> None:
        assert compute_cagr(Decimal("1000"), Decimal("1100"), 0) == Decimal("0")

    def test_manual_one_year(self) -> None:
        # En 252 dias: initial=1000, final=1100
        # CAGR = (1100/1000)^(252/252) - 1 = 0.1 exacto
        result = compute_cagr(Decimal("1000"), Decimal("1100"), 252)
        expected = Decimal(str(1.1 ** 1.0 - 1.0))
        assert abs(result - expected) < Decimal("1e-10")

    def test_manual_two_years(self) -> None:
        # En 504 dias: CAGR = (1210/1000)^(252/504) - 1 = sqrt(1.21) - 1 = 0.1
        result = compute_cagr(Decimal("1000"), Decimal("1210"), 504)
        expected = Decimal("0.1")
        assert abs(result - expected) < Decimal("1e-10")


# ---------------------------------------------------------------------------
# 3. Metricas de riesgo
# ---------------------------------------------------------------------------

class TestVolatility:
    def test_empty(self) -> None:
        assert compute_volatility([]) == Decimal("0")

    def test_one_return(self) -> None:
        assert compute_volatility([Decimal("0.01")]) == Decimal("0")

    def test_flat_returns(self) -> None:
        returns = [Decimal("0.01")] * 50
        assert compute_volatility(returns) == Decimal("0")

    def test_manual(self) -> None:
        # returns = [0, 1] -> std(ddof=1) = sqrt(0.5); vol = sqrt(0.5)*sqrt(252)
        returns = [Decimal("0"), Decimal("1")]
        result = compute_volatility(returns)
        expected = Decimal(str(math.sqrt(0.5) * math.sqrt(252)))
        assert abs(result - expected) < Decimal("1e-10")


class TestMaxDrawdown:
    def test_empty(self) -> None:
        assert compute_max_drawdown([]) == Decimal("0")

    def test_always_rising(self) -> None:
        curve = [(date(2020, 1, i), Decimal(str(100 + i))) for i in range(2, 10)]
        assert compute_max_drawdown(curve) == Decimal("0")

    def test_manual_simple(self) -> None:
        # peak=110, luego cae a 88: DD = (88-110)/110 = -20/110
        curve = [
            (date(2020, 1, 2), Decimal("100")),
            (date(2020, 1, 3), Decimal("110")),
            (date(2020, 1, 6), Decimal("88")),
        ]
        result = compute_max_drawdown(curve)
        expected = (Decimal("88") - Decimal("110")) / Decimal("110")
        assert result == expected  # assertEqual exacto

    def test_negative(self) -> None:
        curve = [
            (date(2020, 1, 2), Decimal("100")),
            (date(2020, 1, 3), Decimal("80")),
        ]
        result = compute_max_drawdown(curve)
        assert result < Decimal("0")


class TestMaxDrawdownDuration:
    def test_empty(self) -> None:
        assert compute_max_drawdown_duration([]) == 0

    def test_no_drawdown(self) -> None:
        curve = [(date(2020, 1, i), Decimal(str(100 + i))) for i in range(2, 8)]
        assert compute_max_drawdown_duration(curve) == 0

    def test_manual_duration(self) -> None:
        # peak en dia 0 (100), cae dias 1-3, recupera en dia 4
        curve = [
            (date(2020, 1, 2), Decimal("100")),
            (date(2020, 1, 3), Decimal("90")),
            (date(2020, 1, 6), Decimal("85")),
            (date(2020, 1, 7), Decimal("80")),
            (date(2020, 1, 8), Decimal("105")),
        ]
        assert compute_max_drawdown_duration(curve) == 3


class TestCalmar:
    def test_zero_drawdown(self) -> None:
        assert compute_calmar(Decimal("0.1"), Decimal("0")) == Decimal("0")

    def test_manual(self) -> None:
        # calmar = cagr / |mdd| = 0.12 / 0.20 = 0.6
        result = compute_calmar(Decimal("0.12"), Decimal("-0.20"))
        expected = Decimal("0.12") / Decimal("0.20")
        assert result == expected  # assertEqual exacto


# ---------------------------------------------------------------------------
# 4. Ratios
# ---------------------------------------------------------------------------

class TestSharpe:
    def test_empty(self) -> None:
        assert compute_sharpe([]) == Decimal("0")

    def test_zero_std(self) -> None:
        returns = [Decimal("0.01")] * 10
        assert compute_sharpe(returns) == Decimal("0")

    def test_manual(self) -> None:
        # returns = [0.01, -0.01] -> mean=0, std>0 -> sharpe=0
        returns = [Decimal("0.01"), Decimal("-0.01")]
        assert compute_sharpe(returns) == Decimal("0")

    def test_positive_trend(self) -> None:
        # Todos positivos: sharpe > 0
        returns = [Decimal("0.01")] * 5 + [Decimal("0.02")] * 5
        sharpe = compute_sharpe(returns)
        assert sharpe > Decimal("0")


class TestSortino:
    def test_no_negative(self) -> None:
        # Sin retornos negativos -> sortino == sharpe
        returns = [Decimal("0.01")] * 10 + [Decimal("0.02")] * 10
        sortino = compute_sortino(returns)
        sharpe = compute_sharpe(returns)
        assert sortino == sharpe

    def test_with_negatives(self) -> None:
        # Sortino usa std solo de retornos negativos.
        # Con retornos negativos variados, la funcion debe devolver un valor != 0.
        returns = (
            [Decimal("0.001")] * 5
            + [Decimal("-0.003"), Decimal("-0.007"), Decimal("-0.005"),
               Decimal("-0.010"), Decimal("-0.002")]
        )
        sortino = compute_sortino(returns)
        # Verificamos que sortino se calculo (no retorno 0 por std=0 en negativos)
        assert sortino != Decimal("0")
        # Verificamos que es un numero finito
        assert sortino == sortino  # no NaN


# ---------------------------------------------------------------------------
# 5. Turnover
# ---------------------------------------------------------------------------

class TestTurnover:
    def test_no_fills(self) -> None:
        curve = _linear_curve(252)
        assert compute_turnover([], curve, 252) == Decimal("0")

    def test_manual(self) -> None:
        # 1 BUY de 100 acciones @ 10 = 1000 notional
        # avg_equity = 10000, n_years = 252/252 = 1
        # turnover = 1000 / (1 * 10000) = 0.1
        fills = [_make_fill("SPY", Side.BUY, "100", "10", "0")]
        curve = _flat_curve(252, Decimal("10000"))
        result = compute_turnover(fills, curve, 252)
        expected = Decimal("1000") / (Decimal("1") * Decimal("10000"))
        assert abs(result - expected) < Decimal("1e-6")


# ---------------------------------------------------------------------------
# 6. compute_full_report
# ---------------------------------------------------------------------------

class TestComputeFullReport:
    def test_minimal_report(self) -> None:
        """Informe minimo con equity curve plana y sin fills."""
        curve = _flat_curve(300)
        report = compute_full_report(
            strategy_id="test",
            equity_curve=curve,
            fills=[],
            initial_equity=Decimal("10000"),
            total_commission=Decimal("0"),
            bootstrap=False,
        )
        assert isinstance(report, BacktestReport)
        assert report.total_return == Decimal("0.0000")
        assert report.max_drawdown == Decimal("0.0000")
        assert report.n_trades == 0
        assert report.sharpe == Decimal("0.0000")

    def test_rising_curve_positive_return(self) -> None:
        curve = _linear_curve(252, Decimal("10000"), Decimal("11000"))
        report = compute_full_report(
            strategy_id="test",
            equity_curve=curve,
            fills=[],
            initial_equity=Decimal("10000"),
            total_commission=Decimal("0"),
            bootstrap=False,
        )
        assert report.total_return > Decimal("0")
        assert report.cagr > Decimal("0")
        assert report.sharpe > Decimal("0")

    def test_pydantic_frozen(self) -> None:
        curve = _flat_curve(252)
        report = compute_full_report(
            strategy_id="test",
            equity_curve=curve,
            fills=[],
            initial_equity=Decimal("10000"),
            total_commission=Decimal("0"),
            bootstrap=False,
        )
        # Pydantic v2 frozen=True lanza ValidationError al asignar un atributo
        from pydantic import ValidationError
        with pytest.raises((ValidationError, TypeError, AttributeError)):
            report.total_return = Decimal("99")  # type: ignore[misc]

    def test_json_serializable(self) -> None:
        import json
        curve = _flat_curve(252)
        report = compute_full_report(
            strategy_id="test",
            equity_curve=curve,
            fills=[],
            initial_equity=Decimal("10000"),
            total_commission=Decimal("0"),
            bootstrap=False,
        )
        json_str = report.to_json()
        obj = json.loads(json_str)
        assert "total_return" in obj
        assert "sharpe" in obj

    def test_report_with_fills(self) -> None:
        """Con fills de compra y venta, win_rate y by_instrument se calculan."""
        curve = _linear_curve(300, Decimal("10000"), Decimal("10500"))
        fills = [
            _make_fill("SPY", Side.BUY, "10", "100", "1.00", date(2020, 1, 2)),
            _make_fill("SPY", Side.SELL, "10", "110", "1.00", date(2020, 2, 3)),
        ]
        report = compute_full_report(
            strategy_id="test",
            equity_curve=curve,
            fills=fills,
            initial_equity=Decimal("10000"),
            total_commission=Decimal("2"),
            bootstrap=False,
        )
        assert report.n_trades >= 1
        # buy 100*10=1000, sell 110*10=1100, commission=1 -> pnl = 99
        assert len(report.by_instrument) >= 1
        assert report.by_instrument[0].symbol == "SPY"

    def test_costs_breakdown(self) -> None:
        curve = _flat_curve(252)
        report = compute_full_report(
            strategy_id="test",
            equity_curve=curve,
            fills=[],
            initial_equity=Decimal("10000"),
            total_commission=Decimal("42.50"),
            bootstrap=False,
        )
        assert report.costs.total_commission == Decimal("42.50")
        assert report.costs.total_costs == Decimal("42.50")

    def test_monthly_annual_pnl(self) -> None:
        from datetime import timedelta
        curve: list[tuple[date, Decimal]] = []
        d = date(2020, 1, 2)
        eq = Decimal("10000")
        for _ in range(504):  # ~2 años
            while d.weekday() >= 5:
                d += timedelta(days=1)
            curve.append((d, eq))
            eq += Decimal("10")
            d += timedelta(days=1)
        report = compute_full_report(
            strategy_id="test",
            equity_curve=curve,
            fills=[],
            initial_equity=Decimal("10000"),
            total_commission=Decimal("0"),
            bootstrap=False,
        )
        assert len(report.monthly_pnl) > 0
        assert len(report.annual_pnl) > 0

    def test_empty_equity_curve_raises(self) -> None:
        with pytest.raises(ValueError, match="equity_curve"):
            compute_full_report(
                strategy_id="test",
                equity_curve=[],
                fills=[],
                initial_equity=Decimal("10000"),
                total_commission=Decimal("0"),
                bootstrap=False,
            )


# ---------------------------------------------------------------------------
# 7. Bootstrap
# ---------------------------------------------------------------------------

class TestBootstrap:
    def test_samples_count(self) -> None:
        returns = [Decimal(str(0.001 * (i % 3 - 1))) for i in range(500)]
        samples = _block_bootstrap_sharpe(returns, block_size=21, n_samples=100, seed=1)
        assert len(samples) == 100

    def test_not_enough_data(self) -> None:
        # Menos de 2*block_size barras -> devuelve lista vacia
        returns = [Decimal("0.001")] * 10
        samples = _block_bootstrap_sharpe(returns, block_size=21, n_samples=100)
        assert samples == []

    def test_ci_contains_point_estimate(self) -> None:
        """Para series > 500 dias, el CI 95% debe contener el estimador puntual."""
        # Serie de 600 retornos con estructura conocida
        import random as rnd
        rnd.seed(99)
        returns = [Decimal(str(0.001 + 0.005 * (rnd.random() - 0.5))) for _ in range(600)]
        point_sharpe = compute_sharpe(returns)
        samples = _block_bootstrap_sharpe(returns, block_size=21, n_samples=1000, seed=42)
        ci = compute_bootstrap_ci(samples, 1000)
        assert ci.lower <= point_sharpe <= ci.upper, (
            f"CI [{ci.lower}, {ci.upper}] no contiene el Sharpe puntual {point_sharpe}"
        )

    def test_bootstrap_deterministic(self) -> None:
        """Misma seed -> mismos resultados."""
        returns = [Decimal(str(0.001 * (i % 5 - 2))) for i in range(300)]
        s1 = _block_bootstrap_sharpe(returns, block_size=21, n_samples=50, seed=7)
        s2 = _block_bootstrap_sharpe(returns, block_size=21, n_samples=50, seed=7)
        assert s1 == s2

    def test_full_report_with_bootstrap(self) -> None:
        """compute_full_report con bootstrap=True produce CIs no nulos para series largas."""
        curve = _linear_curve(700, Decimal("10000"), Decimal("12000"))
        report = compute_full_report(
            strategy_id="test",
            equity_curve=curve,
            fills=[],
            initial_equity=Decimal("10000"),
            total_commission=Decimal("0"),
            bootstrap=True,
            bootstrap_n_samples=100,  # rapido para el test
            bootstrap_block_size=21,
        )
        assert report.sharpe_ci is not None
        assert isinstance(report.sharpe_ci, BootstrapCI)
        assert report.sharpe_ci.lower <= report.sharpe_ci.upper

    def test_full_report_no_bootstrap_no_ci(self) -> None:
        curve = _linear_curve(252)
        report = compute_full_report(
            strategy_id="test",
            equity_curve=curve,
            fills=[],
            initial_equity=Decimal("10000"),
            total_commission=Decimal("0"),
            bootstrap=False,
        )
        assert report.sharpe_ci is None
        assert report.sortino_ci is None


# ---------------------------------------------------------------------------
# 8. Verificacion manual con tolerancia < 1e-10
# ---------------------------------------------------------------------------

class TestManualCalculations:
    """Tres calculos manuales exactos con assertEqual (sin approx)."""

    def test_total_return_exact(self) -> None:
        initial = Decimal("15000")
        final = Decimal("16500")
        result = compute_total_return(initial, final)
        # (16500 - 15000) / 15000 = 1500/15000 = 1/10
        expected = Decimal("1") / Decimal("10")
        assert result == expected

    def test_max_drawdown_exact(self) -> None:
        # pico = 200, valle = 160: DD = (160-200)/200 = -40/200 = -1/5
        curve = [
            (date(2022, 1, 3), Decimal("150")),
            (date(2022, 1, 4), Decimal("200")),
            (date(2022, 1, 5), Decimal("160")),
        ]
        result = compute_max_drawdown(curve)
        expected = (Decimal("160") - Decimal("200")) / Decimal("200")
        assert result == expected  # -0.2 exacto

    def test_calmar_exact(self) -> None:
        # CAGR = 0.15, MaxDD = -0.30 -> Calmar = 0.15/0.30 = 0.5
        cagr = Decimal("3") / Decimal("20")   # 0.15
        mdd = Decimal("-3") / Decimal("10")   # -0.30
        result = compute_calmar(cagr, mdd)
        expected = Decimal("1") / Decimal("2")  # 0.5
        assert result == expected  # assertEqual exacto

    def test_sharpe_formula_manual(self) -> None:
        # returns = [0.01, 0.03] -> mean=0.02, std(ddof=1)=sqrt(0.0002)
        # sharpe = 0.02 * sqrt(252) / sqrt(0.0002)
        returns = [Decimal("0.01"), Decimal("0.03")]
        result = compute_sharpe(returns)
        mean = Decimal("0.02")
        variance = (
            (Decimal("0.01") - mean) ** 2 + (Decimal("0.03") - mean) ** 2
        ) / Decimal("1")
        std = Decimal(str(math.sqrt(float(variance))))
        sqrt252 = Decimal(str(math.sqrt(252)))
        expected = mean * sqrt252 / std
        assert abs(result - expected) < Decimal("1e-10")

    def test_cagr_exact_two_years(self) -> None:
        # final/initial = 1.21, n_days = 504 -> (1.21)^(252/504) - 1 = sqrt(1.21) - 1 = 0.1
        result = compute_cagr(Decimal("10000"), Decimal("12100"), 504)
        expected = Decimal("0.1")
        assert abs(result - expected) < Decimal("1e-10")
