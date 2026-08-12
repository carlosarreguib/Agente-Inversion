"""Tests de la estrategia momentum cross-sectional 12-1 (T3.1)."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from qtrader.strategies.momentum import (
    CrossSectionalMomentumStrategy,
    MomentumEqualWeightPortfolio,
    _is_rebalance_day,
    compute_momentum_score,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_closes(n: int, start_price: float = 100.0, drift: float = 0.0) -> list[Decimal]:
    """Genera n precios de cierre con drift diario constante."""
    closes = []
    price = Decimal(str(start_price))
    factor = Decimal(str(1.0 + drift))
    for _ in range(n):
        closes.append(price)
        price = price * factor
    return closes


def _make_trading_days(n: int, start: date | None = None) -> list[date]:
    """Genera n dias habiles (lun-vie) desde start."""
    start = start or date(2010, 1, 4)
    days: list[date] = []
    cur = start
    while len(days) < n:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)
    return days


# ---------------------------------------------------------------------------
# compute_momentum_score — funcion pura
# ---------------------------------------------------------------------------

class TestComputeMomentumScore:
    def test_insufficient_history_returns_none(self) -> None:
        closes = _make_closes(252)  # necesitamos 253
        assert compute_momentum_score(closes) is None

    def test_exactly_minimum_history(self) -> None:
        closes = _make_closes(253, drift=0.001)
        score = compute_momentum_score(closes)
        assert score is not None

    def test_positive_drift_gives_positive_score(self) -> None:
        closes = _make_closes(300, drift=0.001)
        score = compute_momentum_score(closes)
        assert score is not None
        assert score > Decimal("0")

    def test_negative_drift_gives_negative_score(self) -> None:
        closes = _make_closes(300, drift=-0.001)
        score = compute_momentum_score(closes)
        assert score is not None
        assert score < Decimal("0")

    def test_flat_prices_give_zero_score(self) -> None:
        closes = _make_closes(300, drift=0.0)  # todos iguales
        score = compute_momentum_score(closes)
        assert score is not None
        assert score == Decimal("0")

    def test_manual_calculation_matches_module(self) -> None:
        """Comprueba la formula exacta para 3 instrumentos (assertEqual, no approx)."""
        # Precios constantes salvo el dia t-252 y t-21
        # Construimos closes de longitud 300 con precios fijos
        closes = [Decimal("100")] * 300
        closes[-253] = Decimal("80")   # close(T-252) = denominador
        closes[-22] = Decimal("120")   # close(T-21)  = numerador

        expected = Decimal("120") / Decimal("80") - Decimal("1")
        # expected = Decimal("0.5")
        result = compute_momentum_score(closes)
        assert result == expected  # assertEqual exacto

    def test_manual_calculation_instrument_b(self) -> None:
        closes = [Decimal("200")] * 300
        closes[-253] = Decimal("150")
        closes[-22] = Decimal("210")
        expected = Decimal("210") / Decimal("150") - Decimal("1")
        result = compute_momentum_score(closes)
        assert result == expected

    def test_manual_calculation_instrument_c(self) -> None:
        closes = [Decimal("50")] * 300
        closes[-253] = Decimal("60")
        closes[-22] = Decimal("45")
        expected = Decimal("45") / Decimal("60") - Decimal("1")
        result = compute_momentum_score(closes)
        assert result == expected

    def test_zero_old_price_returns_none(self) -> None:
        closes = [Decimal("100")] * 300
        closes[-253] = Decimal("0")
        assert compute_momentum_score(closes) is None

    def test_zero_recent_price_returns_none(self) -> None:
        closes = [Decimal("100")] * 300
        closes[-22] = Decimal("0")
        assert compute_momentum_score(closes) is None


# ---------------------------------------------------------------------------
# _is_rebalance_day
# ---------------------------------------------------------------------------

class TestIsRebalanceDay:
    def test_friday_is_rebalance_day(self) -> None:
        # 2010-01-08 es viernes
        friday = date(2010, 1, 8)
        assert friday.weekday() == 4
        trading_days = _make_trading_days(50, start=date(2010, 1, 4))
        assert _is_rebalance_day(friday, trading_days) is True

    def test_monday_is_not_rebalance_day(self) -> None:
        # 2010-01-04 es lunes
        monday = date(2010, 1, 4)
        assert monday.weekday() == 0
        trading_days = _make_trading_days(50, start=monday)
        assert _is_rebalance_day(monday, trading_days) is False

    def test_thursday_before_friday_holiday_is_rebalance(self) -> None:
        """Jueves es el ultimo habiles antes del fin de semana si el viernes es festivo."""
        # Construimos una lista donde el dia siguiente al jueves es el lunes (gap de 3 dias)
        thursday = date(2010, 1, 7)  # jueves
        assert thursday.weekday() == 3
        # Lista sin el viernes 2010-01-08 (simulamos festivo)
        days = [
            date(2010, 1, 4),  # lunes
            date(2010, 1, 5),  # martes
            date(2010, 1, 6),  # miercoles
            date(2010, 1, 7),  # jueves
            # 2010-01-08 viernes — omitido (festivo)
            date(2010, 1, 11), # lunes siguiente — gap de 4 dias
        ]
        assert _is_rebalance_day(thursday, days) is True

    def test_normal_thursday_is_not_rebalance(self) -> None:
        thursday = date(2010, 1, 7)
        assert thursday.weekday() == 3
        # El dia siguiente es viernes (gap de 1 dia) — no es rebalanceo
        days = [
            date(2010, 1, 7),
            date(2010, 1, 8),  # viernes
        ]
        assert _is_rebalance_day(thursday, days) is False

    def test_last_trading_day_triggers_rebalance(self) -> None:
        days = [date(2010, 1, 4), date(2010, 1, 5)]
        assert _is_rebalance_day(date(2010, 1, 5), days) is True


# ---------------------------------------------------------------------------
# CrossSectionalMomentumStrategy — logica de señales
# ---------------------------------------------------------------------------

class _StubData:
    """DataView stub que devuelve barras sinteticas fijas."""

    def __init__(self, closes_by_symbol: dict[str, list[Decimal]]) -> None:
        from datetime import UTC, datetime
        self._closes = closes_by_symbol
        self._ts = datetime(2010, 1, 1, tzinfo=UTC)

    def get_bars(self, symbol: str, start: date, end: date) -> list[object]:
        from datetime import UTC, datetime

        from qtrader.core.types import Bar, DataQuality, ValidatedBar

        closes = self._closes.get(symbol, [])
        bars = []
        for i, c in enumerate(closes):
            ts = datetime(2010, 1, 1, tzinfo=UTC) + timedelta(days=i)
            bar = Bar(
                symbol=symbol,
                timestamp=ts,
                open=c, high=c, low=c, close=c, volume=Decimal("1000000"),
            )
            bars.append(ValidatedBar(bar=bar, quality=DataQuality.OK))
        return bars  # type: ignore[return-value]


class _StubInstrument:
    def __init__(self, symbol: str) -> None:
        self.ticker_proxy = symbol
        self.symbol = symbol


class TestCrossSectionalMomentumStrategy:
    def _make_strategy(self, n_days: int = 300) -> CrossSectionalMomentumStrategy:
        trading_days = _make_trading_days(n_days)
        return CrossSectionalMomentumStrategy(trading_days=trading_days)

    def test_constant_scores_equal_weight_signals(self) -> None:
        """Con todos los scores iguales todos van al quintil superior (comportamiento definido)."""
        # Con scores identicos el ranking pone a TODOS en el top 20% si N es pequeno
        closes = [Decimal("100")] * 300  # drift 0 → score 0 para todos
        closes[-253] = Decimal("90")
        closes[-22] = Decimal("110")
        # mismos cierres para todos los simbolos → score identico → top quintil
        n_symbols = 5
        closes_by_sym = {f"A{i}": list(closes) for i in range(n_symbols)}
        data = _StubData(closes_by_sym)

        trading_days = _make_trading_days(300)
        strat = CrossSectionalMomentumStrategy(trading_days=trading_days)
        friday = next(d for d in trading_days if d.weekday() == 4)

        instruments = [_StubInstrument(f"A{i}") for i in range(n_symbols)]
        signals = strat.on_bar(friday, instruments, data)  # type: ignore[arg-type]

        from qtrader.core.types import Direction
        long_signals = [s for s in signals if s.direction == Direction.LONG]
        # Con 5 instrumentos y top 20% → ceil(5 * 0.20) = 1 instrumento
        assert len(long_signals) == 1

    def test_one_dominant_instrument_always_top(self) -> None:
        """Con un instrumento dominando el ranking, siempre sale en LONG."""
        base = [Decimal("100")] * 300

        # Instrumento A: mayor retorno 12-1
        closes_a = list(base)
        closes_a[-253] = Decimal("50")
        closes_a[-22] = Decimal("200")  # score = 3.0

        # Instrumentos B, C, D: retornos neutros
        closes_others = list(base)

        closes_by_sym = {"A": closes_a, "B": closes_others, "C": closes_others, "D": closes_others}
        data = _StubData(closes_by_sym)

        trading_days = _make_trading_days(300)
        strat = CrossSectionalMomentumStrategy(trading_days=trading_days)
        friday = next(d for d in trading_days if d.weekday() == 4)

        instruments = [_StubInstrument(sym) for sym in ["A", "B", "C", "D"]]
        signals = strat.on_bar(friday, instruments, data)  # type: ignore[arg-type]

        from qtrader.core.types import Direction
        a_signal = next((s for s in signals if s.symbol == "A"), None)
        assert a_signal is not None
        assert a_signal.direction == Direction.LONG

    def test_instrument_without_history_excluded_silently(self) -> None:
        """Instrumento sin 253 barras se excluye sin excepcion."""
        closes_short = [Decimal("100")] * 100  # insuficiente

        closes_long = [Decimal("100")] * 300
        closes_long[-253] = Decimal("80")
        closes_long[-22] = Decimal("120")

        data = _StubData({"SHORT": closes_short, "LONG_OK": closes_long})

        trading_days = _make_trading_days(300)
        strat = CrossSectionalMomentumStrategy(trading_days=trading_days)
        friday = next(d for d in trading_days if d.weekday() == 4)
        instruments = [_StubInstrument("SHORT"), _StubInstrument("LONG_OK")]

        signals = strat.on_bar(friday, instruments, data)  # type: ignore[arg-type]

        symbols_with_signal = {s.symbol for s in signals}
        assert "SHORT" not in symbols_with_signal
        assert "LONG_OK" in symbols_with_signal

    def test_non_rebalance_day_returns_last_signals(self) -> None:
        """En dias que no son viernes se devuelven las señales del ultimo rebalanceo."""
        trading_days = _make_trading_days(300)
        strat = CrossSectionalMomentumStrategy(trading_days=trading_days)

        friday = next(d for d in trading_days if d.weekday() == 4)
        monday = next(d for d in trading_days if d > friday and d.weekday() == 0)

        closes = [Decimal("100")] * 300
        closes[-253] = Decimal("70")
        closes[-22] = Decimal("130")
        data = _StubData({"X": closes})
        instruments = [_StubInstrument("X")]

        signals_friday = strat.on_bar(friday, instruments, data)  # type: ignore[arg-type]
        signals_monday = strat.on_bar(monday, instruments, data)  # type: ignore[arg-type]

        # Las señales del lunes son identicas a las del viernes
        assert len(signals_monday) == len(signals_friday)
        if signals_friday and signals_monday:
            assert signals_monday[0].direction == signals_friday[0].direction

    def test_rebalance_on_friday_holiday_moves_to_thursday(self) -> None:
        """El jueves anterior a un viernes festivo actua como dia de rebalanceo."""
        # Construimos trading_days con el viernes omitido
        all_days = _make_trading_days(300)
        friday = next(d for d in all_days if d.weekday() == 4)
        thursday = next(d for d in all_days if d < friday and d.weekday() == 3)

        # Lista sin el viernes
        truncated = [d for d in all_days if d != friday]
        strat = CrossSectionalMomentumStrategy(trading_days=truncated)

        closes = [Decimal("100")] * 300
        closes[-253] = Decimal("70")
        closes[-22] = Decimal("130")
        data = _StubData({"Y": closes})
        instruments = [_StubInstrument("Y")]

        # El jueves debe ser reconocido como dia de rebalanceo
        assert _is_rebalance_day(thursday, truncated) is True
        signals = strat.on_bar(thursday, instruments, data)  # type: ignore[arg-type]
        assert len(signals) > 0


# ---------------------------------------------------------------------------
# MomentumEqualWeightPortfolio
# ---------------------------------------------------------------------------

class TestMomentumEqualWeightPortfolio:
    def test_no_long_signals_returns_empty(self) -> None:
        portfolio = MomentumEqualWeightPortfolio(initial_equity=Decimal("15000"))
        signals = []
        targets = portfolio.build(signals, {}, [], Decimal("15000"))
        assert targets == []

    def test_equal_weight_distributes_evenly(self) -> None:
        from datetime import UTC, datetime

        from qtrader.core.types import Direction
        portfolio = MomentumEqualWeightPortfolio(initial_equity=Decimal("15000"))

        # Actualizar cierres
        portfolio.update_close("A", Decimal("100"))
        portfolio.update_close("B", Decimal("100"))

        ts = datetime(2010, 1, 8, tzinfo=UTC)
        from qtrader.core.types import Signal
        signals = [
            Signal(symbol="A", timestamp=ts, direction=Direction.LONG,
                   strength=Decimal("1"), strategy_id="test"),
            Signal(symbol="B", timestamp=ts, direction=Direction.LONG,
                   strength=Decimal("1"), strategy_id="test"),
        ]
        targets = portfolio.build(signals, {}, [], Decimal("15000"))
        assert len(targets) == 2
        qtys = {t.symbol: t.quantity for t in targets}
        # 15000 / 2 = 7500 por instrumento; 7500 / 100 = 75 acciones cada uno
        assert qtys["A"] == Decimal("75")
        assert qtys["B"] == Decimal("75")

    def test_below_min_order_excluded(self) -> None:
        from datetime import UTC, datetime

        from qtrader.core.types import Direction
        # Equity muy pequeña → notional < min_order_eur → excluido
        portfolio = MomentumEqualWeightPortfolio(
            initial_equity=Decimal("100"), min_order_eur=Decimal("1000")
        )
        portfolio.update_close("X", Decimal("100"))
        ts = datetime(2010, 1, 8, tzinfo=UTC)
        from qtrader.core.types import Signal
        signals = [
            Signal(symbol="X", timestamp=ts, direction=Direction.LONG,
                   strength=Decimal("1"), strategy_id="test"),
        ]
        targets = portfolio.build(signals, {}, [], Decimal("100"))
        assert targets == []
