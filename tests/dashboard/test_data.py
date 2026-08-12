"""Tests de las funciones de lectura del dashboard (Tarea 7B).

Sin Streamlit. Solo funciones puras de data.py contra BDs SQLite en tmp_path.

Cubre:
    test_read_positions_empty_db
    test_read_positions_with_data
    test_read_audit_log_empty
    test_read_audit_log_with_entries
    test_read_metrics_csv_missing_file
    test_health_server_halt_endpoint
"""
from __future__ import annotations

import csv
import json
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from qtrader.dashboard.data import (
    read_audit_log,
    read_positions,
    read_metrics_latest,
    is_kill_switch_active,
)
from qtrader.monitoring.health import HealthServer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BROKER_DDL = """
CREATE TABLE IF NOT EXISTS positions (
    symbol      TEXT PRIMARY KEY,
    quantity    TEXT NOT NULL,
    avg_cost    TEXT NOT NULL,
    currency    TEXT NOT NULL DEFAULT 'EUR',
    updated_at  TEXT NOT NULL
);
"""

_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS audit_log (
    record_id   TEXT NOT NULL UNIQUE,
    timestamp   TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    data        TEXT NOT NULL,
    record_hash TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _make_broker_db(path: Path, rows: list[dict] | None = None) -> Path:
    conn = sqlite3.connect(str(path))
    conn.executescript(_BROKER_DDL)
    if rows:
        for r in rows:
            conn.execute(
                "INSERT INTO positions (symbol, quantity, avg_cost, currency, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (r["symbol"], r["quantity"], r["avg_cost"],
                 r.get("currency", "EUR"), r.get("updated_at", "2024-06-14T10:00:00")),
            )
    conn.commit()
    conn.close()
    return path


def _make_ledger_db(path: Path, entries: list[dict] | None = None) -> Path:
    conn = sqlite3.connect(str(path))
    conn.executescript(_LEDGER_DDL)
    if entries:
        for e in entries:
            conn.execute(
                "INSERT INTO audit_log (record_id, timestamp, event_type, data, record_hash) "
                "VALUES (?, ?, ?, ?, ?)",
                (e["record_id"], e["timestamp"], e["event_type"],
                 json.dumps(e.get("data", {})), e.get("record_hash", "deadbeef")),
            )
    conn.commit()
    conn.close()
    return path


# ---------------------------------------------------------------------------
# 1. test_read_positions_empty_db
# ---------------------------------------------------------------------------


def test_read_positions_empty_db(tmp_path: Path) -> None:
    """read_positions devuelve [] para una BD vacía (sin filas)."""
    db = _make_broker_db(tmp_path / "broker_empty.db")
    result = read_positions(broker_db=db)
    assert result == []


# ---------------------------------------------------------------------------
# 2. test_read_positions_with_data
# ---------------------------------------------------------------------------


def test_read_positions_with_data(tmp_path: Path) -> None:
    """read_positions devuelve las filas correctas."""
    rows = [
        {"symbol": "SPY", "quantity": "10", "avg_cost": "450.00"},
        {"symbol": "QQQ", "quantity": "5", "avg_cost": "380.00"},
    ]
    db = _make_broker_db(tmp_path / "broker.db", rows=rows)
    result = read_positions(broker_db=db)

    assert len(result) == 2
    symbols = {r["symbol"] for r in result}
    assert symbols == {"SPY", "QQQ"}
    spy = next(r for r in result if r["symbol"] == "SPY")
    assert spy["quantity"] == "10"
    assert spy["avg_cost"] == "450.00"


# ---------------------------------------------------------------------------
# 3. test_read_audit_log_empty
# ---------------------------------------------------------------------------


def test_read_audit_log_empty(tmp_path: Path) -> None:
    """read_audit_log devuelve [] si la tabla audit_log está vacía."""
    db = _make_ledger_db(tmp_path / "ledger_empty.db")
    result = read_audit_log(ledger_db=db)
    assert result == []


# ---------------------------------------------------------------------------
# 4. test_read_audit_log_with_entries
# ---------------------------------------------------------------------------


def test_read_audit_log_with_entries(tmp_path: Path) -> None:
    """read_audit_log devuelve las entradas en orden DESC."""
    entries = [
        {
            "record_id": "cycle-001:LOADING_DATA:0001",
            "timestamp": "2024-06-14T08:00:00+00:00",
            "event_type": "CYCLE_START",
            "data": {"phase": "POST_CLOSE"},
        },
        {
            "record_id": "cycle-001:MONITORING:0002",
            "timestamp": "2024-06-14T09:00:00+00:00",
            "event_type": "CYCLE_END",
            "data": {"orders": 3},
        },
    ]
    db = _make_ledger_db(tmp_path / "ledger.db", entries=entries)
    result = read_audit_log(ledger_db=db, limit=20)

    assert len(result) == 2
    # DESC: la más reciente primero
    assert result[0]["event_type"] == "CYCLE_END"
    assert result[1]["event_type"] == "CYCLE_START"
    # data deserializado como dict
    assert isinstance(result[0]["data"], dict)
    assert result[0]["data"]["orders"] == 3


# ---------------------------------------------------------------------------
# 5. test_read_metrics_csv_missing_file
# ---------------------------------------------------------------------------


def test_read_metrics_csv_missing_file(tmp_path: Path) -> None:
    """read_metrics_latest devuelve {} si el CSV no existe."""
    missing = tmp_path / "nonexistent_metrics.csv"
    result = read_metrics_latest(metrics_csv=missing)
    assert result == {}


# ---------------------------------------------------------------------------
# 6. test_health_server_halt_endpoint
# ---------------------------------------------------------------------------


def test_health_server_halt_endpoint(tmp_path: Path) -> None:
    """POST /halt crea el fichero HALT y devuelve 200 {"ok": true}."""
    halt_path = tmp_path / "HALT"
    assert not halt_path.exists()

    server = HealthServer(
        status_fn=lambda: {"status": "ok", "state": "MONITORING", "nav": 15000.0, "kill_switch": False},
        halt_path=halt_path,
        port=0,
    )
    server.start()
    port = server.port

    try:
        url = f"http://127.0.0.1:{port}/halt"
        data = json.dumps({"reason": "test-halt"}).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")

        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            assert resp.status == 200
            body = json.loads(resp.read())
            assert body["ok"] is True

        assert halt_path.exists(), "El fichero HALT debe haberse creado"
        content = halt_path.read_text(encoding="utf-8")
        assert "test-halt" in content

        # Segunda llamada → 409 (ya activo)
        try:
            urllib.request.urlopen(  # noqa: S310
                urllib.request.Request(url, data=data, method="POST"), timeout=5
            )
            pytest.fail("Esperaba HTTPError 409")
        except urllib.error.HTTPError as exc:
            assert exc.code == 409
            body2 = json.loads(exc.read())
            assert body2["ok"] is False
            assert "ya activo" in body2["detail"]

    finally:
        server.stop()
