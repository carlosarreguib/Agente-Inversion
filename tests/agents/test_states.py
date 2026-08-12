"""Tests de la maquina de estados (T6) — modulo puro."""
from __future__ import annotations

import pytest

from qtrader.agents.states import (
    ENTRY_STATE,
    PHASE_STATES,
    RESUME_MAP,
    TERMINAL,
    AgentState,
    InvalidTransition,
    RunPhase,
    next_state,
    phase_of,
    resume_state,
)


class TestTransitions:
    def test_post_close_sequence(self) -> None:
        state = ENTRY_STATE[RunPhase.POST_CLOSE]
        seen = [state]
        while state not in TERMINAL:
            state = next_state(RunPhase.POST_CLOSE, state)
            seen.append(state)
        assert seen == [
            AgentState.LOADING_DATA,
            AgentState.GENERATING_SIGNALS,
            AgentState.EVALUATING_RISK,
            AgentState.SLEEPING,
        ]

    def test_pre_open_sequence(self) -> None:
        state = ENTRY_STATE[RunPhase.PRE_OPEN]
        seen = [state]
        while state not in TERMINAL:
            state = next_state(RunPhase.PRE_OPEN, state)
            seen.append(state)
        assert seen == [
            AgentState.RECONCILING,
            AgentState.SUBMITTING_ORDERS,
            AgentState.MONITORING,
            AgentState.SLEEPING,
        ]

    def test_invalid_transition_raises(self) -> None:
        with pytest.raises(InvalidTransition):
            next_state(RunPhase.POST_CLOSE, AgentState.MONITORING)
        with pytest.raises(InvalidTransition):
            next_state(RunPhase.PRE_OPEN, AgentState.LOADING_DATA)

    def test_terminal_states_have_no_outgoing_edge(self) -> None:
        for phase in RunPhase:
            for state in TERMINAL:
                with pytest.raises(InvalidTransition):
                    next_state(phase, state)


class TestResume:
    def test_submitting_orders_resumes_at_reconciling(self) -> None:
        """NO NEGOCIABLE: nunca reintentar submits directamente."""
        assert resume_state(AgentState.SUBMITTING_ORDERS) == AgentState.RECONCILING

    def test_signal_and_risk_states_resume_at_loading_data(self) -> None:
        # Ambos dependen de datos en memoria que murieron con el proceso.
        assert resume_state(AgentState.GENERATING_SIGNALS) == AgentState.LOADING_DATA
        assert resume_state(AgentState.EVALUATING_RISK) == AgentState.LOADING_DATA

    def test_monitoring_resumes_at_reconciling(self) -> None:
        assert resume_state(AgentState.MONITORING) == AgentState.RECONCILING

    def test_idempotent_states_resume_in_place(self) -> None:
        assert resume_state(AgentState.LOADING_DATA) == AgentState.LOADING_DATA
        assert resume_state(AgentState.RECONCILING) == AgentState.RECONCILING

    def test_terminal_states_are_not_resumable(self) -> None:
        for state in TERMINAL:
            with pytest.raises(InvalidTransition):
                resume_state(state)

    def test_every_non_terminal_state_has_a_resume_target(self) -> None:
        non_terminal = set(AgentState) - TERMINAL
        assert set(RESUME_MAP) == non_terminal

    def test_resume_target_is_never_later_in_the_phase(self) -> None:
        """Reanudar nunca puede saltar hacia adelante: se perderia trabajo."""
        for state, target in RESUME_MAP.items():
            phase = phase_of(state)
            order = PHASE_STATES[phase]
            assert order.index(target) <= order.index(state)


class TestPhaseOf:
    def test_phase_of_known_states(self) -> None:
        assert phase_of(AgentState.LOADING_DATA) == RunPhase.POST_CLOSE
        assert phase_of(AgentState.SUBMITTING_ORDERS) == RunPhase.PRE_OPEN

    def test_phase_of_terminal_raises(self) -> None:
        with pytest.raises(InvalidTransition):
            phase_of(AgentState.SLEEPING)
