"""Tests del ExecutionRateLimiter — Capa 2 (T4.3)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path  # noqa: TCH003

import pytest

from qtrader.execution.rate_limiter import ExecutionRateLimiter, RateLimiterConfig

_ZERO = Decimal("0")
_TODAY = datetime(2024, 6, 10, 9, 30, 0, tzinfo=UTC)
_CFG = RateLimiterConfig()


def _limiter(tmp_path: Path, cfg: RateLimiterConfig | None = None) -> ExecutionRateLimiter:
    return ExecutionRateLimiter(tmp_path / "execution.db", cfg or _CFG)


def _check(
    limiter: ExecutionRateLimiter,
    notional: Decimal = Decimal("1000"),
    ts: datetime = _TODAY,
    symbol: str = "SPY",
    order_id: str = "ord-001",
) -> bool:
    result = limiter.check_and_record(order_id, symbol, notional, ts)
    return result.allowed


# ---------------------------------------------------------------------------
# Tests de limite por minuto
# ---------------------------------------------------------------------------

class TestRateLimitMinute:
    def test_allows_up_to_max_per_minute(self, tmp_path: Path) -> None:
        lim = _limiter(tmp_path, RateLimiterConfig(max_orders_per_minute=5))
        for i in range(5):
            assert _check(lim, ts=_TODAY, order_id=f"o-{i}")
        lim.close()

    def test_blocks_on_sixth_in_one_minute(self, tmp_path: Path) -> None:
        lim = _limiter(tmp_path, RateLimiterConfig(max_orders_per_minute=5))
        for i in range(5):
            _check(lim, ts=_TODAY, order_id=f"o-{i}")
        result = lim.check_and_record("o-5", "SPY", Decimal("1000"), _TODAY)
        assert not result.allowed
        assert result.reason is not None
        assert "RATE_LIMIT_MINUTE" in result.reason
        lim.close()

    def test_allows_after_minute_passes(self, tmp_path: Path) -> None:
        lim = _limiter(tmp_path, RateLimiterConfig(max_orders_per_minute=5))
        for i in range(5):
            _check(lim, ts=_TODAY, order_id=f"o-{i}")
        # Un minuto y un segundo despues → ventana limpia
        later = _TODAY + timedelta(minutes=1, seconds=1)
        result = lim.check_and_record("o-later", "SPY", Decimal("1000"), later)
        assert result.allowed
        lim.close()

    def test_blocked_order_not_counted(self, tmp_path: Path) -> None:
        lim = _limiter(tmp_path, RateLimiterConfig(max_orders_per_minute=3))
        for i in range(3):
            _check(lim, ts=_TODAY, order_id=f"o-{i}")
        # Bloquear
        lim.check_and_record("blocked", "SPY", Decimal("1000"), _TODAY)
        assert lim.orders_today(_TODAY.date()) == 3  # la bloqueada no cuenta
        lim.close()


# ---------------------------------------------------------------------------
# Tests de limite diario de ordenes
# ---------------------------------------------------------------------------

class TestRateLimitDailyOrders:
    def test_allows_up_to_max_per_day(self, tmp_path: Path) -> None:
        cfg = RateLimiterConfig(max_orders_per_day=5, max_orders_per_minute=100)
        lim = _limiter(tmp_path, cfg)
        for i in range(5):
            # Espaciar >1 min para no activar rate_limit_minute
            ts = _TODAY + timedelta(minutes=i * 2)
            assert _check(lim, ts=ts, order_id=f"o-{i}")
        lim.close()

    def test_blocks_on_exceeding_daily_orders(self, tmp_path: Path) -> None:
        cfg = RateLimiterConfig(max_orders_per_day=3, max_orders_per_minute=100)
        lim = _limiter(tmp_path, cfg)
        for i in range(3):
            ts = _TODAY + timedelta(minutes=i * 2)
            _check(lim, ts=ts, order_id=f"o-{i}")
        ts_last = _TODAY + timedelta(minutes=6)
        result = lim.check_and_record("o-last", "SPY", Decimal("1000"), ts_last)
        assert not result.allowed
        assert result.reason is not None
        assert "RATE_LIMIT_ORDERS_DAY" in result.reason
        lim.close()

    def test_new_day_resets_daily_counter(self, tmp_path: Path) -> None:
        cfg = RateLimiterConfig(max_orders_per_day=2, max_orders_per_minute=100)
        lim = _limiter(tmp_path, cfg)
        for i in range(2):
            _check(lim, ts=_TODAY + timedelta(minutes=i * 2), order_id=f"d1-{i}")
        # Al dia siguiente
        tomorrow = _TODAY + timedelta(days=1)
        result = lim.check_and_record("d2-0", "SPY", Decimal("1000"), tomorrow)
        assert result.allowed
        lim.close()


# ---------------------------------------------------------------------------
# Tests de limite diario de notional
# ---------------------------------------------------------------------------

class TestRateLimitDailyNotional:
    def test_blocks_when_notional_exceeded(self, tmp_path: Path) -> None:
        cfg = RateLimiterConfig(
            max_notional_per_day=Decimal("5000"),
            max_orders_per_minute=100,
            max_orders_per_day=100,
        )
        lim = _limiter(tmp_path, cfg)
        _check(lim, notional=Decimal("3000"), order_id="o-1")
        result = lim.check_and_record(
            "o-2", "SPY", Decimal("3000"),
            _TODAY + timedelta(minutes=2),
        )
        assert not result.allowed
        assert result.reason is not None
        assert "RATE_LIMIT_NOTIONAL_DAY" in result.reason
        lim.close()

    def test_allows_partial_notional(self, tmp_path: Path) -> None:
        cfg = RateLimiterConfig(
            max_notional_per_day=Decimal("5000"),
            max_orders_per_minute=100,
            max_orders_per_day=100,
        )
        lim = _limiter(tmp_path, cfg)
        _check(lim, notional=Decimal("2000"), order_id="o-1")
        result = lim.check_and_record(
            "o-2", "SPY", Decimal("2000"),
            _TODAY + timedelta(minutes=2),
        )
        assert result.allowed  # 2000+2000=4000 < 5000
        lim.close()

    def test_notional_accumulates_across_orders(self, tmp_path: Path) -> None:
        cfg = RateLimiterConfig(
            max_notional_per_day=Decimal("3000"),
            max_orders_per_minute=100,
            max_orders_per_day=100,
        )
        lim = _limiter(tmp_path, cfg)
        _check(lim, notional=Decimal("1000"), order_id="o-1")
        _check(lim, notional=Decimal("1000"), ts=_TODAY + timedelta(minutes=2), order_id="o-2")
        assert lim.notional_today(_TODAY.date()) == Decimal("2000")
        lim.close()


# ---------------------------------------------------------------------------
# Tests de persistencia (reconstruct state after restart)
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_state_survives_restart(self, tmp_path: Path) -> None:
        db = tmp_path / "execution.db"
        cfg = RateLimiterConfig(
            max_notional_per_day=Decimal("5000"),
            max_orders_per_minute=100,
            max_orders_per_day=100,
        )
        # Primera instancia
        lim1 = ExecutionRateLimiter(db, cfg)
        lim1.check_and_record("o-1", "SPY", Decimal("4000"), _TODAY)
        lim1.close()

        # Segunda instancia (simula reinicio)
        lim2 = ExecutionRateLimiter(db, cfg)
        result = lim2.check_and_record(
            "o-2", "SPY", Decimal("2000"), _TODAY + timedelta(minutes=2),
        )
        assert not result.allowed  # 4000+2000=6000 > 5000
        assert lim2.notional_today(_TODAY.date()) == Decimal("4000")
        lim2.close()

    def test_orders_today_query(self, tmp_path: Path) -> None:
        lim = _limiter(tmp_path)
        for i in range(3):
            lim.check_and_record(
                f"o-{i}", "SPY", Decimal("100"),
                _TODAY + timedelta(minutes=i * 2),
            )
        assert lim.orders_today(_TODAY.date()) == 3
        lim.close()

    def test_orders_last_minute_query(self, tmp_path: Path) -> None:
        lim = _limiter(tmp_path)
        lim.check_and_record("o-1", "SPY", Decimal("100"), _TODAY)
        lim.check_and_record("o-2", "SPY", Decimal("100"), _TODAY + timedelta(seconds=30))
        assert lim.orders_last_minute(_TODAY + timedelta(seconds=30)) == 2
        lim.close()


# ---------------------------------------------------------------------------
# Tests de config independiente del Risk Engine
# ---------------------------------------------------------------------------

class TestConfigIndependence:
    def test_default_limits_differ_from_risk_engine(self) -> None:
        cfg = RateLimiterConfig()
        # Diferente del Risk Engine (20 ordenes, 15000 EUR)
        assert cfg.max_orders_per_day == 30
        assert cfg.max_notional_per_day == Decimal("20000")
        assert cfg.max_orders_per_minute == 5

    def test_result_is_frozen(self, tmp_path: Path) -> None:
        from pydantic import ValidationError
        lim = _limiter(tmp_path)
        result = lim.check_and_record("o-1", "SPY", Decimal("1000"), _TODAY)
        with pytest.raises((ValidationError, AttributeError)):
            result.allowed = False  # type: ignore[misc]
        lim.close()
