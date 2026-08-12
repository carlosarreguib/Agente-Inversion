"""Tests del BrokerSanityChecker — Capa 3 (T4.3)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from qtrader.execution.sanity import (
    BrokerSanityChecker,
    SanityConfig,
    SanityMarketState,
    SanityResult,
)

_ZERO = Decimal("0")

# Universo minimo para tests
_ACTIVE = frozenset({"SPY", "QQQ", "XLK"})
_CLOSES = {"SPY": Decimal("400"), "QQQ": Decimal("300"), "XLK": Decimal("150")}
_ADV = {"SPY": Decimal("100000"), "QQQ": Decimal("50000"), "XLK": Decimal("20000")}
# Dia de sesion NYSE (lunes no festivo)
_SESSION_DATE = date(2024, 1, 16)   # martes — sesion NYSE normal
_NON_SESSION = date(2024, 1, 13)    # sabado — no es sesion


def _state(
    last_close: dict[str, Decimal] | None = None,
    adv: dict[str, Decimal] | None = None,
    active: frozenset[str] | None = None,
    trading_date: date = _SESSION_DATE,
    exchange: str = "XNYS",
) -> SanityMarketState:
    return SanityMarketState(
        last_close=last_close if last_close is not None else _CLOSES,
        adv=adv if adv is not None else _ADV,
        active_symbols=active if active is not None else _ACTIVE,
        trading_date=trading_date,
        exchange=exchange,
    )


def _check(
    symbol: str = "SPY",
    side: str = "BUY",
    quantity: Decimal = Decimal("5"),
    price: Decimal = Decimal("400"),
    notional: Decimal = Decimal("2000"),
    market_state: SanityMarketState | None = None,
    config: SanityConfig | None = None,
) -> SanityResult:
    checker = BrokerSanityChecker(config)
    return checker.check(
        order_id="test-ord",
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        notional=notional,
        market_state=market_state or _state(),
    )


# ---------------------------------------------------------------------------
# a. Validacion de simbolo
# ---------------------------------------------------------------------------

class TestSymbolValidation:
    def test_known_symbol_passes(self) -> None:
        result = _check(symbol="SPY", price=Decimal("400"), notional=Decimal("2000"))
        assert result.passed

    def test_unknown_symbol_fails(self) -> None:
        result = _check(symbol="AAAA", price=Decimal("100"), notional=Decimal("500"))
        assert not result.passed
        assert result.reason is not None
        assert "UNKNOWN_SYMBOL" in result.reason

    def test_empty_symbol_fails(self) -> None:
        result = _check(symbol="", price=Decimal("100"), notional=Decimal("500"))
        assert not result.passed

    def test_case_sensitive(self) -> None:
        result = _check(symbol="spy")  # minusculas no estan en el universo
        assert not result.passed


# ---------------------------------------------------------------------------
# b. Validacion de precio
# ---------------------------------------------------------------------------

class TestPriceValidation:
    def test_price_within_tolerance_passes(self) -> None:
        # SPY close=400; precio=440 → +10% exacto → pasa (<=)
        result = _check(symbol="SPY", price=Decimal("440"), notional=Decimal("2200"))
        assert result.passed

    def test_price_just_over_tolerance_fails(self) -> None:
        # SPY close=400; precio=441 → +10.25% > 10%
        result = _check(symbol="SPY", price=Decimal("441"), notional=Decimal("2205"))
        assert not result.passed
        assert result.reason is not None
        assert "PRICE_DEVIATION" in result.reason

    def test_price_below_tolerance_passes(self) -> None:
        result = _check(symbol="SPY", price=Decimal("360"), notional=Decimal("1800"))
        assert result.passed

    def test_price_below_tolerance_fails(self) -> None:
        # SPY close=400; precio=359 → -10.25% > 10%
        result = _check(symbol="SPY", price=Decimal("359"), notional=Decimal("1795"))
        assert not result.passed
        assert result.reason is not None
        assert "PRICE_DEVIATION" in result.reason

    def test_no_close_fails(self) -> None:
        state = _state(last_close={})  # sin close para SPY
        result = _check(symbol="SPY", price=Decimal("400"), notional=Decimal("2000"),
                        market_state=state)
        assert not result.passed
        assert result.reason is not None
        assert "NO_CLOSE_PRICE" in result.reason

    def test_custom_tolerance(self) -> None:
        cfg = SanityConfig(max_price_deviation=Decimal("0.05"))  # ±5%
        # SPY close=400; precio=421 → +5.25% > 5%
        result = _check(symbol="SPY", price=Decimal("421"), notional=Decimal("2105"), config=cfg)
        assert not result.passed


# ---------------------------------------------------------------------------
# c. Validacion de ADV
# ---------------------------------------------------------------------------

class TestADVValidation:
    def test_within_adv_passes(self) -> None:
        # SPY ADV=100000; max=10% → 10000 EUR; orden=2000 → pasa
        result = _check(symbol="SPY", price=Decimal("400"), notional=Decimal("2000"))
        assert result.passed

    def test_exceeds_adv_fails(self) -> None:
        # SPY ADV=100000; max=10% → 10000 EUR; orden=15000 → falla
        result = _check(
            symbol="SPY", price=Decimal("400"),
            quantity=Decimal("38"), notional=Decimal("15200"),
        )
        assert not result.passed
        assert result.reason is not None
        assert "ADV_EXCEEDED" in result.reason

    def test_no_adv_uses_fallback(self) -> None:
        # Sin ADV → fallback=2000 EUR; orden=3000 → falla
        state = _state(adv={})
        result = _check(
            symbol="SPY", price=Decimal("400"),
            quantity=Decimal("8"), notional=Decimal("3200"),
            market_state=state,
        )
        assert not result.passed
        assert result.reason is not None
        assert "ADV_EXCEEDED" in result.reason

    def test_no_adv_fallback_allows_small_order(self) -> None:
        state = _state(adv={})
        result = _check(
            symbol="SPY", price=Decimal("400"),
            quantity=Decimal("4"), notional=Decimal("1600"),
            market_state=state,
        )
        assert result.passed  # 1600 < 2000 fallback

    def test_sell_bypasses_adv(self) -> None:
        # SELL de cantidad grande no debe bloquearse por ADV
        state = _state(adv={})
        result = _check(
            symbol="SPY", side="SELL",
            price=Decimal("400"),
            quantity=Decimal("100"), notional=Decimal("40000"),
            market_state=state,
        )
        assert result.passed


# ---------------------------------------------------------------------------
# d. Validacion de mercado abierto
# ---------------------------------------------------------------------------

class TestMarketOpen:
    def test_session_date_passes(self) -> None:
        state = _state(trading_date=_SESSION_DATE)
        result = _check(market_state=state)
        assert result.passed

    def test_non_session_fails(self) -> None:
        state = _state(trading_date=_NON_SESSION)
        result = _check(market_state=state)
        assert not result.passed
        assert result.reason is not None
        assert "MARKET_CLOSED" in result.reason

    def test_unknown_exchange_fails(self) -> None:
        state = _state(exchange="XXXX_INVALID")
        result = _check(market_state=state)
        assert not result.passed
        assert result.reason is not None
        assert "CALENDAR_ERROR" in result.reason


# ---------------------------------------------------------------------------
# Orden valida completa — golden path
# ---------------------------------------------------------------------------

class TestGoldenPath:
    def test_valid_order_passes_all_checks(self) -> None:
        result = _check(
            symbol="SPY",
            side="BUY",
            quantity=Decimal("5"),
            price=Decimal("402"),    # +0.5% — dentro del ±10%
            notional=Decimal("2010"),  # < 10% ADV de SPY
            market_state=_state(trading_date=_SESSION_DATE),
        )
        assert result.passed
        assert result.reason is None

    def test_result_frozen(self) -> None:
        result = _check()
        with pytest.raises((ValidationError, AttributeError)):
            result.passed = False  # type: ignore[misc]
