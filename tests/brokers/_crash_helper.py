"""Script auxiliar para tests de crash de idempotencia (T5.3).

Uso:
    python _crash_helper.py <db_path> <scenario> [signal_file]

Scenarios:
    submit_before_broker  — muere despues del INSERT en order_intentions
                            pero antes del INSERT en orders (crash_1)
    submit_after_broker   — muere despues del INSERT en orders pero antes
                            de UPDATE intention->SUBMITTED (crash_2)
    fill_before_confirmed — muere despues del fill registrado pero antes
                            de UPDATE intention->CONFIRMED (crash_3)
    advance_mid_fill      — muere a mitad de advance_to() (crash_4)

Protocolo de sincronizacion:
    El script imprime "READY:<step>" a stdout en cada punto de sincronizacion.
    El test espera ese mensaje, luego escribe en signal_file para indicar
    que puede continuar (o mata el proceso con SIGKILL si es el punto de crash).
    Si no se proporciona signal_file, el script corre hasta el final (recovery).

El script termina con exit code 0 en exito, != 0 si hay error inesperado.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import date, datetime, UTC
from decimal import Decimal
from pathlib import Path

# Asegurar que el paquete src/ es importable
_repo_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_repo_root / "src"))

from qtrader.brokers.paper_broker import PaperBroker
from qtrader.brokers.order_id import generate_client_order_id, next_sequence
from qtrader.core.types import Side
from qtrader.costs import CostsConfig

import sqlite3


def _wait_signal(signal_file: str | None) -> None:
    """Si signal_file esta definido, espera hasta que exista."""
    if signal_file is None:
        return
    while not Path(signal_file).exists():
        time.sleep(0.01)
    Path(signal_file).unlink(missing_ok=True)


def _print_ready(step: str) -> None:
    print(f"READY:{step}", flush=True)


def _make_order(broker: PaperBroker, symbol: str = "TST") -> tuple[object, str]:
    """Crea un ApprovedOrder minimo y genera su client_order_id."""
    from qtrader.risk.types import ApprovedOrder

    trading_date = date(2026, 1, 5)
    with broker._conn:
        seq = next_sequence(broker._conn)

    coid = generate_client_order_id(
        trading_date=trading_date,
        symbol=symbol,
        side=Side.BUY,
        strategy_id="crash_test",
        sequence_number=seq,
    )
    order = ApprovedOrder(
        order_id=coid,
        symbol=symbol,
        side=Side.BUY,
        original_quantity=Decimal("10"),
        approved_quantity=Decimal("10"),
        notional=Decimal("1000"),
        reduction_reason=None,
    )
    return order, coid


def scenario_submit_before_broker(db_path: str, signal_file: str | None) -> None:
    """crash_1: muere tras INSERT intention, antes de INSERT en orders."""
    broker = PaperBroker(db_path, Decimal("15000"), CostsConfig.zero_costs(), _testing=True)
    order, coid = _make_order(broker)

    # Insertar intencion manualmente (simula el paso 1 de submit_order)
    now = datetime.now(UTC).isoformat()
    with broker._conn:
        broker._conn.execute(
            """
            INSERT OR IGNORE INTO order_intentions
              (client_order_id, symbol, side, quantity, strategy_id,
               trading_date, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (coid, "TST", "BUY", "10", "crash_test", "2026-01-05",
             "PENDING_UNKNOWN", now, now),
        )

    _print_ready("after_intention_insert")
    # El test manda SIGKILL aqui: muere con intencion PENDING_UNKNOWN sin orden en broker
    _wait_signal(signal_file)
    # Si llegamos aqui es porque el test NO mato el proceso (modo recovery)
    # En recovery: no hacemos nada; reconcile() en __init__ ya lo habra manejado
    broker.close()


def scenario_submit_after_broker(db_path: str, signal_file: str | None) -> None:
    """crash_2: muere tras INSERT en orders, antes de UPDATE intention->SUBMITTED."""
    broker = PaperBroker(db_path, Decimal("15000"), CostsConfig.zero_costs(), _testing=True)
    order, coid = _make_order(broker)

    now = datetime.now(UTC).isoformat()
    import uuid
    broker_order_id = str(uuid.uuid4())

    # Paso 1: INSERT intention
    with broker._conn:
        broker._conn.execute(
            """
            INSERT OR IGNORE INTO order_intentions
              (client_order_id, symbol, side, quantity, strategy_id,
               trading_date, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (coid, "TST", "BUY", "10", "crash_test", "2026-01-05",
             "PENDING_UNKNOWN", now, now),
        )

    # Paso 2: INSERT en orders (simulando submit_order interno)
    with broker._conn:
        broker._conn.execute(
            """
            INSERT OR IGNORE INTO orders
              (client_order_id, broker_order_id, symbol, side,
               quantity, approved_qty, status, submitted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (coid, broker_order_id, "TST", "BUY", "10", "10", "SUBMITTED", now),
        )

    _print_ready("after_order_insert")
    # El test manda SIGKILL aqui: intencion=PENDING_UNKNOWN, order=SUBMITTED
    _wait_signal(signal_file)
    # Recovery: reconcile() ya actualizo la intencion a SUBMITTED
    broker.close()


def scenario_fill_before_confirmed(db_path: str, signal_file: str | None) -> None:
    """crash_3: muere tras fill registrado en orders, antes de UPDATE->CONFIRMED."""
    from datetime import date as _date
    broker = PaperBroker(db_path, Decimal("15000"), CostsConfig.zero_costs(), _testing=True)
    order, coid = _make_order(broker)

    now_str = datetime.now(UTC).isoformat()
    import uuid
    broker_order_id = str(uuid.uuid4())
    trading_date = _date(2026, 1, 5)

    # Insertar intencion y orden
    with broker._conn:
        broker._conn.execute(
            """
            INSERT OR IGNORE INTO order_intentions
              (client_order_id, symbol, side, quantity, strategy_id,
               trading_date, status, created_at, updated_at, broker_order_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (coid, "TST", "BUY", "10", "crash_test", "2026-01-05",
             "SUBMITTED", now_str, now_str, broker_order_id),
        )
        broker._conn.execute(
            """
            INSERT OR IGNORE INTO orders
              (client_order_id, broker_order_id, symbol, side,
               quantity, approved_qty, status, submitted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (coid, broker_order_id, "TST", "BUY", "10", "10", "SUBMITTED", now_str),
        )

    # Simular fill: actualizar orders a FILLED y cash_ledger, pero NO intention
    fill_price = Decimal("100")
    fill_qty = Decimal("10")
    cash_delta = -(fill_qty * fill_price)
    with broker._conn:
        broker._conn.execute(
            """
            UPDATE orders SET
                status = 'FILLED', filled_at = ?, fill_price = ?,
                fill_quantity = ?, commission = '0', slippage = '0', spread_cost = '0'
            WHERE client_order_id = ?
            """,
            (now_str, str(fill_price), str(fill_qty), coid),
        )
        # Registrar posicion
        broker._conn.execute(
            """
            INSERT OR REPLACE INTO positions (symbol, quantity, avg_cost, currency, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("TST", str(fill_qty), str(fill_price), "EUR", now_str),
        )
        # Registrar en cash_ledger
        current_cash = broker._get_cash()
        balance_after = current_cash + cash_delta
        broker._conn.execute(
            """
            INSERT INTO cash_ledger (timestamp, amount, reason, balance_after)
            VALUES (?, ?, ?, ?)
            """,
            (now_str, str(cash_delta), f"BUY_FILL:{coid}", str(balance_after)),
        )
        # NO actualizamos order_intentions -> CONFIRMED (simulando crash aqui)

    _print_ready("after_fill_before_confirmed")
    # El test manda SIGKILL aqui: fill en orders, intention sigue SUBMITTED
    _wait_signal(signal_file)
    # Recovery: reconcile() detecta SUBMITTED + fill ya en orders -> CONFIRMED sin duplicar
    broker.close()


def scenario_advance_mid_fill(db_path: str, signal_file: str | None) -> None:
    """crash_4: muere a mitad de advance_to() con multiples ordenes pendientes."""
    from datetime import date as _date
    import uuid as _uuid

    broker = PaperBroker(db_path, Decimal("15000"), CostsConfig.zero_costs(), _testing=True)
    trading_date = _date(2026, 1, 6)
    now_str = datetime.now(UTC).isoformat()

    # Crear 3 ordenes SUBMITTED
    coids = []
    for i, sym in enumerate(["AAA", "BBB", "CCC"]):
        seq = i + 100
        coid = generate_client_order_id(
            trading_date=trading_date, symbol=sym, side=Side.BUY,
            strategy_id="crash_test", sequence_number=seq,
        )
        coids.append((coid, sym))
        broker_oid = str(_uuid.uuid4())
        with broker._conn:
            broker._conn.execute(
                """
                INSERT OR IGNORE INTO order_intentions
                  (client_order_id, symbol, side, quantity, strategy_id,
                   trading_date, status, created_at, updated_at, broker_order_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (coid, sym, "BUY", "10", "crash_test",
                 trading_date.isoformat(), "SUBMITTED", now_str, now_str, broker_oid),
            )
            broker._conn.execute(
                """
                INSERT OR IGNORE INTO orders
                  (client_order_id, broker_order_id, symbol, side,
                   quantity, approved_qty, status, submitted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (coid, broker_oid, sym, "BUY", "10", "10", "SUBMITTED", now_str),
            )

    # Procesar solo el primero (simular crash a mitad de advance_to)
    first_coid, first_sym = coids[0]
    fill_price = Decimal("50")
    fill_qty = Decimal("10")
    cash_delta = -(fill_qty * fill_price)
    with broker._conn:
        broker._conn.execute(
            "UPDATE orders SET status='FILLED', filled_at=?, fill_price=?, "
            "fill_quantity=?, commission='0', slippage='0', spread_cost='0' "
            "WHERE client_order_id=?",
            (now_str, str(fill_price), str(fill_qty), first_coid),
        )
        broker._conn.execute(
            "INSERT OR REPLACE INTO positions (symbol, quantity, avg_cost, currency, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (first_sym, str(fill_qty), str(fill_price), "EUR", now_str),
        )
        current_cash = broker._get_cash()
        broker._conn.execute(
            "INSERT INTO cash_ledger (timestamp, amount, reason, balance_after) "
            "VALUES (?, ?, ?, ?)",
            (now_str, str(cash_delta), f"BUY_FILL:{first_coid}",
             str(current_cash + cash_delta)),
        )
        # NO actualizar intention del primero (crash antes de CONFIRMED)

    _print_ready("after_first_fill_mid_advance")
    # El test manda SIGKILL aqui: AAA filled, BBB y CCC siguen SUBMITTED
    _wait_signal(signal_file)
    # Recovery: reconcile() resuelve el estado inconsistente
    broker.close()


def recovery_only(db_path: str) -> None:
    """Modo recovery: abre el broker (reconcile() en __init__) y cierra.

    No ejecuta ningun scenario de crash. El efecto de recuperacion ocurre
    unicamente en el __init__ de PaperBroker via reconcile().
    """
    broker = PaperBroker(db_path, Decimal("15000"), CostsConfig.zero_costs(), _testing=True)
    # __init__ ya llamo reconcile() — nada mas que hacer
    broker.close()
    _print_ready("recovery_done")


def main() -> None:
    # Uso: _crash_helper.py <db_path> <scenario> [signal_file]
    #   o: _crash_helper.py <db_path> --recovery
    if len(sys.argv) < 3:
        print("Uso: _crash_helper.py <db_path> <scenario|--recovery> [signal_file]", file=sys.stderr)
        sys.exit(1)

    db_path = sys.argv[1]
    mode = sys.argv[2]

    if mode == "--recovery":
        recovery_only(db_path)
        return

    scenario = mode
    signal_file = sys.argv[3] if len(sys.argv) > 3 else None

    scenarios = {
        "submit_before_broker": scenario_submit_before_broker,
        "submit_after_broker": scenario_submit_after_broker,
        "fill_before_confirmed": scenario_fill_before_confirmed,
        "advance_mid_fill": scenario_advance_mid_fill,
    }

    fn = scenarios.get(scenario)
    if fn is None:
        print(f"Scenario desconocido: {scenario}", file=sys.stderr)
        sys.exit(1)

    fn(db_path, signal_file)


if __name__ == "__main__":
    main()
