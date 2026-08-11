from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from qtrader.core.types import AuditRecord, Fill, Side

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
    record_id       TEXT PRIMARY KEY,
    timestamp       TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    data_hash       TEXT NOT NULL,
    previous_hash   TEXT NOT NULL,
    strategy_id     TEXT,
    payload         TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
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
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO audit_log
                    (record_id, timestamp, event_type, data_hash,
                     previous_hash, strategy_id, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.record_id,
                    record.timestamp.isoformat(),
                    record.event_type,
                    record.data_hash,
                    record.previous_hash,
                    record.strategy_id,
                    json.dumps(record.payload),
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
