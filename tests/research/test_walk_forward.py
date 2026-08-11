"""Tests del generador de folds walk-forward."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from qtrader.research.walk_forward import (
    WalkForwardConfig,
    business_days_range,
    generate_folds,
)


def _make_days(n: int, start: date = date(2015, 1, 5)) -> list[date]:
    """Genera n dias habiles consecutivos a partir de start."""
    days: list[date] = []
    current = start
    while len(days) < n:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


class TestWalkForwardConfig:
    def test_defaults(self) -> None:
        cfg = WalkForwardConfig()
        assert cfg.train_days == 756
        assert cfg.test_days == 252
        assert cfg.step_days == 63
        assert cfg.embargo_days == 5

    def test_custom(self) -> None:
        cfg = WalkForwardConfig(train_days=100, test_days=50, step_days=25, embargo_days=2)
        assert cfg.train_days == 100


class TestGenerateFolds:
    def test_returns_empty_when_insufficient_data(self) -> None:
        cfg = WalkForwardConfig(train_days=100, test_days=50, step_days=25, embargo_days=5)
        days = _make_days(149)  # 149 < 100 + 50
        assert generate_folds(days, cfg) == []

    def test_single_fold_exact(self) -> None:
        cfg = WalkForwardConfig(train_days=100, test_days=50, step_days=25, embargo_days=5)
        days = _make_days(150)
        folds = generate_folds(days, cfg)
        assert len(folds) >= 1

    def test_fold_count_formula(self) -> None:
        """Con 756+252+63 dias deberia haber al menos un fold."""
        cfg = WalkForwardConfig(train_days=756, test_days=252, step_days=63, embargo_days=5)
        days = _make_days(756 + 252)
        folds = generate_folds(days, cfg)
        assert len(folds) >= 1

    def test_fold_id_is_sequential(self) -> None:
        cfg = WalkForwardConfig(train_days=100, test_days=50, step_days=25, embargo_days=5)
        days = _make_days(300)
        folds = generate_folds(days, cfg)
        assert [f.fold_id for f in folds] == list(range(len(folds)))

    def test_train_test_no_overlap(self) -> None:
        cfg = WalkForwardConfig(train_days=100, test_days=50, step_days=25, embargo_days=5)
        days = _make_days(300)
        for fold in generate_folds(days, cfg):
            train_set = set(fold.train_days)
            test_set = set(fold.test_days)
            assert train_set.isdisjoint(test_set), f"Fold {fold.fold_id}: overlap train/test"

    def test_embargo_reduces_train_length(self) -> None:
        cfg = WalkForwardConfig(train_days=100, test_days=50, step_days=50, embargo_days=5)
        days = _make_days(300)
        folds = generate_folds(days, cfg)
        assert len(folds) >= 1
        for fold in folds:
            # train_days (purgado) debe tener train_days - embargo_days elementos
            assert len(fold.train_days) == cfg.train_days - cfg.embargo_days

    def test_test_slice_length(self) -> None:
        cfg = WalkForwardConfig(train_days=100, test_days=50, step_days=50, embargo_days=5)
        days = _make_days(300)
        for fold in generate_folds(days, cfg):
            assert len(fold.test_days) == cfg.test_days

    def test_embargo_days_applied_recorded(self) -> None:
        cfg = WalkForwardConfig(train_days=100, test_days=50, step_days=50, embargo_days=7)
        days = _make_days(300)
        for fold in generate_folds(days, cfg):
            assert fold.embargo_days_applied == 7

    def test_folds_are_frozen(self) -> None:
        cfg = WalkForwardConfig(train_days=100, test_days=50, step_days=50, embargo_days=5)
        days = _make_days(200)
        folds = generate_folds(days, cfg)
        assert len(folds) >= 1
        with pytest.raises((AttributeError, TypeError)):
            folds[0].fold_id = 99  # type: ignore[misc]

    def test_consecutive_folds_advance_by_step(self) -> None:
        cfg = WalkForwardConfig(train_days=100, test_days=50, step_days=25, embargo_days=5)
        days = _make_days(400)
        folds = generate_folds(days, cfg)
        assert len(folds) >= 2
        for i in range(len(folds) - 1):
            start_i = days.index(folds[i].train_start)
            start_next = days.index(folds[i + 1].train_start)
            assert start_next - start_i == cfg.step_days

    def test_train_start_end_recorded_correctly(self) -> None:
        cfg = WalkForwardConfig(train_days=100, test_days=50, step_days=50, embargo_days=5)
        days = _make_days(200)
        folds = generate_folds(days, cfg)
        fold = folds[0]
        assert fold.train_start == days[0]
        # train_end es el ultimo dia del train ANTES de purga (indice 99)
        assert fold.train_end == days[cfg.train_days - 1]


class TestBusinessDaysRange:
    def test_excludes_weekends(self) -> None:
        bdays = business_days_range(date(2024, 1, 1), date(2024, 1, 7))
        for d in bdays:
            assert d.weekday() < 5, f"{d} es fin de semana"

    def test_monday_to_friday_returns_5_days(self) -> None:
        bdays = business_days_range(date(2024, 1, 1), date(2024, 1, 5))
        assert len(bdays) == 5

    def test_same_day_returns_one_if_weekday(self) -> None:
        d = date(2024, 1, 2)  # martes
        assert business_days_range(d, d) == [d]

    def test_same_day_returns_empty_if_weekend(self) -> None:
        d = date(2024, 1, 6)  # sabado
        assert business_days_range(d, d) == []
