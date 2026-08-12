"""Configuracion del Risk Engine (T4.2).

Todos los limites son parametros de RiskConfig.
Ningun limite esta hardcodeado en engine.py.
"""
from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class RiskConfig(BaseModel):
    """Parametros del Risk Engine. Inmutable."""

    model_config = ConfigDict(frozen=True)

    # --- Niveles de drawdown ---
    drawdown_caution: Decimal = Decimal("0.05")    # ≥ 5 % DD → CAUTION
    drawdown_risk_off: Decimal = Decimal("0.09")   # ≥ 9 % DD → RISK_OFF
    drawdown_halt: Decimal = Decimal("0.13")       # ≥ 13 % DD → HALT
    recovery_days: int = 5                          # dias bajo umbral para bajar nivel

    # --- Por operacion ---
    max_position_weight: Decimal = Decimal("0.25")  # 25 % NAV por posicion
    max_order_notional: Decimal = Decimal("3000")   # EUR por orden
    min_order_size: Decimal = Decimal("1000")        # EUR minimo para aprobar

    # --- Por portfolio ---
    max_total_exposure: Decimal = Decimal("0.95")   # 95 % NAV invertido
    max_sector_exposure: Decimal = Decimal("0.40")  # 40 % en un sector
    max_region_exposure: Decimal = Decimal("0.60")  # 60 % en una region
    max_single_position: Decimal = Decimal("0.25")  # 25 % en un instrumento

    # --- Por perdida ---
    max_daily_loss: Decimal = Decimal("0.02")       # 2 % NAV en el dia
    max_weekly_loss: Decimal = Decimal("0.04")      # 4 % NAV en la semana

    # --- Rate limits (capa independiente) ---
    max_orders_per_day: int = 20
    max_notional_per_day: Decimal = Decimal("15000")  # EUR en el dia
