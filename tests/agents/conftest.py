"""Fixtures compartidas para los tests del agente (T6).

Construyen un AgentContext completo con colaboradores reales (broker, ledger,
store, rate limiter, sanity) y dobles solo donde hace falta control fino
(kill switch, estrategia, watchdog).
"""
from __future__ import annotations

import hashlib
import random
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path  # noqa: TC003 — runtime: fixtures de pytest
from typing import TYPE_CHECKING

import pytest

from qtrader.agents.context import AgentContext
from qtrader.agents.store import AgentStateStore
from qtrader.brokers.paper_broker import PaperBroker
from qtrader.core.types import (
    Bar,
    DataQuality,
    Direction,
    Instrument,
    InstrumentCategory,
    Signal,
    ValidatedBar,
)
from qtrader.costs import CostsConfig
from qtrader.execution.rate_limiter import ExecutionRateLimiter, RateLimiterConfig
from qtrader.execution.sanity import BrokerSanityChecker, SanityConfig
from qtrader.ledger.sqlite import SQLiteLedger
from qtrader.portfolio.construction import PortfolioConfig, PortfolioConstructor
from qtrader.risk.config import RiskConfig

if TYPE_CHECKING:
    from collections.abc import Iterator

TRADING_DATE = date(2024, 6, 14)

# 6 simbolos y no 3: PortfolioConstructor devuelve cartera VACIA si algun peso
# supera max_position_weight (0.25 por defecto, construction.py:418). Con 3
# instrumentos equiponderados el peso seria ~0.33 y no habria ninguna orden.
SYMBOLS = ("SPY", "QQQ", "IWM", "EFA", "AGG", "GLD")


# ---------------------------------------------------------------------------
# Dobles
# ---------------------------------------------------------------------------


class FakeKillSwitch:
    """Kill switch controlable que cuenta las lecturas."""

    def __init__(self, active: bool = False) -> None:
        self.active = active
        self.calls = 0
        self.activate_after: int | None = None

    def is_active(self) -> bool:
        self.calls += 1
        if self.activate_after is not None and self.calls > self.activate_after:
            self.active = True
        return self.active


class FakeWatchdog:
    """Watchdog que registra los heartbeats sin arrancar hilos."""

    def __init__(self) -> None:
        self.heartbeats = 0
        self.started = False

    def start(self) -> None:
        self.started = True

    def heartbeat(self) -> None:
        self.heartbeats += 1

    def is_alive(self) -> bool:
        return self.started


class FakeStrategy:
    """Estrategia determinista: emite LONG para los simbolos indicados."""

    def __init__(self, longs: tuple[str, ...] = SYMBOLS) -> None:
        self.longs = longs
        self.calls = 0

    def on_bar(
        self, trading_day: date, universe: list[Instrument], data: object
    ) -> list[Signal]:
        self.calls += 1
        return [
            Signal(
                symbol=inst.symbol,
                timestamp=datetime.combine(
                    trading_day, datetime.min.time(), tzinfo=UTC
                ),
                direction=Direction.LONG,
                strength=Decimal("0.8"),
                strategy_id="test",
            )
            for inst in universe
            if inst.symbol in self.longs
        ]


class FakeProvider:
    """Proveedor sintetico determinista con volatilidad realista.

    La serie NO puede ser demasiado suave: con varianza casi nula, el detector
    de spikes de validate_bars mide cualquier retorno minusculo en miles de
    sigma y excluye todas las barras. Se usa random.Random con seed fija para
    que la serie sea reproducible entre procesos.
    """

    def __init__(self, symbols: tuple[str, ...] = SYMBOLS, days: int = 300) -> None:
        self._symbols = symbols
        self._days = days

    def get_bars(
        self, symbol: str, start: datetime, end: datetime, as_of: datetime
    ) -> list[ValidatedBar]:
        if symbol not in self._symbols:
            return []

        # md5 y no hash(): hash() no es estable entre procesos y los tests de
        # crash lanzan subprocesos que deben ver la MISMA serie.
        seed = int(hashlib.md5(symbol.encode(), usedforsecurity=False).hexdigest(), 16)
        rng = random.Random(seed % 10_000)  # noqa: S311 — datos de test
        price = Decimal("100") + Decimal(len(symbol))
        bars: list[ValidatedBar] = []

        for i in range(self._days):
            day = end.date() - timedelta(days=self._days - 1 - i)
            # Deriva ligera + ruido diario ~1 %: suficiente varianza para que
            # el detector de spikes no marque toda la serie.
            shock = Decimal(str(round(rng.gauss(0.0004, 0.01), 6)))
            price = (price * (Decimal("1") + shock)).quantize(Decimal("0.01"))
            if price <= Decimal("1"):
                price = Decimal("1.01")
            high = (price * Decimal("1.005")).quantize(Decimal("0.01"))
            low = (price * Decimal("0.995")).quantize(Decimal("0.01"))
            bars.append(
                ValidatedBar(
                    bar=Bar(
                        symbol=symbol,
                        timestamp=datetime.combine(
                            day, datetime.min.time(), tzinfo=UTC
                        ),
                        open=price,
                        high=high,
                        low=low,
                        close=price,
                        volume=Decimal("1000000"),
                    ),
                    quality=DataQuality.OK,
                )
            )
        return bars

    def get_corporate_actions(
        self, symbol: str, start: datetime, end: datetime
    ) -> list[object]:
        return []


# Categorias variadas a proposito: si todos los instrumentos fueran REGION, el
# cap de region (0.60) aplicaria a la cartera entera y el bucle de restricciones
# de PortfolioConstructor la dejaria vacia.
_CATEGORIES = {
    "SPY": InstrumentCategory.REGION,
    "QQQ": InstrumentCategory.FACTOR,
    "IWM": InstrumentCategory.FACTOR,
    "EFA": InstrumentCategory.REGION,
    "AGG": InstrumentCategory.FIXED_INCOME,
    "GLD": InstrumentCategory.ALTERNATIVE,
}


def make_instrument(symbol: str, exchange: str = "XNYS") -> Instrument:
    return Instrument(
        symbol=symbol,
        name=f"{symbol} ETF",
        exchange=exchange,
        currency="EUR",
        category=_CATEGORIES.get(symbol, InstrumentCategory.FACTOR),
        ticker_proxy=symbol,
        ticker_ucits=f"{symbol}.L",
        declared_on=date(2020, 1, 1),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def instruments() -> dict[str, Instrument]:
    return {s: make_instrument(s) for s in SYMBOLS}


@pytest.fixture()
def kill_switch() -> FakeKillSwitch:
    return FakeKillSwitch()


@pytest.fixture()
def watchdog() -> FakeWatchdog:
    return FakeWatchdog()


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[AgentStateStore]:
    s = AgentStateStore(tmp_path / "agent.db")
    yield s
    s.close()


@pytest.fixture()
def broker(tmp_path: Path, instruments: dict[str, Instrument]) -> Iterator[PaperBroker]:
    b = PaperBroker(
        db_path=tmp_path / "paper.db",
        initial_cash=Decimal("15000"),
        costs_config=CostsConfig.zero_costs(),
        _testing=True,
    )
    opens = {
        symbol: {
            TRADING_DATE + timedelta(days=offset): Decimal("100")
            for offset in range(-5, 6)
        }
        for symbol in instruments
    }
    b.set_market_data(
        data=opens,
        instruments=dict(instruments),
        adv={s: Decimal("10000000") for s in instruments},
    )
    yield b
    b.close()


@pytest.fixture()
def ledger(tmp_path: Path) -> SQLiteLedger:
    led = SQLiteLedger(str(tmp_path / "state.db"))
    led.initialize()   # BD nueva de test: aqui el DROP es inofensivo
    return led


@pytest.fixture()
def rate_limiter(tmp_path: Path) -> Iterator[ExecutionRateLimiter]:
    rl = ExecutionRateLimiter(
        db_path=tmp_path / "exec.db",
        config=RateLimiterConfig(
            max_orders_per_minute=100,
            max_orders_per_day=100,
            max_notional_per_day=Decimal("1000000"),
        ),
    )
    yield rl
    rl.close()


@pytest.fixture()
def context(
    broker: PaperBroker,
    ledger: SQLiteLedger,
    store: AgentStateStore,
    kill_switch: FakeKillSwitch,
    watchdog: FakeWatchdog,
    rate_limiter: ExecutionRateLimiter,
    instruments: dict[str, Instrument],
) -> AgentContext:
    """Contexto completo con colaboradores reales salvo kill switch/watchdog."""
    return AgentContext(
        broker=broker,           # type: ignore[arg-type]
        ledger=ledger,
        store=store,
        kill_switch=kill_switch,
        watchdog=watchdog,       # type: ignore[arg-type]
        rate_limiter=rate_limiter,
        sanity=BrokerSanityChecker(
            # Sin restriccion de calendario en tests: 2024-06-14 es viernes,
            # pero no queremos que el test dependa del calendario real.
            config=SanityConfig(
                max_price_deviation=Decimal("0.50"),
                max_adv_fraction=Decimal("1"),
            )
        ),
        strategy=FakeStrategy(),      # type: ignore[arg-type]
        portfolio_constructor=PortfolioConstructor(
            instruments=instruments,
            config=PortfolioConfig(min_position_size_eur=Decimal("100")),
        ),
        provider=FakeProvider(),      # type: ignore[arg-type]
        instruments=instruments,
        trading_days=[TRADING_DATE - timedelta(days=i) for i in range(300, 0, -1)],
        risk_config=RiskConfig(),
        portfolio_config=PortfolioConfig(min_position_size_eur=Decimal("100")),
        mode="paper",
        strategy_id="test",
        adv={s: Decimal("10000000") for s in instruments},
    )
