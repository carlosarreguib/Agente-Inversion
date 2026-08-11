from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any

from pydantic import AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field, model_validator


def _reject_float(v: Any) -> Any:
    if isinstance(v, float):
        raise ValueError("float values are not allowed; use Decimal or str")
    return v


# Decimal que rechaza float en tiempo de validación (invariante CLAUDE.md §4).
type StrictDecimal = Annotated[Decimal, BeforeValidator(_reject_float)]

_ZERO = Decimal("0")
_ONE = Decimal("1")


class InstrumentType(StrEnum):
    EQUITY = "EQUITY"
    ETF = "ETF"
    BOND = "BOND"
    COMMODITY = "COMMODITY"


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"
    MOO = "MOO"  # Market On Open — orden al precio de apertura


class RiskLevel(StrEnum):
    APPROVE = "APPROVE"
    REDUCE = "REDUCE"
    REJECT = "REJECT"


class Bar(BaseModel):
    """OHLCV diaria. Precios sin ajustar; las corporate actions van en tabla separada."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    timestamp: AwareDatetime
    open: StrictDecimal
    high: StrictDecimal
    low: StrictDecimal
    close: StrictDecimal
    volume: StrictDecimal

    @model_validator(mode="after")
    def _check_ohlcv(self) -> Bar:
        if self.low > self.high:
            raise ValueError(f"low ({self.low}) must be <= high ({self.high})")
        for name, price in (
            ("open", self.open),
            ("high", self.high),
            ("low", self.low),
            ("close", self.close),
        ):
            if price <= _ZERO:
                raise ValueError(f"{name} must be > 0, got {price}")
        if self.volume < _ZERO:
            raise ValueError(f"volume must be >= 0, got {self.volume}")
        return self


class Instrument(BaseModel):
    """Instrumento negociable. El universo se declara en config/universe.yaml."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    name: str
    exchange: str
    currency: str
    instrument_type: InstrumentType


class Signal(BaseModel):
    """Señal de trading generada por una estrategia."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    timestamp: AwareDatetime
    direction: Direction
    strength: StrictDecimal  # [0, 1]
    strategy_id: str

    @model_validator(mode="after")
    def _check_strength(self) -> Signal:
        if not (_ZERO <= self.strength <= _ONE):
            raise ValueError(f"strength must be in [0, 1], got {self.strength}")
        return self


class TargetPosition(BaseModel):
    """Posición objetivo del portfolio constructor."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    quantity: StrictDecimal
    weight: StrictDecimal  # [0, 1]

    @model_validator(mode="after")
    def _check_weight(self) -> TargetPosition:
        if not (_ZERO <= self.weight <= _ONE):
            raise ValueError(f"weight must be in [0, 1], got {self.weight}")
        return self


class Order(BaseModel):
    """Orden de trading. El LLM nunca toca este contrato directamente (invariante §2.1)."""

    model_config = ConfigDict(frozen=True)

    client_order_id: str
    symbol: str
    side: Side
    quantity: StrictDecimal  # siempre positivo; la dirección la da `side`
    order_type: OrderType
    limit_price: StrictDecimal | None = None
    timestamp: AwareDatetime
    strategy_id: str

    @model_validator(mode="after")
    def _check_quantity(self) -> Order:
        if self.quantity <= _ZERO:
            raise ValueError(f"quantity must be > 0, got {self.quantity}")
        return self


class Fill(BaseModel):
    """Confirmación de ejecución devuelta por el broker."""

    model_config = ConfigDict(frozen=True)

    client_order_id: str
    fill_id: str
    symbol: str
    side: Side
    quantity: StrictDecimal
    price: StrictDecimal
    commission: StrictDecimal
    timestamp: AwareDatetime

    @model_validator(mode="after")
    def _check_fill(self) -> Fill:
        if self.quantity <= _ZERO:
            raise ValueError(f"quantity must be > 0, got {self.quantity}")
        if self.price <= _ZERO:
            raise ValueError(f"price must be > 0, got {self.price}")
        if self.commission < _ZERO:
            raise ValueError(f"commission must be >= 0, got {self.commission}")
        return self


class Position(BaseModel):
    """Posición actual en cartera (caché local; el broker es la fuente de verdad)."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    quantity: StrictDecimal
    avg_cost: StrictDecimal
    market_value: StrictDecimal
    unrealized_pnl: StrictDecimal


class RiskDecision(BaseModel):
    """Decisión del Risk Engine. El Risk Engine es puro: sin I/O (invariante §2.2)."""

    model_config = ConfigDict(frozen=True)

    client_order_id: str
    decision: RiskLevel
    adjusted_quantity: StrictDecimal | None = None
    reason: str | None = None
    timestamp: AwareDatetime

    @model_validator(mode="after")
    def _check_reduce(self) -> RiskDecision:
        if self.decision == RiskLevel.REDUCE and self.adjusted_quantity is None:
            raise ValueError("adjusted_quantity is required when decision is REDUCE")
        return self


class AuditRecord(BaseModel):
    """Registro de auditoría inmutable con hash encadenado.

    payload usa dict[str, str] (no Any) para cumplir mypy strict.
    Ver docs/DECISIONS.md ADR-001.
    """

    model_config = ConfigDict(frozen=True)

    record_id: str
    timestamp: AwareDatetime
    event_type: str
    data_hash: str
    previous_hash: str
    strategy_id: str | None = None
    payload: dict[str, str] = Field(default_factory=dict)
