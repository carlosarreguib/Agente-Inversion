"""Tipos de eventos del motor de backtesting event-driven (T2.1).

El motor itera sobre días de trading y emite estos eventos en orden estricto:
  FillEvent     — al inicio de T: fills de órdenes pendientes contra open[T]
  BarEvent      — publicación de la barra de T a las estrategias
  SignalEvent   — señal generada por la estrategia tras ver BarEvent
  OrderEvent    — orden enviada al broker simulado
  RiskEvent     — decisión del risk engine sobre la orden

Los observers (patrón observer) reciben todos los eventos via on_event().
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date  # noqa: TCH003 — dataclass fields evaluated at runtime
from decimal import Decimal  # noqa: TCH003 — dataclass fields evaluated at runtime
from enum import StrEnum

from qtrader.core.types import (  # noqa: TCH001 — dataclass fields
    Fill,
    Instrument,
    Order,
    RiskDecision,
    Signal,
    ValidatedBar,
)


class EventType(StrEnum):
    BAR = "BAR"
    SIGNAL = "SIGNAL"
    ORDER = "ORDER"
    FILL = "FILL"
    RISK = "RISK"
    CYCLE_START = "CYCLE_START"
    CYCLE_END = "CYCLE_END"


@dataclass(frozen=True)
class BarEvent:
    event_type: EventType = field(default=EventType.BAR, init=False)
    trading_day: date
    bars: tuple[ValidatedBar, ...]  # barra de cada símbolo del universo


@dataclass(frozen=True)
class SignalEvent:
    event_type: EventType = field(default=EventType.SIGNAL, init=False)
    trading_day: date
    signals: tuple[Signal, ...]


@dataclass(frozen=True)
class OrderEvent:
    event_type: EventType = field(default=EventType.ORDER, init=False)
    trading_day: date
    order: Order
    intended_fill_date: date


@dataclass(frozen=True)
class FillEvent:
    event_type: EventType = field(default=EventType.FILL, init=False)
    trading_day: date          # día en que ocurre el fill (T+1)
    fill: Fill
    intended_fill_date: date   # debe coincidir con trading_day


@dataclass(frozen=True)
class RiskEvent:
    event_type: EventType = field(default=EventType.RISK, init=False)
    trading_day: date
    decision: RiskDecision


@dataclass(frozen=True)
class CycleStartEvent:
    event_type: EventType = field(default=EventType.CYCLE_START, init=False)
    trading_day: date
    universe: tuple[Instrument, ...]


@dataclass(frozen=True)
class CycleEndEvent:
    event_type: EventType = field(default=EventType.CYCLE_END, init=False)
    trading_day: date
    equity: Decimal


# Unión de todos los eventos (para typing de observers)
BacktestEvent = (
    BarEvent
    | SignalEvent
    | OrderEvent
    | FillEvent
    | RiskEvent
    | CycleStartEvent
    | CycleEndEvent
)
