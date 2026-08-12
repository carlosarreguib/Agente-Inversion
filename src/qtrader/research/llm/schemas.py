"""Schemas Pydantic estrictos para los outputs del LLM Researcher (T8).

Invariante: toda salida del LLM debe parsearse contra uno de estos schemas.
Si no parsea → rechazar, loggear, no usar. (CLAUDE.md §2.9)

Los schemas usan frozen=True para que no sean mutables tras creación.
"""
from __future__ import annotations

from datetime import date, datetime  # noqa: TCH003
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class HypothesisOutput(BaseModel):
    """Hipótesis de investigación propuesta por el LLM."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hypothesis: str = Field(max_length=500)
    rationale: str = Field(max_length=1000)
    suggested_parameters: dict[str, float | int | str]
    confidence: Literal["LOW", "MEDIUM", "HIGH"]
    requires_new_data: bool


class ExperimentConfig(BaseModel):
    """Configuración de un experimento propuesta por el LLM.

    El LLM propone; el sistema ejecuta el backtest de forma independiente.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_id: str = Field(min_length=1, max_length=100)
    parameters: dict[str, float | int | str]
    train_start: date
    train_end: date
    test_start: date
    test_end: date
    hypothesis_id: str = Field(min_length=1, max_length=100)


class ResearchProposal(BaseModel):
    """Propuesta de investigación lista para aprobación humana.

    El LLM no crea este objeto directamente — lo crea el sistema tras
    validar que el backtest supera los umbrales DSR y MaxDD.
    status siempre es PENDING_APPROVAL: la aprobación la da el humano.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    proposal_id: str = Field(min_length=36, max_length=36)  # UUID
    strategy_id: str = Field(min_length=1, max_length=100)
    parameters: dict[str, float | int | str]
    backtest_sharpe_oos: float
    backtest_maxdd: float
    dsr: float
    n_trials: int
    status: Literal["PENDING_APPROVAL"]
    created_at: datetime
    summary: str = Field(max_length=2000)
