"""Toolset cerrado para el LLM Researcher (T8).

Lista exacta de herramientas que el LLM puede llamar. Todas son READ-ONLY.
Ninguna tiene efectos de escritura en brokers, agentes ni producción.

Herramientas disponibles (cerradas — no extensibles sin ADR):
  read_universe()                    → list[dict]
  read_trial_results(strategy_id)    → list[dict]
  read_backtest_report(trial_id)     → dict | None
  propose_experiment(config: dict)   → str  (experiment_id — no ejecuta nada)

Prohibidas (no existen en este módulo, nunca añadir sin ADR):
  submit_order, activate_kill_switch, modify_production_config,
  deploy_strategy, write_to_agent_db, write_to_paper_broker_db.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path  # noqa: TCH003
from typing import Any

from qtrader.data.universe import UniverseManager
from qtrader.research.trials_db import TrialsDB

_log = logging.getLogger(__name__)


@dataclass
class ExperimentProposal:
    """Propuesta de experimento registrada por el LLM.

    No ejecuta el backtest. El caller (researcher.py) lo ejecuta de forma
    independiente y determinista. Esto evita que el LLM pueda influir en
    el proceso de ejecución del backtest.
    """

    experiment_id: str
    config: dict[str, Any]


@dataclass
class ResearchToolset:
    """Conjunto de herramientas read-only disponibles para el LLM.

    Args:
        trials_db:      base de datos de trials (read-only en este contexto).
        universe_mgr:   gestor del universo point-in-time.
        as_of:          fecha de referencia para consultas point-in-time.
    """

    trials_db: TrialsDB
    universe_mgr: UniverseManager
    as_of: date
    _pending_experiments: list[ExperimentProposal] = field(default_factory=list, init=False)

    def read_universe(self) -> list[dict[str, Any]]:
        """Devuelve el universo de instrumentos activo en as_of."""
        instruments = self.universe_mgr.get_universe(self.as_of)
        return [
            {
                "symbol": inst.symbol,
                "name": inst.name,
                "category": inst.category.value,
                "exchange": inst.exchange,
                "currency": inst.currency,
                "declared_on": inst.declared_on.isoformat(),
            }
            for inst in instruments
        ]

    def read_trial_results(self, strategy_id: str) -> list[dict[str, Any]]:
        """Devuelve trials COMPLETED de una estrategia (solo campos relevantes).

        Los valores confidenciales de parámetros internos se exponen tal cual
        porque el LLM los usa para proponer variaciones. El acceso es read-only.
        """
        records = self.trials_db.fetch_completed(strategy_id)
        return [
            {
                "trial_id": r.trial_id,
                "fold_id": r.fold_id,
                "train_start": r.train_start.isoformat(),
                "train_end": r.train_end.isoformat(),
                "test_start": r.test_start.isoformat(),
                "test_end": r.test_end.isoformat(),
                "sharpe_is": float(r.sharpe_is) if r.sharpe_is is not None else None,
                "sharpe_oos": float(r.sharpe_oos) if r.sharpe_oos is not None else None,
                "max_drawdown_oos": (
                    float(r.max_drawdown_oos) if r.max_drawdown_oos is not None else None
                ),
                "num_trades_oos": r.num_trades_oos,
                "parameters": r.parameters,
            }
            for r in records
        ]

    def read_backtest_report(self, trial_id: str) -> dict[str, Any] | None:
        """Devuelve el resumen de un trial específico por ID."""
        all_records = self.trials_db.fetch_all()
        for r in all_records:
            if r.trial_id == trial_id:
                return {
                    "trial_id": r.trial_id,
                    "strategy_id": r.strategy_id,
                    "status": r.status,
                    "sharpe_is": float(r.sharpe_is) if r.sharpe_is is not None else None,
                    "sharpe_oos": float(r.sharpe_oos) if r.sharpe_oos is not None else None,
                    "max_drawdown_oos": (
                        float(r.max_drawdown_oos) if r.max_drawdown_oos is not None else None
                    ),
                    "parameters": r.parameters,
                }
        return None

    def propose_experiment(self, config: dict[str, Any]) -> str:
        """Registra la propuesta de experimento del LLM y devuelve un experiment_id.

        NO ejecuta el backtest. El sistema (researcher.py) ejecuta el backtest
        de forma independiente después de que el LLM termine su turno.
        Así el LLM no puede influir en el proceso de ejecución del backtest.
        """
        experiment_id = str(uuid.uuid4())
        proposal = ExperimentProposal(experiment_id=experiment_id, config=config)
        self._pending_experiments.append(proposal)
        _log.info("LLM propuso experimento %s: %s", experiment_id, json.dumps(config))
        return experiment_id

    def pop_pending_experiments(self) -> list[ExperimentProposal]:
        """Extrae y devuelve las propuestas pendientes (para que el sistema las ejecute)."""
        result = list(self._pending_experiments)
        self._pending_experiments.clear()
        return result

    def to_anthropic_tools(self) -> list[dict[str, Any]]:
        """Genera la definición de herramientas en formato Anthropic tool_use."""
        return [
            {
                "name": "read_universe",
                "description": (
                    "Lee el universo de instrumentos activo en la fecha de referencia. "
                    "Solo lectura."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
            {
                "name": "read_trial_results",
                "description": (
                    "Lee los resultados de trials completados para una estrategia. "
                    "Solo lectura."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "strategy_id": {
                            "type": "string",
                            "description": "ID de la estrategia a consultar",
                        }
                    },
                    "required": ["strategy_id"],
                },
            },
            {
                "name": "read_backtest_report",
                "description": "Lee el informe de un trial específico por su ID. Solo lectura.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "trial_id": {
                            "type": "string",
                            "description": "ID del trial a consultar",
                        }
                    },
                    "required": ["trial_id"],
                },
            },
            {
                "name": "propose_experiment",
                "description": (
                    "Propone una configuración de experimento para que el sistema la evalúe. "
                    "NO ejecuta el backtest — solo registra la propuesta. "
                    "El sistema ejecuta el backtest de forma independiente."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "config": {
                            "type": "object",
                            "description": "Configuración del experimento propuesto",
                        }
                    },
                    "required": ["config"],
                },
            },
        ]

    def dispatch_tool(self, tool_name: str, tool_input: dict[str, Any]) -> Any:
        """Despacha una llamada a herramienta por nombre."""
        if tool_name == "read_universe":
            return self.read_universe()
        if tool_name == "read_trial_results":
            strategy_id = str(tool_input.get("strategy_id", ""))
            return self.read_trial_results(strategy_id)
        if tool_name == "read_backtest_report":
            trial_id = str(tool_input.get("trial_id", ""))
            return self.read_backtest_report(trial_id)
        if tool_name == "propose_experiment":
            config = tool_input.get("config", {})
            if not isinstance(config, dict):
                config = {}
            return self.propose_experiment(config)
        raise ValueError(f"Herramienta desconocida: {tool_name!r}")


def load_toolset(
    trials_db_path: Path,
    config_dir: Path | None = None,
    as_of: date | None = None,
) -> ResearchToolset:
    """Construye un ResearchToolset listo para usar."""
    from pathlib import Path as _Path

    _config_dir = config_dir or (_Path(__file__).resolve().parents[4] / "config")
    _as_of = as_of or date.today()
    trials_db = TrialsDB(trials_db_path)
    universe_mgr = UniverseManager(config_dir=_config_dir)
    return ResearchToolset(
        trials_db=trials_db,
        universe_mgr=universe_mgr,
        as_of=_as_of,
    )
