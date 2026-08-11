"""Orquestador de walk-forward: ejecuta el backtest en cada fold y registra trials.

Responsabilidades:
  1. Recibe un proveedor de datos, un UniverseManager stub (o real) y los
     componentes de la estrategia a evaluar.
  2. Para cada fold generado por generate_folds():
     a. Inserta un trial con status=RUNNING.
     b. Ejecuta BacktestEngine con trading_days=fold.train_days (IS).
     c. Ejecuta BacktestEngine con trading_days=fold.test_days (OOS).
     d. Calcula metricas IS y OOS con compute_metrics().
     e. Inserta trial COMPLETED con sharpe_is / sharpe_oos / etc.
        Si BacktestEngine lanza, inserta trial FAILED con error_message.
  3. Al finalizar todos los folds, calcula DSR sobre los Sharpes OOS y lo
     devuelve en WalkForwardReport.

Nota: runner.py NO importa brokers/ ni agents/ (contrato import-linter).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from qtrader.backtesting.engine import (  # noqa: TCH001
    BacktestEngine,
    EngineObserver,
    RiskEngineProtocol,
)
from qtrader.backtesting.metrics import GoldenMetrics, compute_metrics
from qtrader.data.provider import MarketDataProvider  # noqa: TCH001
from qtrader.research.dsr import DSRResult, compute_dsr
from qtrader.research.trials_db import TrialRecord, TrialsDB, now_utc
from qtrader.research.walk_forward import Fold, WalkForwardConfig, generate_folds

if TYPE_CHECKING:
    from datetime import date

_ZERO = Decimal("0")


@runtime_checkable
class UniverseManagerProtocol(Protocol):
    """Subset del protocolo de UniverseManager necesario para el runner."""

    def get_universe(self, as_of: date) -> list[Any]: ...


@dataclass(frozen=True)
class FoldResult:
    """Resultado de un fold individual."""

    fold: Fold
    trial_id: str
    metrics_is: GoldenMetrics | None
    metrics_oos: GoldenMetrics | None
    status: str
    error_message: str | None = None


@dataclass(frozen=True)
class WalkForwardReport:
    """Informe completo de una corrida walk-forward."""

    strategy_id: str
    fold_results: tuple[FoldResult, ...]
    dsr_result: DSRResult
    n_trials_total: int         # segun la DB (todos los trials de la estrategia)

    @property
    def completed_folds(self) -> list[FoldResult]:
        return [f for f in self.fold_results if f.status == "COMPLETED"]

    @property
    def sharpes_oos(self) -> list[Decimal]:
        return [
            f.metrics_oos.sharpe_ratio
            for f in self.completed_folds
            if f.metrics_oos is not None
        ]

    @property
    def best_fold(self) -> FoldResult | None:
        completed = self.completed_folds
        if not completed:
            return None
        return max(
            (f for f in completed if f.metrics_oos is not None),
            key=lambda f: f.metrics_oos.sharpe_ratio,  # type: ignore[union-attr]
            default=None,
        )

    @property
    def worst_fold(self) -> FoldResult | None:
        completed = self.completed_folds
        if not completed:
            return None
        return min(
            (f for f in completed if f.metrics_oos is not None),
            key=lambda f: f.metrics_oos.sharpe_ratio,  # type: ignore[union-attr]
            default=None,
        )

    @property
    def sharpe_oos_std(self) -> Decimal:
        """Desviacion estandar del Sharpe OOS entre folds (estabilidad)."""
        sharpes = self.sharpes_oos
        if len(sharpes) < 2:
            return _ZERO
        n = Decimal(len(sharpes))
        mean = sum(sharpes, _ZERO) / n
        variance = sum((s - mean) ** 2 for s in sharpes) / (n - 1)
        import math
        return Decimal(str(round(math.sqrt(float(variance)), 6)))


class WalkForwardRunner:
    """Ejecuta walk-forward sobre una estrategia y registra todos los trials.

    Args:
        provider:       proveedor de datos de mercado.
        universe_mgr:   gestor del universo (point-in-time).
        strategy_factory: callable sin args que devuelve una instancia nueva
                          de Strategy para cada fold (estrategias son stateful).
        portfolio_factory: callable sin args que devuelve PortfolioConstructor.
        risk:           risk engine (se reutiliza entre folds; debe ser stateless).
        broker_factory: callable sin args que devuelve SimBroker.
        strategy_id:    identificador de la estrategia.
        parameters:     parametros de la estrategia (para trazabilidad en DB).
        trading_days:   lista completa de dias habiles para el walk-forward.
        db:             instancia de TrialsDB para persistir los resultados.
        wf_config:      parametros de ventana del walk-forward.
        observers:      observers adicionales del motor (opcional).
        initial_equity: capital de simulacion por fold.
    """

    def __init__(
        self,
        *,
        provider: MarketDataProvider,
        universe_mgr: UniverseManagerProtocol,
        strategy_factory: Any,           # () -> Strategy
        portfolio_factory: Any,          # () -> PortfolioConstructor
        risk: RiskEngineProtocol,
        broker_factory: Any,             # () -> SimBroker
        strategy_id: str,
        parameters: dict[str, str],
        trading_days: list[date],
        db: TrialsDB,
        wf_config: WalkForwardConfig | None = None,
        observers: list[EngineObserver] | None = None,
        initial_equity: Decimal = Decimal("15000"),
    ) -> None:
        self._provider = provider
        self._universe_mgr = universe_mgr
        self._strategy_factory = strategy_factory
        self._portfolio_factory = portfolio_factory
        self._risk = risk
        self._broker_factory = broker_factory
        self._strategy_id = strategy_id
        self._parameters = parameters
        self._trading_days = trading_days
        self._db = db
        self._wf_config = wf_config or WalkForwardConfig()
        self._observers = observers or []
        self._initial_equity = initial_equity

    def run(self) -> WalkForwardReport:
        """Ejecuta todos los folds y devuelve el informe completo."""
        folds = generate_folds(self._trading_days, self._wf_config)
        fold_results: list[FoldResult] = []

        for fold in folds:
            result = self._run_fold(fold)
            fold_results.append(result)

        # Calcular DSR sobre todos los Sharpes OOS registrados en DB
        all_completed = self._db.fetch_completed(self._strategy_id)
        sharpes_oos_db = [
            r.sharpe_oos for r in all_completed if r.sharpe_oos is not None
        ]
        t_obs = self._wf_config.test_days
        dsr_result = compute_dsr(sharpes_oos_db, t_obs)
        n_total = self._db.count(self._strategy_id)

        return WalkForwardReport(
            strategy_id=self._strategy_id,
            fold_results=tuple(fold_results),
            dsr_result=dsr_result,
            n_trials_total=n_total,
        )

    # ------------------------------------------------------------------
    # Privados
    # ------------------------------------------------------------------

    def _run_fold(self, fold: Fold) -> FoldResult:
        trial_id = str(uuid.uuid4())
        ts = now_utc()

        # Registrar como RUNNING antes de ejecutar
        self._db.insert(TrialRecord(
            trial_id=trial_id,
            timestamp=ts,
            strategy_id=self._strategy_id,
            parameters=self._parameters,
            fold_id=fold.fold_id,
            train_start=fold.train_start,
            train_end=fold.train_end,
            test_start=fold.test_start,
            test_end=fold.test_end,
            sharpe_is=None,
            sharpe_oos=None,
            max_drawdown_oos=None,
            num_trades_oos=None,
            status="RUNNING",
        ))

        try:
            # --- IS backtest (train, ya purgado) ---
            result_is = self._run_engine(list(fold.train_days))
            metrics_is = compute_metrics(result_is)

            # --- OOS backtest (test) ---
            result_oos = self._run_engine(list(fold.test_days))
            metrics_oos = compute_metrics(result_oos)

            # Registrar COMPLETED
            self._db.insert(TrialRecord(
                trial_id=str(uuid.uuid4()),   # nuevo ID para el registro COMPLETED
                timestamp=now_utc(),
                strategy_id=self._strategy_id,
                parameters=self._parameters,
                fold_id=fold.fold_id,
                train_start=fold.train_start,
                train_end=fold.train_end,
                test_start=fold.test_start,
                test_end=fold.test_end,
                sharpe_is=metrics_is.sharpe_ratio,
                sharpe_oos=metrics_oos.sharpe_ratio,
                max_drawdown_oos=metrics_oos.max_drawdown,
                num_trades_oos=metrics_oos.num_trades,
                status="COMPLETED",
            ))

            return FoldResult(
                fold=fold,
                trial_id=trial_id,
                metrics_is=metrics_is,
                metrics_oos=metrics_oos,
                status="COMPLETED",
            )

        except Exception as exc:  # noqa: BLE001
            error_msg = f"{type(exc).__name__}: {exc}"
            self._db.insert(TrialRecord(
                trial_id=str(uuid.uuid4()),
                timestamp=now_utc(),
                strategy_id=self._strategy_id,
                parameters=self._parameters,
                fold_id=fold.fold_id,
                train_start=fold.train_start,
                train_end=fold.train_end,
                test_start=fold.test_start,
                test_end=fold.test_end,
                sharpe_is=None,
                sharpe_oos=None,
                max_drawdown_oos=None,
                num_trades_oos=None,
                status="FAILED",
                error_message=error_msg,
            ))
            return FoldResult(
                fold=fold,
                trial_id=trial_id,
                metrics_is=None,
                metrics_oos=None,
                status="FAILED",
                error_message=error_msg,
            )

    def _run_engine(self, trading_days: list[date]) -> Any:

        strategy = self._strategy_factory()
        portfolio = self._portfolio_factory()
        broker = self._broker_factory()

        engine = BacktestEngine(
            provider=self._provider,
            universe_mgr=self._universe_mgr,  # type: ignore[arg-type]
            strategy=strategy,
            portfolio=portfolio,
            risk=self._risk,
            broker=broker,
            trading_days=trading_days,
            initial_equity=self._initial_equity,
            observers=self._observers,
        )
        return engine.run()
