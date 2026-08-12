"""Risk Engine package (T4.2).

Exports publicos para uso externo:
  - RiskConfig       — parametros de configuracion
  - RiskEngine       — evaluador puro de ordenes
  - RiskLevel        — enum de nivel de riesgo
  - RiskDecision     — resultado completo de evaluate()
  - CurrentPortfolio — estado del portfolio en la evaluacion
  - ProposedOrder    — orden propuesta
  - MarketState      — estado del mercado
  - PortfolioRiskMetrics — metricas calculadas
  - ApprovedOrder / RejectedOrder — resultados por orden
  - PositionSnapshot — snapshot de una posicion
  - LEVEL_MULTIPLIER — multiplicadores de tamano por nivel
"""
from qtrader.risk.config import RiskConfig
from qtrader.risk.engine import RiskEngine, compute_risk_level
from qtrader.risk.types import (
    LEVEL_MULTIPLIER,
    ApprovedOrder,
    CurrentPortfolio,
    MarketState,
    PortfolioRiskMetrics,
    PositionSnapshot,
    ProposedOrder,
    RejectedOrder,
    RiskDecision,
    RiskLevel,
)

__all__ = [
    "ApprovedOrder",
    "CurrentPortfolio",
    "LEVEL_MULTIPLIER",
    "MarketState",
    "PortfolioRiskMetrics",
    "PositionSnapshot",
    "ProposedOrder",
    "RejectedOrder",
    "RiskConfig",
    "RiskDecision",
    "RiskEngine",
    "RiskLevel",
    "compute_risk_level",
]
