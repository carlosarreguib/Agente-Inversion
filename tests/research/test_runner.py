"""Tests del WalkForwardRunner y WalkForwardReport."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from qtrader.research.dsr import compute_dsr
from qtrader.research.runner import FoldResult, WalkForwardReport
from qtrader.research.walk_forward import Fold


def _make_fold(fold_id: int = 0) -> Fold:
    start = date(2020, 1, 2)
    train_days = tuple(
        d for i in range(100)
        if (d := start + timedelta(days=i)).weekday() < 5
    )
    test_start = train_days[-1] + timedelta(days=3)
    test_days = tuple(
        d for i in range(50)
        if (d := test_start + timedelta(days=i)).weekday() < 5
    )
    return Fold(
        fold_id=fold_id,
        train_days=train_days,
        test_days=test_days,
        train_start=train_days[0],
        train_end=train_days[-1],
        test_start=test_days[0],
        test_end=test_days[-1],
        embargo_days_applied=5,
    )


def _make_fold_result(
    fold_id: int = 0,
    sharpe_oos: str = "0.85",
    status: str = "COMPLETED",
) -> FoldResult:
    from qtrader.backtesting.metrics import GoldenMetrics

    metrics = GoldenMetrics(
        total_return=Decimal("0.05"),
        sharpe_ratio=Decimal(sharpe_oos),
        max_drawdown=Decimal("-0.10"),
        num_trades=20,
        total_commission=Decimal("15.00"),
        total_slippage=Decimal("0.00"),
        final_equity=Decimal("15750.00"),
    )
    return FoldResult(
        fold=_make_fold(fold_id),
        trial_id=f"tid-{fold_id}",
        metrics_is=metrics,
        metrics_oos=metrics if status == "COMPLETED" else None,
        status=status,
    )


class TestWalkForwardReport:
    def _make_report(
        self, sharpes: list[str], statuses: list[str] | None = None
    ) -> WalkForwardReport:
        n = len(sharpes)
        statuses = statuses or ["COMPLETED"] * n
        fold_results = tuple(
            _make_fold_result(fold_id=i, sharpe_oos=sharpes[i], status=statuses[i])
            for i in range(n)
        )
        sharpes_dec = [
            Decimal(s) for s, st in zip(sharpes, statuses, strict=True) if st == "COMPLETED"
        ]
        dsr = compute_dsr(sharpes_dec, t_obs=50)
        return WalkForwardReport(
            strategy_id="test",
            fold_results=fold_results,
            dsr_result=dsr,
            n_trials_total=n,
        )

    def test_completed_folds_count(self) -> None:
        report = self._make_report(["0.8", "0.6"], ["COMPLETED", "COMPLETED"])
        assert len(report.completed_folds) == 2

    def test_failed_folds_excluded_from_completed(self) -> None:
        report = self._make_report(["0.8", "0.6"], ["COMPLETED", "FAILED"])
        assert len(report.completed_folds) == 1

    def test_sharpes_oos_extracts_from_completed(self) -> None:
        report = self._make_report(["0.8", "1.2"])
        sharpes = report.sharpes_oos
        assert len(sharpes) == 2
        assert Decimal("1.2") in sharpes

    def test_best_fold_has_max_sharpe(self) -> None:
        report = self._make_report(["0.8", "1.2", "0.5"])
        best = report.best_fold
        assert best is not None
        assert best.metrics_oos is not None
        assert best.metrics_oos.sharpe_ratio == Decimal("1.2")

    def test_worst_fold_has_min_sharpe(self) -> None:
        report = self._make_report(["0.8", "1.2", "0.5"])
        worst = report.worst_fold
        assert worst is not None
        assert worst.metrics_oos is not None
        assert worst.metrics_oos.sharpe_ratio == Decimal("0.5")

    def test_best_worst_none_when_no_completed(self) -> None:
        report = self._make_report(["0.8"], ["FAILED"])
        assert report.best_fold is None
        assert report.worst_fold is None

    def test_sharpe_oos_std_zero_for_single_fold(self) -> None:
        report = self._make_report(["1.0"])
        assert report.sharpe_oos_std == Decimal("0")

    def test_sharpe_oos_std_positive_for_multiple(self) -> None:
        report = self._make_report(["0.5", "1.5"])
        assert report.sharpe_oos_std > Decimal("0")

    def test_report_is_frozen(self) -> None:
        report = self._make_report(["1.0"])
        with pytest.raises((AttributeError, TypeError)):
            report.strategy_id = "other"  # type: ignore[misc]


class TestFoldResult:
    def test_fold_result_is_frozen(self) -> None:
        fr = _make_fold_result()
        with pytest.raises((AttributeError, TypeError)):
            fr.status = "FAILED"  # type: ignore[misc]
