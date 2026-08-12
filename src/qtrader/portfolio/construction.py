"""Construccion de portfolio con volatility targeting (T4.1).

Algoritmo (en orden estricto):
  1. Filtrar senales LONG del quintil superior.
  2. Inverse-vol weighting: w_i = (1/sigma_i) / sum(1/sigma_j).
     Fallback: sigma=0.15 si < 63 barras disponibles.
  3. Aplicar restricciones en orden:
     a. cap por posicion (max_position_weight)
     b. cap por sector   (max_sector_exposure)
     c. cap por region   (max_region_exposure)
     d. eliminar posiciones < min_position_size EUR
     e. cap por numero de posiciones (max_positions)
     f. renormalizar a sum=1 despues de cada paso
  4. Banda de no-rebalanceo: si max(|w_actual - w_obj|) < threshold → skip.
  5. Convertir pesos a cantidades enteras de acciones.

Invariantes:
  - sum(weights) = 1.0 (tolerancia 1e-10) cuando rebalance_needed=True
  - Ninguna restriccion configurada se viola en la salida
  - Sin I/O, sin red, sin estado global (Risk Engine puro: §2.2)
"""
from __future__ import annotations

import math
from datetime import datetime
from decimal import ROUND_FLOOR, ROUND_UP, Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from qtrader.core.types import Direction, Instrument, InstrumentCategory, Signal, TargetPosition

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

_ZERO = Decimal("0")
_ONE = Decimal("1")
_ANNUALIZE = Decimal(str(math.sqrt(252)))
_TOL = Decimal("1e-10")


# ---------------------------------------------------------------------------
# Modelos de configuracion y salida
# ---------------------------------------------------------------------------

class PortfolioConfig(BaseModel):
    """Parametros de construccion del portfolio. Inmutable."""

    model_config = ConfigDict(frozen=True)

    max_position_weight: Decimal = Decimal("0.25")   # 25 % por posicion
    max_sector_exposure: Decimal = Decimal("0.40")   # 40 % en un sector
    max_region_exposure: Decimal = Decimal("0.60")   # 60 % en una region
    min_position_size_eur: Decimal = Decimal("1000") # minimo notional
    max_positions: int = 10
    vol_fallback: Decimal = Decimal("0.15")          # vol anualizada si < 63 barras
    vol_lookback: int = 63                           # dias para vol realizada
    rebalance_threshold: Decimal = Decimal("0.05")   # banda de no-rebalanceo


class PortfolioTarget(BaseModel):
    """Resultado de la construccion. Inmutable y serializable."""

    model_config = ConfigDict(frozen=True)

    targets: tuple[TargetPosition, ...]
    total_weight: Decimal
    rebalance_needed: bool
    constraints_applied: tuple[str, ...]
    timestamp: datetime


# ---------------------------------------------------------------------------
# Helpers internos (funciones puras)
# ---------------------------------------------------------------------------

def _compute_vol(closes: list[Decimal], lookback: int, fallback: Decimal) -> Decimal:
    """Volatilidad anualizada de los ultimos `lookback` cierres.

    Si hay menos de `lookback` barras, devuelve `fallback`.
    """
    if len(closes) < lookback:
        return fallback
    tail = closes[-lookback:]
    n = len(tail)
    if n < 2:
        return fallback
    returns: list[Decimal] = []
    for i in range(1, n):
        if tail[i - 1] > _ZERO:
            returns.append((tail[i] - tail[i - 1]) / tail[i - 1])
    if len(returns) < 2:
        return fallback
    mean = sum(returns, _ZERO) / Decimal(len(returns))
    variance = sum((r - mean) ** 2 for r in returns) / Decimal(len(returns) - 1)
    if variance <= _ZERO:
        return fallback
    daily_vol = Decimal(str(math.sqrt(float(variance))))
    return daily_vol * _ANNUALIZE


def _normalize(weights: dict[str, Decimal]) -> dict[str, Decimal]:
    """Renormaliza para que sum=1. Si suma=0, devuelve igual weight."""
    total = sum(weights.values(), _ZERO)
    if total <= _ZERO:
        return dict(weights)
    return {sym: w / total for sym, w in weights.items()}


def _apply_cap(
    weights: dict[str, Decimal],
    cap: Decimal,
    group_fn: Any,  # callable(sym) -> str | None  -- grupo del simbolo
    constraint_name: str,
    constraints_applied: list[str],
) -> dict[str, Decimal]:
    """Aplica un cap de exposicion por grupo.

    Estrategia iterativa:
      1. Calcula exposicion de cada grupo.
      2. Si algun grupo supera el cap:
         a. Si hay instrumentos fuera del grupo, recorta proporcionalmente
            los pesos del grupo y redistribuye entre los de fuera.
         b. Si TODOS los instrumentos son del mismo grupo (no hay donde
            redistribuir), elimina el de menor peso del grupo hasta que
            la exposicion sea <= cap.
      3. Renormaliza.
    """
    MAX_ITER = 200
    for _iter in range(MAX_ITER):
        # Calcular exposicion por grupo
        group_exposure: dict[str, Decimal] = {}
        for sym, w in weights.items():
            grp = group_fn(sym)
            if grp is not None:
                group_exposure[grp] = group_exposure.get(grp, _ZERO) + w

        violated = {g: e for g, e in group_exposure.items() if e > cap + _TOL}
        if not violated:
            break

        changed = False
        for grp, _exposure in violated.items():
            syms_in_grp = [s for s in weights if group_fn(s) == grp]
            syms_out_grp = [s for s in weights if group_fn(s) != grp]

            if syms_out_grp:
                # Hay instrumentos fuera del grupo: escalar el grupo a cap
                # y redistribuir el exceso entre los de fuera.
                grp_total = sum(weights[s] for s in syms_in_grp)
                if grp_total <= _ZERO:
                    continue
                out_total = sum(weights[s] for s in syms_out_grp)
                target_out = _ONE - cap

                # Escalar proporciones del grupo
                scale_in = cap / grp_total
                for sym in syms_in_grp:
                    weights[sym] = weights[sym] * scale_in

                # Escalar proporciones fuera del grupo
                if out_total > _ZERO:
                    scale_out = target_out / out_total
                    for sym in syms_out_grp:
                        weights[sym] = weights[sym] * scale_out
                changed = True
            else:
                # Todos en el mismo grupo: eliminar el de menor peso
                # hasta que la exposicion sea <= cap o quede 1 instrumento.
                if not syms_in_grp or len(syms_in_grp) <= 1:
                    # No se puede satisfacer el cap: dejar como esta
                    continue
                min_sym = min(syms_in_grp, key=lambda s: weights[s])
                del weights[min_sym]
                constraints_applied.append(f"{constraint_name}(drop:{min_sym})")
                changed = True
                break  # reiniciar la iteracion exterior

        if changed:
            if weights:
                weights = _normalize(weights)
            constraints_applied.append(constraint_name)

    return weights


def _apply_position_cap(
    weights: dict[str, Decimal],
    cap: Decimal,
    constraints_applied: list[str],
) -> dict[str, Decimal]:
    """Recorta cualquier posicion que supere `cap` y redistribuye el exceso."""
    MAX_ITER = 100
    for _ in range(MAX_ITER):
        excess_total = _ZERO
        new_weights: dict[str, Decimal] = {}
        uncapped: list[str] = []

        for sym, w in weights.items():
            if w > cap + _TOL:
                excess_total += w - cap
                new_weights[sym] = cap
                constraints_applied.append(f"max_position_weight({sym})")
            else:
                new_weights[sym] = w
                uncapped.append(sym)

        if excess_total <= _TOL:
            return new_weights

        # Redistribuir exceso entre posiciones no saturadas
        uncapped_total = sum(new_weights[s] for s in uncapped)
        if uncapped_total <= _ZERO:
            # Todos saturados: distribuir uniformemente entre todos
            n = Decimal(len(new_weights))
            for sym in new_weights:
                new_weights[sym] = _ONE / n
            return new_weights

        scale = (uncapped_total + excess_total) / uncapped_total
        for sym in uncapped:
            new_weights[sym] = new_weights[sym] * scale

        weights = _normalize(new_weights)

    return weights


def _apply_min_position(
    weights: dict[str, Decimal],
    nav: Decimal,
    min_size: Decimal,
    constraints_applied: list[str],
) -> dict[str, Decimal]:
    """Elimina posiciones cuyo notional < min_size. Renormaliza."""
    changed = True
    while changed:
        changed = False
        to_remove = [
            sym for sym, w in weights.items()
            if nav > _ZERO and w * nav < min_size
        ]
        if to_remove:
            for sym in to_remove:
                del weights[sym]
                constraints_applied.append(f"min_position_size({sym})")
            weights = _normalize(weights)
            changed = True
    return weights


def _apply_max_positions(
    weights: dict[str, Decimal],
    max_pos: int,
    constraints_applied: list[str],
) -> dict[str, Decimal]:
    """Conserva las `max_pos` posiciones de mayor peso. Elimina el resto."""
    if len(weights) <= max_pos:
        return weights
    sorted_syms = sorted(weights, key=lambda s: weights[s], reverse=True)
    removed = sorted_syms[max_pos:]
    for sym in removed:
        del weights[sym]
        constraints_applied.append(f"max_positions({sym})")
    return _normalize(weights)


# ---------------------------------------------------------------------------
# Constructor principal
# ---------------------------------------------------------------------------

class PortfolioConstructor:
    """Construye el portfolio objetivo dado un conjunto de senales y datos de mercado.

    El constructor es puro dentro de cada llamada a `build()`:
    sin I/O, sin red, sin estado mutable compartido.
    El historial de closes se pasa explicitamente como argumento.
    """

    def __init__(
        self,
        instruments: dict[str, Instrument],
        config: PortfolioConfig | None = None,
    ) -> None:
        self._instruments = instruments
        self._config = config or PortfolioConfig()

    def build(
        self,
        signals: list[Signal],
        closes_history: dict[str, list[Decimal]],
        current_weights: dict[str, Decimal],
        nav: Decimal,
        timestamp: datetime,
    ) -> PortfolioTarget:
        """Construye el portfolio objetivo.

        Args:
            signals:         Senales LONG del quintil superior (direction=LONG).
            closes_history:  {symbol: [close_0, ..., close_T]} sin look-ahead.
            current_weights: Pesos actuales en cartera {symbol: weight}.
            nav:             NAV disponible en EUR.
            timestamp:       Timestamp de la construccion.

        Returns:
            PortfolioTarget con los targets objetivo.
        """
        cfg = self._config
        constraints_applied: list[str] = []

        # --- Paso 1: seleccion de candidatos ---
        candidates = [s.symbol for s in signals if s.direction == Direction.LONG]
        if not candidates:
            return PortfolioTarget(
                targets=(),
                total_weight=_ZERO,
                rebalance_needed=False,
                constraints_applied=tuple(constraints_applied),
                timestamp=timestamp,
            )

        # --- Paso 2: inverse-vol weighting ---
        inv_vols: dict[str, Decimal] = {}
        for sym in candidates:
            closes = closes_history.get(sym, [])
            vol = _compute_vol(closes, cfg.vol_lookback, cfg.vol_fallback)
            if vol <= _ZERO:
                vol = cfg.vol_fallback
            inv_vols[sym] = _ONE / vol

        total_inv_vol = sum(inv_vols.values(), _ZERO)
        if total_inv_vol <= _ZERO:
            # Fallback: equal weight
            n = Decimal(len(candidates))
            weights: dict[str, Decimal] = {sym: _ONE / n for sym in candidates}
        else:
            weights = {sym: iv / total_inv_vol for sym, iv in inv_vols.items()}

        # --- Paso 3: aplicar restricciones en bucle hasta convergencia ---
        # Las restricciones interactuan: min_position_size puede invalidar
        # los caps de sector/region al renormalizar. Iteramos hasta que
        # ninguna restriccion actua (punto fijo), maximo 20 rondas.
        inst_map = self._instruments

        def _sector_group(sym: str) -> str | None:
            inst = inst_map.get(sym)
            if inst is None or inst.category != InstrumentCategory.SECTOR:
                return None
            return "sector"

        def _region_group(sym: str) -> str | None:
            inst = inst_map.get(sym)
            if inst is None or inst.category != InstrumentCategory.REGION:
                return None
            return "region"

        MAX_OUTER = 20
        for _outer in range(MAX_OUTER):
            prev_syms = frozenset(weights)
            prev_weights = dict(weights)

            # 3a. cap por posicion
            weights = _apply_position_cap(weights, cfg.max_position_weight, constraints_applied)
            weights = _normalize(weights)

            # 3b. cap por sector
            weights = _apply_cap(
                weights, cfg.max_sector_exposure,
                _sector_group, "max_sector_exposure", constraints_applied,
            )
            if not weights:
                break
            weights = _normalize(weights)

            # 3c. cap por region
            weights = _apply_cap(
                weights, cfg.max_region_exposure,
                _region_group, "max_region_exposure", constraints_applied,
            )
            if not weights:
                break
            weights = _normalize(weights)

            # 3d. eliminar posiciones < min_position_size
            if nav > _ZERO:
                weights = _apply_min_position(
                    weights, nav, cfg.min_position_size_eur, constraints_applied,
                )
            if not weights:
                break

            # 3e. cap por numero de posiciones
            weights = _apply_max_positions(weights, cfg.max_positions, constraints_applied)
            if not weights:
                break
            weights = _normalize(weights)

            # Verificar convergencia: mismo conjunto de simbolos y pesos estables
            if (
                frozenset(weights) == prev_syms
                and all(abs(weights.get(s, _ZERO) - prev_weights.get(s, _ZERO)) < _TOL
                        for s in prev_syms)
            ):
                break

        if not weights:
            return PortfolioTarget(
                targets=(),
                total_weight=_ZERO,
                rebalance_needed=False,
                constraints_applied=tuple(constraints_applied),
                timestamp=timestamp,
            )

        # Verificacion final: si algun peso viola el cap (caso 1 instrumento),
        # devolver cartera vacia — no hay solucion valida.
        if any(w > cfg.max_position_weight + _TOL for w in weights.values()):
            return PortfolioTarget(
                targets=(),
                total_weight=_ZERO,
                rebalance_needed=False,
                constraints_applied=tuple(constraints_applied),
                timestamp=timestamp,
            )

        # Renormalizacion final exacta
        weights = _normalize(weights)

        # --- Paso 4: banda de no-rebalanceo ---
        rebalance_needed = self._needs_rebalance(weights, current_weights, cfg.rebalance_threshold)

        if not rebalance_needed:
            # Mantener posiciones actuales tal cual
            existing_targets = tuple(
                TargetPosition(symbol=sym, quantity=_ZERO, weight=w)
                for sym, w in current_weights.items()
            )
            return PortfolioTarget(
                targets=existing_targets,
                total_weight=sum(current_weights.values(), _ZERO),
                rebalance_needed=False,
                constraints_applied=tuple(constraints_applied),
                timestamp=timestamp,
            )

        # --- Paso 5: convertir a cantidades de acciones ---
        targets: list[TargetPosition] = []
        for sym, weight in sorted(weights.items()):
            notional = weight * nav
            price = Decimal(str(closes_history.get(sym, [_ONE])[-1]))
            if price > _ZERO:
                qty_raw = notional / price
                qty = qty_raw.to_integral_value(rounding=ROUND_FLOOR)
            else:
                qty = _ZERO
            targets.append(TargetPosition(
                symbol=sym,
                quantity=qty,
                weight=weight,
            ))

        total_weight = sum(weights.values(), _ZERO)

        return PortfolioTarget(
            targets=tuple(targets),
            total_weight=total_weight,
            rebalance_needed=True,
            constraints_applied=tuple(constraints_applied),
            timestamp=timestamp,
        )

    @staticmethod
    def _needs_rebalance(
        target_weights: dict[str, Decimal],
        current_weights: dict[str, Decimal],
        threshold: Decimal,
    ) -> bool:
        """True si algun peso difiere mas de `threshold` del objetivo."""
        all_symbols = set(target_weights) | set(current_weights)
        for sym in all_symbols:
            w_target = target_weights.get(sym, _ZERO)
            w_current = current_weights.get(sym, _ZERO)
            if abs(w_target - w_current) > threshold:
                return True
        return False
