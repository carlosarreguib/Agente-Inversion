"""Modelo de costes realista compartido entre backtesting y paper broker (T2.2).

Función pura: calculate_costs(order, bar, instrument, config) → CostBreakdown.
Sin I/O, sin estado global. Mismas entradas → misma salida.

Fórmulas (todas con Decimal, nunca float):

  1. commission = max(min_commission, rate × notional)
     Aplicada por cada lado (entrada Y salida). El llamador la aplica dos veces.

  2. spread_cost = 0.5 × spread_bps / 10000 × notional
     half-spread al cruzar. Se aplica una vez por operación (al entrar).
     spread_bps: instrumento.spread_bps si está definido,
                 sino config.spread_bps_by_category[instrument.category].

  3. slippage = slippage_factor × sqrt(qty / adv) × price × qty
     Si adv=0 o no disponible → slippage_bps_fallback / 10000 × notional.

  4. participation_limit: si qty > max_participation × adv,
     qty_adjusted = max_participation × adv (truncado a entero).
     Se registra via participation_adjusted=True en el resultado.

  5. min_order check: si notional < min_order_eur → OrderTooSmall.

total = commission + spread_cost + slippage
El precio neto de fill ajustado por spread+slippage:
  BUY:  fill_price = open × (1 + half_spread + slippage_per_share_frac)
  SELL: fill_price = open × (1 - half_spread - slippage_per_share_frac)
La comisión sale del cash directamente (fill.commission).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from pathlib import Path  # noqa: TCH003 — used in classmethod signature at runtime
from typing import Any

import yaml

from qtrader.core.types import (  # noqa: TCH001 — usado en firmas de función y dataclass
    Instrument,
    InstrumentCategory,
    Order,
    Side,
    ValidatedBar,
)

_ZERO = Decimal("0")
_TEN_THOUSAND = Decimal("10000")
_TWO = Decimal("2")


# ---------------------------------------------------------------------------
# Excepción
# ---------------------------------------------------------------------------

class OrderTooSmall(Exception):
    """Raised when order notional < min_order_eur (invariante §02-AJUSTES)."""


# ---------------------------------------------------------------------------
# Resultado del modelo de costes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CostBreakdown:
    """Desglose completo de costes para una operación.

    Todos los valores en la moneda del instrumento (USD para proxy ETFs).
    commission: coste de comisión (por lado; aplicar dos veces en round-trip).
    spread_cost: half-spread al cruzar.
    slippage:    impacto de mercado.
    total:       commission + spread_cost + slippage.
    participation_adjusted: True si la qty fue reducida por el límite de participación.
    adjusted_qty: qty usada tras aplicar participation_limit (puede coincidir con original).
    price_adjustment: fracción total (spread + slippage) aplicada al precio de fill.
                      BUY: fill = open × (1 + price_adjustment)
                      SELL: fill = open × (1 - price_adjustment)
    """

    commission: Decimal
    spread_cost: Decimal
    slippage: Decimal
    total: Decimal
    participation_adjusted: bool
    adjusted_qty: Decimal
    price_adjustment: Decimal  # fracción, no bps


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CostsConfig:
    """Parámetros del modelo de costes. Cargado desde config/costs.yaml."""

    commission_rate: Decimal
    min_commission: Decimal
    spread_bps_by_category: dict[InstrumentCategory, int]
    slippage_factor: Decimal
    slippage_bps_fallback: int
    max_participation: Decimal
    min_order_eur: Decimal

    @classmethod
    def from_yaml(cls, path: Path) -> CostsConfig:
        with path.open(encoding="utf-8") as fh:
            raw: dict[str, Any] = yaml.safe_load(fh)

        spread_raw: dict[str, int] = raw["spread_bps_by_category"]
        spread_by_cat = {
            InstrumentCategory(k): int(v)
            for k, v in spread_raw.items()
        }
        return cls(
            commission_rate=Decimal(str(raw["commission_rate"])),
            min_commission=Decimal(str(raw["min_commission"])),
            spread_bps_by_category=spread_by_cat,
            slippage_factor=Decimal(str(raw["slippage_factor"])),
            slippage_bps_fallback=int(raw["slippage_bps_fallback"]),
            max_participation=Decimal(str(raw["max_participation"])),
            min_order_eur=Decimal(str(raw["min_order_eur"])),
        )

    @classmethod
    def zero_costs(cls) -> CostsConfig:
        """Configuración sin costes para comparación explícita en tests."""
        return cls(
            commission_rate=_ZERO,
            min_commission=_ZERO,
            spread_bps_by_category=dict.fromkeys(InstrumentCategory, 0),
            slippage_factor=_ZERO,
            slippage_bps_fallback=0,
            max_participation=Decimal("1"),
            min_order_eur=_ZERO,
        )


# ---------------------------------------------------------------------------
# Función pura principal
# ---------------------------------------------------------------------------

def calculate_costs(
    order: Order,
    bar: ValidatedBar,
    instrument: Instrument,
    config: CostsConfig,
    avg_daily_volume: Decimal = _ZERO,
) -> CostBreakdown:
    """Calcula el desglose de costes para una orden.

    Args:
        order:            orden a evaluar.
        bar:              barra del día de la señal (usamos open para notional).
        instrument:       metadatos del instrumento (categoría, spread propio).
        config:           parámetros del modelo.
        avg_daily_volume: volumen diario medio en unidades (0 → usar fallback).

    Returns:
        CostBreakdown con todos los componentes de coste.

    Raises:
        OrderTooSmall: si notional < config.min_order_eur.
    """
    open_price = bar.bar.open
    qty = order.quantity

    # --- 4. Participation limit ---
    participation_adjusted = False
    adjusted_qty = qty
    if avg_daily_volume > _ZERO:
        max_qty = (config.max_participation * avg_daily_volume).to_integral_value(
            rounding=ROUND_DOWN
        )
        if qty > max_qty and max_qty > _ZERO:
            adjusted_qty = max_qty
            participation_adjusted = True
        elif max_qty == _ZERO:
            # volumen tan pequeño que el límite es 0 → usar qty original
            pass

    notional = adjusted_qty * open_price

    # --- 5. Minimum order size ---
    if config.min_order_eur > _ZERO and notional < config.min_order_eur:
        raise OrderTooSmall(
            f"Order notional {notional:.2f} < min_order_eur {config.min_order_eur:.2f} "
            f"(symbol={order.symbol}, qty={adjusted_qty}, price={open_price})"
        )

    # --- 1. Commission ---
    commission = max(config.min_commission, config.commission_rate * notional)

    # --- 2. Spread (half-spread) ---
    if instrument.spread_bps is not None:
        spread_bps = Decimal(instrument.spread_bps)
    else:
        category_bps = config.spread_bps_by_category.get(instrument.category, 5)
        spread_bps = Decimal(category_bps)
    half_spread_frac = Decimal("0.5") * spread_bps / _TEN_THOUSAND
    spread_cost = half_spread_frac * notional

    # --- 3. Slippage ---
    if avg_daily_volume > _ZERO:
        ratio = float(adjusted_qty / avg_daily_volume)
        slippage_frac = Decimal(str(float(config.slippage_factor) * math.sqrt(ratio)))
        slippage = slippage_frac * notional
    else:
        slippage_frac = Decimal(config.slippage_bps_fallback) / _TEN_THOUSAND
        slippage = slippage_frac * notional

    # price_adjustment: fracción total aplicada al precio (spread + slippage).
    # BUY paga más, SELL recibe menos.
    price_adjustment = half_spread_frac + slippage_frac

    total = commission + spread_cost + slippage

    return CostBreakdown(
        commission=commission,
        spread_cost=spread_cost,
        slippage=slippage,
        total=total,
        participation_adjusted=participation_adjusted,
        adjusted_qty=adjusted_qty,
        price_adjustment=price_adjustment,
    )


# ---------------------------------------------------------------------------
# Helper: fill price ajustado por spread + slippage
# ---------------------------------------------------------------------------

def adjusted_fill_price(
    open_price: Decimal,
    side: Side,
    price_adjustment: Decimal,
) -> Decimal:
    """Aplica el ajuste de precio por spread+slippage al open."""
    if side == Side.BUY:
        return open_price * (1 + price_adjustment)
    return open_price * (1 - price_adjustment)
