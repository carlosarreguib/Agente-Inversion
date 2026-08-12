"""Observers del motor de backtesting.

Patrón observer: el motor llama on_event() en cada BacktestEvent emitido.
Los observers no deben modificar el estado del motor ni lanzar excepciones
que interrumpan el bucle (deben capturarlas internamente si es necesario).
"""
from __future__ import annotations

from datetime import date  # noqa: TCH003 — usado en anotaciones runtime
from decimal import Decimal  # noqa: TCH003 — usado en anotaciones runtime

from qtrader.backtesting.events import (
    BacktestEvent,
    CycleEndEvent,
    FillEvent,
)
from qtrader.core.types import Fill  # noqa: TCH001


class AuditObserver:
    """Registra todos los eventos en el audit log (stub — T3.2 lo implementa)."""

    def on_event(self, event: BacktestEvent) -> None:
        pass  # TODO T3.2: persistir en SQLiteLedger.audit_log con hash chain


class MetricsObserver:
    """Acumula equity curve y fills del backtest para calcular metricas completas.

    Recoge CycleEndEvent (equity NAV por dia) y FillEvent (fills ejecutados).
    La equity curve y los fills se pasan a compute_full_report() al finalizar.
    """

    def __init__(self) -> None:
        self.equity_curve: list[tuple[date, Decimal]] = []
        self.fills: list[Fill] = []

    def on_event(self, event: BacktestEvent) -> None:
        if isinstance(event, CycleEndEvent):
            self.equity_curve.append((event.trading_day, event.equity))
        elif isinstance(event, FillEvent):
            self.fills.append(event.fill)

    def reset(self) -> None:
        """Limpia el estado acumulado (util en tests)."""
        self.equity_curve.clear()
        self.fills.clear()
