from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

from qtrader.core.types import RiskDecision, RiskLevel

_MAX_WEIGHT = Decimal("0.20")
_ZERO = Decimal("0")


def evaluate(
    client_order_id: str,
    proposed_quantity: Decimal,
    equity: Decimal,
    fill_price: Decimal,
    timestamp: datetime,
    max_weight: Decimal = _MAX_WEIGHT,
) -> RiskDecision:
    """Risk Engine puro: sin I/O, sin estado, sin aleatoriedad (invariante §2.2).

    Devuelve APPROVE, REDUCE (con cantidad ajustada) o REJECT.
    """
    if fill_price <= _ZERO or equity <= _ZERO:
        return RiskDecision(
            client_order_id=client_order_id,
            decision=RiskLevel.REJECT,
            reason="fill_price or equity is zero or negative",
            timestamp=timestamp,
        )

    max_nominal = equity * max_weight
    max_qty = (max_nominal / fill_price).quantize(Decimal("1"), rounding=ROUND_DOWN)

    if max_qty <= _ZERO:
        return RiskDecision(
            client_order_id=client_order_id,
            decision=RiskLevel.REJECT,
            reason=f"max_qty=0 at price {fill_price} with equity {equity}",
            timestamp=timestamp,
        )

    if proposed_quantity <= max_qty:
        return RiskDecision(
            client_order_id=client_order_id,
            decision=RiskLevel.APPROVE,
            adjusted_quantity=proposed_quantity,
            timestamp=timestamp,
        )

    return RiskDecision(
        client_order_id=client_order_id,
        decision=RiskLevel.REDUCE,
        adjusted_quantity=max_qty,
        reason=f"reduced {proposed_quantity}→{max_qty} (max_weight={max_weight})",
        timestamp=timestamp,
    )
