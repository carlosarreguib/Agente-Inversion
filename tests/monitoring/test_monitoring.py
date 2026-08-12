"""Tests de observabilidad (Tarea 7A).

Cubre:
    test_structured_log_has_required_fields
    test_no_api_keys_in_logs
    test_metrics_csv_append_only
    test_metrics_thread_safe
    test_alert_logged_on_kill_switch
    test_alert_telegram_skipped_if_no_token
    test_health_ok_when_agent_running
    test_health_503_when_kill_switch_active
"""
from __future__ import annotations

import csv
import io
import json
import logging
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import structlog

from qtrader.monitoring.alerts import AlertCondition, AlertManager
from qtrader.monitoring.health import HealthServer
from qtrader.monitoring.logging import RedactProcessor, configure_logging
from qtrader.monitoring.metrics import MetricsCollector


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_metrics_singleton() -> None:
    """Garantiza que cada test parte de un MetricsCollector limpio."""
    MetricsCollector.reset()


@pytest.fixture()
def tmp_metrics(tmp_path: Path) -> Path:
    return tmp_path / "metrics.csv"


@pytest.fixture()
def tmp_alerts(tmp_path: Path) -> Path:
    return tmp_path / "alerts.log"


# ---------------------------------------------------------------------------
# 1. test_structured_log_has_required_fields
# ---------------------------------------------------------------------------


def test_structured_log_has_required_fields() -> None:
    """La cadena de procesadores añade todos los campos obligatorios del spec."""
    from unittest.mock import MagicMock

    from qtrader.monitoring.logging import (
        RedactProcessor,
        _add_defaults,
        _add_git_sha,
    )

    # Simular la cadena tal como la construye configure_logging:
    # contextvars → add_logger_name → add_log_level → TimeStamper → _add_defaults → _add_git_sha → redact
    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        _add_defaults,
        _add_git_sha,
        RedactProcessor(),
    ]

    # Enlazar contexto obligatorio
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        trading_date="2024-06-14",
        cycle_id="cycle-001",
        agent_state="MONITORING",
    )

    event_dict: dict[str, Any] = {"event": "test event", "_record": None}
    mock_logger = MagicMock()
    mock_logger.name = "test.required_fields"

    for proc in processors:
        event_dict = proc(mock_logger, "info", event_dict)  # type: ignore[assignment]

    structlog.contextvars.clear_contextvars()

    required = {"timestamp", "level", "logger", "event", "trading_date", "cycle_id", "agent_state", "git_sha"}
    missing = required - set(event_dict.keys())
    assert not missing, f"Campos obligatorios ausentes: {missing}"


# ---------------------------------------------------------------------------
# 2. test_no_api_keys_in_logs
# ---------------------------------------------------------------------------


def test_no_api_keys_in_logs() -> None:
    """El RedactProcessor reemplaza strings de ≥32 alfanuméricos con [REDACTED]."""
    processor = RedactProcessor()

    event_dict: dict[str, Any] = {
        "event": "fetching data",
        "api_key": "abcdefghijklmnopqrstuvwxyz123456",   # 32 chars — debe redactar
        "token": "ABCDEFGHIJKLMNOPQRSTUVWXYZ12345678",    # 38 chars — debe redactar
        "short": "abc123",                                  # corto — no toca
        "git_sha": "a" * 40,                               # clave segura — no toca
    }

    result = processor(MagicMock(), "info", event_dict)

    assert result["api_key"] == "[REDACTED]"
    assert result["token"] == "[REDACTED]"
    assert result["short"] == "abc123"
    assert result["git_sha"] == "a" * 40, "git_sha es clave segura y no debe redactarse"


# ---------------------------------------------------------------------------
# 3. test_metrics_csv_append_only
# ---------------------------------------------------------------------------


def test_metrics_csv_append_only(tmp_metrics: Path) -> None:
    """El CSV tiene cabecera una sola vez y las filas se acumulan."""
    collector = MetricsCollector(path=tmp_metrics)
    MetricsCollector._instance = collector  # registrar como singleton

    collector.emit("portfolio.nav", 15000.0)
    collector.emit("portfolio.nav", 15100.0, {"cycle": "c2"})

    rows = list(csv.DictReader(tmp_metrics.open(encoding="utf-8")))

    # Exactamente dos filas de datos (sin la cabecera)
    assert len(rows) == 2, f"Esperadas 2 filas, encontradas {len(rows)}"
    assert rows[0]["metric_name"] == "portfolio.nav"
    assert rows[1]["value"] == "15100.0"

    # Verificar que no hay cabecera duplicada al reabrir
    collector.emit("portfolio.nav", 15200.0)
    rows2 = list(csv.DictReader(tmp_metrics.open(encoding="utf-8")))
    assert len(rows2) == 3

    # Verificar que las columnas son exactamente las del spec
    assert set(rows[0].keys()) == {"timestamp", "metric_name", "value", "labels"}


# ---------------------------------------------------------------------------
# 4. test_metrics_thread_safe
# ---------------------------------------------------------------------------


def test_metrics_thread_safe(tmp_metrics: Path) -> None:
    """10 hilos emitiendo simultáneamente no corrompen el CSV."""
    collector = MetricsCollector(path=tmp_metrics)
    MetricsCollector._instance = collector

    n_threads = 10
    emits_per_thread = 20

    def worker(idx: int) -> None:
        for i in range(emits_per_thread):
            collector.emit("agent.cycle.duration_ms", float(idx * 100 + i), {"thread": idx})

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rows = list(csv.DictReader(tmp_metrics.open(encoding="utf-8")))
    expected = n_threads * emits_per_thread
    assert len(rows) == expected, f"Esperadas {expected} filas, encontradas {len(rows)}"

    # Verificar que cada fila tiene JSON válido en labels
    for row in rows:
        json.loads(row["labels"])


# ---------------------------------------------------------------------------
# 5. test_alert_logged_on_kill_switch
# ---------------------------------------------------------------------------


def test_alert_logged_on_kill_switch(tmp_alerts: Path, caplog: pytest.LogCaptureFixture) -> None:
    """KILL_SWITCH_ACTIVATED emite CRITICAL en el logger y escribe en alerts.log."""
    manager = AlertManager(alerts_log=tmp_alerts)

    with caplog.at_level(logging.CRITICAL, logger="qtrader.monitoring.alerts"):
        manager.kill_switch_activated()

    # Verificar log CRITICAL
    critical_records = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert critical_records, "No se emitió ningún log CRITICAL"
    assert any("KILL_SWITCH_ACTIVATED" in r.message for r in critical_records)

    # Verificar fichero alerts.log
    assert tmp_alerts.exists(), "alerts.log no fue creado"
    lines = tmp_alerts.read_text(encoding="utf-8").strip().splitlines()
    assert lines, "alerts.log está vacío"
    payload = json.loads(lines[0])
    assert payload["condition"] == AlertCondition.KILL_SWITCH_ACTIVATED


# ---------------------------------------------------------------------------
# 6. test_alert_telegram_skipped_if_no_token
# ---------------------------------------------------------------------------


def test_alert_telegram_skipped_if_no_token(tmp_alerts: Path) -> None:
    """Sin token de Telegram, ninguna llamada HTTP se realiza."""
    # Asegurar que las variables de entorno NO están presentes
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("urllib.request.urlopen") as mock_urlopen,
    ):
        manager = AlertManager(alerts_log=tmp_alerts)
        # No pasar token ni chat_id → Telegram desactivado
        manager.kill_switch_activated()

        # Dar tiempo al daemon thread si lo hubiera creado (no debería)
        time.sleep(0.05)
        mock_urlopen.assert_not_called()


# ---------------------------------------------------------------------------
# 7. test_health_ok_when_agent_running
# ---------------------------------------------------------------------------


def test_health_ok_when_agent_running() -> None:
    """GET /health devuelve 200 con los campos del spec cuando el agente está OK."""
    state: dict[str, Any] = {
        "status": "ok",
        "state": "MONITORING",
        "nav": 15234.50,
        "kill_switch": False,
    }
    server = HealthServer(status_fn=lambda: dict(state), port=0)
    # port=0 → el SO elige un puerto libre
    server.start()
    port = server.port

    try:
        url = f"http://127.0.0.1:{port}/health"
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310
            assert resp.status == 200
            body = json.loads(resp.read())
            assert body["status"] == "ok"
            assert body["state"] == "MONITORING"
            assert body["kill_switch"] is False
            assert body["nav"] == pytest.approx(15234.50)
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# 8. test_health_503_when_kill_switch_active
# ---------------------------------------------------------------------------


def test_health_503_when_kill_switch_active() -> None:
    """GET /health devuelve 503 cuando kill_switch=True."""
    state: dict[str, Any] = {
        "status": "halt",
        "state": "MONITORING",
        "nav": 15000.0,
        "kill_switch": True,
    }
    server = HealthServer(status_fn=lambda: dict(state), port=0)
    server.start()
    port = server.port

    try:
        url = f"http://127.0.0.1:{port}/health"
        req = urllib.request.Request(url)
        try:
            urllib.request.urlopen(req, timeout=5)  # noqa: S310
            pytest.fail("Esperaba HTTPError 503")
        except urllib.error.HTTPError as exc:
            assert exc.code == 503
            body = json.loads(exc.read())
            assert body["kill_switch"] is True
    finally:
        server.stop()
