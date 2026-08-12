"""LLMResearcher — agente offline de investigación con LLM (T8).

Invariantes críticas (CLAUDE.md §2):
  - El LLM solo puede llamar a las herramientas del toolset (read-only).
  - Toda salida del LLM se valida contra schemas Pydantic antes de usarse.
  - Si la validación falla → rechazar, loggear; no reintentar más de MAX_RETRIES.
  - El pipeline: LLM genera → sistema valida → sistema ejecuta backtest
    (no el LLM) → sistema evalúa → si pasa umbrales → propuesta humana.
  - Budget: max_tokens_per_run y max_runs_per_day se comprueban al inicio.
  - proposals.db es SEPARADA de trials.db.

Pipeline completo (sección 8 del spec):
  1. LLM genera HypothesisOutput (validado)
  2. Sistema ejecuta backtest con parámetros propuestos (determinista)
  3. Sistema ejecuta walk-forward
  4. Sistema calcula DSR con N acumulado de trials.db
  5. Si DSR > DSR_THRESHOLD y MaxDD < MAXDD_THRESHOLD:
       → crear ResearchProposal (PENDING_APPROVAL) en proposals.db
       → notificar via AlertManager
     Si no: registrar como completed, no crear propuesta
  6. La propuesta espera firma humana (`research approve`)
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import ValidationError

from qtrader.research.llm.prompt_builder import (
    build_hypothesis_prompt,
    get_system_prompt,
)
from qtrader.research.llm.proposals_db import ProposalRecord, ProposalsDB
from qtrader.research.llm.schemas import HypothesisOutput
from qtrader.research.llm.toolset import ResearchToolset  # noqa: TCH001

_log = logging.getLogger(__name__)

MAX_RETRIES = 2
DSR_THRESHOLD = 0.3
MAXDD_THRESHOLD = 0.35  # 35 % MaxDD máximo


@dataclass(frozen=True)
class BudgetConfig:
    max_tokens_per_run: int = 2000
    max_runs_per_day: int = 10
    budget_log_path: Path = field(default_factory=lambda: Path("data/research_budget.log"))


@dataclass(frozen=True)
class ResearchRunResult:
    """Resultado de una ejecución del investigador."""

    hypothesis_id: str
    hypothesis: HypothesisOutput | None
    proposal_id: str | None
    proposal_created: bool
    rejection_reason: str | None
    tokens_used: int
    runs_today: int


@runtime_checkable
class LLMClientProtocol(Protocol):
    """Protocolo mínimo para un cliente LLM compatible con Anthropic."""

    def create_message(
        self,
        *,
        model: str,
        max_tokens: int,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]: ...


class BudgetExceeded(Exception):
    pass


class LLMResearcher:
    """Orquesta el ciclo completo Research → Backtest → OOS → Validación → Propuesta.

    Args:
        toolset:        herramientas read-only disponibles para el LLM.
        llm_client:     cliente LLM (Anthropic o mock en tests).
        proposals_db:   base de datos de propuestas (SEPARADA de trials.db).
        backtest_runner: callable que ejecuta el backtest y devuelve BacktestRunResult.
        budget:         configuración de presupuesto de tokens y runs.
        model:          modelo LLM a usar.
        alert_manager:  gestor de alertas (opcional).
    """

    def __init__(
        self,
        *,
        toolset: ResearchToolset,
        llm_client: LLMClientProtocol,
        proposals_db: ProposalsDB,
        backtest_runner: BacktestRunnerProtocol,
        budget: BudgetConfig | None = None,
        model: str = "claude-sonnet-4-6",
        alert_manager: Any | None = None,
    ) -> None:
        self._toolset = toolset
        self._llm_client = llm_client
        self._proposals_db = proposals_db
        self._backtest_runner = backtest_runner
        self._budget = budget or BudgetConfig()
        self._model = model
        self._alert_manager = alert_manager

    def run(self, hypothesis: str) -> ResearchRunResult:
        """Ejecuta el pipeline completo para una hipótesis."""
        hypothesis_id = str(uuid.uuid4())
        _log.info("Researcher iniciado. hypothesis_id=%s", hypothesis_id)

        runs_today = self._count_runs_today()
        if runs_today >= self._budget.max_runs_per_day:
            msg = f"Budget diario agotado ({runs_today}/{self._budget.max_runs_per_day} runs)"
            _log.error(msg)
            raise BudgetExceeded(msg)

        # --- Paso 1: LLM genera HypothesisOutput ---
        hypothesis_output, tokens_step1 = self._generate_hypothesis(hypothesis, hypothesis_id)
        if hypothesis_output is None:
            self._log_run(hypothesis_id, tokens_step1)
            return ResearchRunResult(
                hypothesis_id=hypothesis_id,
                hypothesis=None,
                proposal_id=None,
                proposal_created=False,
                rejection_reason="LLM no generó HypothesisOutput válido",
                tokens_used=tokens_step1,
                runs_today=runs_today + 1,
            )

        # --- Paso 2-4: Sistema ejecuta backtest + walk-forward + DSR ---
        strategy_id_val = hypothesis_output.suggested_parameters.get("strategy_id", "momentum")
        backtest_result = self._backtest_runner.run(
            strategy_id=str(strategy_id_val),
            parameters=hypothesis_output.suggested_parameters,
        )

        # --- Paso 5: Evaluar umbrales ---
        sharpe_oos = backtest_result.sharpe_oos
        maxdd = backtest_result.max_drawdown
        dsr = backtest_result.dsr
        n_trials = backtest_result.n_trials

        _log.info(
            "Backtest completado: sharpe_oos=%.4f maxdd=%.4f dsr=%.4f n_trials=%d",
            sharpe_oos, maxdd, dsr, n_trials,
        )

        if dsr < DSR_THRESHOLD or abs(maxdd) > MAXDD_THRESHOLD:
            reason = (
                f"DSR={dsr:.4f} < {DSR_THRESHOLD} o |MaxDD|={abs(maxdd):.4f} > {MAXDD_THRESHOLD}"
            )
            _log.info("Propuesta rechazada: %s", reason)
            self._log_run(hypothesis_id, tokens_step1)
            return ResearchRunResult(
                hypothesis_id=hypothesis_id,
                hypothesis=hypothesis_output,
                proposal_id=None,
                proposal_created=False,
                rejection_reason=reason,
                tokens_used=tokens_step1,
                runs_today=runs_today + 1,
            )

        # --- Paso 6: Crear propuesta PENDING_APPROVAL ---
        proposal_id = str(uuid.uuid4())
        summary = (
            f"Hipótesis: {hypothesis_output.hypothesis}\n"
            f"Confianza LLM: {hypothesis_output.confidence}\n"
            f"Parámetros: {json.dumps(hypothesis_output.suggested_parameters)}\n"
            f"Sharpe OOS: {sharpe_oos:.4f}  MaxDD: {maxdd:.4f}"
            f"  DSR: {dsr:.4f}  N trials: {n_trials}"
        )
        proposal_record = ProposalRecord(
            proposal_id=proposal_id,
            strategy_id=str(hypothesis_output.suggested_parameters.get("strategy_id", "momentum")),
            parameters={
                k: v for k, v in hypothesis_output.suggested_parameters.items()
                if isinstance(v, (float, int, str))
            },
            sharpe_oos=sharpe_oos,
            maxdd=maxdd,
            dsr=dsr,
            n_trials=n_trials,
            status="PENDING_APPROVAL",
            created_at=datetime.now(UTC),
            summary=summary[:2000],
        )
        self._proposals_db.insert(proposal_record)
        _log.info("Propuesta creada: %s (PENDING_APPROVAL)", proposal_id)

        if self._alert_manager is not None and hasattr(self._alert_manager, "fire"):
            import contextlib
            with contextlib.suppress(Exception):
                self._alert_manager.fire(
                    f"Nueva propuesta de investigación: {proposal_id}",
                )

        self._log_run(hypothesis_id, tokens_step1)
        return ResearchRunResult(
            hypothesis_id=hypothesis_id,
            hypothesis=hypothesis_output,
            proposal_id=proposal_id,
            proposal_created=True,
            rejection_reason=None,
            tokens_used=tokens_step1,
            runs_today=runs_today + 1,
        )

    # ------------------------------------------------------------------
    # Privados
    # ------------------------------------------------------------------

    def _generate_hypothesis(
        self, hypothesis: str, hypothesis_id: str
    ) -> tuple[HypothesisOutput | None, int]:
        """Llama al LLM y valida el output contra HypothesisOutput."""
        context = {
            "universe": self._toolset.read_universe(),
            "trial_summary": "Consulta read_trial_results para más detalle",
        }
        user_prompt = build_hypothesis_prompt(hypothesis, context, self._toolset.as_of)
        system_prompt = get_system_prompt()
        tools = self._toolset.to_anthropic_tools()

        messages: list[dict[str, Any]] = [{"role": "user", "content": user_prompt}]

        tokens_total = 0
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = self._llm_client.create_message(
                    model=self._model,
                    max_tokens=self._budget.max_tokens_per_run,
                    system=system_prompt,
                    messages=messages,
                    tools=tools,
                )
            except Exception as exc:  # noqa: BLE001
                _log.error("Error llamando al LLM (intento %d): %s", attempt + 1, exc)
                return None, tokens_total

            tokens_used = response.get("usage", {}).get("output_tokens", 0)
            tokens_total += tokens_used
            _log.info("LLM respondió (intento %d), tokens_output=%d", attempt + 1, tokens_used)

            # Manejar tool_use en la respuesta
            content = response.get("content", [])
            tool_results = self._dispatch_tool_calls(content)

            # Buscar texto JSON en la respuesta
            raw_text = _extract_text(content)
            if not raw_text and not tool_results:
                _log.warning("LLM no devolvió texto (intento %d)", attempt + 1)
                continue

            if raw_text:
                validated = _parse_hypothesis(raw_text)
                if validated is not None:
                    return validated, tokens_total
                _log.warning("Output LLM no pasó validación (intento %d)", attempt + 1)

            # Si hay tool_use, añadir resultados y continuar
            if tool_results:
                messages = _append_tool_results(messages, content, tool_results)
                messages.append({
                    "role": "user",
                    "content": "Ahora genera el JSON de HypothesisOutput.",
                })
                continue

            if attempt >= MAX_RETRIES:
                break

        return None, tokens_total

    def _dispatch_tool_calls(
        self, content: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Despacha llamadas a herramientas del LLM y devuelve los resultados."""
        results = []
        for block in content:
            if block.get("type") != "tool_use":
                continue
            tool_name = block.get("name", "")
            tool_input = block.get("input", {})
            tool_id = block.get("id", "")
            try:
                result = self._toolset.dispatch_tool(tool_name, tool_input)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                })
                _log.debug("Tool %s ejecutada ok", tool_name)
            except ValueError as exc:
                _log.warning("Tool desconocida o error: %s → %s", tool_name, exc)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": f"Error: {exc}",
                    "is_error": True,
                })
        return results

    def _count_runs_today(self) -> int:
        log_path = self._budget.budget_log_path
        if not log_path.exists():
            return 0
        today = date.today().isoformat()
        count = 0
        try:
            with log_path.open(encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith(today):
                        count += 1
        except OSError:
            pass
        return count

    def _log_run(self, hypothesis_id: str, tokens: int) -> None:
        log_path = self._budget.budget_log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = f"{date.today().isoformat()} hypothesis_id={hypothesis_id} tokens={tokens}\n"
        try:
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(entry)
        except OSError as exc:
            _log.error("No se pudo escribir budget log: %s", exc)


@runtime_checkable
class BacktestRunnerProtocol(Protocol):
    """Protocolo para ejecutar el backtest desde el researcher."""

    def run(
        self,
        strategy_id: str,
        parameters: dict[str, float | int | str],
    ) -> BacktestRunResult: ...


@dataclass(frozen=True)
class BacktestRunResult:
    """Resultado de un backtest ejecutado por el researcher."""

    sharpe_oos: float
    max_drawdown: float
    dsr: float
    n_trials: int


# ------------------------------------------------------------------
# Helpers privados
# ------------------------------------------------------------------

def _extract_text(content: list[dict[str, Any]]) -> str:
    """Extrae texto plano de los bloques de contenido de la respuesta."""
    parts = []
    for block in content:
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts).strip()


def _parse_hypothesis(raw: str) -> HypothesisOutput | None:
    """Parsea texto crudo como HypothesisOutput. Devuelve None si falla."""
    raw = raw.strip()
    # Intentar extraer JSON si hay texto alrededor
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start < 0 or end <= start:
        _log.warning("No se encontró JSON en output del LLM")
        return None
    candidate = raw[start:end]
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        _log.warning("JSON inválido en output del LLM: %s", exc)
        return None
    try:
        return HypothesisOutput.model_validate(data)
    except ValidationError as exc:
        _log.warning("Schema HypothesisOutput no válido: %s", exc)
        return None


def _append_tool_results(
    messages: list[dict[str, Any]],
    assistant_content: list[dict[str, Any]],
    tool_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Añade la respuesta del asistente y los resultados de tools al historial."""
    return [
        *messages,
        {"role": "assistant", "content": assistant_content},
        {"role": "user", "content": tool_results},
    ]
