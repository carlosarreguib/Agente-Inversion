"""Generacion determinista de client_order_id (T5.3).

Formula:
    client_order_id = SHA256(
        trading_date.isoformat() + symbol + side.value +
        strategy_id + str(sequence_number)
    )[:16]

sequence_number se persiste en SQLite (tabla order_sequence, 1 fila).
Se incrementa ANTES de usar el valor en la misma transaccion que
el INSERT de order_intentions. Si el proceso muere antes del INSERT,
el seq ya esta incrementado: el id nunca se reutiliza.
"""
from __future__ import annotations

import hashlib
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3 as _sqlite3
    from qtrader.core.types import Side


_DDL_SEQUENCE = """
CREATE TABLE IF NOT EXISTS order_sequence (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    next_seq INTEGER NOT NULL DEFAULT 1
);
INSERT OR IGNORE INTO order_sequence (id, next_seq) VALUES (1, 1);
"""


def init_sequence_table(conn: _sqlite3.Connection) -> None:
    """Crea la tabla order_sequence si no existe."""
    with conn:
        conn.executescript(_DDL_SEQUENCE)


def next_sequence(conn: _sqlite3.Connection) -> int:
    """Incrementa y devuelve el siguiente sequence_number de forma atomica.

    El incremento ocurre DENTRO de una transaccion. Al llamador le corresponde
    hacer commit antes de salir del contexto — aqui no se hace commit para
    que el caller pueda componer esta operacion con el INSERT de order_intentions
    en una unica transaccion atomica.
    """
    conn.execute(
        "UPDATE order_sequence SET next_seq = next_seq + 1 WHERE id = 1"
    )
    row = conn.execute("SELECT next_seq FROM order_sequence WHERE id = 1").fetchone()
    return int(row[0])


def generate_client_order_id(
    trading_date: date,
    symbol: str,
    side: Side,
    strategy_id: str,
    sequence_number: int,
) -> str:
    """Genera un client_order_id deterministico de 16 caracteres hex.

    Mismos argumentos => mismo id, siempre.
    """
    raw = (
        trading_date.isoformat()
        + symbol
        + side.value
        + strategy_id
        + str(sequence_number)
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
