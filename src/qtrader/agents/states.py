"""Maquina de estados del agente trader (T6) — modulo puro.

Sin I/O, sin imports fuera de stdlib. Las tablas son datos, no if/elif,
para que los tests puedan hacer aserciones directamente sobre ellas.

WARNING no es un estado. Un estado en el que nunca permaneces y del que
siempre sales hacia donde venias no es un estado: es una severidad.
Modelarlo como AgentState obligaria a cada estado a recordar su predecesor
y haria que current_state='WARNING' fuera irreanudable al reiniciar
(reanudar, pero donde?). Se maneja dentro de cada handler: log a WARNING,
registro AGENT_WARNING en auditoria, continuar.
"""
from __future__ import annotations

from enum import StrEnum


class AgentState(StrEnum):
    """Estados de la maquina. Persistidos como TEXT en agent_state."""

    LOADING_DATA = "LOADING_DATA"
    GENERATING_SIGNALS = "GENERATING_SIGNALS"
    EVALUATING_RISK = "EVALUATING_RISK"
    RECONCILING = "RECONCILING"
    SUBMITTING_ORDERS = "SUBMITTING_ORDERS"
    MONITORING = "MONITORING"
    SLEEPING = "SLEEPING"
    ERROR = "ERROR"


class RunPhase(StrEnum):
    """Las dos ejecuciones diarias del agente."""

    POST_CLOSE = "POST_CLOSE"
    PRE_OPEN = "PRE_OPEN"


class InvalidTransition(Exception):
    """Transicion no declarada en la tabla."""


# ---------------------------------------------------------------------------
# Tabla de transiciones
# ---------------------------------------------------------------------------

TRANSITIONS: dict[tuple[RunPhase, AgentState], AgentState] = {
    (RunPhase.POST_CLOSE, AgentState.LOADING_DATA): AgentState.GENERATING_SIGNALS,
    (RunPhase.POST_CLOSE, AgentState.GENERATING_SIGNALS): AgentState.EVALUATING_RISK,
    (RunPhase.POST_CLOSE, AgentState.EVALUATING_RISK): AgentState.SLEEPING,
    (RunPhase.PRE_OPEN, AgentState.RECONCILING): AgentState.SUBMITTING_ORDERS,
    (RunPhase.PRE_OPEN, AgentState.SUBMITTING_ORDERS): AgentState.MONITORING,
    (RunPhase.PRE_OPEN, AgentState.MONITORING): AgentState.SLEEPING,
}

ENTRY_STATE: dict[RunPhase, AgentState] = {
    RunPhase.POST_CLOSE: AgentState.LOADING_DATA,
    RunPhase.PRE_OPEN: AgentState.RECONCILING,
}

TERMINAL: frozenset[AgentState] = frozenset({AgentState.SLEEPING, AgentState.ERROR})

# Estados que pertenecen a cada fase, en orden de ejecucion.
PHASE_STATES: dict[RunPhase, tuple[AgentState, ...]] = {
    RunPhase.POST_CLOSE: (
        AgentState.LOADING_DATA,
        AgentState.GENERATING_SIGNALS,
        AgentState.EVALUATING_RISK,
    ),
    RunPhase.PRE_OPEN: (
        AgentState.RECONCILING,
        AgentState.SUBMITTING_ORDERS,
        AgentState.MONITORING,
    ),
}


# ---------------------------------------------------------------------------
# Tabla de recuperacion tras crash
# ---------------------------------------------------------------------------

# Donde reanudar segun el estado persistido cuando murio el proceso.
#
# SUBMITTING_ORDERS -> RECONCILING es NO NEGOCIABLE (CLAUDE.md 2.4):
#   Si el proceso muere dentro de SUBMITTING_ORDERS, el conjunto de ordenes
#   que llego al broker es desconocido para el agente. submit_order tiene tres
#   pasos durables y el crash puede caer entre cualquiera de ellos; ademas el
#   bucle puede morir entre la orden k y la k+1. El estado real es una
#   particion del lote en {nunca enviada, enviada-desconocida, enviada-confirmada}.
#   Reintentar los submits directamente duplicaria exactamente el subconjunto
#   "enviada-desconocida". RECONCILING es la funcion que resuelve la particion
#   consultando el estado autoritativo del broker por client_order_id.
#
# GENERATING_SIGNALS y EVALUATING_RISK -> LOADING_DATA porque ambos dependen
# de datos en memoria (closes_history, PortfolioTarget) que murieron con el
# proceso. La estrategia es stateful; reanudar ahi produciria cero senales en
# silencio. Recomputar es puro y determinista.
#
# MONITORING -> RECONCILING porque advance_to() muta cash y posiciones;
# reentrar por RECONCILING revalida el WAL primero.
RESUME_MAP: dict[AgentState, AgentState] = {
    AgentState.LOADING_DATA: AgentState.LOADING_DATA,
    AgentState.GENERATING_SIGNALS: AgentState.LOADING_DATA,
    AgentState.EVALUATING_RISK: AgentState.LOADING_DATA,
    AgentState.SUBMITTING_ORDERS: AgentState.RECONCILING,
    AgentState.RECONCILING: AgentState.RECONCILING,
    AgentState.MONITORING: AgentState.RECONCILING,
}


def next_state(phase: RunPhase, state: AgentState) -> AgentState:
    """Devuelve el siguiente estado. Lanza InvalidTransition si no esta declarado."""
    key = (phase, state)
    if key not in TRANSITIONS:
        raise InvalidTransition(
            f"Transicion no declarada: fase={phase.value} estado={state.value}"
        )
    return TRANSITIONS[key]


def resume_state(persisted: AgentState) -> AgentState:
    """Devuelve el estado desde el que reanudar tras un crash.

    SLEEPING y ERROR no se reanudan: SLEEPING inicia un ciclo nuevo y ERROR
    rechaza arrancar. Ambos se manejan en el llamante, no aqui.
    """
    if persisted in TERMINAL:
        raise InvalidTransition(
            f"{persisted.value} es terminal: no se reanuda desde aqui"
        )
    return RESUME_MAP[persisted]


def phase_of(state: AgentState) -> RunPhase:
    """Devuelve la fase a la que pertenece un estado no terminal."""
    for phase, states in PHASE_STATES.items():
        if state in states:
            return phase
    raise InvalidTransition(f"{state.value} no pertenece a ninguna fase")
