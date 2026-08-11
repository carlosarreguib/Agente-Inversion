from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any

from pydantic import AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# Tipos de datos de mercado — añadidos en T1.1
# ---------------------------------------------------------------------------


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


class CorporateActionType(StrEnum):
    SPLIT = "SPLIT"
    DIVIDEND = "DIVIDEND"
    SPIN_OFF = "SPIN_OFF"


class CorporateAction(BaseModel):
    """Corporate action que afecta al precio histórico de un instrumento.

    Para un split N:1, factor = N (los precios anteriores se dividen por N).
    Para dividendos, factor = (price - dividend) / price.
    Almacenada por separado de los precios sin ajustar (invariante §3.2).
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    action_type: CorporateActionType
    effective_date: AwareDatetime
    factor: StrictDecimal  # multiplicador que se aplica a los precios anteriores

    @model_validator(mode="after")
    def _check_factor(self) -> CorporateAction:
        if self.factor <= _ZERO:
            raise ValueError(f"factor must be > 0, got {self.factor}")
        return self


class DataQuality(StrEnum):
    OK = "OK"
    SUSPECT = "SUSPECT"


class ValidatedBar(BaseModel):
    """Bar con metadatos de calidad de datos.

    suspect=True cuando dos fuentes difieren > 0.5 % en el cierre.
    Un bar SUSPECT se excluye del universo ese día y se registra en auditoría.
    """

    model_config = ConfigDict(frozen=True)

    bar: Bar
    quality: DataQuality = DataQuality.OK
    quality_reason: str = ""


class Severity(StrEnum):
    WARN = "WARN"        # anómalo pero se puede operar; se registra
    EXCLUDE = "EXCLUDE"  # bar excluido del universo ese día
    HALT = "HALT"        # corrupción sistémica; el agente debe detenerse


class ValidationResult(BaseModel):
    """Resultado de una regla de validación sobre un bar o serie de bars.

    El validador es puro: devuelve estos objetos; el llamador decide qué hacer.
    Pydantic frozen=True para que no mute tras creación.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    date: AwareDatetime
    rule_violated: str  # slug: gap, duplicate, ohlc, negative_price, zero_volume,
    #                      price_spike, frozen_series, cross_validation
    severity: Severity
    details: str


class AuditRecord(BaseModel):
    """Registro de auditoría inmutable con hash encadenado.

    payload usa dict[str, str] (no Any) para cumplir mypy strict (ADR-001).
    data_hash: hash del snapshot de datos usado para generar la señal.
    previous_hash: record_hash del registro anterior en la cadena (ADR-003).
    """

    model_config = ConfigDict(frozen=True)

    record_id: str
    timestamp: AwareDatetime
    event_type: str
    data_hash: str
    previous_hash: str
    strategy_id: str | None = None
    payload: dict[str, str] = Field(default_factory=dict)

    # Campos añadidos en T0.3 — con defaults para no romper tests existentes
    git_sha: str = "unknown"
    config_hash: str = "0" * 64
    parameters: dict[str, str] = Field(default_factory=dict)
    risk_output: dict[str, str] = Field(default_factory=dict)
    decision: str = ""
    reason: str = ""
