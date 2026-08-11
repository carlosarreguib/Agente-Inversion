"""Registro persistente de trials de investigacion (T2.4).

Invariantes de la DB:
  - INSERT-only. NUNCA UPDATE ni DELETE. Si un trial falla, se registra
    con status=FAILED y error_message. La historia es inmutable.
  - trials.db es independiente de state.db. No mezclar (invariante T2.4).
  - Todo acceso pasa por TrialsDB. Nunca SQL raw fuera de este modulo.

Esquema de la tabla trials:
  trial_id        TEXT PRIMARY KEY   -- UUID v4
  timestamp       TEXT               -- ISO8601 UTC
  strategy_id     TEXT
  parameters      TEXT               -- JSON serializado
  fold_id         INTEGER
  train_start     TEXT               -- fecha ISO8601
  train_end       TEXT
  test_start      TEXT
  test_end        TEXT
  sharpe_is       REAL               -- in-sample (puede ser NULL si FAILED)
  sharpe_oos      REAL               -- out-of-sample (puede ser NULL si FAILED)
  max_drawdown_oos REAL
  num_trades_oos  INTEGER
  status          TEXT               -- COMPLETED / FAILED / RUNNING
  error_message   TEXT               -- NULL si COMPLETED
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator  # noqa: TCH003
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime  # noqa: TCH003
from decimal import Decimal
from pathlib import Path  # noqa: TCH003

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS trials (
    trial_id         TEXT    PRIMARY KEY,
    timestamp        TEXT    NOT NULL,
    strategy_id      TEXT    NOT NULL,
    parameters       TEXT    NOT NULL,
    fold_id          INTEGER NOT NULL,
    train_start      TEXT    NOT NULL,
    train_end        TEXT    NOT NULL,
    test_start       TEXT    NOT NULL,
    test_end         TEXT    NOT NULL,
    sharpe_is        REAL,
    sharpe_oos       REAL,
    max_drawdown_oos REAL,
    num_trades_oos   INTEGER,
    status           TEXT    NOT NULL DEFAULT 'RUNNING',
    error_message    TEXT
);

CREATE TABLE IF NOT EXISTS _schema_version (
    version INTEGER PRIMARY KEY
);

INSERT OR IGNORE INTO _schema_version (version) VALUES (1);
"""

_INSERT_SQL = """
INSERT INTO trials (
    trial_id, timestamp, strategy_id, parameters, fold_id,
    train_start, train_end, test_start, test_end,
    sharpe_is, sharpe_oos, max_drawdown_oos, num_trades_oos,
    status, error_message
) VALUES (
    :trial_id, :timestamp, :strategy_id, :parameters, :fold_id,
    :train_start, :train_end, :test_start, :test_end,
    :sharpe_is, :sharpe_oos, :max_drawdown_oos, :num_trades_oos,
    :status, :error_message
)
"""


@dataclass(frozen=True)
class TrialRecord:
    """Registro inmutable de un trial."""

    trial_id: str
    timestamp: datetime
    strategy_id: str
    parameters: dict[str, str]
    fold_id: int
    train_start: date
    train_end: date
    test_start: date
    test_end: date
    sharpe_is: Decimal | None
    sharpe_oos: Decimal | None
    max_drawdown_oos: Decimal | None
    num_trades_oos: int | None
    status: str                  # COMPLETED / FAILED / RUNNING
    error_message: str | None = None


class TrialsDB:
    """Acceso INSERT-only a trials.db.

    Garantiza que no se ejecutan UPDATE ni DELETE sobre la tabla trials.
    """

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._init_schema()

    # ------------------------------------------------------------------
    # API publica
    # ------------------------------------------------------------------

    def insert(self, record: TrialRecord) -> None:
        """Inserta un trial. Lanza ValueError si trial_id ya existe."""
        row = {
            "trial_id": record.trial_id,
            "timestamp": record.timestamp.isoformat(),
            "strategy_id": record.strategy_id,
            "parameters": json.dumps(record.parameters, ensure_ascii=False),
            "fold_id": record.fold_id,
            "train_start": record.train_start.isoformat(),
            "train_end": record.train_end.isoformat(),
            "test_start": record.test_start.isoformat(),
            "test_end": record.test_end.isoformat(),
            "sharpe_is": float(record.sharpe_is) if record.sharpe_is is not None else None,
            "sharpe_oos": float(record.sharpe_oos) if record.sharpe_oos is not None else None,
            "max_drawdown_oos": (
                float(record.max_drawdown_oos)
                if record.max_drawdown_oos is not None
                else None
            ),
            "num_trades_oos": record.num_trades_oos,
            "status": record.status,
            "error_message": record.error_message,
        }
        with self._connect() as conn:
            try:
                conn.execute(_INSERT_SQL, row)
            except sqlite3.IntegrityError as exc:
                raise ValueError(
                    f"trial_id {record.trial_id!r} ya existe en la DB"
                ) from exc

    def count(self, strategy_id: str | None = None) -> int:
        """Numero total de trials, opcionalmente filtrado por estrategia."""
        with self._connect() as conn:
            if strategy_id is None:
                row = conn.execute("SELECT COUNT(*) FROM trials").fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM trials WHERE strategy_id = ?",
                    (strategy_id,),
                ).fetchone()
        return int(row[0])

    def fetch_completed(self, strategy_id: str) -> list[TrialRecord]:
        """Todos los trials COMPLETED de una estrategia, ordenados por timestamp."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT trial_id, timestamp, strategy_id, parameters, fold_id,
                       train_start, train_end, test_start, test_end,
                       sharpe_is, sharpe_oos, max_drawdown_oos, num_trades_oos,
                       status, error_message
                FROM trials
                WHERE strategy_id = ? AND status = 'COMPLETED'
                ORDER BY timestamp ASC
                """,
                (strategy_id,),
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    def fetch_all(self, strategy_id: str | None = None) -> list[TrialRecord]:
        """Todos los trials (cualquier status), opcionalm. filtrados."""
        with self._connect() as conn:
            if strategy_id is None:
                rows = conn.execute(
                    "SELECT * FROM trials ORDER BY timestamp ASC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM trials WHERE strategy_id = ? ORDER BY timestamp ASC",
                    (strategy_id,),
                ).fetchall()
        return [_row_to_record(r) for r in rows]

    # ------------------------------------------------------------------
    # Internos
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA_SQL)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        # Deshabilitar UPDATE y DELETE con un trigger no es portable;
        # la garantia INSERT-only la da la API (no hay metodo update/delete).
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _row_to_record(row: sqlite3.Row) -> TrialRecord:
    return TrialRecord(
        trial_id=row["trial_id"],
        timestamp=datetime.fromisoformat(row["timestamp"]).replace(tzinfo=UTC),
        strategy_id=row["strategy_id"],
        parameters=json.loads(row["parameters"]),
        fold_id=row["fold_id"],
        train_start=date.fromisoformat(row["train_start"]),
        train_end=date.fromisoformat(row["train_end"]),
        test_start=date.fromisoformat(row["test_start"]),
        test_end=date.fromisoformat(row["test_end"]),
        sharpe_is=Decimal(str(row["sharpe_is"])) if row["sharpe_is"] is not None else None,
        sharpe_oos=(
            Decimal(str(row["sharpe_oos"])) if row["sharpe_oos"] is not None else None
        ),
        max_drawdown_oos=(
            Decimal(str(row["max_drawdown_oos"]))
            if row["max_drawdown_oos"] is not None
            else None
        ),
        num_trades_oos=row["num_trades_oos"],
        status=row["status"],
        error_message=row["error_message"],
    )


def now_utc() -> datetime:
    return datetime.now(tz=UTC)
