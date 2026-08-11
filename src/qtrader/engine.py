from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

from qtrader.brokers.paper import fill_at_open
from qtrader.core.types import (
    AuditRecord,
    Direction,
    Fill,
    Order,
    OrderType,
    RiskDecision,
    RiskLevel,
    Side,
)
from qtrader.data.synthetic import SyntheticProvider
from qtrader.ledger.sqlite import SQLiteLedger
from qtrader.risk.engine import evaluate as risk_evaluate
from qtrader.strategies.sma import generate_signal

_ZERO = Decimal("0")
_LARGE_QTY = Decimal("9999")  # propuesta inicial; el Risk Engine la limita
_NULL_HASH = "0" * 64

# Parámetros del stub de config — en fases posteriores vendrán de un fichero YAML
_CONFIG_STUB: dict[str, str] = {
    "strategy": "sma20",
    "sma_period": "20",
    "risk_max_weight": "0.20",
    "demo_equity": "1000",
}
_CONFIG_HASH: str = hashlib.sha256(
    json.dumps(_CONFIG_STUB, sort_keys=True).encode()
).hexdigest()

_SMA_PARAMS: dict[str, str] = {"sma_period": "20"}


def _get_git_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            sha = result.stdout.strip()
            if sha:
                return sha
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return "unknown"


_GIT_SHA: str = _get_git_sha()


@dataclass(frozen=True)
class DemoResult:
    initial_equity: Decimal
    final_equity: Decimal
    trades: int
    pnl: Decimal


def run_demo(
    symbol: str = "SPY",
    days: int = 60,
    initial_equity: Decimal = Decimal("1000"),
    seed: int = 42,
    db_path: str = "data/state.db",
) -> DemoResult:
    """Camino end-to-end con datos sintéticos.

    Señal generada con datos de bars[i] (cierre de T).
    Fill ejecutado al precio de apertura de bars[i+1] (T+1).
    """
    bars = SyntheticProvider(seed=seed).get_bars_n(symbol, days + 1)

    ledger = SQLiteLedger(db_path)
    ledger.initialize()

    cash = initial_equity
    position_qty = _ZERO
    trades = 0
    order_seq = 0

    for i in range(days):
        signal_bars = bars[: i + 1]
        signal = generate_signal(signal_bars)

        if signal is None:
            continue

        fill_bar = bars[i + 1]
        order_ts = signal_bars[-1].timestamp

        if signal.direction == Direction.LONG and position_qty == _ZERO:
            order_seq += 1
            order_id = f"ord-{order_seq:04d}"

            risk = risk_evaluate(
                client_order_id=order_id,
                proposed_quantity=_LARGE_QTY,
                equity=cash,
                fill_price=fill_bar.open,
                timestamp=order_ts,
            )

            if risk.decision == RiskLevel.REJECT or risk.adjusted_quantity is None:
                _record_audit(ledger, "RISK_REJECT", order_id, order_ts, {}, risk)
                continue

            qty = risk.adjusted_quantity
            order = Order(
                client_order_id=order_id,
                symbol=symbol,
                side=Side.BUY,
                quantity=qty,
                order_type=OrderType.MOO,
                timestamp=order_ts,
                strategy_id="sma20",
            )
            fill = fill_at_open(order, fill_bar)
            cash -= fill.quantity * fill.price + fill.commission
            position_qty = fill.quantity
            ledger.record_fill(fill)
            _record_audit_fill(ledger, fill, risk)
            trades += 1

        elif signal.direction != Direction.LONG and position_qty > _ZERO:
            order_seq += 1
            order_id = f"ord-{order_seq:04d}"

            order = Order(
                client_order_id=order_id,
                symbol=symbol,
                side=Side.SELL,
                quantity=position_qty,
                order_type=OrderType.MOO,
                timestamp=order_ts,
                strategy_id="sma20",
            )
            fill = fill_at_open(order, fill_bar)
            cash += fill.quantity * fill.price - fill.commission
            position_qty = _ZERO
            ledger.record_fill(fill)
            _record_audit_fill(ledger, fill)
            trades += 1

    last_bar = bars[days]
    final_equity = (cash + position_qty * last_bar.close).quantize(
        Decimal("0.01"), rounding=ROUND_DOWN
    )
    pnl = (final_equity - initial_equity).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

    return DemoResult(
        initial_equity=initial_equity,
        final_equity=final_equity,
        trades=trades,
        pnl=pnl,
    )


# ---------------------------------------------------------------------------
# Helpers de auditoría — T0.3: previous_hash encadenado, hash real
# ---------------------------------------------------------------------------


def _hash_payload(payload: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _risk_to_dict(risk: RiskDecision) -> dict[str, str]:
    out: dict[str, str] = {"decision": risk.decision.value}
    if risk.adjusted_quantity is not None:
        out["adjusted_quantity"] = str(risk.adjusted_quantity)
    if risk.reason is not None:
        out["reason"] = risk.reason
    return out


def _record_audit_fill(
    ledger: SQLiteLedger,
    fill: Fill,
    risk: RiskDecision | None = None,
) -> None:
    previous_hash = ledger.get_last_record_hash()
    payload: dict[str, str] = {
        "fill_id": fill.fill_id,
        "symbol": fill.symbol,
        "side": fill.side.value,
        "quantity": str(fill.quantity),
        "price": str(fill.price),
        "commission": str(fill.commission),
    }
    risk_output = _risk_to_dict(risk) if risk is not None else {}
    rec = AuditRecord(
        record_id=f"audit-{fill.fill_id}",
        timestamp=fill.timestamp,
        event_type="FILL",
        data_hash=_hash_payload(payload),
        previous_hash=previous_hash,
        strategy_id="sma20",
        payload=payload,
        git_sha=_GIT_SHA,
        config_hash=_CONFIG_HASH,
        parameters=_SMA_PARAMS,
        risk_output=risk_output,
        decision="FILL",
        reason="",
    )
    ledger.record_audit(rec)


def _record_audit(
    ledger: SQLiteLedger,
    event_type: str,
    order_id: str,
    ts: datetime,
    extra: dict[str, str],
    risk: RiskDecision | None = None,
) -> None:
    previous_hash = ledger.get_last_record_hash()
    payload: dict[str, str] = {"order_id": order_id, **extra}
    risk_output = _risk_to_dict(risk) if risk is not None else {}
    rec = AuditRecord(
        record_id=f"audit-{event_type}-{order_id}",
        timestamp=ts,
        event_type=event_type,
        data_hash=_hash_payload(payload),
        previous_hash=previous_hash,
        strategy_id="sma20",
        payload=payload,
        git_sha=_GIT_SHA,
        config_hash=_CONFIG_HASH,
        parameters=_SMA_PARAMS,
        risk_output=risk_output,
        decision=event_type,
        reason=risk.reason if risk is not None and risk.reason is not None else "",
    )
    ledger.record_audit(rec)
