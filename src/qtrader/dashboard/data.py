"""Funciones puras de lectura de datos para el dashboard.

Lee directamente de SQLite y CSV sin importar lógica de negocio.
Cada función devuelve datos o None/[] si la fuente no existe.
Nunca lanza excepciones — las errores se loggean y se devuelve vacío.

Las funciones son puras: sin estado, sin side-effects, testables sin Streamlit.
"""
from __future__ import annotations

import csv
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

# Rutas por defecto (relativas al cwd del proceso)
DEFAULT_AGENT_DB = Path("data/agent.db")
DEFAULT_BROKER_DB = Path("data/paper_broker.db")
DEFAULT_LEDGER_DB = Path("data/state.db")
DEFAULT_METRICS_CSV = Path("data/metrics.csv")
DEFAULT_TRIALS_DB = Path("data/research/trials.db")
DEFAULT_HALT_PATH = Path("data/HALT")
DEFAULT_HEALTH_URL = "http://127.0.0.1:8765/health"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_db(path: Path) -> sqlite3.Connection | None:
    """Abre una conexión SQLite en modo read-only. Devuelve None si no existe."""
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.OperationalError as exc:
        _log.warning("No se pudo abrir %s: %s", path, exc)
        return None


# ---------------------------------------------------------------------------
# Portfolio — paper_broker.db
# ---------------------------------------------------------------------------


def read_positions(broker_db: Path = DEFAULT_BROKER_DB) -> list[dict[str, Any]]:
    """Posiciones abiertas: símbolo, cantidad, avg_cost, market_value, unrealized_pnl."""
    conn = _open_db(broker_db)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT symbol, quantity, avg_cost, currency, updated_at FROM positions"
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error as exc:
        _log.warning("read_positions: %s", exc)
        return []
    finally:
        conn.close()


def read_cash(broker_db: Path = DEFAULT_BROKER_DB) -> float | None:
    """Último balance de cash del cash_ledger."""
    conn = _open_db(broker_db)
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT balance_after FROM cash_ledger ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return float(row["balance_after"]) if row else None
    except sqlite3.Error as exc:
        _log.warning("read_cash: %s", exc)
        return None
    finally:
        conn.close()


def read_equity_curve(broker_db: Path = DEFAULT_BROKER_DB) -> list[dict[str, Any]]:
    """Curva de equity desde cash_ledger: timestamp + nav (= cash, sin MTM)."""
    conn = _open_db(broker_db)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT timestamp, balance_after FROM cash_ledger ORDER BY id"
        ).fetchall()
        return [{"timestamp": r["timestamp"], "nav": float(r["balance_after"])} for r in rows]
    except sqlite3.Error as exc:
        _log.warning("read_equity_curve: %s", exc)
        return []
    finally:
        conn.close()


def read_fills(
    broker_db: Path = DEFAULT_BROKER_DB,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Últimos N fills: symbol, side, quantity, price, commission, timestamp."""
    conn = _open_db(broker_db)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            """
            SELECT symbol, side, quantity, fill_price, commission, filled_at
            FROM orders
            WHERE status = 'FILLED'
            ORDER BY filled_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error as exc:
        _log.warning("read_fills: %s", exc)
        return []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Agent — agent.db
# ---------------------------------------------------------------------------


def read_agent_state(agent_db: Path = DEFAULT_AGENT_DB) -> dict[str, Any] | None:
    """Último estado persistido de la máquina de estados del agente."""
    conn = _open_db(agent_db)
    if conn is None:
        return None
    try:
        row = conn.execute(
            """
            SELECT current_state, trading_date, updated_at, cycle_id, error_message
            FROM agent_state
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        return dict(row) if row else None
    except sqlite3.Error as exc:
        _log.warning("read_agent_state: %s", exc)
        return None
    finally:
        conn.close()


def read_nav_history(agent_db: Path = DEFAULT_AGENT_DB) -> list[dict[str, Any]]:
    """Historial de NAV diario desde agent_nav_history."""
    conn = _open_db(agent_db)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT trading_date, nav, cash, positions_value FROM agent_nav_history ORDER BY trading_date"
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error as exc:
        _log.warning("read_nav_history: %s", exc)
        return []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Audit log — state.db (ledger)
# ---------------------------------------------------------------------------


def read_audit_log(
    ledger_db: Path = DEFAULT_LEDGER_DB,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Últimas N entradas del audit_log: record_id, timestamp, event_type, data."""
    conn = _open_db(ledger_db)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            """
            SELECT record_id, timestamp, event_type, data
            FROM audit_log
            ORDER BY rowid DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        result = []
        for r in rows:
            entry = dict(r)
            try:
                entry["data"] = json.loads(entry["data"])
            except (json.JSONDecodeError, TypeError):
                pass
            result.append(entry)
        return result
    except sqlite3.Error as exc:
        _log.warning("read_audit_log: %s", exc)
        return []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Métricas — data/metrics.csv
# ---------------------------------------------------------------------------


def read_metrics_latest(
    metrics_csv: Path = DEFAULT_METRICS_CSV,
    metric_names: list[str] | None = None,
) -> dict[str, float]:
    """Último valor de cada métrica en el CSV. Devuelve {} si no existe."""
    if not metrics_csv.exists():
        return {}
    latest: dict[str, float] = {}
    try:
        with metrics_csv.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                name = row.get("metric_name", "")
                if metric_names and name not in metric_names:
                    continue
                try:
                    latest[name] = float(row["value"])
                except (ValueError, KeyError):
                    pass
    except OSError as exc:
        _log.warning("read_metrics_latest: %s", exc)
    return latest


def read_metrics_history(
    metrics_csv: Path = DEFAULT_METRICS_CSV,
    metric_name: str = "portfolio.nav",
) -> list[dict[str, Any]]:
    """Serie temporal de una métrica: [{timestamp, value}, ...]."""
    if not metrics_csv.exists():
        return []
    result: list[dict[str, Any]] = []
    try:
        with metrics_csv.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("metric_name") != metric_name:
                    continue
                try:
                    result.append({"timestamp": row["timestamp"], "value": float(row["value"])})
                except (ValueError, KeyError):
                    pass
    except OSError as exc:
        _log.warning("read_metrics_history: %s", exc)
    return result


# ---------------------------------------------------------------------------
# Research — trials.db
# ---------------------------------------------------------------------------


def read_trials(trials_db: Path = DEFAULT_TRIALS_DB) -> list[dict[str, Any]]:
    """Todos los trials de la tabla trials."""
    conn = _open_db(trials_db)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            """
            SELECT trial_id, timestamp, strategy_id, fold_id,
                   sharpe_is, sharpe_oos, max_drawdown_oos, num_trades_oos,
                   status, train_start, train_end, test_start, test_end
            FROM trials
            ORDER BY timestamp DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error as exc:
        _log.warning("read_trials: %s", exc)
        return []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


def is_kill_switch_active(halt_path: Path = DEFAULT_HALT_PATH) -> bool:
    """True si el fichero HALT existe."""
    return halt_path.exists()


def post_halt(reason: str, health_url: str = DEFAULT_HEALTH_URL) -> tuple[bool, str]:
    """Llama a POST /halt del HealthServer. Devuelve (ok, mensaje)."""
    import urllib.error
    import urllib.request

    url = health_url.replace("/health", "/halt")
    data = json.dumps({"reason": reason}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            body = json.loads(resp.read())
            return body.get("ok", False), body.get("detail", "ok")
    except urllib.error.HTTPError as exc:
        body_bytes = exc.read()
        try:
            body = json.loads(body_bytes)
            return False, body.get("detail", str(exc))
        except json.JSONDecodeError:
            return False, str(exc)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return False, str(exc)
