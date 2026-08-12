"""Tipos de contrato del Risk Engine (T4.2).

Estos tipos son PROPIOS del Risk Engine: no se importan desde core.types
ni desde portfolio. El Risk Engine evalua lo que le llega; no sabe como
se construyo el portfolio ni quien propone las ordenes.

sector_exposures y region_exposures son tuple[tuple[str, Decimal], ...]
(no dict) para garantizar determinismo byte a byte en P8 y compatibilidad
con mypy strict en modelos frozen=True.
"""
from __future__ import annotations

from datetime import date, datetime  # noqa: TCH003 — runtime Pydantic
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from qtrader.core.types import InstrumentCategory, Side  # noqa: TCH001


class RiskLevel(StrEnum):
    """Nivel de riesgo operativo. Transiciones controladas por drawdown."""

    NORMAL = "NORMAL"
    CAUTION = "CAUTION"
    RISK_OFF = "RISK_OFF"
    HALT = "HALT"


# Multiplicadores de tamanio de orden por nivel
LEVEL_MULTIPLIER: dict[RiskLevel, Decimal] = {
    RiskLevel.NORMAL: Decimal("1.0"),
    RiskLevel.CAUTION: Decimal("0.6"),
    RiskLevel.RISK_OFF: Decimal("0.3"),
    RiskLevel.HALT: Decimal("0.0"),
}


class PositionSnapshot(BaseModel):
    """Snapshot de una posicion abierta en el momento de la evaluacion."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    quantity: Decimal
    price: Decimal                   # precio de mercado actual (mid)
    category: InstrumentCategory
    notional: Decimal                # quantity * price


class CurrentPortfolio(BaseModel):
    """Estado actual del portfolio en el momento de la evaluacion."""

    model_config = ConfigDict(frozen=True)

    positions: tuple[PositionSnapshot, ...]
    nav: Decimal                     # NAV actual en EUR
    peak_nav: Decimal                # NAV maximo historico (para drawdown)
    nav_open_today: Decimal          # NAV al inicio de la sesion de hoy
    nav_open_week: Decimal           # NAV al inicio de la semana
    orders_today: int                # ordenes ya emitidas hoy (antes de evaluate)
    notional_today: Decimal          # notional ya emitido hoy


class ProposedOrder(BaseModel):
    """Orden propuesta por el portfolio constructor o la estrategia."""

    model_config = ConfigDict(frozen=True)

    order_id: str
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal                   # precio de mercado para sizing
    category: InstrumentCategory


class MarketState(BaseModel):
    """Estado del mercado en el momento de la evaluacion."""

    model_config = ConfigDict(frozen=True)

    trading_date: date
    days_below_threshold: int        # dias consecutivos bajo el umbral del nivel inferior
    is_market_open: bool


class ApprovedOrder(BaseModel):
    """Orden aprobada (posiblemente reducida en tamano)."""

    model_config = ConfigDict(frozen=True)

    order_id: str
    symbol: str
    side: Side
    original_quantity: Decimal
    approved_quantity: Decimal       # puede ser < original si fue reducida
    notional: Decimal                # approved_quantity * price
    reduction_reason: str | None     # None si no fue reducida


class RejectedOrder(BaseModel):
    """Orden rechazada, con motivo explicito."""

    model_config = ConfigDict(frozen=True)

    order_id: str
    symbol: str
    side: Side
    quantity: Decimal
    notional: Decimal
    reason: str                      # siempre presente


class PortfolioRiskMetrics(BaseModel):
    """Metricas de riesgo del portfolio ANTES de ejecutar las ordenes aprobadas."""

    model_config = ConfigDict(frozen=True)

    current_drawdown: Decimal                        # (nav - peak_nav)/peak_nav ≤ 0
    daily_loss: Decimal                              # (nav - nav_open_today)/nav_open_today ≤ 0
    weekly_loss: Decimal                             # (nav - nav_open_week)/nav_open_week ≤ 0
    total_exposure: Decimal                          # sum(notional)/nav ∈ [0,1]
    max_single_position: Decimal                     # max(pos.notional/nav) ∈ [0,1]
    sector_exposures: tuple[tuple[str, Decimal], ...]   # ordenado por sector
    region_exposures: tuple[tuple[str, Decimal], ...]   # ordenado por region
    orders_today: int
    notional_today: Decimal


class RiskDecision(BaseModel):
    """Decision completa del Risk Engine. Inmutable."""

    model_config = ConfigDict(frozen=True)

    level: RiskLevel
    approved_orders: tuple[ApprovedOrder, ...]
    rejected_orders: tuple[RejectedOrder, ...]
    portfolio_metrics: PortfolioRiskMetrics
    warnings: tuple[str, ...]
    timestamp: datetime
