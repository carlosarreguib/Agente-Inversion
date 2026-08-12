"""Framework de metricas completo para backtesting (T3.2).

Todas las funciones son puras: sin I/O, sin estado global.
Mismas entradas -> misma salida.

Formulas (anualizadas donde aplica, base 252 dias habiles):

  total_return  = final_equity / initial_equity - 1
  cagr          = (final_equity / initial_equity)^(252/n_days) - 1
  volatility    = std(daily_returns) * sqrt(252)
  sharpe        = mean(r)*252 / (std(r)*sqrt(252))
               = mean(r)*sqrt(252) / std(r)
  sortino       = mean(r)*252 / (std(r_neg)*sqrt(252))
               = mean(r)*sqrt(252) / std(r_neg)
               donde r_neg = retornos diarios < 0 (0 -> penaliza solo la cara negativa)
  max_drawdown  = min(equity[t] / max(equity[0..t]) - 1)   <= 0
  calmar        = cagr / abs(max_drawdown)    (0 si max_drawdown == 0)
  max_dd_duration: maximo numero de dias consecutivos en que equity < pico previo
  win_rate      = fills con P&L > 0 / total fills (round-trip: par entrada/salida)
  profit_factor = sum(ganancias) / sum(|perdidas|)  (inf si perdidas=0)
  turnover      = sum(|notional de trades|) / (n_years * avg_equity)

Bootstrap (block bootstrap, block_size=21, n_samples=1000):
  Preserva la autocorrelacion de los retornos diarios.
  CI_95 = [percentil_2.5, percentil_97.5].
  Sin scipy ni numpy — usa solo stdlib (random, math).
"""
from __future__ import annotations

import math
import random
from datetime import date  # noqa: TCH003 — usado en Pydantic y firmas runtime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from qtrader.core.types import Fill, Side  # noqa: TCH001

_ZERO = Decimal("0")
_ONE = Decimal("1")
_SQRT252 = Decimal(str(math.sqrt(252)))
_252 = Decimal("252")


# ---------------------------------------------------------------------------
# Helpers aritmeticos (Decimal puro, sin float en intermedios criticos)
# ---------------------------------------------------------------------------

def _mean(values: list[Decimal]) -> Decimal:
    if not values:
        return _ZERO
    return sum(values, _ZERO) / Decimal(len(values))


def _std(values: list[Decimal], ddof: int = 1) -> Decimal:
    n = len(values)
    if n <= ddof:
        return _ZERO
    mu = _mean(values)
    variance = sum((v - mu) ** 2 for v in values) / Decimal(n - ddof)
    return Decimal(str(math.sqrt(float(variance))))


def _daily_returns(equity_curve: list[tuple[date, Decimal]]) -> list[Decimal]:
    """Retornos diarios (r_t = equity_t/equity_{t-1} - 1) desde la equity curve."""
    returns: list[Decimal] = []
    for i in range(1, len(equity_curve)):
        prev = equity_curve[i - 1][1]
        curr = equity_curve[i][1]
        if prev > _ZERO:
            returns.append((curr - prev) / prev)
    return returns


# ---------------------------------------------------------------------------
# Modelos Pydantic (frozen, serializable)
# ---------------------------------------------------------------------------

class BootstrapCI(BaseModel):
    """Intervalo de confianza bootstrap al 95 %."""
    model_config = ConfigDict(frozen=True)

    lower: Decimal
    upper: Decimal
    n_samples: int


class InstrumentStats(BaseModel):
    """Estadisticas por instrumento."""
    model_config = ConfigDict(frozen=True)

    symbol: str
    n_trades: int
    pnl_eur: Decimal
    win_rate: Decimal          # [0, 1]


class PeriodPnL(BaseModel):
    """P&L de un periodo (mes o anio)."""
    model_config = ConfigDict(frozen=True)

    period: str                # "2023-01" o "2023"
    pnl_eur: Decimal
    return_pct: Decimal        # retorno porcentual del periodo


class CostBreakdownReport(BaseModel):
    """Desglose de costes del backtest."""
    model_config = ConfigDict(frozen=True)

    total_commission: Decimal
    total_spread: Decimal      # estimado como (total_costs - commission)
    total_slippage: Decimal    # aproximacion: 0 (no disponible directamente)
    total_costs: Decimal


class BacktestReport(BaseModel):
    """Informe completo de un backtest. Serializable a JSON."""
    model_config = ConfigDict(frozen=True)

    # --- Identificacion ---
    strategy_id: str
    start_date: date
    end_date: date
    n_trading_days: int

    # --- Retorno ---
    total_return: Decimal
    cagr: Decimal

    # --- Riesgo ---
    volatility: Decimal
    max_drawdown: Decimal            # <= 0
    max_drawdown_duration_days: int  # dias consecutivos en drawdown

    # --- Ratios ---
    sharpe: Decimal
    sortino: Decimal
    calmar: Decimal

    # --- Actividad ---
    n_trades: int
    win_rate: Decimal               # [0, 1]
    profit_factor: Decimal          # inf = 0 perdidas
    avg_win_eur: Decimal
    avg_loss_eur: Decimal
    avg_win_pct: Decimal
    avg_loss_pct: Decimal
    turnover_annual: Decimal

    # --- Exposicion ---
    avg_exposure: Decimal           # [0, 1]
    max_exposure: Decimal           # [0, 1]

    # --- Costes ---
    costs: CostBreakdownReport

    # --- Bootstrap CIs (metricas principales) ---
    sharpe_ci: BootstrapCI | None = None
    sortino_ci: BootstrapCI | None = None
    calmar_ci: BootstrapCI | None = None
    max_drawdown_ci: BootstrapCI | None = None

    # --- Detalle ---
    by_instrument: tuple[InstrumentStats, ...] = Field(default_factory=tuple)
    monthly_pnl: tuple[PeriodPnL, ...] = Field(default_factory=tuple)
    annual_pnl: tuple[PeriodPnL, ...] = Field(default_factory=tuple)

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)


# ---------------------------------------------------------------------------
# Calculo de metricas individuales (funciones puras)
# ---------------------------------------------------------------------------

def compute_total_return(initial: Decimal, final: Decimal) -> Decimal:
    if initial == _ZERO:
        return _ZERO
    return (final - initial) / initial


def compute_cagr(initial: Decimal, final: Decimal, n_days: int) -> Decimal:
    if initial <= _ZERO or final <= _ZERO or n_days <= 0:
        return _ZERO
    ratio = float(final / initial)
    if ratio <= 0:
        return Decimal("-1")
    exponent = 252.0 / n_days
    return Decimal(str(ratio ** exponent - 1.0))


def compute_volatility(returns: list[Decimal]) -> Decimal:
    if len(returns) < 2:
        return _ZERO
    return _std(returns) * _SQRT252


def compute_sharpe(returns: list[Decimal]) -> Decimal:
    if len(returns) < 2:
        return _ZERO
    std = _std(returns)
    if std == _ZERO:
        return _ZERO
    return _mean(returns) * _SQRT252 / std


def compute_sortino(returns: list[Decimal]) -> Decimal:
    if len(returns) < 2:
        return _ZERO
    neg = [r for r in returns if r < _ZERO]
    if not neg:
        # Sin retornos negativos -> sortino infinito; convencion: devolver Sharpe
        return compute_sharpe(returns)
    std_neg = _std(neg, ddof=1)
    if std_neg == _ZERO:
        return _ZERO
    return _mean(returns) * _SQRT252 / std_neg


def compute_max_drawdown(equity_curve: list[tuple[date, Decimal]]) -> Decimal:
    """Max drawdown <= 0. Formula: min(equity[t]/peak[t] - 1)."""
    if not equity_curve:
        return _ZERO
    peak = equity_curve[0][1]
    max_dd = _ZERO
    for _, eq in equity_curve:
        if eq > peak:
            peak = eq
        if peak > _ZERO:
            dd = (eq - peak) / peak
            if dd < max_dd:
                max_dd = dd
    return max_dd


def compute_max_drawdown_duration(equity_curve: list[tuple[date, Decimal]]) -> int:
    """Maximo numero de dias consecutivos con equity por debajo del pico anterior."""
    if not equity_curve:
        return 0
    peak = equity_curve[0][1]
    current_duration = 0
    max_duration = 0
    for _, eq in equity_curve:
        if eq >= peak:
            peak = eq
            current_duration = 0
        else:
            current_duration += 1
            if current_duration > max_duration:
                max_duration = current_duration
    return max_duration


def compute_calmar(cagr: Decimal, max_drawdown: Decimal) -> Decimal:
    if max_drawdown >= _ZERO:
        return _ZERO
    return cagr / abs(max_drawdown)


def _compute_trade_pnl(fills: list[Fill]) -> list[dict[str, Any]]:
    """Empareja fills BUY/SELL por simbolo (FIFO) y calcula P&L de round-trips."""
    # Agrupar por simbolo
    by_symbol: dict[str, list[Fill]] = {}
    for f in fills:
        by_symbol.setdefault(f.symbol, []).append(f)

    trades: list[dict[str, Any]] = []
    for sym, sym_fills in by_symbol.items():
        queue: list[tuple[Decimal, Decimal]] = []  # (qty, price)
        for f in sym_fills:
            if f.side == Side.BUY:
                queue.append((f.quantity, f.price))
            else:
                # SELL: cerrar contra la cola FIFO
                remaining = f.quantity
                sell_price = f.price
                while remaining > _ZERO and queue:
                    buy_qty, buy_price = queue[0]
                    matched = min(remaining, buy_qty)
                    pnl = (sell_price - buy_price) * matched - f.commission
                    trades.append({
                        "symbol": sym,
                        "pnl": pnl,
                        "entry_price": buy_price,
                        "exit_price": sell_price,
                        "quantity": matched,
                    })
                    remaining -= matched
                    if matched >= buy_qty:
                        queue.pop(0)
                    else:
                        queue[0] = (buy_qty - matched, buy_price)
    return trades


def compute_trade_stats(
    fills: list[Fill],
    initial_equity: Decimal,
) -> dict[str, Any]:
    """Calcula estadisticas de trades: win_rate, profit_factor, avg_win/loss."""
    trades = _compute_trade_pnl(fills)
    if not trades:
        return {
            "n_trades": 0,
            "win_rate": _ZERO,
            "profit_factor": _ZERO,
            "avg_win_eur": _ZERO,
            "avg_loss_eur": _ZERO,
            "avg_win_pct": _ZERO,
            "avg_loss_pct": _ZERO,
        }

    wins = [t for t in trades if t["pnl"] > _ZERO]
    losses = [t for t in trades if t["pnl"] <= _ZERO]

    win_rate = Decimal(len(wins)) / Decimal(len(trades)) if trades else _ZERO
    gross_wins = sum(t["pnl"] for t in wins) if wins else _ZERO
    gross_losses = abs(sum(t["pnl"] for t in losses)) if losses else _ZERO
    profit_factor = (
        gross_wins / gross_losses if gross_losses > _ZERO
        else (Decimal("999999") if gross_wins > _ZERO else _ZERO)
    )
    avg_win_eur = (
        sum(t["pnl"] for t in wins) / Decimal(len(wins)) if wins else _ZERO
    )
    avg_loss_eur = (
        sum(t["pnl"] for t in losses) / Decimal(len(losses)) if losses else _ZERO
    )
    avg_win_pct = avg_win_eur / initial_equity if initial_equity > _ZERO else _ZERO
    avg_loss_pct = avg_loss_eur / initial_equity if initial_equity > _ZERO else _ZERO

    return {
        "n_trades": len(trades),
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "avg_win_eur": avg_win_eur,
        "avg_loss_eur": avg_loss_eur,
        "avg_win_pct": avg_win_pct,
        "avg_loss_pct": avg_loss_pct,
    }


def compute_by_instrument(fills: list[Fill]) -> list[InstrumentStats]:
    """P&L y estadisticas por instrumento."""
    trades = _compute_trade_pnl(fills)
    by_sym: dict[str, list[dict[str, Any]]] = {}
    for t in trades:
        by_sym.setdefault(t["symbol"], []).append(t)

    result: list[InstrumentStats] = []
    for sym, sym_trades in sorted(by_sym.items()):
        pnl = sum(t["pnl"] for t in sym_trades)
        wins = sum(1 for t in sym_trades if t["pnl"] > _ZERO)
        wr = Decimal(wins) / Decimal(len(sym_trades)) if sym_trades else _ZERO
        result.append(InstrumentStats(
            symbol=sym,
            n_trades=len(sym_trades),
            pnl_eur=pnl,
            win_rate=wr,
        ))
    return result


def compute_period_pnl(
    equity_curve: list[tuple[date, Decimal]],
    period: str = "monthly",
) -> list[PeriodPnL]:
    """P&L por periodo (monthly o annual)."""
    if not equity_curve:
        return []

    def _key(d: date) -> str:
        if period == "monthly":
            return f"{d.year}-{d.month:02d}"
        return str(d.year)

    periods: dict[str, list[Decimal]] = {}
    period_starts: dict[str, Decimal] = {}
    for d, eq in equity_curve:
        k = _key(d)
        if k not in period_starts:
            period_starts[k] = eq
        periods.setdefault(k, []).append(eq)

    result: list[PeriodPnL] = []
    for k in sorted(periods):
        start_eq = period_starts[k]
        end_eq = periods[k][-1]
        pnl = end_eq - start_eq
        ret_pct = (end_eq / start_eq - _ONE) if start_eq > _ZERO else _ZERO
        result.append(PeriodPnL(period=k, pnl_eur=pnl, return_pct=ret_pct))
    return result


def compute_exposure(
    equity_curve: list[tuple[date, Decimal]],
    fills: list[Fill],
) -> tuple[Decimal, Decimal]:
    """avg_exposure, max_exposure como fraccion del NAV.

    Aproximacion: calcula el valor invertido como la suma de notional de BUYs
    sin cerrar en cada dia. Limitado a [0, 1].
    """
    if not equity_curve or not fills:
        return _ZERO, _ZERO

    trading_days = [d for d, _ in equity_curve]

    # Proxy de exposicion: fraccion del tiempo con posicion abierta.
    # Para estimacion exacta se necesitaria la serie de posiciones del motor
    # (no disponible via eventos). Se mejorara en T4+.
    avg_nav = _mean([eq for _, eq in equity_curve])
    n_days = Decimal(len(equity_curve))

    if avg_nav <= _ZERO or n_days <= _ZERO:
        return _ZERO, _ZERO

    # Fraccion del tiempo con posicion abierta (proxy)
    days_with_fills: set[date] = {f.timestamp.date() for f in fills}
    days_invested = sum(1 for d in trading_days if d >= min(days_with_fills, default=d))
    avg_exp_raw = Decimal(days_invested) / n_days
    avg_exp = min(_ONE, avg_exp_raw)
    max_exp = min(_ONE, _ONE)  # sin datos de posicion dia a dia, asumimos 100% en maximo
    return avg_exp, max_exp


def compute_turnover(
    fills: list[Fill],
    equity_curve: list[tuple[date, Decimal]],
    n_trading_days: int,
) -> Decimal:
    """Rotacion anual = sum(|notional|) / (n_anios * avg_equity)."""
    if not fills or not equity_curve or n_trading_days <= 0:
        return _ZERO
    total_notional = sum(f.quantity * f.price for f in fills)
    avg_equity = _mean([eq for _, eq in equity_curve])
    if avg_equity <= _ZERO:
        return _ZERO
    n_years = Decimal(str(n_trading_days)) / _252
    if n_years <= _ZERO:
        return _ZERO
    return total_notional / (n_years * avg_equity)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

def _block_bootstrap_sharpe(
    returns: list[Decimal],
    block_size: int = 21,
    n_samples: int = 1000,
    seed: int = 42,
) -> list[Decimal]:
    """Block bootstrap del Sharpe ratio.

    Devuelve una lista de n_samples valores del Sharpe calculados sobre
    muestras bootstrap con bloques de longitud block_size.
    """
    n = len(returns)
    if n < block_size * 2:
        return []

    rng = random.Random(seed)
    n_blocks = math.ceil(n / block_size)
    bootstrapped: list[Decimal] = []

    for _ in range(n_samples):
        sample: list[Decimal] = []
        for _ in range(n_blocks):
            start = rng.randint(0, n - block_size)
            sample.extend(returns[start: start + block_size])
        sample = sample[:n]  # truncar al tamanio original
        sharpe = compute_sharpe(sample)
        bootstrapped.append(sharpe)

    return bootstrapped


def _block_bootstrap_sortino(
    returns: list[Decimal],
    block_size: int = 21,
    n_samples: int = 1000,
    seed: int = 42,
) -> list[Decimal]:
    n = len(returns)
    if n < block_size * 2:
        return []
    rng = random.Random(seed)
    n_blocks = math.ceil(n / block_size)
    bootstrapped: list[Decimal] = []
    for _ in range(n_samples):
        sample: list[Decimal] = []
        for _ in range(n_blocks):
            start = rng.randint(0, n - block_size)
            sample.extend(returns[start: start + block_size])
        sample = sample[:n]
        bootstrapped.append(compute_sortino(sample))
    return bootstrapped


def _block_bootstrap_calmar(
    returns: list[Decimal],
    equity_curve: list[tuple[date, Decimal]],
    n_trading_days: int,
    block_size: int = 21,
    n_samples: int = 1000,
    seed: int = 42,
) -> list[Decimal]:
    n = len(returns)
    if n < block_size * 2:
        return []
    rng = random.Random(seed)
    n_blocks = math.ceil(n / block_size)
    bootstrapped: list[Decimal] = []

    initial = equity_curve[0][1] if equity_curve else _ONE

    for _ in range(n_samples):
        sample: list[Decimal] = []
        for _ in range(n_blocks):
            start = rng.randint(0, n - block_size)
            sample.extend(returns[start: start + block_size])
        sample = sample[:n]
        # Reconstruir equity curve bootstrap
        eq = initial
        boot_curve: list[tuple[date, Decimal]] = []
        for i, r in enumerate(sample):
            eq = eq * (_ONE + r)
            d = equity_curve[min(i, len(equity_curve) - 1)][0]
            boot_curve.append((d, eq))
        if not boot_curve:
            continue
        boot_cagr = compute_cagr(initial, boot_curve[-1][1], n_trading_days)
        boot_mdd = compute_max_drawdown(boot_curve)
        bootstrapped.append(compute_calmar(boot_cagr, boot_mdd))
    return bootstrapped


def _block_bootstrap_max_drawdown(
    returns: list[Decimal],
    equity_curve: list[tuple[date, Decimal]],
    block_size: int = 21,
    n_samples: int = 1000,
    seed: int = 42,
) -> list[Decimal]:
    n = len(returns)
    if n < block_size * 2:
        return []
    rng = random.Random(seed)
    n_blocks = math.ceil(n / block_size)
    bootstrapped: list[Decimal] = []
    initial = equity_curve[0][1] if equity_curve else _ONE

    for _ in range(n_samples):
        sample: list[Decimal] = []
        for _ in range(n_blocks):
            start = rng.randint(0, n - block_size)
            sample.extend(returns[start: start + block_size])
        sample = sample[:n]
        eq = initial
        boot_curve: list[tuple[date, Decimal]] = []
        for i, r in enumerate(sample):
            eq = eq * (_ONE + r)
            d = equity_curve[min(i, len(equity_curve) - 1)][0]
            boot_curve.append((d, eq))
        if boot_curve:
            bootstrapped.append(compute_max_drawdown(boot_curve))
    return bootstrapped


def _percentile(values: list[Decimal], p: float) -> Decimal:
    """Percentil p (0-100) de una lista de Decimals."""
    if not values:
        return _ZERO
    sorted_vals = sorted(values)
    idx = (len(sorted_vals) - 1) * p / 100.0
    lo = int(idx)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = Decimal(str(idx - lo))
    return sorted_vals[lo] + frac * (sorted_vals[hi] - sorted_vals[lo])


def compute_bootstrap_ci(
    samples: list[Decimal],
    n_samples: int = 1000,
) -> BootstrapCI:
    lower = _percentile(samples, 2.5)
    upper = _percentile(samples, 97.5)
    return BootstrapCI(lower=lower, upper=upper, n_samples=n_samples)


# ---------------------------------------------------------------------------
# Funcion principal
# ---------------------------------------------------------------------------

def compute_full_report(
    *,
    strategy_id: str,
    equity_curve: list[tuple[date, Decimal]],
    fills: list[Fill],
    initial_equity: Decimal,
    total_commission: Decimal,
    bootstrap: bool = True,
    bootstrap_n_samples: int = 1000,
    bootstrap_block_size: int = 21,
    bootstrap_seed: int = 42,
) -> BacktestReport:
    """Calcula el informe completo a partir de la equity curve y los fills.

    Args:
        strategy_id:   identificador de la estrategia.
        equity_curve:  lista (date, NAV) del motor.
        fills:         todos los fills ejecutados.
        initial_equity: capital inicial.
        total_commission: suma de comisiones (de GoldenMetrics o fills).
        bootstrap:     si True, calcula intervalos de confianza.
        bootstrap_n_samples: numero de muestras bootstrap.
        bootstrap_block_size: tamano del bloque bootstrap (dias habiles).
        bootstrap_seed: semilla para reproducibilidad.
    """
    if not equity_curve:
        raise ValueError("equity_curve no puede estar vacia")

    n_days = len(equity_curve)
    start_date = equity_curve[0][0]
    end_date = equity_curve[-1][0]
    final_equity = equity_curve[-1][1]

    returns = _daily_returns(equity_curve)

    # --- Retorno ---
    total_return = compute_total_return(initial_equity, final_equity)
    cagr = compute_cagr(initial_equity, final_equity, n_days)

    # --- Riesgo ---
    vol = compute_volatility(returns)
    mdd = compute_max_drawdown(equity_curve)
    mdd_dur = compute_max_drawdown_duration(equity_curve)

    # --- Ratios ---
    sharpe = compute_sharpe(returns)
    sortino = compute_sortino(returns)
    calmar = compute_calmar(cagr, mdd)

    # --- Trades ---
    trade_stats = compute_trade_stats(fills, initial_equity)

    # --- Turnover ---
    turnover = compute_turnover(fills, equity_curve, n_days)

    # --- Exposicion ---
    avg_exp, max_exp = compute_exposure(equity_curve, fills)

    # --- Costes ---
    costs = CostBreakdownReport(
        total_commission=total_commission,
        total_spread=_ZERO,    # no disponible desagregado sin hook especifico
        total_slippage=_ZERO,  # idem
        total_costs=total_commission,
    )

    # --- Por instrumento ---
    by_instrument = tuple(compute_by_instrument(fills))

    # --- P&L por periodo ---
    monthly = tuple(compute_period_pnl(equity_curve, "monthly"))
    annual = tuple(compute_period_pnl(equity_curve, "annual"))

    # --- Bootstrap CIs ---
    sharpe_ci: BootstrapCI | None = None
    sortino_ci: BootstrapCI | None = None
    calmar_ci: BootstrapCI | None = None
    mdd_ci: BootstrapCI | None = None

    if bootstrap and len(returns) >= bootstrap_block_size * 2:
        sh_samples = _block_bootstrap_sharpe(
            returns, bootstrap_block_size, bootstrap_n_samples, bootstrap_seed
        )
        if sh_samples:
            sharpe_ci = compute_bootstrap_ci(sh_samples, bootstrap_n_samples)

        so_samples = _block_bootstrap_sortino(
            returns, bootstrap_block_size, bootstrap_n_samples, bootstrap_seed + 1
        )
        if so_samples:
            sortino_ci = compute_bootstrap_ci(so_samples, bootstrap_n_samples)

        cal_samples = _block_bootstrap_calmar(
            returns, equity_curve, n_days,
            bootstrap_block_size, bootstrap_n_samples, bootstrap_seed + 2
        )
        if cal_samples:
            calmar_ci = compute_bootstrap_ci(cal_samples, bootstrap_n_samples)

        mdd_samples = _block_bootstrap_max_drawdown(
            returns, equity_curve,
            bootstrap_block_size, bootstrap_n_samples, bootstrap_seed + 3
        )
        if mdd_samples:
            mdd_ci = compute_bootstrap_ci(mdd_samples, bootstrap_n_samples)

    def _q(v: Decimal, prec: str = "0.0001") -> Decimal:
        return v.quantize(Decimal(prec), rounding=ROUND_HALF_UP)

    return BacktestReport(
        strategy_id=strategy_id,
        start_date=start_date,
        end_date=end_date,
        n_trading_days=n_days,
        total_return=_q(total_return),
        cagr=_q(cagr),
        volatility=_q(vol),
        max_drawdown=_q(mdd),
        max_drawdown_duration_days=mdd_dur,
        sharpe=_q(sharpe),
        sortino=_q(sortino),
        calmar=_q(calmar),
        n_trades=int(trade_stats["n_trades"]),
        win_rate=_q(trade_stats["win_rate"]),
        profit_factor=_q(trade_stats["profit_factor"]),
        avg_win_eur=_q(trade_stats["avg_win_eur"], "0.01"),
        avg_loss_eur=_q(trade_stats["avg_loss_eur"], "0.01"),
        avg_win_pct=_q(trade_stats["avg_win_pct"]),
        avg_loss_pct=_q(trade_stats["avg_loss_pct"]),
        turnover_annual=_q(turnover),
        avg_exposure=_q(avg_exp),
        max_exposure=_q(max_exp),
        costs=costs,
        sharpe_ci=sharpe_ci,
        sortino_ci=sortino_ci,
        calmar_ci=calmar_ci,
        max_drawdown_ci=mdd_ci,
        by_instrument=by_instrument,
        monthly_pnl=monthly,
        annual_pnl=annual,
    )
