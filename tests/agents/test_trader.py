"""Tests del ciclo del agente (T6).

Cubre: ciclo completo, kill switch, HALT de validacion, HALT de riesgo,
rate limiter, sanity checker, heartbeat y auditoria.

Los ciclos son async (BrokerInterface lo es); se ejecutan con asyncio.run en
el borde, igual que en tests/brokers/test_idempotency.py, para no anadir una
dependencia de plugin async al proyecto.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path  # noqa: TC003 — runtime: fixtures de pytest
from typing import TYPE_CHECKING, Any

import pytest

from qtrader.agents.states import AgentState, RunPhase
from qtrader.agents.trader import AgentHalted, CycleResult, TraderAgent
from qtrader.core.types import Severity, ValidationResult
from qtrader.execution.rate_limiter import ExecutionRateLimiter, RateLimiterConfig
from qtrader.execution.sanity import BrokerSanityChecker, SanityConfig
from qtrader.risk.types import RiskLevel
from tests.agents.conftest import TRADING_DATE, FakeKillSwitch, FakeWatchdog

if TYPE_CHECKING:
    from qtrader.agents.context import AgentContext
    from qtrader.agents.store import AgentStateStore
    from qtrader.ledger.sqlite import SQLiteLedger

# 2024-06-14 es viernes: la ejecucion T+1 cae en el lunes 2024-06-17, que SI es
# sesion de mercado. Con el sabado, el sanity checker rechazaria todo por
# MARKET_CLOSED al consultar exchange_calendars.
_NEXT_DAY = date(2024, 6, 17)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run_cycle(ctx: AgentContext, phase: RunPhase, trading_date: date) -> CycleResult:
    return asyncio.run(TraderAgent(ctx).run_once(phase, trading_date))


def run_full_cycle(ctx: AgentContext) -> tuple[CycleResult, CycleResult]:
    post = run_cycle(ctx, RunPhase.POST_CLOSE, TRADING_DATE)
    pre = run_cycle(ctx, RunPhase.PRE_OPEN, _NEXT_DAY)
    return (post, pre)


def with_changes(ctx: AgentContext, **changes: Any) -> AgentContext:
    return dataclasses.replace(ctx, **changes)


def audit_events(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT DISTINCT event_type FROM audit_log").fetchall()
    conn.close()
    return {r[0] for r in rows}


def _strict_sanity() -> BrokerSanityChecker:
    """Checker que rechaza todo: sin ADV, el fallback tope es muy bajo."""
    return BrokerSanityChecker(
        config=SanityConfig(
            max_price_deviation=Decimal("0.50"),
            max_adv_fraction=Decimal("0.10"),
            adv_fallback_eur=Decimal("1"),
        )
    )


def halt_validation(*args: object, **kwargs: object) -> list[ValidationResult]:
    """Doble de validate_bars que reporta corrupcion sistemica."""
    return [
        ValidationResult(
            symbol="SPY",
            date=datetime.now(UTC),
            rule_violated="negative_price",
            severity=Severity.HALT,
            details="systemic provider corruption",
        )
    ]


# ---------------------------------------------------------------------------
# Ciclo completo
# ---------------------------------------------------------------------------


class TestFullCycle:
    def test_agent_completes_full_cycle(self, context: AgentContext) -> None:
        post, pre = run_full_cycle(context)

        assert post.states_entered == [
            AgentState.LOADING_DATA,
            AgentState.GENERATING_SIGNALS,
            AgentState.EVALUATING_RISK,
            AgentState.SLEEPING,
        ]
        assert pre.states_entered == [
            AgentState.RECONCILING,
            AgentState.SUBMITTING_ORDERS,
            AgentState.MONITORING,
            AgentState.SLEEPING,
        ]
        assert pre.orders_submitted > 0
        assert pre.fills > 0

    def test_orders_reach_the_broker(
        self, context: AgentContext, tmp_path: Path
    ) -> None:
        run_full_cycle(context)
        conn = sqlite3.connect(str(tmp_path / "paper.db"))
        filled = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE status = 'FILLED'"
        ).fetchone()[0]
        conn.close()
        assert filled > 0

    def test_toctou_window_under_budget(self, context: AgentContext) -> None:
        _, pre = run_full_cycle(context)
        assert pre.toctou_max_ms < 100.0

    def test_client_order_ids_are_unique(
        self, context: AgentContext, tmp_path: Path
    ) -> None:
        run_full_cycle(context)
        conn = sqlite3.connect(str(tmp_path / "paper.db"))
        total, distinct = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT client_order_id) FROM orders"
        ).fetchone()
        conn.close()
        assert total == distinct


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


class TestKillSwitch:
    def test_kill_switch_stops_agent_before_submit(
        self, context: AgentContext, kill_switch: FakeKillSwitch, tmp_path: Path
    ) -> None:
        run_cycle(context, RunPhase.POST_CLOSE, TRADING_DATE)

        kill_switch.active = True
        result = run_cycle(context, RunPhase.PRE_OPEN, _NEXT_DAY)

        assert result.halted_by_kill_switch
        assert result.orders_submitted == 0

        conn = sqlite3.connect(str(tmp_path / "paper.db"))
        submitted = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        conn.close()
        assert submitted == 0, "No debe llegar ninguna orden al broker"

    def test_kill_switch_stops_agent_mid_cycle(
        self, context: AgentContext, kill_switch: FakeKillSwitch
    ) -> None:
        kill_switch.activate_after = 1   # pasa el primer check, falla el segundo

        result = run_cycle(context, RunPhase.POST_CLOSE, TRADING_DATE)

        assert result.halted_by_kill_switch
        assert AgentState.LOADING_DATA in result.states_entered
        assert AgentState.EVALUATING_RISK not in result.states_entered

    def test_kill_switch_checked_at_start_of_every_state(
        self, context: AgentContext, kill_switch: FakeKillSwitch
    ) -> None:
        result = run_cycle(context, RunPhase.POST_CLOSE, TRADING_DATE)
        non_terminal = [s for s in result.states_entered if s != AgentState.SLEEPING]
        assert kill_switch.calls >= len(non_terminal)

    def test_kill_switch_checked_before_each_order(
        self, context: AgentContext, kill_switch: FakeKillSwitch
    ) -> None:
        run_cycle(context, RunPhase.POST_CLOSE, TRADING_DATE)
        before = kill_switch.calls
        result = run_cycle(context, RunPhase.PRE_OPEN, _NEXT_DAY)
        consumed = kill_switch.calls - before
        # 3 estados + un check por cada orden enviada.
        assert consumed >= 3 + result.orders_submitted

    def test_kill_switch_halt_is_not_an_error(
        self, context: AgentContext, kill_switch: FakeKillSwitch, store: AgentStateStore
    ) -> None:
        kill_switch.active = True
        result = run_cycle(context, RunPhase.POST_CLOSE, TRADING_DATE)

        assert result.halted_by_kill_switch
        row = store.load()
        assert row is not None
        assert row.current_state == AgentState.SLEEPING


# ---------------------------------------------------------------------------
# HALT de validacion
# ---------------------------------------------------------------------------


class TestValidationHalt:
    def test_validation_halt_activates_kill_switch(
        self, context: AgentContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Severity.HALT -> ERROR + peticion de HALT.

        El agente NO llama a kill_switch.activate(): usa el halt_requester
        inyectado (CLAUDE.md 2.6). Por eso el test comprueba el requester.
        """
        requested: list[str] = []
        ctx = with_changes(context, halt_requester=requested.append)
        monkeypatch.setattr("qtrader.agents.trader.validate_bars", halt_validation)

        with pytest.raises(AgentHalted, match="VALIDATION_HALT"):
            run_cycle(ctx, RunPhase.POST_CLOSE, TRADING_DATE)

        assert requested, "Debe pedirse el HALT via halt_requester"
        assert requested[0].startswith("AGENT_ERROR:")

    def test_agent_never_calls_activate_directly(
        self, context: AgentContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El kill switch del agente no expone activate(): solo is_active()."""
        assert not hasattr(context.kill_switch, "activate")

    def test_error_state_is_persisted_and_blocks_restart(
        self,
        context: AgentContext,
        store: AgentStateStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("qtrader.agents.trader.validate_bars", halt_validation)
        with pytest.raises(AgentHalted):
            run_cycle(context, RunPhase.POST_CLOSE, TRADING_DATE)

        row = store.load()
        assert row is not None
        assert row.current_state == AgentState.ERROR

        # Un ERROR persistido rechaza arrancar: requiere intervencion humana.
        monkeypatch.undo()
        with pytest.raises(AgentHalted, match="ERROR persistido"):
            run_cycle(context, RunPhase.POST_CLOSE, TRADING_DATE)


# ---------------------------------------------------------------------------
# HALT de riesgo
# ---------------------------------------------------------------------------


class TestRiskHalt:
    def test_risk_halt_generates_no_orders(
        self, context: AgentContext, store: AgentStateStore
    ) -> None:
        """Drawdown por encima del umbral de HALT: ninguna orden persistida."""
        # NAV historico muy alto -> drawdown enorme -> RiskLevel.HALT.
        store.record_nav(TRADING_DATE - timedelta(days=10), Decimal("100000"))

        result = run_cycle(context, RunPhase.POST_CLOSE, TRADING_DATE)

        assert result.risk_level == RiskLevel.HALT
        assert store.pending_up_to(TRADING_DATE) == []

    def test_risk_halt_submits_nothing_downstream(
        self, context: AgentContext, store: AgentStateStore, tmp_path: Path
    ) -> None:
        store.record_nav(TRADING_DATE - timedelta(days=10), Decimal("100000"))
        run_cycle(context, RunPhase.POST_CLOSE, TRADING_DATE)
        result = run_cycle(context, RunPhase.PRE_OPEN, _NEXT_DAY)

        assert result.orders_submitted == 0
        conn = sqlite3.connect(str(tmp_path / "paper.db"))
        count = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        conn.close()
        assert count == 0


# ---------------------------------------------------------------------------
# Capas de defensa
# ---------------------------------------------------------------------------


class TestDefenceLayers:
    def test_rate_limiter_blocks_excess_orders(
        self, context: AgentContext, tmp_path: Path
    ) -> None:
        limited = ExecutionRateLimiter(
            db_path=tmp_path / "exec_limited.db",
            config=RateLimiterConfig(
                max_orders_per_minute=100,
                max_orders_per_day=1,
                max_notional_per_day=Decimal("1000000"),
            ),
        )
        ctx = with_changes(context, rate_limiter=limited)
        try:
            run_cycle(ctx, RunPhase.POST_CLOSE, TRADING_DATE)
            result = run_cycle(ctx, RunPhase.PRE_OPEN, _NEXT_DAY)
            assert result.orders_submitted == 1
            assert result.orders_blocked >= 1
        finally:
            limited.close()

    def test_sanity_checker_blocks_bad_orders(self, context: AgentContext) -> None:
        """Un cap de ADV minusculo hace que la capa 3 rechace todas las ordenes."""
        ctx = with_changes(context, sanity=_strict_sanity(), adv={})

        run_cycle(ctx, RunPhase.POST_CLOSE, TRADING_DATE)
        result = run_cycle(ctx, RunPhase.PRE_OPEN, _NEXT_DAY)

        assert result.orders_submitted == 0
        assert result.orders_blocked > 0

    def test_blocked_orders_are_audited(
        self, context: AgentContext, tmp_path: Path
    ) -> None:
        ctx = with_changes(context, sanity=_strict_sanity(), adv={})
        run_cycle(ctx, RunPhase.POST_CLOSE, TRADING_DATE)
        run_cycle(ctx, RunPhase.PRE_OPEN, _NEXT_DAY)

        assert "AGENT_ORDER_REJECTED_SANITY" in audit_events(tmp_path / "state.db")

    def test_layers_run_in_order_sanity_then_rate_limit(
        self, context: AgentContext, tmp_path: Path
    ) -> None:
        """Si sanity bloquea, el rate limiter no debe consumir cuota."""
        ctx = with_changes(context, sanity=_strict_sanity(), adv={})
        run_cycle(ctx, RunPhase.POST_CLOSE, TRADING_DATE)
        run_cycle(ctx, RunPhase.PRE_OPEN, _NEXT_DAY)

        assert ctx.rate_limiter.orders_today(_NEXT_DAY) == 0


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


class TestHeartbeat:
    def test_heartbeat_sent_in_monitoring_state(
        self, context: AgentContext, watchdog: FakeWatchdog
    ) -> None:
        run_cycle(context, RunPhase.POST_CLOSE, TRADING_DATE)
        before = watchdog.heartbeats
        run_cycle(context, RunPhase.PRE_OPEN, _NEXT_DAY)
        # 3 estados de PRE_OPEN + el heartbeat explicito de MONITORING.
        assert watchdog.heartbeats - before >= 4

    def test_heartbeat_in_every_state(
        self, context: AgentContext, watchdog: FakeWatchdog
    ) -> None:
        result = run_cycle(context, RunPhase.POST_CLOSE, TRADING_DATE)
        non_terminal = [s for s in result.states_entered if s != AgentState.SLEEPING]
        assert watchdog.heartbeats >= len(non_terminal)


# ---------------------------------------------------------------------------
# Auditoria
# ---------------------------------------------------------------------------


class TestAudit:
    def test_audit_record_created_for_every_decision(
        self, context: AgentContext, tmp_path: Path
    ) -> None:
        run_full_cycle(context)
        events = audit_events(tmp_path / "state.db")

        for expected in (
            "AGENT_DATA_LOADED",
            "AGENT_SIGNALS",
            "AGENT_PORTFOLIO_TARGET",
            "AGENT_RISK_DECISION",
            "AGENT_RECONCILED",
            "AGENT_ORDER_SUBMITTED",
            "AGENT_FILLS",
            "AGENT_CYCLE_COMPLETE",
        ):
            assert expected in events, f"Falta el registro {expected}"

    def test_audit_chain_is_valid(
        self, context: AgentContext, ledger: SQLiteLedger
    ) -> None:
        run_full_cycle(context)
        assert ledger.verify_chain() == []

    def test_audit_record_ids_are_unique(
        self, context: AgentContext, tmp_path: Path
    ) -> None:
        """record_audit usa INSERT OR IGNORE: un id repetido se pierde en silencio."""
        run_full_cycle(context)

        conn = sqlite3.connect(str(tmp_path / "state.db"))
        total, distinct = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT record_id) FROM audit_log"
        ).fetchone()
        conn.close()
        assert total == distinct

    def test_every_decision_references_the_data_hash(
        self, context: AgentContext, tmp_path: Path
    ) -> None:
        """Invariante 3.10: cada decision referencia el hash del snapshot."""
        run_cycle(context, RunPhase.POST_CLOSE, TRADING_DATE)

        conn = sqlite3.connect(str(tmp_path / "state.db"))
        rows = conn.execute(
            "SELECT data FROM audit_log WHERE event_type = 'AGENT_RISK_DECISION'"
        ).fetchall()
        conn.close()

        assert rows
        payload = json.loads(rows[0][0])
        assert payload["data_hash"] != "0" * 64

    def test_audit_payload_values_are_all_strings(
        self, context: AgentContext, tmp_path: Path
    ) -> None:
        run_full_cycle(context)
        conn = sqlite3.connect(str(tmp_path / "state.db"))
        rows = conn.execute("SELECT data FROM audit_log").fetchall()
        conn.close()

        for (raw,) in rows:
            record = json.loads(raw)
            for field in ("payload", "parameters", "risk_output"):
                for value in record[field].values():
                    assert isinstance(value, str)
