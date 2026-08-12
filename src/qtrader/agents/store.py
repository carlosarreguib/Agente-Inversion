"""Persistencia del estado del agente (T6).

El agente posee su propio fichero SQLite (default data/agent.db) con DDL
idempotente. NUNCA llama a SQLiteLedger.initialize(), que ejecuta DROP de
todas las tablas (ledger/sqlite.py:82-86) y que `qtrader demo` invoca con
default data/state.db.

agent_state es append-only: encaja con la cultura de auditoria del repo y
hace triviales de inspeccionar los tests de crash. load() = ultima fila.
Todos los Decimal se almacenan como TEXT, como en el resto del repo.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime  # noqa: TCH003 — runtime pydantic
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from qtrader.agents.states import AgentState
from qtrader.brokers.order_id import init_sequence_table

_DDL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS agent_state (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    current_state  TEXT    NOT NULL,
    trading_date   TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL,
    cycle_id       TEXT    NOT NULL,
    error_message  TEXT
);

CREATE INDEX IF NOT EXISTS ix_agent_state_updated ON agent_state(updated_at DESC);

CREATE TABLE IF NOT EXISTS agent_pending_orders (
    cycle_id          TEXT    NOT NULL,
    order_id          TEXT    NOT NULL,
    trading_date      TEXT    NOT NULL,
    symbol            TEXT    NOT NULL,
    side              TEXT    NOT NULL,
    original_quantity TEXT    NOT NULL,
    approved_quantity TEXT    NOT NULL,
    price             TEXT    NOT NULL,
    notional          TEXT    NOT NULL,
    category          TEXT    NOT NULL,
    reduction_reason  TEXT,
    sequence_number   INTEGER,
    client_order_id   TEXT,
    submitted_at      TEXT,
    -- La clave es (trading_date, order_id) y NO incluye cycle_id: las ordenes
    -- las aprueba el ciclo POST_CLOSE del dia T y las envia el ciclo PRE_OPEN
    -- del dia siguiente, que tiene otro cycle_id. Con cycle_id en la clave, un
    -- recompute tras crash insertaria filas duplicadas de la misma orden.
    PRIMARY KEY (trading_date, order_id)
);

CREATE TABLE IF NOT EXISTS agent_nav_history (
    trading_date TEXT PRIMARY KEY,
    nav          TEXT NOT NULL
);
"""


class AgentStateRow(BaseModel):
    """Fila de agent_state. Inmutable."""

    model_config = ConfigDict(frozen=True)

    current_state: AgentState
    trading_date: date
    updated_at: datetime
    cycle_id: str
    error_message: str | None


class PendingOrderRow(BaseModel):
    """Orden aprobada pendiente de enviar. Inmutable."""

    model_config = ConfigDict(frozen=True)

    cycle_id: str
    order_id: str
    trading_date: date
    symbol: str
    side: str
    original_quantity: Decimal
    approved_quantity: Decimal
    price: Decimal
    notional: Decimal
    category: str
    reduction_reason: str | None
    sequence_number: int | None
    client_order_id: str | None
    submitted_at: datetime | None


class AgentStateStore:
    """Owner de agent_state, agent_pending_orders y agent_nav_history."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(_DDL)
        # order_sequence vive tambien aqui: el agente compone next_sequence()
        # con su escritura pre-submit en una unica transaccion, y eso exige que
        # ambas esten en la MISMA conexion. init_sequence_table es idempotente.
        init_sequence_table(self._conn)

    @property
    def conn(self) -> sqlite3.Connection:
        """Conexion cruda: la necesita next_sequence() para componer la
        asignacion de secuencia y la escritura pre-submit en una transaccion."""
        return self._conn

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # agent_state
    # ------------------------------------------------------------------

    def load(self) -> AgentStateRow | None:
        """Ultima fila persistida, o None si nunca se ha ejecutado."""
        row = self._conn.execute(
            """
            SELECT current_state, trading_date, updated_at, cycle_id, error_message
            FROM agent_state ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        return AgentStateRow(
            current_state=AgentState(row["current_state"]),
            trading_date=date.fromisoformat(row["trading_date"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            cycle_id=row["cycle_id"],
            error_message=row["error_message"],
        )

    def persist(
        self,
        state: AgentState,
        trading_date: date,
        cycle_id: str,
        error_message: str | None = None,
    ) -> None:
        """Anade una fila. Append-only: nunca UPDATE sobre la anterior."""
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO agent_state
                  (current_state, trading_date, updated_at, cycle_id, error_message)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    state.value,
                    trading_date.isoformat(),
                    datetime.now(UTC).isoformat(),
                    cycle_id,
                    error_message,
                ),
            )

    def state_history(self) -> list[AgentState]:
        """Secuencia de estados en orden de insercion. Para tests e inspeccion."""
        rows = self._conn.execute(
            "SELECT current_state FROM agent_state ORDER BY id"
        ).fetchall()
        return [AgentState(r["current_state"]) for r in rows]

    # ------------------------------------------------------------------
    # agent_pending_orders
    # ------------------------------------------------------------------

    def insert_pending(self, rows: list[PendingOrderRow]) -> None:
        """Persiste las ordenes aprobadas del ciclo POST_CLOSE."""
        with self._conn:
            for row in rows:
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO agent_pending_orders
                      (cycle_id, order_id, trading_date, symbol, side,
                       original_quantity, approved_quantity, price, notional,
                       category, reduction_reason, sequence_number,
                       client_order_id, submitted_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row.cycle_id,
                        row.order_id,
                        row.trading_date.isoformat(),
                        row.symbol,
                        row.side,
                        str(row.original_quantity),
                        str(row.approved_quantity),
                        str(row.price),
                        str(row.notional),
                        row.category,
                        row.reduction_reason,
                        row.sequence_number,
                        row.client_order_id,
                        row.submitted_at.isoformat() if row.submitted_at else None,
                    ),
                )

    def pending_up_to(self, execution_date: date) -> list[PendingOrderRow]:
        """Ordenes sin enviar cuya fecha de senal es <= execution_date.

        Dos motivos para que la busqueda NO sea por cycle_id ni por igualdad
        de fecha:

        1. cycle_id: las ordenes las aprueba el ciclo POST_CLOSE del dia T y
           las envia el ciclo PRE_OPEN, que es otro proceso con su propio
           cycle_id. Filtrar por cycle_id haria que PRE_OPEN nunca encontrase
           nada.
        2. Igualdad de fecha: la senal se genera con el cierre de T y se
           ejecuta contra la apertura de T+1 o posterior (invariante 3.1), asi
           que trading_date de la orden es SIEMPRE anterior a la fecha de
           ejecucion. Ademas, tras un fin de semana o un festivo la orden puede
           tener varios dias: <= recoge esos casos en vez de perderlos.
        """
        rows = self._conn.execute(
            """
            SELECT * FROM agent_pending_orders
            WHERE trading_date <= ? AND submitted_at IS NULL
            ORDER BY trading_date, order_id
            """,
            (execution_date.isoformat(),),
        ).fetchall()
        return [self._to_pending_row(r) for r in rows]

    def all_orders_for_date(self, trading_date: date) -> list[PendingOrderRow]:
        """Todas las ordenes de la fecha, enviadas o no. Para auditoria y tests."""
        rows = self._conn.execute(
            "SELECT * FROM agent_pending_orders WHERE trading_date = ? ORDER BY order_id",
            (trading_date.isoformat(),),
        ).fetchall()
        return [self._to_pending_row(r) for r in rows]

    def write_precommit(
        self,
        order_id: str,
        trading_date: date,
        client_order_id: str,
        sequence_number: int,
    ) -> None:
        """Escribe coid y secuencia ANTES del await submit_order.

        Es el WAL del lado del agente: sin esto, tras un crash el
        client_order_id no seria reconstruible y no se podria consultar el
        estado de la orden en el broker antes de reenviarla (CLAUDE.md 2.4).

        NO abre transaccion propia: el llamante la compone con next_sequence(),
        que deliberadamente no hace commit (order_id.py:40-52).
        """
        self._conn.execute(
            """
            UPDATE agent_pending_orders
            SET client_order_id = ?, sequence_number = ?
            WHERE order_id = ? AND trading_date = ?
            """,
            (client_order_id, sequence_number, order_id, trading_date.isoformat()),
        )

    def mark_submitted(self, order_id: str, trading_date: date) -> None:
        """Marca la orden como enviada tras recibir el acknowledgement."""
        with self._conn:
            self._conn.execute(
                """
                UPDATE agent_pending_orders SET submitted_at = ?
                WHERE order_id = ? AND trading_date = ?
                """,
                (datetime.now(UTC).isoformat(), order_id, trading_date.isoformat()),
            )

    def clear_unsubmitted(self, trading_date: date) -> None:
        """Borra las ordenes no enviadas de la fecha.

        Se usa al reanudar EVALUATING_RISK: el ciclo se recomputa entero y las
        filas a medio escribir deben desaparecer antes de reinsertar.
        Nunca borra ordenes ya enviadas.
        """
        with self._conn:
            self._conn.execute(
                """
                DELETE FROM agent_pending_orders
                WHERE trading_date = ? AND submitted_at IS NULL
                """,
                (trading_date.isoformat(),),
            )

    # ------------------------------------------------------------------
    # agent_nav_history
    # ------------------------------------------------------------------

    def record_nav(self, trading_date: date, nav: Decimal) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO agent_nav_history (trading_date, nav) VALUES (?, ?)",
                (trading_date.isoformat(), str(nav)),
            )

    def nav_history(self) -> list[tuple[date, Decimal]]:
        rows = self._conn.execute(
            "SELECT trading_date, nav FROM agent_nav_history ORDER BY trading_date"
        ).fetchall()
        return [(date.fromisoformat(r["trading_date"]), Decimal(r["nav"])) for r in rows]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _to_pending_row(row: sqlite3.Row) -> PendingOrderRow:
        submitted = row["submitted_at"]
        return PendingOrderRow(
            cycle_id=row["cycle_id"],
            order_id=row["order_id"],
            trading_date=date.fromisoformat(row["trading_date"]),
            symbol=row["symbol"],
            side=row["side"],
            original_quantity=Decimal(row["original_quantity"]),
            approved_quantity=Decimal(row["approved_quantity"]),
            price=Decimal(row["price"]),
            notional=Decimal(row["notional"]),
            category=row["category"],
            reduction_reason=row["reduction_reason"],
            sequence_number=row["sequence_number"],
            client_order_id=row["client_order_id"],
            submitted_at=datetime.fromisoformat(submitted) if submitted else None,
        )
