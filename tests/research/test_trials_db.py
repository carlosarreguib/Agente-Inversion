"""Tests de TrialsDB: contrato INSERT-only y operaciones basicas."""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path  # noqa: TCH003

import pytest

from qtrader.research.trials_db import TrialRecord, TrialsDB, now_utc


def _make_record(
    *,
    trial_id: str | None = None,
    strategy_id: str = "momentum",
    fold_id: int = 0,
    status: str = "COMPLETED",
    sharpe_oos: Decimal | None = Decimal("0.85"),
) -> TrialRecord:
    return TrialRecord(
        trial_id=trial_id or str(uuid.uuid4()),
        timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC),
        strategy_id=strategy_id,
        parameters={"window": "20"},
        fold_id=fold_id,
        train_start=date(2018, 1, 2),
        train_end=date(2020, 12, 31),
        test_start=date(2021, 1, 4),
        test_end=date(2021, 12, 31),
        sharpe_is=Decimal("1.10"),
        sharpe_oos=sharpe_oos,
        max_drawdown_oos=Decimal("-0.15"),
        num_trades_oos=42,
        status=status,
        error_message=None,
    )


@pytest.fixture()
def db(tmp_path: Path) -> TrialsDB:
    return TrialsDB(tmp_path / "trials.db")


class TestInsertAndCount:
    def test_empty_db_count_is_zero(self, db: TrialsDB) -> None:
        assert db.count() == 0

    def test_insert_increments_count(self, db: TrialsDB) -> None:
        db.insert(_make_record())
        assert db.count() == 1

    def test_count_filtered_by_strategy(self, db: TrialsDB) -> None:
        db.insert(_make_record(strategy_id="mom"))
        db.insert(_make_record(strategy_id="mean_rev"))
        assert db.count("mom") == 1
        assert db.count("mean_rev") == 1
        assert db.count() == 2

    def test_duplicate_trial_id_raises(self, db: TrialsDB) -> None:
        tid = str(uuid.uuid4())
        db.insert(_make_record(trial_id=tid))
        with pytest.raises(ValueError, match="ya existe"):
            db.insert(_make_record(trial_id=tid))

    def test_insert_preserves_fields(self, db: TrialsDB) -> None:
        rec = _make_record(strategy_id="test_strat", fold_id=3)
        db.insert(rec)
        rows = db.fetch_all("test_strat")
        assert len(rows) == 1
        got = rows[0]
        assert got.strategy_id == "test_strat"
        assert got.fold_id == 3
        assert got.sharpe_oos == Decimal("0.85")


class TestInsertOnly:
    """Verificacion del contrato INSERT-only: no existen metodos update/delete."""

    def test_no_update_method(self, db: TrialsDB) -> None:
        assert not hasattr(db, "update")
        assert not hasattr(db, "update_trial")

    def test_no_delete_method(self, db: TrialsDB) -> None:
        assert not hasattr(db, "delete")
        assert not hasattr(db, "delete_trial")
        assert not hasattr(db, "remove")

    def test_no_drop_method(self, db: TrialsDB) -> None:
        assert not hasattr(db, "drop")
        assert not hasattr(db, "truncate")

    def test_records_immutable_after_insert(self, db: TrialsDB) -> None:
        rec = _make_record()
        db.insert(rec)
        # La unica forma de "modificar" es insertar otro registro con distinto trial_id
        rec2 = _make_record(
            strategy_id=rec.strategy_id,
            fold_id=rec.fold_id,
            status="FAILED",
        )
        db.insert(rec2)
        # Ambos registros coexisten
        all_rows = db.fetch_all(rec.strategy_id)
        statuses = {r.status for r in all_rows}
        assert "COMPLETED" in statuses
        assert "FAILED" in statuses


class TestFetchCompleted:
    def test_fetch_completed_excludes_running_and_failed(self, db: TrialsDB) -> None:
        db.insert(_make_record(status="COMPLETED"))
        db.insert(_make_record(status="RUNNING", sharpe_oos=None))
        db.insert(_make_record(status="FAILED", sharpe_oos=None))
        completed = db.fetch_completed("momentum")
        assert len(completed) == 1
        assert completed[0].status == "COMPLETED"

    def test_fetch_completed_returns_correct_strategy(self, db: TrialsDB) -> None:
        db.insert(_make_record(strategy_id="A"))
        db.insert(_make_record(strategy_id="B"))
        assert len(db.fetch_completed("A")) == 1
        assert len(db.fetch_completed("B")) == 1


class TestFetchAll:
    def test_fetch_all_no_filter(self, db: TrialsDB) -> None:
        db.insert(_make_record(strategy_id="X"))
        db.insert(_make_record(strategy_id="Y"))
        assert len(db.fetch_all()) == 2

    def test_fetch_all_with_filter(self, db: TrialsDB) -> None:
        db.insert(_make_record(strategy_id="X"))
        db.insert(_make_record(strategy_id="X"))
        db.insert(_make_record(strategy_id="Y"))
        assert len(db.fetch_all("X")) == 2


class TestNullHandling:
    def test_null_sharpe_survives_roundtrip(self, db: TrialsDB) -> None:
        rec = _make_record(status="FAILED", sharpe_oos=None)
        db.insert(rec)
        rows = db.fetch_all(rec.strategy_id)
        assert rows[0].sharpe_oos is None
        assert rows[0].sharpe_is is None or rows[0].sharpe_is == Decimal("1.10")

    def test_error_message_stored(self, db: TrialsDB) -> None:
        rec = TrialRecord(
            trial_id=str(uuid.uuid4()),
            timestamp=datetime(2024, 1, 1, tzinfo=UTC),
            strategy_id="err_strat",
            parameters={},
            fold_id=0,
            train_start=date(2020, 1, 1),
            train_end=date(2022, 12, 31),
            test_start=date(2023, 1, 2),
            test_end=date(2023, 12, 29),
            sharpe_is=None,
            sharpe_oos=None,
            max_drawdown_oos=None,
            num_trades_oos=None,
            status="FAILED",
            error_message="ValueError: datos insuficientes",
        )
        db.insert(rec)
        rows = db.fetch_all("err_strat")
        assert rows[0].error_message == "ValueError: datos insuficientes"


class TestNowUtc:
    def test_now_utc_is_tz_aware(self) -> None:
        ts = now_utc()
        assert ts.tzinfo is not None
        assert ts.tzinfo == UTC
