"""MetricsCollector — singleton por proceso, thread-safe.

Escribe CSV append-only en data/metrics.csv.
Campos: timestamp (ISO 8601 UTC), metric_name, value, labels (JSON).

Métricas que emite el agente al final de cada ciclo:
    agent.cycle.duration_ms
    agent.orders.submitted
    agent.orders.blocked
    agent.fills.count
    portfolio.nav
    portfolio.drawdown
    portfolio.exposure
    risk.level          (0=NORMAL, 1=CAUTION, 2=RISK_OFF, 3=HALT)
    data.symbols_excluded
    data.validation_warnings
"""
from __future__ import annotations

import csv
import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

_DEFAULT_PATH = Path("data/metrics.csv")
_CSV_FIELDS = ("timestamp", "metric_name", "value", "labels")


class MetricsCollector:
    """Singleton thread-safe que escribe métricas en CSV append-only.

    Uso típico:
        collector = MetricsCollector.get()
        collector.emit("portfolio.nav", 15234.50, {"cycle_id": "abc"})
    """

    _instance: MetricsCollector | None = None
    _lock: threading.Lock = threading.Lock()

    def __init__(self, path: str | Path = _DEFAULT_PATH) -> None:
        self._path = Path(path)
        self._write_lock = threading.Lock()
        self._ensure_header()

    # ------------------------------------------------------------------
    # Singleton
    # ------------------------------------------------------------------

    @classmethod
    def get(cls, path: str | Path = _DEFAULT_PATH) -> MetricsCollector:
        """Devuelve la instancia singleton, creándola si no existe."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(path)
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Resetea el singleton. Solo para tests."""
        with cls._lock:
            cls._instance = None

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    def emit(
        self,
        metric_name: str,
        value: float | int,
        labels: dict[str, Any] | None = None,
    ) -> None:
        """Añade una fila al CSV. No lanza excepciones; loggea y continúa."""
        row = {
            "timestamp": datetime.now(UTC).isoformat(),
            "metric_name": metric_name,
            "value": str(value),
            "labels": json.dumps(labels or {}),
        }
        try:
            with self._write_lock:
                with self._path.open("a", newline="", encoding="utf-8") as fh:
                    writer = csv.DictWriter(fh, fieldnames=_CSV_FIELDS)
                    writer.writerow(row)
        except OSError as exc:
            _log.error("metrics emit failed: %s", exc)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _ensure_header(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists() or self._path.stat().st_size == 0:
            with self._path.open("w", newline="", encoding="utf-8") as fh:
                csv.DictWriter(fh, fieldnames=_CSV_FIELDS).writeheader()
