"""Monte Carlo de robustez — T9 Parte B.

Dos variantes:
  1. Block bootstrap sobre la equity curve (block_size=21, n=1000).
     Preserva la autocorrelación de los retornos. Calcula P5/P25/P50/P75/P95
     de Sharpe, MaxDD y CAGR.

  2. Bootstrap del orden de trades (n=1000).
     Reordena los trade P&Ls de forma aleatoria y recalcula la equity curve.
     Permite separar el alfa de la suerte en el orden de ejecución.

Umbral de robustez (criterio cuantitativo C):
  P5(Sharpe) >= -0.5  →  la estrategia no es catastrófica en el 5 % peor

Sin dependencias externas (numpy, scipy). Solo stdlib + qtrader.backtesting.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import date  # noqa: TCH003
from decimal import Decimal

from qtrader.backtesting.full_metrics import (
    _block_bootstrap_max_drawdown,
    _block_bootstrap_sharpe,
    _daily_returns,
    _percentile,
    compute_cagr,
    compute_max_drawdown,
    compute_sharpe,
)

_ZERO = Decimal("0")
_ONE = Decimal("1")

BLOCK_SIZE = 21        # 1 mes en días hábiles
N_SAMPLES = 1000
SEED = 42
P5_SHARPE_MIN = Decimal("-0.5")  # criterio de robustez


@dataclass(frozen=True)
class Percentiles:
    """Percentiles P5/P25/P50/P75/P95 de una métrica."""

    p5: Decimal
    p25: Decimal
    p50: Decimal
    p75: Decimal
    p95: Decimal

    def to_dict(self) -> dict[str, str]:
        return {
            "p5": str(self.p5),
            "p25": str(self.p25),
            "p50": str(self.p50),
            "p75": str(self.p75),
            "p95": str(self.p95),
        }


@dataclass(frozen=True)
class MonteCarloResult:
    """Resultado del Monte Carlo completo."""

    # --- Block bootstrap sobre equity curve ---
    sharpe_pctls: Percentiles
    maxdd_pctls: Percentiles
    cagr_pctls: Percentiles
    n_bootstrap_samples: int

    # --- Bootstrap del orden de trades ---
    trade_order_sharpe_pctls: Percentiles | None
    n_trade_bootstrap_samples: int

    # --- Criterio cuantitativo C ---
    p5_sharpe_passes: bool     # True si p5(Sharpe) >= P5_SHARPE_MIN
    has_enough_data: bool      # False si no hay suficientes datos para bootstrap


def _compute_percentiles(values: list[Decimal]) -> Percentiles:
    return Percentiles(
        p5=_percentile(values, 5.0),
        p25=_percentile(values, 25.0),
        p50=_percentile(values, 50.0),
        p75=_percentile(values, 75.0),
        p95=_percentile(values, 95.0),
    )


def run_equity_curve_bootstrap(
    equity_curve: list[tuple[date, Decimal]],
    *,
    block_size: int = BLOCK_SIZE,
    n_samples: int = N_SAMPLES,
    seed: int = SEED,
) -> MonteCarloResult:
    """Bootstrap de la equity curve. Sin bootstrap de trades."""
    returns = _daily_returns(equity_curve)
    n = len(returns)
    initial = equity_curve[0][1] if equity_curve else _ONE
    n_trading_days = len(equity_curve)

    if n < block_size * 2:
        empty_p = Percentiles(p5=_ZERO, p25=_ZERO, p50=_ZERO, p75=_ZERO, p95=_ZERO)
        return MonteCarloResult(
            sharpe_pctls=empty_p,
            maxdd_pctls=empty_p,
            cagr_pctls=empty_p,
            n_bootstrap_samples=0,
            trade_order_sharpe_pctls=None,
            n_trade_bootstrap_samples=0,
            p5_sharpe_passes=False,
            has_enough_data=False,
        )

    sh_samples = _block_bootstrap_sharpe(returns, block_size, n_samples, seed)
    mdd_samples = _block_bootstrap_max_drawdown(returns, equity_curve, block_size, n_samples, seed + 1)

    # Bootstrap CAGR: reconstruir equity desde retornos bootstrapped
    rng = random.Random(seed + 2)
    n_blocks = math.ceil(n / block_size)
    cagr_samples: list[Decimal] = []
    for _ in range(n_samples):
        sample: list[Decimal] = []
        for _ in range(n_blocks):
            start = rng.randint(0, n - block_size)
            sample.extend(returns[start: start + block_size])
        sample = sample[:n]
        eq = initial
        for r in sample:
            eq = eq * (_ONE + r)
        cagr_samples.append(compute_cagr(initial, eq, n_trading_days))

    sharpe_p = _compute_percentiles(sh_samples)
    maxdd_p = _compute_percentiles(mdd_samples)
    cagr_p = _compute_percentiles(cagr_samples)

    return MonteCarloResult(
        sharpe_pctls=sharpe_p,
        maxdd_pctls=maxdd_p,
        cagr_pctls=cagr_p,
        n_bootstrap_samples=n_samples,
        trade_order_sharpe_pctls=None,
        n_trade_bootstrap_samples=0,
        p5_sharpe_passes=sharpe_p.p5 >= P5_SHARPE_MIN,
        has_enough_data=True,
    )


def run_trade_order_bootstrap(
    trade_pnls: list[Decimal],
    initial_equity: Decimal,
    *,
    n_samples: int = N_SAMPLES,
    seed: int = SEED + 3,
) -> Percentiles | None:
    """Bootstrap del orden de trades.

    Reordena los P&Ls de trades aleatoriamente y recalcula Sharpe de la
    equity curve resultante. Cuantifica cuánto del resultado depende del
    orden de ejecución vs. del valor esperado de la señal.

    Returns:
        Percentiles del Sharpe, o None si hay < 2 trades.
    """
    if len(trade_pnls) < 2:
        return None

    rng = random.Random(seed)
    sharpes: list[Decimal] = []

    for _ in range(n_samples):
        shuffled = list(trade_pnls)
        rng.shuffle(shuffled)

        # Reconstruir equity diaria: cada trade ocupa un "día"
        eq = initial_equity
        equity_pts: list[tuple[date, Decimal]] = []
        from datetime import date as _date
        fake_day = _date(2020, 1, 2)
        from datetime import timedelta
        for pnl in shuffled:
            eq = max(_ONE, eq + pnl)
            equity_pts.append((fake_day, eq))
            fake_day = fake_day + timedelta(days=1)

        returns = _daily_returns(equity_pts)
        sharpes.append(compute_sharpe(returns))

    return _compute_percentiles(sharpes)


def run_full_monte_carlo(
    equity_curve: list[tuple[date, Decimal]],
    trade_pnls: list[Decimal],
    initial_equity: Decimal,
    *,
    block_size: int = BLOCK_SIZE,
    n_samples: int = N_SAMPLES,
    seed: int = SEED,
) -> MonteCarloResult:
    """Ejecuta ambas variantes de Monte Carlo."""
    base = run_equity_curve_bootstrap(
        equity_curve,
        block_size=block_size,
        n_samples=n_samples,
        seed=seed,
    )

    trade_p = run_trade_order_bootstrap(
        trade_pnls,
        initial_equity,
        n_samples=n_samples,
        seed=seed + 3,
    )

    return MonteCarloResult(
        sharpe_pctls=base.sharpe_pctls,
        maxdd_pctls=base.maxdd_pctls,
        cagr_pctls=base.cagr_pctls,
        n_bootstrap_samples=base.n_bootstrap_samples,
        trade_order_sharpe_pctls=trade_p,
        n_trade_bootstrap_samples=n_samples if trade_p is not None else 0,
        p5_sharpe_passes=base.p5_sharpe_passes,
        has_enough_data=base.has_enough_data,
    )
