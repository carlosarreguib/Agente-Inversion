from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from qtrader.core.types import AuditRecord, Fill, Side

_NULL_HASH = "0" * 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fills (
    fill_id         TEXT PRIMARY KEY,
    client_order_id TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    quantity        TEXT NOT NULL,
    price           TEXT NOT NULL,
    commission      TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS positions (
    symbol          TEXT PRIMARY KEY,
    quantity        TEXT NOT NULL,
    avg_cost        TEXT NOT NULL,
    market_value    TEXT NOT NULL,
    unrealized_pnl  TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    record_id   TEXT NOT NULL UNIQUE,
    timestamp   TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    data        TEXT NOT NULL,
    record_hash TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_DROP = """
DROP TABLE IF EXISTS fills;
DROP TABLE IF EXISTS positions;
DROP TABLE IF EXISTS audit_log;
"""

_ZERO = Decimal("0")


class SQLiteLedger:
    """Ledger persistente en SQLite.

    El broker es la fuente de verdad; este ledger es una caché local (invariante §2.5).
    audit_log usa record_hash = SHA-256(data_json) para integridad verificable (ADR-003).
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Inicialización
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """Crea las tablas desde cero. Llamar al inicio del modo demo."""
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.executescript(_DROP + _SCHEMA)
            conn.commit()

    # ------------------------------------------------------------------
    # Escritura
    # ------------------------------------------------------------------

    def record_fill(self, fill: Fill) -> None:
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO fills
                    (fill_id, client_order_id, symbol, side, quantity, price,
                     commission, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fill.fill_id,
                    fill.client_order_id,
                    fill.symbol,
                    fill.side.value,
                    str(fill.quantity),
                    str(fill.price),
                    str(fill.commission),
                    fill.timestamp.isoformat(),
                ),
            )
            self._upsert_position(conn, fill)
            conn.commit()

    def record_audit(self, record: AuditRecord) -> None:
        """Serializa el registro como JSON, computa SHA-256 y persiste en audit_log."""
        data_json = json.dumps(record.model_dump(mode="json"), sort_keys=True)
        record_hash = hashlib.sha256(data_json.encode()).hexdigest()
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO audit_log
                    (record_id, timestamp, event_type, data, record_hash)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    record.record_id,
                    record.timestamp.isoformat(),
                    record.event_type,
                    data_json,
                    record_hash,
                ),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Lectura
    # ------------------------------------------------------------------

    def fill_count(self) -> int:
        with closing(sqlite3.connect(self._db_path)) as conn:
            cursor = conn.execute("SELECT count(*) FROM fills")
            row = cursor.fetchone()
            return int(row[0])

    def get_last_record_hash(self) -> str:
        """Devuelve el record_hash del último registro en audit_log, o NULL_HASH si vacío."""
        with closing(sqlite3.connect(self._db_path)) as conn:
            cursor = conn.execute(
                "SELECT record_hash FROM audit_log ORDER BY rowid DESC LIMIT 1"
            )
            row = cursor.fetchone()
            return str(row[0]) if row else _NULL_HASH

    def verify_chain(self) -> list[int]:
        """Verifica integridad de la cadena de hashes.

        Devuelve lista de rowids con registros inválidos (vacía = OK).
        Un registro es inválido si:
          1. SHA-256(data) != record_hash almacenado, o
          2. previous_hash embebido en data != record_hash del registro anterior.
        """
        with closing(sqlite3.connect(self._db_path)) as conn:
            rows = conn.execute(
                "SELECT rowid, data, record_hash FROM audit_log ORDER BY rowid"
            ).fetchall()

        corrupt: list[int] = []
        expected_prev_hash = _NULL_HASH

        for row in rows:
            rowid = int(row[0])
            data_str = str(row[1])
            stored_hash = str(row[2])

            # Check 1: integridad del registro
            computed = hashlib.sha256(data_str.encode()).hexdigest()
            if computed != stored_hash:
                corrupt.append(rowid)
                expected_prev_hash = stored_hash  # avanza con el hash original
                continue

            # Check 2: enlace de la cadena
            raw = json.loads(data_str)
            rec_prev = str(raw.get("previous_hash", "")) if isinstance(raw, dict) else ""

            if rec_prev != expected_prev_hash:
                corrupt.append(rowid)

            expected_prev_hash = stored_hash

        return corrupt

    # ------------------------------------------------------------------
    # Interno
    # ------------------------------------------------------------------

    def _upsert_position(self, conn: sqlite3.Connection, fill: Fill) -> None:
        """Actualiza la tabla de posiciones tras un fill."""
        cursor = conn.execute(
            "SELECT quantity, avg_cost FROM positions WHERE symbol = ?",
            (fill.symbol,),
        )
        row = cursor.fetchone()

        if fill.side == Side.BUY:
            if row is None:
                new_qty = fill.quantity
                new_avg_cost = fill.price
            else:
                old_qty = Decimal(str(row[0]))
                old_avg = Decimal(str(row[1]))
                new_qty = old_qty + fill.quantity
                new_avg_cost = (old_qty * old_avg + fill.quantity * fill.price) / new_qty

            market_value = new_qty * fill.price
            unrealized_pnl = market_value - new_qty * new_avg_cost

            conn.execute(
                """
                INSERT INTO positions (symbol, quantity, avg_cost,
                    market_value, unrealized_pnl, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    quantity       = excluded.quantity,
                    avg_cost       = excluded.avg_cost,
                    market_value   = excluded.market_value,
                    unrealized_pnl = excluded.unrealized_pnl,
                    updated_at     = excluded.updated_at
                """,
                (
                    fill.symbol,
                    str(new_qty),
                    str(new_avg_cost),
                    str(market_value),
                    str(unrealized_pnl),
                    fill.timestamp.isoformat(),
                ),
            )
        else:  # SELL
            if row is not None:
                old_qty = Decimal(str(row[0]))
                old_avg = Decimal(str(row[1]))
                new_qty = old_qty - fill.quantity
                if new_qty <= _ZERO:
                    conn.execute(
                        "DELETE FROM positions WHERE symbol = ?",
                        (fill.symbol,),
                    )
                else:
                    market_value = new_qty * fill.price
                    unrealized_pnl = market_value - new_qty * old_avg
                    conn.execute(
                        """
                        UPDATE positions SET quantity=?, market_value=?,
                            unrealized_pnl=?, updated_at=?
                        WHERE symbol=?
                        """,
                        (
                            str(new_qty),
                            str(market_value),
                            str(unrealized_pnl),
                            fill.timestamp.isoformat(),
                            fill.symbol,
                        ),
                    )
