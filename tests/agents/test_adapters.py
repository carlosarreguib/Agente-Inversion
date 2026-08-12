"""Tests de los adaptadores puros (T6).

Incluye el test de mayor valor de la tarea: el guard de rebalance_needed.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from qtrader.agents.adapters import (
    build_current_portfolio,
    build_proposed_orders,
    build_sanity_market_states,
    compute_data_hash,
    strmap,
)
from qtrader.core.types import (
    Bar,
    DataQuality,
    Instrument,
    InstrumentCategory,
    Side,
    TargetPosition,
    ValidatedBar,
)
from qtrader.portfolio.construction import PortfolioTarget

_TS = datetime(2026, 6, 15, tzinfo=UTC)
_TRADING_DATE = date(2026, 6, 15)


def _instrument(symbol: str, exchange: str = "XNYS") -> Instrument:
    return Instrument(
        symbol=symbol,
        name=f"{symbol} ETF",
        exchange=exchange,
        currency="EUR",
        category=InstrumentCategory.REGION,
        ticker_proxy=symbol,
        ticker_ucits=f"{symbol}.L",
        declared_on=date(2020, 1, 1),
    )


def _target(
    positions: dict[str, str], *, rebalance: bool = True
) -> PortfolioTarget:
    return PortfolioTarget(
        targets=tuple(
            TargetPosition(
                symbol=sym, quantity=Decimal(qty), weight=Decimal("0.1")
            )
            for sym, qty in positions.items()
        ),
        total_weight=Decimal("0.5"),
        rebalance_needed=rebalance,
        constraints_applied=(),
        timestamp=_TS,
    )


_INSTRUMENTS = {s: _instrument(s) for s in ("SPY", "QQQ", "IWM")}
_PRICES = {"SPY": Decimal("100"), "QQQ": Decimal("200"), "IWM": Decimal("50")}


class TestRebalanceGuard:
    """El guard mas importante del modulo."""

    def test_no_rebalance_returns_empty_not_sell_all(self) -> None:
        """rebalance_needed=False NO debe liquidar la cartera.

        construction.py replica current_weights con quantity=0 cuando no hay
        rebalanceo. Diferenciar contra esos ceros emitiria un SELL por cada
        posicion: liquidaria la cartera entera en un dia tranquilo.
        """
        held = {"SPY": Decimal("10"), "QQQ": Decimal("5")}
        # Asi es exactamente como PortfolioConstructor devuelve un no-rebalanceo.
        target = _target({"SPY": "0", "QQQ": "0"}, rebalance=False)

        orders, price_map, warnings = build_proposed_orders(
            target=target,
            current_positions=held,
            prices=_PRICES,
            instruments=_INSTRUMENTS,
            trading_date=_TRADING_DATE,
        )

        assert orders == []
        assert price_map == {}
        assert not [o for o in orders if o.side == Side.SELL]


class TestDiff:
    def test_diffs_target_vs_current_positions(self) -> None:
        held = {"SPY": Decimal("10"), "QQQ": Decimal("5")}
        target = _target({"SPY": "15", "IWM": "8"})

        orders, price_map, _ = build_proposed_orders(
            target=target,
            current_positions=held,
            prices=_PRICES,
            instruments=_INSTRUMENTS,
            trading_date=_TRADING_DATE,
        )

        by_symbol = {o.symbol: o for o in orders}
        assert by_symbol["SPY"].side == Side.BUY
        assert by_symbol["SPY"].quantity == Decimal("5")
        assert by_symbol["QQQ"].side == Side.SELL      # ausente del target -> salir
        assert by_symbol["QQQ"].quantity == Decimal("5")
        assert by_symbol["IWM"].side == Side.BUY
        assert by_symbol["IWM"].quantity == Decimal("8")
        assert set(price_map) == {o.order_id for o in orders}

    def test_sells_come_before_buys(self) -> None:
        """Vender primero libera headroom de notional antes de evaluar compras."""
        held = {"QQQ": Decimal("5"), "IWM": Decimal("3")}
        target = _target({"SPY": "15"})

        orders, _, _ = build_proposed_orders(
            target=target,
            current_positions=held,
            prices=_PRICES,
            instruments=_INSTRUMENTS,
            trading_date=_TRADING_DATE,
        )

        sides = [o.side for o in orders]
        first_buy = sides.index(Side.BUY) if Side.BUY in sides else len(sides)
        last_sell = (
            len(sides) - 1 - sides[::-1].index(Side.SELL)
            if Side.SELL in sides else -1
        )
        assert last_sell < first_buy

        sells = [o.symbol for o in orders if o.side == Side.SELL]
        buys = [o.symbol for o in orders if o.side == Side.BUY]
        assert sells == sorted(sells)
        assert buys == sorted(buys)

    def test_zero_delta_emits_no_order(self) -> None:
        held = {"SPY": Decimal("10")}
        target = _target({"SPY": "10"})
        orders, _, _ = build_proposed_orders(
            target=target,
            current_positions=held,
            prices=_PRICES,
            instruments=_INSTRUMENTS,
            trading_date=_TRADING_DATE,
        )
        assert orders == []

    def test_missing_price_warns_and_skips(self) -> None:
        """Sin precio se excluye el simbolo. Nunca interpolar (3.4)."""
        target = _target({"SPY": "10"})
        orders, _, warnings = build_proposed_orders(
            target=target,
            current_positions={},
            prices={},          # sin precios
            instruments=_INSTRUMENTS,
            trading_date=_TRADING_DATE,
        )
        assert orders == []
        assert any(w.startswith("NO_PRICE:") for w in warnings)

    def test_unknown_instrument_warns_and_skips(self) -> None:
        target = _target({"ZZZ": "10"})
        orders, _, warnings = build_proposed_orders(
            target=target,
            current_positions={},
            prices={"ZZZ": Decimal("10")},
            instruments=_INSTRUMENTS,
            trading_date=_TRADING_DATE,
        )
        assert orders == []
        assert any(w.startswith("UNKNOWN_INSTRUMENT:") for w in warnings)

    def test_order_id_is_deterministic(self) -> None:
        """Debe sobrevivir a un recompute tras crash sin cambiar."""
        target = _target({"SPY": "15"})
        args = {
            "target": target,
            "current_positions": {"SPY": Decimal("10")},
            "prices": _PRICES,
            "instruments": _INSTRUMENTS,
            "trading_date": _TRADING_DATE,
        }
        first, _, _ = build_proposed_orders(**args)   # type: ignore[arg-type]
        second, _, _ = build_proposed_orders(**args)  # type: ignore[arg-type]
        assert [o.order_id for o in first] == [o.order_id for o in second]
        assert first[0].order_id == "2026-06-15:SPY:BUY"


class TestProperties:
    """Property-based, obligatorio para position sizing (CLAUDE.md 5)."""

    @settings(max_examples=100, deadline=None)
    @given(
        held=st.dictionaries(
            st.sampled_from(["SPY", "QQQ", "IWM"]),
            st.integers(min_value=0, max_value=100).map(Decimal),
            max_size=3,
        ),
        wanted=st.dictionaries(
            st.sampled_from(["SPY", "QQQ", "IWM"]),
            st.integers(min_value=0, max_value=100).map(Decimal),
            max_size=3,
        ),
    )
    def test_invariants(self, held: dict[str, Decimal], wanted: dict[str, Decimal]) -> None:
        target = _target({k: str(v) for k, v in wanted.items()})
        orders, price_map, _ = build_proposed_orders(
            target=target,
            current_positions=held,
            prices=_PRICES,
            instruments=_INSTRUMENTS,
            trading_date=_TRADING_DATE,
        )

        for order in orders:
            # Toda orden tiene cantidad estrictamente positiva.
            assert order.quantity > Decimal("0")
            # price_map cubre toda orden emitida.
            assert order.order_id in price_map
            # BUY <=> want > held
            want = wanted.get(order.symbol, Decimal("0"))
            have = held.get(order.symbol, Decimal("0"))
            if order.side == Side.BUY:
                assert want > have
            else:
                assert want < have
            assert order.quantity == abs(want - have)

        # Un simbolo aparece como mucho una vez.
        symbols = [o.symbol for o in orders]
        assert len(symbols) == len(set(symbols))


class TestCurrentPortfolio:
    def test_builds_snapshots_sorted_with_notional(self) -> None:
        portfolio = build_current_portfolio(
            positions={"QQQ": Decimal("5"), "SPY": Decimal("10")},
            prices=_PRICES,
            instruments=_INSTRUMENTS,
            nav=Decimal("15000"),
            peak_nav=Decimal("16000"),
            nav_open_today=Decimal("15100"),
            nav_open_week=Decimal("15200"),
            orders_today=2,
            notional_today=Decimal("3000"),
        )
        assert [p.symbol for p in portfolio.positions] == ["QQQ", "SPY"]
        assert portfolio.positions[0].notional == Decimal("1000")   # 5 * 200
        assert portfolio.positions[1].notional == Decimal("1000")   # 10 * 100
        assert portfolio.orders_today == 2

    def test_zero_quantity_positions_excluded(self) -> None:
        portfolio = build_current_portfolio(
            positions={"SPY": Decimal("0")},
            prices=_PRICES,
            instruments=_INSTRUMENTS,
            nav=Decimal("15000"),
            peak_nav=Decimal("15000"),
            nav_open_today=Decimal("15000"),
            nav_open_week=Decimal("15000"),
            orders_today=0,
            notional_today=Decimal("0"),
        )
        assert portfolio.positions == ()


class TestSanityMarketStates:
    def test_groups_by_exchange(self) -> None:
        instruments = {
            "SPY": _instrument("SPY", "XNYS"),
            "QQQ": _instrument("QQQ", "XNAS"),
        }
        states = build_sanity_market_states(
            instruments=instruments,
            last_close=_PRICES,
            adv={},
            trading_date=_TRADING_DATE,
        )
        assert set(states) == {"XNYS", "XNAS"}
        assert states["XNYS"].active_symbols == frozenset({"SPY"})
        assert states["XNAS"].active_symbols == frozenset({"QQQ"})


class TestDataHash:
    def _bar(self, symbol: str, close: str) -> ValidatedBar:
        return ValidatedBar(
            bar=Bar(
                symbol=symbol,
                timestamp=_TS,
                open=Decimal(close),
                high=Decimal(close),
                low=Decimal(close),
                close=Decimal(close),
                volume=Decimal("1000"),
            ),
            quality=DataQuality.OK,
        )

    def test_deterministic_and_order_independent(self) -> None:
        a = {"SPY": [self._bar("SPY", "100")], "QQQ": [self._bar("QQQ", "200")]}
        b = {"QQQ": [self._bar("QQQ", "200")], "SPY": [self._bar("SPY", "100")]}
        assert compute_data_hash(a) == compute_data_hash(b)

    def test_changes_with_data(self) -> None:
        a = {"SPY": [self._bar("SPY", "100")]}
        b = {"SPY": [self._bar("SPY", "101")]}
        assert compute_data_hash(a) != compute_data_hash(b)


class TestStrmap:
    def test_stringifies_decimals_and_enums(self) -> None:
        out = strmap({
            "qty": Decimal("1.5"),
            "side": Side.BUY,
            "count": 3,
            "flag": True,
        })
        assert out == {"qty": "1.5", "side": "BUY", "count": "3", "flag": "True"}
        assert all(isinstance(v, str) for v in out.values())
