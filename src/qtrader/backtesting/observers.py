"""Observers del motor de backtesting (T2.1 — stubs; se rellenan en T2.2 y T3.2).

Patrón observer: el motor llama on_event() en cada BacktestEvent emitido.
Los observers no deben modificar el estado del motor ni lanzar excepciones
que interrumpan el bucle (deben capturarlas internamente si es necesario).
"""
from __future__ import annotations

from qtrader.backtesting.events import BacktestEvent  # noqa: TCH001 — used in method signature


class AuditObserver:
    """Registra todos los eventos en el audit log (stub — T3.2 lo implementa)."""

    def on_event(self, event: BacktestEvent) -> None:
        pass  # TODO T3.2: persistir en SQLiteLedger.audit_log con hash chain


class MetricsObserver:
    """Calcula métricas de rendimiento en tiempo real (stub — T2.2 lo implementa).

    Acumula equity_curve para calcular Sharpe, max drawdown, etc.
    """

    def __init__(self) -> None:
        self.events: list[BacktestEvent] = []

    def on_event(self, event: BacktestEvent) -> None:
        self.events.append(event)  # TODO T2.2: calcular métricas incremental
