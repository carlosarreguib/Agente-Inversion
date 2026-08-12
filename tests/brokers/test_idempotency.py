"""Tests de crash de idempotencia (T5.3).

Verifican que matar el proceso en cada punto del flujo WAL no produce
ordenes duplicadas ni perdida de estado.

Mecanismo de sincronizacion:
  1. El test lanza _crash_helper.py en subprocess.
  2. Lee stdout hasta "READY:<step>".
  3. Mata el proceso con process.kill() (TerminateProcess en Windows).
  4. Lanza el helper de nuevo en modo recovery (sin signal_file).
  5. Verifica el estado final de la BD.

Invariantes verificados en cada crash:
  - Numero de filas en orders (sin duplicados).
  - Estado de order_intentions (reconciliado correctamente).
  - Cash invariant (balance = initial + sum(amounts)).
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest

_HELPER = Path(__file__).parent / "_crash_helper.py"
_PYTHON = sys.executable
_INITIAL_CASH = Decimal("15000")


def _run_recovery(db_path: Path, timeout: float = 15.0) -> None:
    """Lanza el helper en modo recovery y espera a que termine."""
    proc = subprocess.Popen(
        [_PYTHON, str(_HELPER), str(db_path), "--recovery"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise AssertionError(f"Recovery timeout ({timeout}s)")
    assert proc.returncode == 0, (
        f"Recovery fallo con returncode={proc.returncode}\n"
        f"stderr={stderr!r}"
    )


def _run_helper(
    db_path: Path,
    scenario: str,
    signal_file: Path | None = None,
    *,
    wait_ready: str | None = None,
    timeout: float = 10.0,
) -> subprocess.Popen[str]:
    """Lanza el helper y opcionalmente espera hasta READY:<step>."""
    args = [_PYTHON, str(_HELPER), str(db_path), scenario]
    if signal_file is not None:
        args.append(str(signal_file))

    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    if wait_ready is not None:
        deadline = time.monotonic() + timeout
        assert proc.stdout is not None
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            if line.strip() == f"READY:{wait_ready}":
                return proc
        # Si no recibimos la señal, matar y fallar
        proc.kill()
        proc.wait()
        stdout, stderr = proc.communicate()
        raise AssertionError(
            f"El helper no emitio READY:{wait_ready} en {timeout}s.\n"
            f"stdout={stdout!r}\nstderr={stderr!r}"
        )

    return proc


def _kill_and_wait(proc: subprocess.Popen[str]) -> None:
    proc.kill()
    proc.wait(timeout=5)


def _get_cash_balance(db_path: Path) -> Decimal:
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT balance_after FROM cash_ledger ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return Decimal(row[0]) if row else Decimal("0")


def _get_cash_sum(db_path: Path) -> Decimal:
    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT SUM(CAST(amount AS REAL)) FROM cash_ledger").fetchone()
    conn.close()
    return Decimal(str(row[0] or 0))


def _count_orders(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT COUNT(*) FROM orders").fetchone()
    conn.close()
    return int(row[0])


def _get_intention_status(db_path: Path, client_order_id: str) -> str | None:
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT status FROM order_intentions WHERE client_order_id = ?",
        (client_order_id,),
    ).fetchone()
    conn.close()
    return row[0] if row else None


def _get_all_intention_statuses(db_path: Path) -> list[str]:
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT status FROM order_intentions").fetchall()
    conn.close()
    return [r[0] for r in rows]


def _get_all_order_statuses(db_path: Path) -> list[str]:
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT status FROM orders").fetchall()
    conn.close()
    return [r[0] for r in rows]


def _verify_cash_invariant(db_path: Path) -> None:
    """Lanza AssertionError si el invariante de cash esta roto."""
    balance = _get_cash_balance(db_path)
    total = _get_cash_sum(db_path)
    diff = abs(balance - total)
    assert diff <= Decimal("0.01"), (
        f"CASH INVARIANT VIOLATED: balance={balance} != sum(amounts)={total} diff={diff}"
    )


class TestCrash1SubmitBeforeBroker:
    """crash_1: SIGKILL entre INSERT intention y submit_order.

    Estado al crash: intention=PENDING_UNKNOWN, sin fila en orders.
    Recovery esperado: reconcile() detecta PENDING_UNKNOWN + UNKNOWN en broker
                       -> intencion queda PENDING_UNKNOWN (pendiente de reenvio).
    Sin duplicados: orders sigue vacia.
    """

    def test_crash1_no_duplicate_on_recovery(self, tmp_path: Path) -> None:
        db_path = tmp_path / "paper.db"
        signal_file = tmp_path / "signal.txt"

        # Fase 1: lanzar helper, esperar READY, matar
        proc = _run_helper(
            db_path, "submit_before_broker",
            signal_file=signal_file,
            wait_ready="after_intention_insert",
        )
        _kill_and_wait(proc)

        # Verificar estado pre-recovery
        orders_before = _count_orders(db_path)
        conn = sqlite3.connect(str(db_path))
        intentions_before = conn.execute(
            "SELECT status FROM order_intentions"
        ).fetchall()
        conn.close()
        assert orders_before == 0, "Antes de recovery no debe haber ordenes en orders"
        assert len(intentions_before) == 1
        assert intentions_before[0][0] == "PENDING_UNKNOWN"

        # Fase 2: recovery — abrir el broker (reconcile() en __init__) y cerrar
        _run_recovery(db_path)

        # Verificar: sin duplicados en orders
        orders_after = _count_orders(db_path)
        assert orders_after == 0, (
            f"No debe haber ordenes duplicadas tras recovery. orders={orders_after}"
        )

        # La intencion puede quedar PENDING_UNKNOWN (la orden nunca llego al broker)
        statuses = _get_all_intention_statuses(db_path)
        assert len(statuses) == 1
        assert statuses[0] in ("PENDING_UNKNOWN", "FAILED"), (
            f"Estado inesperado: {statuses[0]}"
        )

        _verify_cash_invariant(db_path)


class TestCrash2SubmitAfterBroker:
    """crash_2: SIGKILL entre submit_order y UPDATE intention->SUBMITTED.

    Estado al crash: intention=PENDING_UNKNOWN, orders=SUBMITTED.
    Recovery esperado: reconcile() ve PENDING_UNKNOWN pero el broker ya tiene
                       la orden (SUBMITTED en orders) -> actualiza a SUBMITTED.
    Sin duplicados: orders sigue con 1 fila.
    """

    def test_crash2_no_resend_when_broker_has_order(self, tmp_path: Path) -> None:
        db_path = tmp_path / "paper.db"
        signal_file = tmp_path / "signal.txt"

        # Fase 1: lanzar helper, esperar READY, matar
        proc = _run_helper(
            db_path, "submit_after_broker",
            signal_file=signal_file,
            wait_ready="after_order_insert",
        )
        _kill_and_wait(proc)

        # Verificar estado pre-recovery
        orders_before = _count_orders(db_path)
        assert orders_before == 1, "Debe haber exactamente 1 orden en orders"
        statuses_before = _get_all_intention_statuses(db_path)
        assert statuses_before == ["PENDING_UNKNOWN"]

        # Fase 2: recovery
        _run_recovery(db_path)

        # Verificar: 1 orden, sin duplicados
        orders_after = _count_orders(db_path)
        assert orders_after == 1, (
            f"No debe duplicar la orden. orders={orders_after}"
        )

        # La intencion debe estar SUBMITTED (reconcile la actualizo)
        statuses_after = _get_all_intention_statuses(db_path)
        assert len(statuses_after) == 1
        assert statuses_after[0] in ("SUBMITTED", "CONFIRMED"), (
            f"Estado inesperado tras recovery: {statuses_after[0]}"
        )

        _verify_cash_invariant(db_path)


class TestCrash3FillBeforeConfirmed:
    """crash_3: SIGKILL despues del fill registrado, antes de UPDATE->CONFIRMED.

    Estado al crash: intention=SUBMITTED, orders=FILLED, cash_ledger actualizado.
    Recovery esperado: reconcile() detecta SUBMITTED + fill en orders ->
                       marca CONFIRMED sin registrar un segundo fill.
    Cash invariant: debe mantenerse.
    """

    def test_crash3_fill_not_duplicated(self, tmp_path: Path) -> None:
        db_path = tmp_path / "paper.db"
        signal_file = tmp_path / "signal.txt"

        # Fase 1
        proc = _run_helper(
            db_path, "fill_before_confirmed",
            signal_file=signal_file,
            wait_ready="after_fill_before_confirmed",
        )
        _kill_and_wait(proc)

        # Verificar estado pre-recovery
        orders_before = _count_orders(db_path)
        assert orders_before == 1
        order_statuses = _get_all_order_statuses(db_path)
        assert order_statuses == ["FILLED"]
        intention_statuses = _get_all_intention_statuses(db_path)
        assert intention_statuses == ["SUBMITTED"]

        # Fase 2: recovery
        _run_recovery(db_path)

        # Verificar: 1 orden FILLED, sin segundo fill
        orders_after = _count_orders(db_path)
        assert orders_after == 1, (
            f"No debe haber ordenes duplicadas. orders={orders_after}"
        )
        order_statuses_after = _get_all_order_statuses(db_path)
        assert order_statuses_after == ["FILLED"]

        # La intencion debe estar CONFIRMED
        intention_statuses_after = _get_all_intention_statuses(db_path)
        assert len(intention_statuses_after) == 1
        assert intention_statuses_after[0] == "CONFIRMED", (
            f"Intencion no CONFIRMED: {intention_statuses_after[0]}"
        )

        # Cash invariant critico: no debe haber doble descuento
        _verify_cash_invariant(db_path)

        # Verificar que el cash es coherente: 15000 - (10 * 100) = 14000
        cash = _get_cash_balance(db_path)
        assert cash == Decimal("14000"), (
            f"Cash incorrecto tras recovery: {cash} (esperado 14000)"
        )


class TestCrash4AdvanceMidFill:
    """crash_4: SIGKILL durante advance_to() a mitad de procesar fills.

    Estado al crash: AAA=FILLED, BBB+CCC=SUBMITTED.
    Recovery esperado: BBB y CCC siguen SUBMITTED (se procesaran en advance_to()),
                       AAA queda CONFIRMED, cash invariant se mantiene.
    Sin duplicados de AAA.
    """

    def test_crash4_state_consistent_after_mid_advance(self, tmp_path: Path) -> None:
        db_path = tmp_path / "paper.db"
        signal_file = tmp_path / "signal.txt"

        # Fase 1
        proc = _run_helper(
            db_path, "advance_mid_fill",
            signal_file=signal_file,
            wait_ready="after_first_fill_mid_advance",
        )
        _kill_and_wait(proc)

        # Verificar estado pre-recovery
        conn = sqlite3.connect(str(db_path))
        orders = conn.execute("SELECT symbol, status FROM orders ORDER BY symbol").fetchall()
        conn.close()
        assert len(orders) == 3
        symbols_filled = [r[0] for r in orders if r[1] == "FILLED"]
        symbols_submitted = [r[0] for r in orders if r[1] == "SUBMITTED"]
        assert symbols_filled == ["AAA"], f"Solo AAA debe estar FILLED: {orders}"
        assert set(symbols_submitted) == {"BBB", "CCC"}

        # Fase 2: recovery
        _run_recovery(db_path)

        # Verificar: 3 ordenes (sin duplicados de AAA)
        orders_after = _count_orders(db_path)
        assert orders_after == 3, (
            f"No debe haber duplicados. orders={orders_after}"
        )

        # AAA debe seguir FILLED (no duplicada)
        conn = sqlite3.connect(str(db_path))
        aaa_rows = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE symbol='AAA' AND status='FILLED'"
        ).fetchone()
        conn.close()
        assert aaa_rows[0] == 1, f"AAA duplicada: {aaa_rows[0]} filas FILLED"

        # Cash invariant
        _verify_cash_invariant(db_path)

        # Cash despues de solo el fill de AAA (10 * 50 = 500): 15000 - 500 = 14500
        cash = _get_cash_balance(db_path)
        assert cash == Decimal("14500"), (
            f"Cash incorrecto: {cash} (esperado 14500 con solo AAA filled)"
        )


class TestClientOrderIdDeterminism:
    """Verifica que el mismo input siempre produce el mismo client_order_id."""

    def test_same_input_same_id(self) -> None:
        from datetime import date

        from qtrader.brokers.order_id import generate_client_order_id
        from qtrader.core.types import Side

        coid1 = generate_client_order_id(
            trading_date=date(2026, 1, 5),
            symbol="TST",
            side=Side.BUY,
            strategy_id="test",
            sequence_number=42,
        )
        coid2 = generate_client_order_id(
            trading_date=date(2026, 1, 5),
            symbol="TST",
            side=Side.BUY,
            strategy_id="test",
            sequence_number=42,
        )
        assert coid1 == coid2
        assert len(coid1) == 16
        assert all(c in "0123456789abcdef" for c in coid1)

    def test_different_seq_different_id(self) -> None:
        from datetime import date

        from qtrader.brokers.order_id import generate_client_order_id
        from qtrader.core.types import Side

        coid1 = generate_client_order_id(
            trading_date=date(2026, 1, 5),
            symbol="TST",
            side=Side.BUY,
            strategy_id="test",
            sequence_number=1,
        )
        coid2 = generate_client_order_id(
            trading_date=date(2026, 1, 5),
            symbol="TST",
            side=Side.BUY,
            strategy_id="test",
            sequence_number=2,
        )
        assert coid1 != coid2

    def test_different_symbol_different_id(self) -> None:
        from datetime import date

        from qtrader.brokers.order_id import generate_client_order_id
        from qtrader.core.types import Side

        coid1 = generate_client_order_id(
            trading_date=date(2026, 1, 5),
            symbol="AAA",
            side=Side.BUY,
            strategy_id="test",
            sequence_number=1,
        )
        coid2 = generate_client_order_id(
            trading_date=date(2026, 1, 5),
            symbol="BBB",
            side=Side.BUY,
            strategy_id="test",
            sequence_number=1,
        )
        assert coid1 != coid2


class TestSequenceNumber:
    """Verifica que sequence_number incrementa correctamente y persiste."""

    def test_sequence_increments(self, tmp_path: Path) -> None:
        import sqlite3 as _sqlite3

        from qtrader.brokers.order_id import init_sequence_table, next_sequence

        conn = _sqlite3.connect(str(tmp_path / "seq.db"))
        init_sequence_table(conn)

        seq1 = next_sequence(conn)
        conn.commit()
        seq2 = next_sequence(conn)
        conn.commit()
        seq3 = next_sequence(conn)
        conn.commit()

        assert seq2 == seq1 + 1
        assert seq3 == seq2 + 1
        conn.close()

    def test_sequence_persists_across_connections(self, tmp_path: Path) -> None:
        import sqlite3 as _sqlite3

        from qtrader.brokers.order_id import init_sequence_table, next_sequence

        db = tmp_path / "seq.db"

        conn1 = _sqlite3.connect(str(db))
        init_sequence_table(conn1)
        seq1 = next_sequence(conn1)
        conn1.commit()
        conn1.close()

        conn2 = _sqlite3.connect(str(db))
        init_sequence_table(conn2)
        seq2 = next_sequence(conn2)
        conn2.commit()
        conn2.close()

        assert seq2 == seq1 + 1


class TestWALFlowIntegration:
    """Verifica el flujo WAL completo sin crash (camino feliz)."""

    def test_intention_confirmed_after_fill(self, tmp_path: Path) -> None:
        """submit_order + advance_to -> intencion CONFIRMED, sin duplicados."""
        import asyncio
        from datetime import date

        from qtrader.brokers.paper_broker import PaperBroker
        from qtrader.brokers.order_id import generate_client_order_id, next_sequence
        from qtrader.core.types import Side
        from qtrader.costs import CostsConfig
        from qtrader.risk.types import ApprovedOrder

        db_path = tmp_path / "paper.db"
        broker = PaperBroker(str(db_path), Decimal("15000"), CostsConfig.zero_costs(), _testing=True)

        trading_date = date(2026, 1, 5)
        with broker._conn:
            seq = next_sequence(broker._conn)

        coid = generate_client_order_id(
            trading_date=trading_date,
            symbol="TST",
            side=Side.BUY,
            strategy_id="test",
            sequence_number=seq,
        )

        order = ApprovedOrder(
            order_id=coid,
            symbol="TST",
            side=Side.BUY,
            original_quantity=Decimal("5"),
            approved_quantity=Decimal("5"),
            notional=Decimal("500"),
            reduction_reason=None,
        )

        asyncio.run(broker.submit_order(order, coid))

        # Verificar intencion SUBMITTED
        conn = sqlite3.connect(str(db_path))
        intent_row = conn.execute(
            "SELECT status FROM order_intentions WHERE client_order_id = ?", (coid,)
        ).fetchone()
        conn.close()
        assert intent_row is not None
        assert intent_row[0] == "SUBMITTED"

        # Inyectar datos de mercado y avanzar
        broker.set_market_data(
            data={"TST": {trading_date: Decimal("100")}},
            instruments={},
        )
        broker.advance_to(trading_date)

        # Tras advance_to: intencion debe ser CONFIRMED
        conn = sqlite3.connect(str(db_path))
        intent_after = conn.execute(
            "SELECT status FROM order_intentions WHERE client_order_id = ?", (coid,)
        ).fetchone()
        order_count = conn.execute("SELECT COUNT(*) FROM orders WHERE status='FILLED'").fetchone()
        conn.close()

        assert intent_after[0] == "CONFIRMED", f"Intencion no CONFIRMED: {intent_after[0]}"
        assert order_count[0] == 1, "Debe haber exactamente 1 fill"

        broker.close()
