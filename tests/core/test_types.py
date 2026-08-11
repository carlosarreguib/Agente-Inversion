"""Tests de validación para qtrader.core.types.

Cobertura: invariantes de tipos, inmutabilidad, rechazo de float,
timestamps tz-aware, y las validaciones específicas de cada modelo.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from qtrader.core.types import (
    AuditRecord,
    Bar,
    Direction,
    Fill,
    Instrument,
    InstrumentType,
    Order,
    OrderType,
    Position,
    RiskDecision,
    RiskLevel,
    Side,
    Signal,
    TargetPosition,
)

NOW = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)
D = Decimal  # alias corto para tests


# ---------------------------------------------------------------------------
# Helpers de construcción
# ---------------------------------------------------------------------------


def _bar(**kwargs: object) -> Bar:
    defaults: dict[str, object] = {
        "symbol": "SPY",
        "timestamp": NOW,
        "open": D("400"),
        "high": D("405"),
        "low": D("398"),
        "close": D("402"),
        "volume": D("1000000"),
    }
    defaults.update(kwargs)
    return Bar(**defaults)


def _order(**kwargs: object) -> Order:
    defaults: dict[str, object] = {
        "client_order_id": "ord-001",
        "symbol": "SPY",
        "side": Side.BUY,
        "quantity": D("10"),
        "order_type": OrderType.LIMIT,
        "limit_price": D("400"),
        "timestamp": NOW,
        "strategy_id": "momentum",
    }
    defaults.update(kwargs)
    return Order(**defaults)


def _fill(**kwargs: object) -> Fill:
    defaults: dict[str, object] = {
        "client_order_id": "ord-001",
        "fill_id": "fill-001",
        "symbol": "SPY",
        "side": Side.BUY,
        "quantity": D("10"),
        "price": D("400.50"),
        "commission": D("1.25"),
        "timestamp": NOW,
    }
    defaults.update(kwargs)
    return Fill(**defaults)


def _signal(**kwargs: object) -> Signal:
    defaults: dict[str, object] = {
        "symbol": "SPY",
        "timestamp": NOW,
        "direction": Direction.LONG,
        "strength": D("0.8"),
        "strategy_id": "momentum",
    }
    defaults.update(kwargs)
    return Signal(**defaults)


def _risk_decision(**kwargs: object) -> RiskDecision:
    defaults: dict[str, object] = {
        "client_order_id": "ord-001",
        "decision": RiskLevel.APPROVE,
        "timestamp": NOW,
    }
    defaults.update(kwargs)
    return RiskDecision(**defaults)


def _audit(**kwargs: object) -> AuditRecord:
    defaults: dict[str, object] = {
        "record_id": "rec-001",
        "timestamp": NOW,
        "event_type": "ORDER_PROPOSED",
        "data_hash": "abc123",
        "previous_hash": "000000",
    }
    defaults.update(kwargs)
    return AuditRecord(**defaults)


# ---------------------------------------------------------------------------
# Order — criterio de aceptación explícito
# ---------------------------------------------------------------------------


def test_order_rejects_negative_quantity() -> None:
    with pytest.raises(ValidationError):
        _order(quantity=D("-1"))


def test_order_rejects_zero_quantity() -> None:
    with pytest.raises(ValidationError):
        _order(quantity=D("0"))


def test_order_accepts_positive_quantity() -> None:
    o = _order(quantity=D("5"))
    assert o.quantity == D("5")


# ---------------------------------------------------------------------------
# Bar — OHLCV constraints
# ---------------------------------------------------------------------------


def test_bar_rejects_low_greater_than_high() -> None:
    with pytest.raises(ValidationError):
        _bar(low=D("410"), high=D("405"))


def test_bar_rejects_zero_open() -> None:
    with pytest.raises(ValidationError):
        _bar(open=D("0"))


def test_bar_rejects_negative_close() -> None:
    with pytest.raises(ValidationError):
        _bar(close=D("-1"), low=D("-1"))


def test_bar_rejects_negative_volume() -> None:
    with pytest.raises(ValidationError):
        _bar(volume=D("-1"))


def test_bar_accepts_valid() -> None:
    b = _bar()
    assert b.low <= b.high


# ---------------------------------------------------------------------------
# Signal — strength ∈ [0, 1]
# ---------------------------------------------------------------------------


def test_signal_rejects_strength_above_one() -> None:
    with pytest.raises(ValidationError):
        _signal(strength=D("1.01"))


def test_signal_rejects_strength_below_zero() -> None:
    with pytest.raises(ValidationError):
        _signal(strength=D("-0.01"))


def test_signal_accepts_boundary_strength() -> None:
    assert _signal(strength=D("0")).strength == D("0")
    assert _signal(strength=D("1")).strength == D("1")


# ---------------------------------------------------------------------------
# TargetPosition — weight ∈ [0, 1]
# ---------------------------------------------------------------------------


def test_target_position_rejects_weight_out_of_range() -> None:
    with pytest.raises(ValidationError):
        TargetPosition(symbol="SPY", quantity=D("10"), weight=D("1.5"))


def test_target_position_accepts_boundary_weight() -> None:
    tp = TargetPosition(symbol="SPY", quantity=D("10"), weight=D("0"))
    assert tp.weight == D("0")


# ---------------------------------------------------------------------------
# Fill — price, quantity, commission constraints
# ---------------------------------------------------------------------------


def test_fill_rejects_zero_price() -> None:
    with pytest.raises(ValidationError):
        _fill(price=D("0"))


def test_fill_rejects_negative_commission() -> None:
    with pytest.raises(ValidationError):
        _fill(commission=D("-0.01"))


def test_fill_rejects_zero_quantity() -> None:
    with pytest.raises(ValidationError):
        _fill(quantity=D("0"))


# ---------------------------------------------------------------------------
# RiskDecision — REDUCE requires adjusted_quantity
# ---------------------------------------------------------------------------


def test_risk_decision_reduce_without_quantity_fails() -> None:
    with pytest.raises(ValidationError):
        _risk_decision(decision=RiskLevel.REDUCE, adjusted_quantity=None)


def test_risk_decision_reduce_with_quantity_ok() -> None:
    rd = _risk_decision(decision=RiskLevel.REDUCE, adjusted_quantity=D("5"))
    assert rd.adjusted_quantity == D("5")


def test_risk_decision_approve_without_quantity_ok() -> None:
    rd = _risk_decision(decision=RiskLevel.APPROVE)
    assert rd.adjusted_quantity is None


# ---------------------------------------------------------------------------
# Inmutabilidad — frozen=True
# ---------------------------------------------------------------------------


def test_bar_is_immutable() -> None:
    b = _bar()
    with pytest.raises((ValidationError, TypeError)):
        b.symbol = "QQQ"  # type: ignore[misc]


def test_order_is_immutable() -> None:
    o = _order()
    with pytest.raises((ValidationError, TypeError)):
        o.quantity = D("999")  # type: ignore[misc]


def test_signal_is_immutable() -> None:
    s = _signal()
    with pytest.raises((ValidationError, TypeError)):
        s.strength = D("0.5")  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Timestamps — deben ser tz-aware
# ---------------------------------------------------------------------------


def test_bar_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError):
        _bar(timestamp=datetime(2024, 1, 15, 10, 0, 0))  # sin tz


def test_order_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError):
        _order(timestamp=datetime(2024, 1, 15, 10, 0, 0))


def test_signal_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError):
        _signal(timestamp=datetime(2024, 1, 15, 10, 0, 0))


# ---------------------------------------------------------------------------
# Campos monetarios rechazan float
# ---------------------------------------------------------------------------


def test_bar_rejects_float_price() -> None:
    with pytest.raises(ValidationError):
        _bar(open=400.0)  # type: ignore[arg-type]


def test_order_rejects_float_quantity() -> None:
    with pytest.raises(ValidationError):
        _order(quantity=10.0)  # type: ignore[arg-type]


def test_fill_rejects_float_commission() -> None:
    with pytest.raises(ValidationError):
        _fill(commission=1.25)  # type: ignore[arg-type]


def test_signal_rejects_float_strength() -> None:
    with pytest.raises(ValidationError):
        _signal(strength=0.8)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Construcción correcta de todos los modelos
# ---------------------------------------------------------------------------


def test_instrument_roundtrip() -> None:
    inst = Instrument(
        symbol="CSPX",
        name="iShares Core S&P 500 UCITS ETF",
        exchange="LSE",
        currency="USD",
        instrument_type=InstrumentType.ETF,
    )
    assert inst.symbol == "CSPX"


def test_position_construction() -> None:
    pos = Position(
        symbol="CSPX",
        quantity=D("5"),
        avg_cost=D("450"),
        market_value=D("2275"),
        unrealized_pnl=D("25"),
    )
    assert pos.unrealized_pnl == D("25")


def test_audit_record_construction() -> None:
    rec = _audit(payload={"strategy": "momentum", "cycle": "2024-01-15"})
    assert rec.payload["strategy"] == "momentum"
    assert rec.strategy_id is None


def test_audit_record_with_strategy_id() -> None:
    rec = _audit(strategy_id="momentum")
    assert rec.strategy_id == "momentum"
