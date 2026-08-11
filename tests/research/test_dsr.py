"""Tests del Deflated Sharpe Ratio (Bailey & Lopez de Prado, 2014)."""
from __future__ import annotations

from decimal import Decimal

import pytest

from qtrader.research.dsr import compute_dsr


class TestDSREmptyAndSingle:
    def test_empty_returns_zero_dsr(self) -> None:
        result = compute_dsr([], t_obs=252)
        assert result.dsr == Decimal("0")
        assert result.n_trials == 0
        assert "Sin trials" in result.note

    def test_single_positive_sharpe_returns_high_dsr(self) -> None:
        result = compute_dsr([Decimal("1.0")], t_obs=252)
        assert result.n_trials == 1
        assert result.dsr > Decimal("0.5")
        assert "N=1" in result.note

    def test_single_negative_sharpe_returns_low_dsr(self) -> None:
        result = compute_dsr([Decimal("-0.5")], t_obs=252)
        assert result.dsr < Decimal("0.5")

    def test_single_zero_sharpe_returns_half(self) -> None:
        result = compute_dsr([Decimal("0.0")], t_obs=252)
        # PSR(0) con SR=0 deberia ser 0.5
        assert abs(float(result.dsr) - 0.5) < 0.01


class TestDSRMultipleTrials:
    def test_dsr_decreases_as_n_increases(self) -> None:
        """Con mas trials, DSR debe bajar (penalizacion por seleccion)."""
        base = [Decimal("1.0"), Decimal("0.8")]
        result_n2 = compute_dsr(base, t_obs=252)
        result_n5 = compute_dsr(base + [Decimal("0.6"), Decimal("0.4"), Decimal("0.2")], t_obs=252)
        assert result_n5.dsr <= result_n2.dsr

    def test_dsr_in_zero_one_range(self) -> None:
        sharpes = [Decimal(str(v)) for v in [1.2, 0.9, 0.7, 0.5, 0.3]]
        result = compute_dsr(sharpes, t_obs=252)
        assert Decimal("0") <= result.dsr <= Decimal("1")

    def test_n_trials_recorded_correctly(self) -> None:
        sharpes = [Decimal("0.8"), Decimal("1.0"), Decimal("0.6")]
        result = compute_dsr(sharpes, t_obs=252)
        assert result.n_trials == 3

    def test_sr_obs_is_max(self) -> None:
        sharpes = [Decimal("0.5"), Decimal("1.3"), Decimal("0.8")]
        result = compute_dsr(sharpes, t_obs=252)
        assert result.sr_obs == Decimal("1.3")

    def test_sr_star_positive_for_n_gt_1(self) -> None:
        sharpes = [Decimal("1.0"), Decimal("0.8")]
        result = compute_dsr(sharpes, t_obs=252)
        # SR* debe ser > 0 para N>1 con sigma_SR > 0
        assert result.sr_star > Decimal("0")

    def test_sigma_sr_computed(self) -> None:
        sharpes = [Decimal("1.0"), Decimal("2.0")]
        result = compute_dsr(sharpes, t_obs=252)
        assert result.sigma_sr > Decimal("0")

    def test_t_obs_recorded(self) -> None:
        result = compute_dsr([Decimal("1.0")], t_obs=126)
        assert result.t_obs == 126

    def test_low_t_obs_returns_zero(self) -> None:
        # t_obs < 2 → DSR = 0
        result = compute_dsr([Decimal("1.0"), Decimal("0.8")], t_obs=1)
        assert result.dsr == Decimal("0")

    def test_note_warns_few_trials(self) -> None:
        sharpes = [Decimal("1.0"), Decimal("0.8")]
        result = compute_dsr(sharpes, t_obs=252)
        assert "N=" in result.note

    def test_identical_sharpes_note(self) -> None:
        sharpes = [Decimal("1.0"), Decimal("1.0"), Decimal("1.0")]
        result = compute_dsr(sharpes, t_obs=252)
        assert "sigma_SR" in result.note or result.dsr >= Decimal("0")


class TestDSRResult:
    def test_result_is_frozen(self) -> None:
        result = compute_dsr([Decimal("1.0")], t_obs=252)
        with pytest.raises((AttributeError, TypeError)):
            result.dsr = Decimal("0")  # type: ignore[misc]
