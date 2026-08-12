"""BD separada de proposals.db para propuestas de investigación pendientes de aprobación.

Invariantes:
  - NUNCA mezclar con trials.db (CLAUDE.md §2.9 — separación explícita).
  - INSERT-only para propuestas. El estado puede actualizarse a APPROVED/REJECTED
    pero SOLO via el comando `research approve` con confirmación interactiva.
  - Una propuesta PENDING_APPROVAL no tiene efecto en el sistema de producción
    hasta que se aprueba manualmente.

Esquema:
  proposal_id   TEXT PRIMARY KEY   -- UUID v4
  strategy_id   TEXT
  parameters    TEXT               -- JSON
  sharpe_oos    REAL
  maxdd         REAL
  dsr           REAL
  n_trials      INTEGER
  status        TEXT               -- PENDING_APPROVAL | APPROVED | REJECTED
  created_at    TEXT               -- ISO8601 UTC
  summary       TEXT
  reviewed_at   TEXT               -- NULL hasta revisión
  reviewed_by   TEXT               -- NULL hasta revisión
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator  # noqa: TCH003
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path  # noqa: TCH003

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id  TEXT    PRIMARY KEY,
    strategy_id  TEXT    NOT NULL,
    parameters   TEXT    NOT NULL,
    sharpe_oos   REAL    NOT NULL,
    maxdd        REAL    NOT NULL,
    dsr          REAL    NOT NULL,
    n_trials     INTEGER NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'PENDING_APPROVAL',
    created_at   TEXT    NOT NULL,
    summary      TEXT    NOT NULL,
    reviewed_at  TEXT,
    reviewed_by  TEXT
);

CREATE TABLE IF NOT EXISTS _schema_version (
    version INTEGER PRIMARY KEY
);

INSERT OR IGNORE INTO _schema_version (version) VALUES (1);
"""

_INSERT_SQL = """
INSERT INTO proposals (
    proposal_id, strategy_id, parameters, sharpe_oos, maxdd, dsr,
    n_trials, status, created_at, summary
) VALUES (
    :proposal_id, :strategy_id, :parameters, :sharpe_oos, :maxdd, :dsr,
    :n_trials, :status, :created_at, :summary
)
"""

_UPDATE_STATUS_SQL = """
UPDATE proposals
SET status = :status, reviewed_at = :reviewed_at, reviewed_by = :reviewed_by
WHERE proposal_id = :proposal_id AND status = 'PENDING_APPROVAL'
"""


@dataclass(frozen=True)
class ProposalRecord:
    proposal_id: str
    strategy_id: str
    parameters: dict[str, float | int | str]
    sharpe_oos: float
    maxdd: float
    dsr: float
    n_trials: int
    status: str
    created_at: datetime
    summary: str
    reviewed_at: datetime | None = None
    reviewed_by: str | None = None


class ProposalsDB:
    """Acceso a proposals.db — separada de trials.db."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._init_schema()

    def insert(self, record: ProposalRecord) -> None:
        """Inserta una propuesta nueva. Lanza ValueError si proposal_id ya existe."""
        row = {
            "proposal_id": record.proposal_id,
            "strategy_id": record.strategy_id,
            "parameters": json.dumps(record.parameters, ensure_ascii=False),
            "sharpe_oos": record.sharpe_oos,
            "maxdd": record.maxdd,
            "dsr": record.dsr,
            "n_trials": record.n_trials,
            "status": record.status,
            "created_at": record.created_at.isoformat(),
            "summary": record.summary,
        }
        with self._connect() as conn:
            try:
                conn.execute(_INSERT_SQL, row)
            except sqlite3.IntegrityError as exc:
                raise ValueError(
                    f"proposal_id {record.proposal_id!r} ya existe en proposals.db"
                ) from exc

    def approve(self, proposal_id: str, reviewed_by: str) -> bool:
        """Marca propuesta como APPROVED. Devuelve True si se actualizó, False si no existía."""
        with self._connect() as conn:
            cur = conn.execute(
                _UPDATE_STATUS_SQL,
                {
                    "status": "APPROVED",
                    "reviewed_at": datetime.now(UTC).isoformat(),
                    "reviewed_by": reviewed_by,
                    "proposal_id": proposal_id,
                },
            )
            return cur.rowcount > 0

    def reject(self, proposal_id: str, reviewed_by: str) -> bool:
        """Marca propuesta como REJECTED."""
        with self._connect() as conn:
            cur = conn.execute(
                _UPDATE_STATUS_SQL,
                {
                    "status": "REJECTED",
                    "reviewed_at": datetime.now(UTC).isoformat(),
                    "reviewed_by": reviewed_by,
                    "proposal_id": proposal_id,
                },
            )
            return cur.rowcount > 0

    def fetch_pending(self) -> list[ProposalRecord]:
        """Devuelve todas las propuestas PENDING_APPROVAL."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM proposals WHERE status = 'PENDING_APPROVAL' ORDER BY created_at ASC"
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    def fetch_all(self) -> list[ProposalRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM proposals ORDER BY created_at ASC"
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    def get(self, proposal_id: str) -> ProposalRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM proposals WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
        return _row_to_record(row) if row else None

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA_SQL)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _row_to_record(row: sqlite3.Row) -> ProposalRecord:
    reviewed_at = (
        datetime.fromisoformat(row["reviewed_at"]).replace(tzinfo=UTC)
        if row["reviewed_at"]
        else None
    )
    return ProposalRecord(
        proposal_id=row["proposal_id"],
        strategy_id=row["strategy_id"],
        parameters=json.loads(row["parameters"]),
        sharpe_oos=float(row["sharpe_oos"]),
        maxdd=float(row["maxdd"]),
        dsr=float(row["dsr"]),
        n_trials=int(row["n_trials"]),
        status=row["status"],
        created_at=datetime.fromisoformat(row["created_at"]).replace(tzinfo=UTC),
        summary=row["summary"],
        reviewed_at=reviewed_at,
        reviewed_by=row["reviewed_by"],
    )
