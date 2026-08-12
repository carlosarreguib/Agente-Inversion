"""Tests de T5.1: BrokerInterface Protocol y tipos del contrato.

Tres verificaciones:
  1. Un mock que implementa BrokerInterface pasa isinstance() check.
  2. Round-trip JSON (serialize → deserialize) para cada tipo nuevo.
  3. OrderStatus tiene exactamente los 7 valores esperados.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from qtrader.brokers import (
    AccountState,
    BrokerInterface,
    BrokerPosition,
    CancelResult,
    OrderAcknowledgement,
    OrderStatus,
)

_NOW = datetime(2024, 1, 16, 10, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Mock que implementa el Protocol estructuralmente
# ---------------------------------------------------------------------------

class _MockBroker:
    """Implementación mínima del Protocol para tests isinstance."""

    async def submit_order(self, order, client_order_id):  # type: ignore[override]
        raise NotImplementedError

    async def cancel_order(self, client_order_id):  # type: ignore[override]
        raise NotImplementedError

    async def get_order_status(self, client_order_id):  # type: ignore[override]
        raise NotImplementedError

    async def get_positions(self):  # type: ignore[override]
        raise NotImplementedError

    async def get_account(self):  # type: ignore[override]
        raise NotImplementedError

    async def get_fills_since(self, since):  # type: ignore[override]
        raise NotImplementedError


# ---------------------------------------------------------------------------
# 1. isinstance() check
# ---------------------------------------------------------------------------

def test_mock_broker_is_instance_of_protocol() -> None:
    """Un objeto con los 6 métodos async pasa isinstance(BrokerInterface)."""
    mock = _MockBroker()
    assert isinstance(mock, BrokerInterface)


def test_plain_object_is_not_instance_of_protocol() -> None:
    """Un objeto sin los métodos no pasa el check."""
    assert not isinstance(object(), BrokerInterface)


def test_partial_implementation_is_not_instance() -> None:
    """Un objeto con solo algunos métodos no pasa el check."""
    class _Partial:
        async def submit_order(self, order, client_order_id):  # type: ignore[override]
            ...

    assert not isinstance(_Partial(), BrokerInterface)


# ---------------------------------------------------------------------------
# 2. Round-trip JSON para cada tipo nuevo
# ---------------------------------------------------------------------------

def test_order_status_roundtrip() -> None:
    s = OrderStatus.SUBMITTED
    assert OrderStatus(s.value) == s


def test_order_acknowledgement_roundtrip() -> None:
    ack = OrderAcknowledgement(
        client_order_id="ord-0001",
        broker_order_id="BRK-42",
        status=OrderStatus.SUBMITTED,
        timestamp=_NOW,
    )
    data = ack.model_dump_json()
    restored = OrderAcknowledgement.model_validate_json(data)
    assert restored == ack


def test_order_acknowledgement_none_broker_id_roundtrip() -> None:
    ack = OrderAcknowledgement(
        client_order_id="ord-0002",
        broker_order_id=None,
        status=OrderStatus.PENDING,
        timestamp=_NOW,
    )
    data = ack.model_dump_json()
    restored = OrderAcknowledgement.model_validate_json(data)
    assert restored == ack
    assert restored.broker_order_id is None


def test_cancel_result_success_roundtrip() -> None:
    r = CancelResult(client_order_id="ord-0003", success=True, reason=None)
    restored = CancelResult.model_validate_json(r.model_dump_json())
    assert restored == r
    assert restored.reason is None


def test_cancel_result_failure_roundtrip() -> None:
    r = CancelResult(
        client_order_id="ord-0004",
        success=False,
        reason="ORDER_ALREADY_FILLED",
    )
    restored = CancelResult.model_validate_json(r.model_dump_json())
    assert restored == r


def test_account_state_roundtrip() -> None:
    acc = AccountState(
        cash=Decimal("12345.67"),
        nav=Decimal("15000.00"),
        buying_power=Decimal("12000.00"),
        currency="EUR",
        timestamp=_NOW,
    )
    restored = AccountState.model_validate_json(acc.model_dump_json())
    assert restored == acc
    assert restored.cash == Decimal("12345.67")


def test_broker_position_roundtrip() -> None:
    pos = BrokerPosition(
        symbol="SPY",
        quantity=Decimal("10"),
        avg_cost=Decimal("450.00"),
        market_value=Decimal("4600.00"),
        unrealized_pnl=Decimal("100.00"),
        currency="USD",
    )
    restored = BrokerPosition.model_validate_json(pos.model_dump_json())
    assert restored == pos


# ---------------------------------------------------------------------------
# 3. OrderStatus — valores exactos
# ---------------------------------------------------------------------------

_EXPECTED_STATUSES = {
    "PENDING", "SUBMITTED", "PARTIAL", "FILLED",
    "CANCELLED", "REJECTED", "UNKNOWN",
}


def test_order_status_has_exactly_seven_values() -> None:
    assert {s.value for s in OrderStatus} == _EXPECTED_STATUSES


@pytest.mark.parametrize("value", sorted(_EXPECTED_STATUSES))
def test_order_status_constructible_from_string(value: str) -> None:
    assert OrderStatus(value).value == value


# ---------------------------------------------------------------------------
# 4. Tipos frozen — inmutabilidad
# ---------------------------------------------------------------------------

def test_order_acknowledgement_is_frozen() -> None:
    ack = OrderAcknowledgement(
        client_order_id="ord-x",
        broker_order_id=None,
        status=OrderStatus.PENDING,
        timestamp=_NOW,
    )
    with pytest.raises(Exception):
        ack.status = OrderStatus.FILLED  # type: ignore[misc]


def test_account_state_is_frozen() -> None:
    acc = AccountState(
        cash=Decimal("1000"),
        nav=Decimal("1000"),
        buying_power=Decimal("1000"),
        currency="EUR",
        timestamp=_NOW,
    )
    with pytest.raises(Exception):
        acc.cash = Decimal("0")  # type: ignore[misc]


def test_broker_position_is_frozen() -> None:
    pos = BrokerPosition(
        symbol="GLD",
        quantity=Decimal("5"),
        avg_cost=Decimal("180"),
        market_value=Decimal("900"),
        unrealized_pnl=Decimal("0"),
        currency="USD",
    )
    with pytest.raises(Exception):
        pos.quantity = Decimal("10")  # type: ignore[misc]
