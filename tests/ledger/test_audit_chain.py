"""Tests de integridad de la cadena de hashes en audit_log."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from qtrader.core.types import AuditRecord
from qtrader.ledger.sqlite import SQLiteLedger

if TYPE_CHECKING:
    from pathlib import Path

_NULL_HASH = "0" * 64


def _ts(hour: int) -> datetime:
    return datetime(2024, 1, 1, hour, 0, 0, tzinfo=UTC)


def _add_record(ledger: SQLiteLedger, idx: int) -> None:
    """Añade un AuditRecord con previous_hash correcto al ledger."""
    previous_hash = ledger.get_last_record_hash()
    rec = AuditRecord(
        record_id=f"rec-{idx:04d}",
        timestamp=_ts(idx % 24),
        event_type="TEST",
        data_hash=_NULL_HASH,
        previous_hash=previous_hash,
        payload={"idx": str(idx)},
    )
    ledger.record_audit(rec)


# ---------------------------------------------------------------------------
# Tests de get_last_record_hash
# ---------------------------------------------------------------------------


def test_get_last_record_hash_empty_returns_null_hash(tmp_path: Path) -> None:
    ledger = SQLiteLedger(str(tmp_path / "test.db"))
    ledger.initialize()
    assert ledger.get_last_record_hash() == _NULL_HASH


def test_get_last_record_hash_returns_hash_after_insert(tmp_path: Path) -> None:
    ledger = SQLiteLedger(str(tmp_path / "test.db"))
    ledger.initialize()

    _add_record(ledger, 1)
    h = ledger.get_last_record_hash()

    assert len(h) == 64
    assert h != _NULL_HASH


# ---------------------------------------------------------------------------
# Tests de verify_chain — cadena válida
# ---------------------------------------------------------------------------


def test_verify_chain_empty_db_is_ok(tmp_path: Path) -> None:
    ledger = SQLiteLedger(str(tmp_path / "test.db"))
    ledger.initialize()
    assert ledger.verify_chain() == []


def test_verify_chain_single_record_ok(tmp_path: Path) -> None:
    ledger = SQLiteLedger(str(tmp_path / "test.db"))
    ledger.initialize()
    _add_record(ledger, 1)
    assert ledger.verify_chain() == []


def test_verify_chain_multiple_records_ok(tmp_path: Path) -> None:
    ledger = SQLiteLedger(str(tmp_path / "test.db"))
    ledger.initialize()
    for i in range(1, 6):
        _add_record(ledger, i)
    assert ledger.verify_chain() == []


# ---------------------------------------------------------------------------
# Tests de verify_chain — detección de corrupción
# ---------------------------------------------------------------------------


def _corrupt_data(db_path: str, rowid: int) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            "UPDATE audit_log SET data='{\"corrupted\":true}' WHERE rowid=?",
            (rowid,),
        )
        conn.commit()


def test_verify_detects_first_record_corruption(tmp_path: Path) -> None:
    db = str(tmp_path / "test.db")
    ledger = SQLiteLedger(db)
    ledger.initialize()
    _add_record(ledger, 1)

    _corrupt_data(db, 1)

    corrupt = ledger.verify_chain()
    assert corrupt == [1]


def test_verify_detects_middle_record_corruption(tmp_path: Path) -> None:
    db = str(tmp_path / "test.db")
    ledger = SQLiteLedger(db)
    ledger.initialize()
    for i in range(1, 6):
        _add_record(ledger, i)

    _corrupt_data(db, 3)

    corrupt = ledger.verify_chain()
    assert 3 in corrupt
    assert corrupt[0] == 3  # el primero detectado debe ser el 3


def test_verify_last_record_corruption(tmp_path: Path) -> None:
    db = str(tmp_path / "test.db")
    ledger = SQLiteLedger(db)
    ledger.initialize()
    for i in range(1, 4):
        _add_record(ledger, i)

    _corrupt_data(db, 3)

    corrupt = ledger.verify_chain()
    assert 3 in corrupt


# ---------------------------------------------------------------------------
# Tests del encadenamiento — previous_hash correcto en cada registro
# ---------------------------------------------------------------------------


def test_chain_previous_hash_matches_record_hash_of_prior(tmp_path: Path) -> None:
    """Verifica que previous_hash[N] == record_hash[N-1] en la cadena real."""
    import hashlib
    import json

    db = str(tmp_path / "test.db")
    ledger = SQLiteLedger(db)
    ledger.initialize()
    for i in range(1, 4):
        _add_record(ledger, i)

    with closing(sqlite3.connect(db)) as conn:
        rows = conn.execute(
            "SELECT rowid, data, record_hash FROM audit_log ORDER BY rowid"
        ).fetchall()

    # Verifica que record_hash == SHA-256(data)
    for row in rows:
        data_str = str(row[1])
        stored_hash = str(row[2])
        computed = hashlib.sha256(data_str.encode()).hexdigest()
        assert computed == stored_hash, f"rowid={row[0]}: hash mismatch"

    # Verifica que previous_hash[N] == record_hash[N-1]
    prev_hash = _NULL_HASH
    for row in rows:
        data_dict = json.loads(str(row[1]))
        assert data_dict["previous_hash"] == prev_hash, (
            f"rowid={row[0]}: previous_hash no apunta al registro anterior"
        )
        prev_hash = str(row[2])


def test_genesis_record_has_null_previous_hash(tmp_path: Path) -> None:
    import json

    db = str(tmp_path / "test.db")
    ledger = SQLiteLedger(db)
    ledger.initialize()
    _add_record(ledger, 1)

    with closing(sqlite3.connect(db)) as conn:
        row = conn.execute(
            "SELECT data FROM audit_log ORDER BY rowid LIMIT 1"
        ).fetchone()

    data_dict = json.loads(str(row[0]))
    assert data_dict["previous_hash"] == _NULL_HASH
