"""Tipos del contrato BrokerInterface (T5.1).

Todos los tipos son Pydantic frozen=True.
Ninguno importa de qtrader.risk, qtrader.agents ni qtrader.portfolio.
"""
from __future__ import annotations

from datetime import datetime  # noqa: TCH003
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

_ZERO = Decimal("0")


class OrderStatus(StrEnum):
    """Estado de una orden según el broker."""

    PENDING = "PENDING"       # recibida localmente, no enviada aún
    SUBMITTED = "SUBMITTED"   # enviada al broker, sin confirmación de fill
    PARTIAL = "PARTIAL"       # fill parcial recibido
    FILLED = "FILLED"         # fill completo
    CANCELLED = "CANCELLED"   # cancelada con éxito
    REJECTED = "REJECTED"     # rechazada por el broker
    UNKNOWN = "UNKNOWN"       # estado no determinable (p. ej. timeout)


class OrderAcknowledgement(BaseModel):
    """Acuse de recibo del broker tras submit_order.

    broker_order_id es None cuando el broker no confirma el ID interno
    de forma síncrona (p. ej. IBKR en modo async). En ese caso se resuelve
    con get_order_status(client_order_id).
    """

    model_config = ConfigDict(frozen=True)

    client_order_id: str
    broker_order_id: str | None
    status: OrderStatus
    timestamp: datetime


class CancelResult(BaseModel):
    """Resultado de cancel_order."""

    model_config = ConfigDict(frozen=True)

    client_order_id: str
    success: bool
    reason: str | None  # None si success=True


class AccountState(BaseModel):
    """Estado de la cuenta tal como lo reporta el broker.

    Todos los valores monetarios en la divisa declarada en `currency`.
    """

    model_config = ConfigDict(frozen=True)

    cash: Decimal
    nav: Decimal           # Net Asset Value = cash + market_value de posiciones
    buying_power: Decimal
    currency: str
    timestamp: datetime


class BrokerPosition(BaseModel):
    """Posición abierta según el broker (fuente de verdad).

    Distinta de core.types.Position (caché local): incluye `currency`
    y `market_value` tal como los reporta el broker en tiempo real.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    quantity: Decimal
    avg_cost: Decimal
    market_value: Decimal
    unrealized_pnl: Decimal
    currency: str
