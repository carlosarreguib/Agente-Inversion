"""Análisis de sensibilidad de parámetros — T9 Parte A.

Ejecuta 5×3×3×3 = 135 configuraciones del momentum y registra cada una en
trials.db. El objetivo no es encontrar la mejor config, sino comprobar que los
resultados son cualitativamente estables ante variaciones de parámetros.

Parámetros variados:
  lookback_months : [6, 9, 12, 15, 18]
  skip_months     : [0, 1, 2]
  rebalance_freq  : ["weekly", "biweekly", "monthly"]
  top_quantile    : [0.10, 0.20, 0.30]

Umbral de robustez (criterio cuantitativo B):
  >= 60 % de configs con Sharpe OOS > 0 Y |MaxDD| < 50 %

Invariantes heredadas (CLAUDE.md):
  - Ningún resultado se interpola ni corrige silenciosamente.
  - Todo se registra en trials.db, incluidas las configs que fallan.
  - Datos sintéticos → no hay red, no hay proveedor real.
"""
from __future__ import annotations

import itertools
import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from qtrader.research.dsr import compute_dsr
from qtrader.research.trials_db import TrialRecord, TrialsDB, now_utc

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Parámetros del barrido
# ---------------------------------------------------------------------------

LOOKBACK_MONTHS: list[int] = [6, 9, 12, 15, 18]
SKIP_MONTHS: list[int] = [0, 1, 2]
REBALANCE_FREQS: list[str] = ["weekly", "biweekly", "monthly"]
TOP_QUANTILES: list[float] = [0.10, 0.20, 0.30]

ROBUSTNESS_MIN_PASS_FRACTION = 0.60  # ≥60 % deben pasar los filtros
ROBUSTNESS_SHARPE_MIN = 0.0
ROBUSTNESS_MAXDD_MAX = 0.50          # |MaxDD| < 50 %

_TRADING_DAYS_PER_MONTH = 21


@dataclass(frozen=True)
class SensitivityConfig:
    """Una configuración del barrido de sensibilidad."""

    lookback_months: int
    skip_months: int
    rebalance_freq: str
    top_quantile: float

    @property
    def lookback_bars(self) -> int:
        return self.lookback_months * _TRADING_DAYS_PER_MONTH

    @property
    def skip_bars(self) -> int:
        return self.skip_months * _TRADING_DAYS_PER_MONTH

    def to_params_dict(self) -> dict[str, str]:
        return {
            "lookback_months": str(self.lookback_months),
            "skip_months": str(self.skip_months),
            "rebalance_freq": self.rebalance_freq,
            "top_quantile": str(self.top_quantile),
        }


@dataclass(frozen=True)
class SensitivityResult:
    """Resultado de una configuración individual."""

    config: SensitivityConfig
    trial_id: str
    sharpe_oos: Decimal | None
    max_drawdown: Decimal | None
    dsr: Decimal | None
    n_trials_total: int
    status: str
    error_message: str | None = None

    @property
    def passes_robustness(self) -> bool:
        if self.sharpe_oos is None or self.max_drawdown is None:
            return False
        return (
            float(self.sharpe_oos) > ROBUSTNESS_SHARPE_MIN
            and abs(float(self.max_drawdown)) < ROBUSTNESS_MAXDD_MAX
        )


@dataclass(frozen=True)
class SensitivityReport:
    """Informe del análisis de sensibilidad completo."""

    strategy_id: str
    results: tuple[SensitivityResult, ...]
    n_configs: int
    n_passed: int
    pass_fraction: float
    is_robust: bool          # True si pass_fraction >= ROBUSTNESS_MIN_PASS_FRACTION
    as_of: date

    @property
    def failed_configs(self) -> list[SensitivityResult]:
        return [r for r in self.results if not r.passes_robustness]

    @property
    def passed_configs(self) -> list[SensitivityResult]:
        return [r for r in self.results if r.passes_robustness]

    @property
    def sharpes_oos(self) -> list[Decimal]:
        return [r.sharpe_oos for r in self.results if r.sharpe_oos is not None]


# ---------------------------------------------------------------------------
# Protocolos inyectables (para tests sin red)
# ---------------------------------------------------------------------------

@runtime_checkable
class SensitivityBacktestRunner(Protocol):
    """Interfaz para ejecutar un backtest de una config de sensibilidad."""

    def run(
        self,
        config: SensitivityConfig,
        strategy_id: str,
        trading_days: list[date],
    ) -> tuple[Decimal, Decimal]:
        """Devuelve (sharpe_oos, max_drawdown_oos). Lanza si falla."""
        ...


# ---------------------------------------------------------------------------
# Motor del análisis
# ---------------------------------------------------------------------------

def all_configs() -> list[SensitivityConfig]:
    """Genera las 135 combinaciones de parámetros."""
    configs = []
    for lookback, skip, freq, quantile in itertools.product(
        LOOKBACK_MONTHS, SKIP_MONTHS, REBALANCE_FREQS, TOP_QUANTILES
    ):
        configs.append(SensitivityConfig(
            lookback_months=lookback,
            skip_months=skip,
            rebalance_freq=freq,
            top_quantile=quantile,
        ))
    return configs


def run_sensitivity_sweep(
    *,
    trials_db: TrialsDB,
    backtest_runner: SensitivityBacktestRunner,
    trading_days: list[date],
    strategy_id: str = "momentum_sensitivity",
    as_of: date | None = None,
) -> SensitivityReport:
    """Ejecuta el barrido completo de 135 configuraciones.

    Registra cada config en trials.db (RUNNING → COMPLETED o FAILED).
    No interrumpe ante errores: una config que falla se registra como FAILED.
    """
    _as_of = as_of or date.today()
    configs = all_configs()
    results: list[SensitivityResult] = []

    _log.info(
        "Iniciando sensitivity sweep: %d configs, strategy_id=%s",
        len(configs), strategy_id,
    )

    for i, cfg in enumerate(configs):
        trial_id = str(uuid.uuid4())
        params = cfg.to_params_dict()
        ts = now_utc()

        # Extraer fechas del periodo de test OOS (último 20% del rango)
        n_test = max(21, len(trading_days) // 5)
        n_train = len(trading_days) - n_test
        if n_train < 1:
            n_train = 1
            n_test = len(trading_days) - 1

        train_days = trading_days[:n_train]
        test_days = trading_days[n_train:]
        train_start = train_days[0] if train_days else trading_days[0]
        train_end = train_days[-1] if train_days else trading_days[0]
        test_start = test_days[0] if test_days else trading_days[-1]
        test_end = test_days[-1] if test_days else trading_days[-1]

        # Registrar RUNNING
        trials_db.insert(TrialRecord(
            trial_id=trial_id,
            timestamp=ts,
            strategy_id=strategy_id,
            parameters=params,
            fold_id=i,
            train_start=train_start,
            train_end=train_end,
            test_start=test_start,
            test_end=test_end,
            sharpe_is=None,
            sharpe_oos=None,
            max_drawdown_oos=None,
            num_trades_oos=None,
            status="RUNNING",
        ))

        try:
            sharpe_oos, max_drawdown = backtest_runner.run(
                config=cfg,
                strategy_id=strategy_id,
                trading_days=trading_days,
            )

            # Calcular DSR sobre los trials acumulados
            completed = trials_db.fetch_completed(strategy_id)
            sharpes_db = [r.sharpe_oos for r in completed if r.sharpe_oos is not None]
            t_obs = len(test_days)
            dsr_result = compute_dsr(sharpes_db, t_obs)

            completed_id = str(uuid.uuid4())
            trials_db.insert(TrialRecord(
                trial_id=completed_id,
                timestamp=now_utc(),
                strategy_id=strategy_id,
                parameters=params,
                fold_id=i,
                train_start=train_start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
                sharpe_is=sharpe_oos,        # IS = mismo período simplificado
                sharpe_oos=sharpe_oos,
                max_drawdown_oos=max_drawdown,
                num_trades_oos=None,
                status="COMPLETED",
            ))

            n_total = trials_db.count(strategy_id)
            results.append(SensitivityResult(
                config=cfg,
                trial_id=trial_id,
                sharpe_oos=sharpe_oos,
                max_drawdown=max_drawdown,
                dsr=dsr_result.dsr,
                n_trials_total=n_total,
                status="COMPLETED",
            ))
            _log.debug(
                "Config %d/%d OK: sharpe=%.4f mdd=%.4f",
                i + 1, len(configs), float(sharpe_oos), float(max_drawdown),
            )

        except Exception as exc:  # noqa: BLE001
            error_msg = f"{type(exc).__name__}: {exc}"
            failed_id = str(uuid.uuid4())
            trials_db.insert(TrialRecord(
                trial_id=failed_id,
                timestamp=now_utc(),
                strategy_id=strategy_id,
                parameters=params,
                fold_id=i,
                train_start=train_start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
                sharpe_is=None,
                sharpe_oos=None,
                max_drawdown_oos=None,
                num_trades_oos=None,
                status="FAILED",
                error_message=error_msg,
            ))
            n_total = trials_db.count(strategy_id)
            results.append(SensitivityResult(
                config=cfg,
                trial_id=trial_id,
                sharpe_oos=None,
                max_drawdown=None,
                dsr=None,
                n_trials_total=n_total,
                status="FAILED",
                error_message=error_msg,
            ))
            _log.warning("Config %d/%d FAILED: %s", i + 1, len(configs), error_msg)

    n_passed = sum(1 for r in results if r.passes_robustness)
    n_configs = len(results)
    pass_fraction = n_passed / n_configs if n_configs > 0 else 0.0

    _log.info(
        "Sensitivity sweep completado: %d/%d configs robustas (%.1f%%)",
        n_passed, n_configs, pass_fraction * 100,
    )

    return SensitivityReport(
        strategy_id=strategy_id,
        results=tuple(results),
        n_configs=n_configs,
        n_passed=n_passed,
        pass_fraction=pass_fraction,
        is_robust=pass_fraction >= ROBUSTNESS_MIN_PASS_FRACTION,
        as_of=_as_of,
    )
