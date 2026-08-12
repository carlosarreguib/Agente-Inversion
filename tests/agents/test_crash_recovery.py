"""Tests de recuperacion tras crash (T6).

Enfoque hibrido:
  - 6 tests en proceso (uno por estado): escriben la fila de agent_state con el
    estado deseado y arrancan un agente nuevo sobre esa BD. Rapidos y
    deterministas; cubren la logica de reanudacion de los 6 estados.
  - 1 test de subprocess con kill() real para SUBMITTING_ORDERS, el caso
    critico: es el unico que prueba que un SIGKILL deja la BD recuperable y
    que no se duplican ordenes.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from qtrader.agents.states import AgentState, RunPhase
from tests.agents.conftest import TRADING_DATE
from tests.agents.test_trader import run_cycle

if TYPE_CHECKING:
    from qtrader.agents.context import AgentContext
    from qtrader.agents.store import AgentStateStore

_HELPER = Path(__file__).parent / "_agent_crash_helper.py"
_PYTHON = sys.executable
_EXEC_DATE = date(2024, 6, 17)


# ---------------------------------------------------------------------------
# 6 tests en proceso: uno por estado
# ---------------------------------------------------------------------------


def _simulate_crash_in(
    store: AgentStateStore, state: AgentState, trading_date: date
) -> str:
    """Deja persistido el estado como si el proceso hubiera muerto ahi."""
    cycle_id = "crashed-cycle-0001"
    store.persist(state, trading_date, cycle_id)
    return cycle_id


class TestResumePerState:
    """Un test por estado. El agente reanuda donde toca, no desde el principio."""

    def test_resumes_after_crash_in_loading_data(
        self, context: AgentContext, store: AgentStateStore
    ) -> None:
        _simulate_crash_in(store, AgentState.LOADING_DATA, TRADING_DATE)
        result = run_cycle(context, RunPhase.POST_CLOSE, TRADING_DATE)
        assert result.states_entered[0] == AgentState.LOADING_DATA

    def test_resumes_after_crash_in_generating_signals(
        self, context: AgentContext, store: AgentStateStore
    ) -> None:
        """Reanuda en LOADING_DATA: los datos en memoria murieron con el proceso."""
        _simulate_crash_in(store, AgentState.GENERATING_SIGNALS, TRADING_DATE)
        result = run_cycle(context, RunPhase.POST_CLOSE, TRADING_DATE)
        assert result.states_entered[0] == AgentState.LOADING_DATA

    def test_resumes_after_crash_in_evaluating_risk(
        self, context: AgentContext, store: AgentStateStore
    ) -> None:
        """Igual: RiskEngine necesita el PortfolioTarget que estaba en memoria."""
        _simulate_crash_in(store, AgentState.EVALUATING_RISK, TRADING_DATE)
        result = run_cycle(context, RunPhase.POST_CLOSE, TRADING_DATE)
        assert result.states_entered[0] == AgentState.LOADING_DATA

    def test_resumes_after_crash_in_reconciling(
        self, context: AgentContext, store: AgentStateStore
    ) -> None:
        _simulate_crash_in(store, AgentState.RECONCILING, _EXEC_DATE)
        result = run_cycle(context, RunPhase.PRE_OPEN, _EXEC_DATE)
        assert result.states_entered[0] == AgentState.RECONCILING

    def test_resumes_after_crash_in_submitting_orders(
        self, context: AgentContext, store: AgentStateStore
    ) -> None:
        """NO NEGOCIABLE: reanuda en RECONCILING, nunca en SUBMITTING_ORDERS.

        Tras un crash a mitad del envio, el conjunto de ordenes que llego al
        broker es desconocido. Reintentar los submits directamente duplicaria
        las que si llegaron (CLAUDE.md 2.4).
        """
        _simulate_crash_in(store, AgentState.SUBMITTING_ORDERS, _EXEC_DATE)
        result = run_cycle(context, RunPhase.PRE_OPEN, _EXEC_DATE)

        assert result.states_entered[0] == AgentState.RECONCILING
        assert result.states_entered[0] != AgentState.SUBMITTING_ORDERS

    def test_resumes_after_crash_in_monitoring(
        self, context: AgentContext, store: AgentStateStore
    ) -> None:
        """MONITORING muta cash y posiciones: reentrar por RECONCILING."""
        _simulate_crash_in(store, AgentState.MONITORING, _EXEC_DATE)
        result = run_cycle(context, RunPhase.PRE_OPEN, _EXEC_DATE)
        assert result.states_entered[0] == AgentState.RECONCILING


class TestResumeSemantics:
    def test_resume_keeps_the_same_cycle_id(
        self, context: AgentContext, store: AgentStateStore
    ) -> None:
        cycle_id = _simulate_crash_in(store, AgentState.LOADING_DATA, TRADING_DATE)
        result = run_cycle(context, RunPhase.POST_CLOSE, TRADING_DATE)
        assert result.cycle_id == cycle_id

    def test_completed_cycle_starts_a_new_one(
        self, context: AgentContext, store: AgentStateStore
    ) -> None:
        first = run_cycle(context, RunPhase.POST_CLOSE, TRADING_DATE)
        second = run_cycle(context, RunPhase.POST_CLOSE, TRADING_DATE)
        assert second.cycle_id != first.cycle_id
        assert second.states_entered[0] == AgentState.LOADING_DATA

    def test_resume_does_not_duplicate_pending_orders(
        self, context: AgentContext, store: AgentStateStore
    ) -> None:
        """Recomputar EVALUATING_RISK no debe duplicar filas."""
        run_cycle(context, RunPhase.POST_CLOSE, TRADING_DATE)
        before = len(store.all_orders_for_date(TRADING_DATE))
        assert before > 0

        _simulate_crash_in(store, AgentState.EVALUATING_RISK, TRADING_DATE)
        run_cycle(context, RunPhase.POST_CLOSE, TRADING_DATE)

        after = len(store.all_orders_for_date(TRADING_DATE))
        assert after == before, "El recompute no debe duplicar ordenes pendientes"

    def test_already_submitted_orders_are_never_resent(
        self, context: AgentContext, store: AgentStateStore, tmp_path: Path
    ) -> None:
        """Reejecutar PRE_OPEN no reenvia lo ya enviado."""
        run_cycle(context, RunPhase.POST_CLOSE, TRADING_DATE)
        first = run_cycle(context, RunPhase.PRE_OPEN, _EXEC_DATE)
        assert first.orders_submitted > 0

        conn = sqlite3.connect(str(tmp_path / "paper.db"))
        after_first = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        conn.close()

        second = run_cycle(context, RunPhase.PRE_OPEN, _EXEC_DATE)
        assert second.orders_submitted == 0

        conn = sqlite3.connect(str(tmp_path / "paper.db"))
        after_second = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        conn.close()
        assert after_second == after_first, "No debe duplicar ordenes"


# ---------------------------------------------------------------------------
# Crash real con subprocess: el caso critico
# ---------------------------------------------------------------------------


def _wait_for_ready(proc: subprocess.Popen[str], token: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    assert proc.stdout is not None
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        if line.strip() == f"READY:{token}":
            return
    proc.kill()
    stdout, stderr = proc.communicate()
    raise AssertionError(
        f"El helper no emitio READY:{token} en {timeout}s.\n"
        f"stdout={stdout!r}\nstderr={stderr!r}"
    )


class TestRealCrashInSubmittingOrders:
    """SIGKILL real a mitad de SUBMITTING_ORDERS.

    Es el unico test que prueba que una muerte subita del proceso deja la BD
    en estado recuperable y que la reanudacion no duplica ordenes.
    """

    def test_crash_mid_submit_resumes_at_reconciling_without_duplicates(
        self, tmp_path: Path
    ) -> None:
        proc = subprocess.Popen(
            [_PYTHON, str(_HELPER), str(tmp_path), "submit"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        _wait_for_ready(proc, "after_first_submit", timeout=90.0)

        # Matar con una orden ya enviada y el resto sin enviar.
        proc.kill()
        proc.wait(timeout=10)

        conn = sqlite3.connect(str(tmp_path / "paper.db"))
        orders_before = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        conn.close()
        assert orders_before >= 1, "Debe haber al menos una orden enviada"

        conn = sqlite3.connect(str(tmp_path / "agent.db"))
        state_before = conn.execute(
            "SELECT current_state FROM agent_state ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        conn.close()
        assert state_before == AgentState.SUBMITTING_ORDERS.value

        # Recovery
        recovery = subprocess.run(
            [_PYTHON, str(_HELPER), str(tmp_path), "--recovery"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        assert recovery.returncode == 0, (
            f"Recovery fallo: {recovery.returncode}\nstderr={recovery.stderr}"
        )

        resumed_line = next(
            (
                line
                for line in recovery.stdout.splitlines()
                if line.startswith("RESUMED:")
            ),
            None,
        )
        assert resumed_line is not None, f"Sin RESUMED en stdout: {recovery.stdout!r}"

        states = resumed_line.removeprefix("RESUMED:").split(",")
        assert states[0] == AgentState.RECONCILING.value, (
            f"Debe reanudar en RECONCILING, no en {states[0]}"
        )

        # Sin duplicados: cada client_order_id aparece una sola vez.
        conn = sqlite3.connect(str(tmp_path / "paper.db"))
        total, distinct = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT client_order_id) FROM orders"
        ).fetchone()
        by_symbol = conn.execute(
            "SELECT symbol, COUNT(*) FROM orders GROUP BY symbol HAVING COUNT(*) > 1"
        ).fetchall()
        conn.close()

        assert total == distinct, "client_order_id duplicado tras recovery"
        assert by_symbol == [], f"Simbolos con orden duplicada: {by_symbol}"
