"""Risk Engine puro (T4.2) — invariante §2.2.

PURO:        sin I/O, sin red, sin estado global, sin aleatoriedad.
DETERMINISTA: mismas entradas → misma salida, siempre.
INDEPENDIENTE: no importa portfolio/, brokers/, ni agents/.
               Tiene su propia config (RiskConfig) y sus propios tipos.

Logica de evaluacion (orden fijo, sin path-dependencia):
  1. Calcular nivel de riesgo por drawdown y perdidas.
  2. Comprobar rate limits (capa independiente, nunca superables).
  3. Para cada orden propuesta (en el orden recibido):
     a. Si HALT: rechazar toda orden BUY; solo SELL de posiciones existentes.
     b. Si rate limit global agotado: rechazar.
     c. Calcular notional propuesto.
     d. Aplicar reducciones en orden fijo:
        i.   max_order_notional
        ii.  max_position_weight (posicion resultante / nav)
        iii. max_single_position (mismo calculo, mismo resultado)
        iv.  max_total_exposure
        v.   max_sector_exposure
        vi.  max_region_exposure
     e. Aplicar multiplicador de nivel (despues de todos los limites).
     f. Si qty_final * price < min_order_size: rechazar.
     g. Acumular notional del dia; si supera rate limit diario: rechazar.
  4. Calcular metricas del portfolio actual.
  5. Construir RiskDecision inmutable.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal

from qtrader.core.types import InstrumentCategory, Side
from qtrader.risk.config import RiskConfig  # noqa: TCH001
from qtrader.risk.types import (
    LEVEL_MULTIPLIER,
    ApprovedOrder,
    CurrentPortfolio,
    MarketState,
    PortfolioRiskMetrics,
    ProposedOrder,
    RejectedOrder,
    RiskDecision,
    RiskLevel,
)

_ZERO = Decimal("0")
_ONE = Decimal("1")
_NEG_ONE = Decimal("-1")


# ---------------------------------------------------------------------------
# Helpers puros
# ---------------------------------------------------------------------------

def compute_risk_level(
    current_drawdown: Decimal,
    daily_loss: Decimal,
    weekly_loss: Decimal,
    days_below_threshold: int,
    current_level: RiskLevel,
    config: RiskConfig,
) -> tuple[RiskLevel, list[str]]:
    """Calcula el nivel de riesgo resultante y los warnings asociados.

    Transiciones de subida: inmediatas al cruzar el umbral.
    Transiciones de bajada: solo si days_below_threshold >= config.recovery_days.

    Returns:
        (nivel, warnings)
    """
    warnings: list[str] = []

    # HALT por perdida diaria (capa de proteccion independiente del drawdown)
    if daily_loss < -config.max_daily_loss:
        warnings.append(
            f"HALT_DAILY_LOSS: daily_loss={daily_loss:.4f} < -{config.max_daily_loss}"
        )
        return RiskLevel.HALT, warnings

    # HALT por perdida semanal excesiva → RISK_OFF minimo
    weekly_forced_risk_off = weekly_loss < -config.max_weekly_loss

    # Nivel por drawdown
    dd = abs(current_drawdown)
    if dd >= config.drawdown_halt:
        warnings.append(
            f"HALT_DRAWDOWN: drawdown={current_drawdown:.4f} ≤ -{config.drawdown_halt}"
        )
        level_by_dd = RiskLevel.HALT
    elif dd >= config.drawdown_risk_off:
        level_by_dd = RiskLevel.RISK_OFF
    elif dd >= config.drawdown_caution:
        level_by_dd = RiskLevel.CAUTION
    else:
        level_by_dd = RiskLevel.NORMAL

    # Tomar el mas restrictivo entre drawdown y perdida semanal
    if weekly_forced_risk_off:
        warnings.append(
            f"RISK_OFF_WEEKLY_LOSS: weekly_loss={weekly_loss:.4f} < -{config.max_weekly_loss}"
        )
        _LEVEL_ORDER = [RiskLevel.NORMAL, RiskLevel.CAUTION, RiskLevel.RISK_OFF, RiskLevel.HALT]
        target = _LEVEL_ORDER[max(
            _LEVEL_ORDER.index(level_by_dd),
            _LEVEL_ORDER.index(RiskLevel.RISK_OFF),
        )]
    else:
        target = level_by_dd

    # Transiciones de bajada: solo si recovery sostenida
    _LEVEL_ORDER2 = [RiskLevel.NORMAL, RiskLevel.CAUTION, RiskLevel.RISK_OFF, RiskLevel.HALT]
    current_idx = _LEVEL_ORDER2.index(current_level)
    target_idx = _LEVEL_ORDER2.index(target)

    if target_idx < current_idx:
        # Intento de bajar el nivel
        if days_below_threshold >= config.recovery_days:
            final_level = target
        else:
            final_level = current_level
            warnings.append(
                f"RECOVERY_PENDING: {days_below_threshold}/{config.recovery_days} dias"
            )
    else:
        final_level = target

    return final_level, warnings


def _compute_portfolio_metrics(
    portfolio: CurrentPortfolio,
    config: RiskConfig,
) -> PortfolioRiskMetrics:
    """Calcula las metricas de riesgo del portfolio actual. Pura."""
    nav = portfolio.nav

    # Drawdown
    if portfolio.peak_nav > _ZERO:
        current_drawdown = (nav - portfolio.peak_nav) / portfolio.peak_nav
    else:
        current_drawdown = _ZERO

    # Perdidas
    if portfolio.nav_open_today > _ZERO:
        daily_loss = (nav - portfolio.nav_open_today) / portfolio.nav_open_today
    else:
        daily_loss = _ZERO

    if portfolio.nav_open_week > _ZERO:
        weekly_loss = (nav - portfolio.nav_open_week) / portfolio.nav_open_week
    else:
        weekly_loss = _ZERO

    # Exposicion total
    total_notional = sum(p.notional for p in portfolio.positions)
    total_exposure = total_notional / nav if nav > _ZERO else _ZERO

    # Posicion maxima individual
    max_single = _ZERO
    for p in portfolio.positions:
        w = p.notional / nav if nav > _ZERO else _ZERO
        if w > max_single:
            max_single = w

    # Exposicion por sector (sorted para determinismo)
    sector_map: dict[str, Decimal] = {}
    region_map: dict[str, Decimal] = {}
    for p in portfolio.positions:
        if p.category == InstrumentCategory.SECTOR:
            sector_map["sector"] = sector_map.get("sector", _ZERO) + (
                p.notional / nav if nav > _ZERO else _ZERO
            )
        elif p.category == InstrumentCategory.REGION:
            region_map["region"] = region_map.get("region", _ZERO) + (
                p.notional / nav if nav > _ZERO else _ZERO
            )

    sector_exposures = tuple(sorted(sector_map.items()))
    region_exposures = tuple(sorted(region_map.items()))

    return PortfolioRiskMetrics(
        current_drawdown=current_drawdown,
        daily_loss=daily_loss,
        weekly_loss=weekly_loss,
        total_exposure=total_exposure,
        max_single_position=max_single,
        sector_exposures=sector_exposures,
        region_exposures=region_exposures,
        orders_today=portfolio.orders_today,
        notional_today=portfolio.notional_today,
    )


def _position_notional(symbol: str, portfolio: CurrentPortfolio) -> Decimal:
    """Notional actual de una posicion en el portfolio."""
    for p in portfolio.positions:
        if p.symbol == symbol:
            return p.notional
    return _ZERO


def _position_qty(symbol: str, portfolio: CurrentPortfolio) -> Decimal:
    """Cantidad actual de una posicion en el portfolio."""
    for p in portfolio.positions:
        if p.symbol == symbol:
            return p.quantity
    return _ZERO


def _has_position(symbol: str, portfolio: CurrentPortfolio) -> bool:
    return any(p.symbol == symbol and p.quantity > _ZERO for p in portfolio.positions)


def _sector_notional(portfolio: CurrentPortfolio) -> Decimal:
    return sum(
        (p.notional for p in portfolio.positions
         if p.category == InstrumentCategory.SECTOR),
        _ZERO,
    )


def _region_notional(portfolio: CurrentPortfolio) -> Decimal:
    return sum(
        (p.notional for p in portfolio.positions
         if p.category == InstrumentCategory.REGION),
        _ZERO,
    )


def _floor_qty(qty: Decimal) -> Decimal:
    return qty.to_integral_value(rounding=ROUND_DOWN)


def _evaluate_single_order(
    order: ProposedOrder,
    portfolio: CurrentPortfolio,
    config: RiskConfig,
    level: RiskLevel,
    orders_approved_so_far: int,
    notional_approved_so_far: Decimal,
) -> tuple[ApprovedOrder | None, RejectedOrder | None, str | None]:
    """Evalua una sola orden propuesta. Devuelve (approved|None, rejected|None, warning|None).

    La logica de reduccion es secuencial y conservadora:
    cada limite recibe la cantidad ya reducida por los anteriores.
    """
    nav = portfolio.nav
    notional_order = order.quantity * order.price

    def _reject(reason: str) -> tuple[None, RejectedOrder, None]:
        return (
            None,
            RejectedOrder(
                order_id=order.order_id,
                symbol=order.symbol,
                side=order.side,
                quantity=order.quantity,
                notional=notional_order,
                reason=reason,
            ),
            None,
        )

    # --- HALT: solo SELLs de posiciones existentes ---
    if level == RiskLevel.HALT:
        if order.side == Side.BUY:
            return _reject("HALT: no new BUY orders allowed")
        if not _has_position(order.symbol, portfolio):
            return _reject("HALT: no existing position to close")

    # --- RISK_OFF: solo SELLs (sin nuevas compras) ---
    if level == RiskLevel.RISK_OFF and order.side == Side.BUY:
        return _reject("RISK_OFF: only SELL orders allowed")

    # --- Rate limit: ordenes por dia ---
    if orders_approved_so_far >= config.max_orders_per_day:
        return _reject(
            f"RATE_LIMIT_ORDERS: {orders_approved_so_far} >= {config.max_orders_per_day}"
        )

    # --- Rate limit: notional por dia ---
    remaining_daily_notional = config.max_notional_per_day - notional_approved_so_far
    if remaining_daily_notional <= _ZERO:
        return _reject(
            f"RATE_LIMIT_NOTIONAL: daily notional exhausted "
            f"({notional_approved_so_far} >= {config.max_notional_per_day})"
        )

    # --- Inicio de reducciones ---
    qty = order.quantity
    reduction_reasons: list[str] = []

    # i. max_order_notional
    if qty * order.price > config.max_order_notional:
        max_qty_by_notional = _floor_qty(config.max_order_notional / order.price)
        if max_qty_by_notional < qty:
            reduction_reasons.append(
                f"max_order_notional: {qty}→{max_qty_by_notional}"
            )
            qty = max_qty_by_notional

    if qty <= _ZERO:
        return _reject("reduced to 0 by max_order_notional")

    # ii-iii. max_position_weight / max_single_position (mismo calculo, aplicar el mas restrictivo)
    if nav > _ZERO and order.side == Side.BUY:
        cap = min(config.max_position_weight, config.max_single_position)
        current_pos_notional = _position_notional(order.symbol, portfolio)
        max_additional_notional = cap * nav - current_pos_notional
        if max_additional_notional <= _ZERO:
            return _reject(
                f"max_position_weight: position already at {current_pos_notional/nav:.4f} >= {cap}"
            )
        max_qty_by_weight = _floor_qty(max_additional_notional / order.price)
        if max_qty_by_weight < qty:
            reduction_reasons.append(
                f"max_position_weight: {qty}→{max_qty_by_weight}"
            )
            qty = max_qty_by_weight

    if qty <= _ZERO:
        return _reject("reduced to 0 by max_position_weight")

    # iv. max_total_exposure
    if nav > _ZERO and order.side == Side.BUY:
        current_total_notional = sum(p.notional for p in portfolio.positions)
        max_additional_exp = config.max_total_exposure * nav - current_total_notional
        if max_additional_exp <= _ZERO:
            return _reject(
                f"max_total_exposure: already at {current_total_notional/nav:.4f} >= "
                f"{config.max_total_exposure}"
            )
        max_qty_by_exposure = _floor_qty(max_additional_exp / order.price)
        if max_qty_by_exposure < qty:
            reduction_reasons.append(
                f"max_total_exposure: {qty}→{max_qty_by_exposure}"
            )
            qty = max_qty_by_exposure

    if qty <= _ZERO:
        return _reject("reduced to 0 by max_total_exposure")

    # v. max_sector_exposure
    if nav > _ZERO and order.side == Side.BUY and order.category == InstrumentCategory.SECTOR:
        current_sector_notional = _sector_notional(portfolio)
        max_additional_sector = config.max_sector_exposure * nav - current_sector_notional
        if max_additional_sector <= _ZERO:
            return _reject(
                f"max_sector_exposure: already at {current_sector_notional/nav:.4f} >= "
                f"{config.max_sector_exposure}"
            )
        max_qty_by_sector = _floor_qty(max_additional_sector / order.price)
        if max_qty_by_sector < qty:
            reduction_reasons.append(
                f"max_sector_exposure: {qty}→{max_qty_by_sector}"
            )
            qty = max_qty_by_sector

    if qty <= _ZERO:
        return _reject("reduced to 0 by max_sector_exposure")

    # vi. max_region_exposure
    if nav > _ZERO and order.side == Side.BUY and order.category == InstrumentCategory.REGION:
        current_region_notional = _region_notional(portfolio)
        max_additional_region = config.max_region_exposure * nav - current_region_notional
        if max_additional_region <= _ZERO:
            return _reject(
                f"max_region_exposure: already at {current_region_notional/nav:.4f} >= "
                f"{config.max_region_exposure}"
            )
        max_qty_by_region = _floor_qty(max_additional_region / order.price)
        if max_qty_by_region < qty:
            reduction_reasons.append(
                f"max_region_exposure: {qty}→{max_qty_by_region}"
            )
            qty = max_qty_by_region

    if qty <= _ZERO:
        return _reject("reduced to 0 by max_region_exposure")

    # --- Aplicar multiplicador de nivel (DESPUES de todos los limites) ---
    # Las ventas en HALT/RISK_OFF no se reducen por multiplicador:
    # el objetivo de HALT es cerrar posiciones, no bloquearlas.
    if order.side != Side.SELL:
        multiplier = LEVEL_MULTIPLIER[level]
        if multiplier < _ONE:
            qty_after_level = _floor_qty(qty * multiplier)
            if qty_after_level < qty:
                reduction_reasons.append(
                    f"level_{level}: {qty}→{qty_after_level} (x{multiplier})"
                )
                qty = qty_after_level

        if qty <= _ZERO:
            return _reject(f"reduced to 0 by level multiplier ({level})")

    # --- min_order_size ---
    final_notional = qty * order.price
    if final_notional < config.min_order_size:
        return _reject(
            f"min_order_size: {final_notional:.2f} < {config.min_order_size}"
        )

    # --- Rate limit de notional del dia (acumulado) ---
    if notional_approved_so_far + final_notional > config.max_notional_per_day:
        allowed = config.max_notional_per_day - notional_approved_so_far
        if allowed < config.min_order_size:
            return _reject(
                f"RATE_LIMIT_NOTIONAL: only {allowed:.2f} EUR remaining < min_order_size"
            )
        qty_by_rate = _floor_qty(allowed / order.price)
        if qty_by_rate < qty:
            reduction_reasons.append(
                f"rate_limit_notional: {qty}→{qty_by_rate}"
            )
            qty = qty_by_rate
        final_notional = qty * order.price
        if final_notional < config.min_order_size:
            return _reject(
                f"RATE_LIMIT_NOTIONAL: after reduction {final_notional:.2f} < min_order_size"
            )

    reduction_reason = "; ".join(reduction_reasons) if reduction_reasons else None

    approved = ApprovedOrder(
        order_id=order.order_id,
        symbol=order.symbol,
        side=order.side,
        original_quantity=order.quantity,
        approved_quantity=qty,
        notional=qty * order.price,
        reduction_reason=reduction_reason,
    )
    warning = (
        f"REDUCED {order.order_id}: {reduction_reason}"
        if reduction_reason else None
    )
    return approved, None, warning


# ---------------------------------------------------------------------------
# Risk Engine: funcion principal
# ---------------------------------------------------------------------------

class RiskEngine:
    """Namespace del Risk Engine. evaluate() es el unico punto de entrada.

    No tiene estado. No tiene __init__ con atributos mutables.
    """

    @staticmethod
    def evaluate(
        portfolio: CurrentPortfolio,
        proposed_orders: list[ProposedOrder],
        market_state: MarketState,
        config: RiskConfig,
    ) -> RiskDecision:
        """Evalua las ordenes propuestas contra los limites de riesgo.

        Puro: sin I/O, sin red, sin estado global, sin aleatoriedad.
        Determinista: mismas entradas → misma salida, siempre.
        """
        now = datetime.now(UTC)

        # --- Paso 1: calcular metricas actuales ---
        metrics = _compute_portfolio_metrics(portfolio, config)

        # --- Paso 2: calcular nivel de riesgo ---
        level, level_warnings = compute_risk_level(
            current_drawdown=metrics.current_drawdown,
            daily_loss=metrics.daily_loss,
            weekly_loss=metrics.weekly_loss,
            days_below_threshold=market_state.days_below_threshold,
            current_level=RiskLevel.NORMAL,  # nivel previo no disponible → inferido del drawdown
            config=config,
        )

        warnings: list[str] = list(level_warnings)

        # --- Paso 3: mercado cerrado → rechazar todo ---
        if not market_state.is_market_open:
            closed_rejected = tuple(
                RejectedOrder(
                    order_id=o.order_id,
                    symbol=o.symbol,
                    side=o.side,
                    quantity=o.quantity,
                    notional=o.quantity * o.price,
                    reason="MARKET_CLOSED",
                )
                for o in proposed_orders
            )
            return RiskDecision(
                level=level,
                approved_orders=(),
                rejected_orders=closed_rejected,
                portfolio_metrics=metrics,
                warnings=tuple(warnings),
                timestamp=now,
            )

        # --- Paso 4: evaluar cada orden en orden de llegada ---
        approved: list[ApprovedOrder] = []
        rejected: list[RejectedOrder] = []
        orders_approved_count = portfolio.orders_today
        notional_approved_acc = portfolio.notional_today

        for order in proposed_orders:
            appr, rej, warn = _evaluate_single_order(
                order=order,
                portfolio=portfolio,
                config=config,
                level=level,
                orders_approved_so_far=orders_approved_count,
                notional_approved_so_far=notional_approved_acc,
            )
            if appr is not None:
                approved.append(appr)
                orders_approved_count += 1
                notional_approved_acc += appr.notional
            elif rej is not None:
                rejected.append(rej)
            if warn is not None:
                warnings.append(warn)

        return RiskDecision(
            level=level,
            approved_orders=tuple(approved),
            rejected_orders=tuple(rejected),
            portfolio_metrics=metrics,
            warnings=tuple(warnings),
            timestamp=now,
        )
