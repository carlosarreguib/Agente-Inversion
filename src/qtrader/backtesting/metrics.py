"""Cálculo de métricas de backtest para el golden test (T2.3).

Función pura: compute_metrics(result, fills) → GoldenMetrics.
Sin I/O, sin estado global. Mismas entradas → misma salida.

Métricas:
  - total_return:    (final_equity - initial_equity) / initial_equity, 4 decimales.
  - sharpe_ratio:    retorno medio diario / desviación diaria * sqrt(252), 4 decimales.
  - max_drawdown:    máxima caída pico-valle de la equity curve, 4 decimales.
  - num_trades:      número total de fills ejecutados.
  - total_commission: suma de comisiones de todos los fills, 2 decimales EUR.
  - total_slippage:  suma de (|fill.price - open_proxy| * qty) — no disponible
                     directamente; se reporta como 0 si no hay proxy. 2 decimales.
  - final_equity:    equity final, 2 decimales EUR.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from qtrader.backtesting.engine import BacktestResult  # noqa: TCH001

_ZERO = Decimal("0")
_TWO = Decimal("2")
_FOUR = Decimal("4")
_SQRT252 = Decimal(str(math.sqrt(252)))


@dataclass(frozen=True)
class GoldenMetrics:
    """Métricas congeladas para comparación determinista."""

    total_return: Decimal    # 4 decimales
    sharpe_ratio: Decimal    # 4 decimales
    max_drawdown: Decimal    # 4 decimales (negativo o 0)
    num_trades: int
    total_commission: Decimal  # 2 decimales EUR
    total_slippage: Decimal    # 2 decimales EUR (0 si no hay proxy)
    final_equity: Decimal      # 2 decimales EUR

    def to_dict(self) -> dict[str, str | int]:
        return {
            "total_return": str(self.total_return),
            "sharpe_ratio": str(self.sharpe_ratio),
            "max_drawdown": str(self.max_drawdown),
            "num_trades": self.num_trades,
            "total_commission": str(self.total_commission),
            "total_slippage": str(self.total_slippage),
            "final_equity": str(self.final_equity),
        }

    @classmethod
    def from_dict(cls, d: dict[str, str | int]) -> GoldenMetrics:
        return cls(
            total_return=Decimal(str(d["total_return"])),
            sharpe_ratio=Decimal(str(d["sharpe_ratio"])),
            max_drawdown=Decimal(str(d["max_drawdown"])),
            num_trades=int(d["num_trades"]),
            total_commission=Decimal(str(d["total_commission"])),
            total_slippage=Decimal(str(d["total_slippage"])),
            final_equity=Decimal(str(d["final_equity"])),
        )


def compute_metrics(result: BacktestResult) -> GoldenMetrics:
    """Calcula las métricas golden a partir del BacktestResult.

    Todos los valores redondeados a precisión fija para comparación exacta.
    """
    # --- total_return ---
    if result.initial_equity == _ZERO:
        total_return = _ZERO
    else:
        total_return = (result.final_equity - result.initial_equity) / result.initial_equity
    total_return = total_return.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    # --- sharpe_ratio --- (anualizado, asume 252 días/año)
    equity_curve = result.equity_curve
    if len(equity_curve) < 2:
        sharpe_ratio = _ZERO
    else:
        daily_returns: list[Decimal] = []
        for i in range(1, len(equity_curve)):
            prev_eq = equity_curve[i - 1][1]
            curr_eq = equity_curve[i][1]
            if prev_eq > _ZERO:
                daily_returns.append((curr_eq - prev_eq) / prev_eq)

        if len(daily_returns) < 2:
            sharpe_ratio = _ZERO
        else:
            n = Decimal(len(daily_returns))
            mean_ret = sum(daily_returns, _ZERO) / n
            variance = sum((r - mean_ret) ** 2 for r in daily_returns) / (n - 1)
            std_ret = Decimal(str(math.sqrt(float(variance))))
            sharpe_ratio = _ZERO if std_ret == _ZERO else (mean_ret / std_ret) * _SQRT252

    sharpe_ratio = sharpe_ratio.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    # --- max_drawdown ---
    if not equity_curve:
        max_drawdown = _ZERO
    else:
        peak = equity_curve[0][1]
        max_dd = _ZERO
        for _, eq in equity_curve:
            if eq > peak:
                peak = eq
            if peak > _ZERO:
                dd = (eq - peak) / peak
                if dd < max_dd:
                    max_dd = dd
        max_drawdown = max_dd.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    # --- fills ---
    num_trades = len(result.fills)
    total_commission = sum(
        (f.commission for f in result.fills), _ZERO
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    # total_slippage: no tenemos precio teórico limpio en este contexto.
    # Se registra como 0 — el golden test detecta cambios en equity que
    # incluyen implícitamente slippage (porque final_equity cambia).
    total_slippage = _ZERO.quantize(Decimal("0.01"))

    final_equity = result.final_equity.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    return GoldenMetrics(
        total_return=total_return,
        sharpe_ratio=sharpe_ratio,
        max_drawdown=max_drawdown,
        num_trades=num_trades,
        total_commission=total_commission,
        total_slippage=total_slippage,
        final_equity=final_equity,
    )
