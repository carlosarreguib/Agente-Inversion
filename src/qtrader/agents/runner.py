"""Cableado del agente (T6).

Construye los colaboradores y los inyecta en AgentContext. Vive fuera de
trader.py para que el agente no construya nada por si mismo.

El proveedor de datos se INYECTA desde el CLI: SyntheticMultiProvider vive en
qtrader.backtesting, que agents/ tiene prohibido importar (contrato de
import-linter). El agente solo conoce el Protocol BarSource.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from qtrader.agents.context import AgentContext, BarSource
from qtrader.agents.states import RunPhase
from qtrader.agents.store import AgentStateStore
from qtrader.agents.trader import AgentHalted, CycleResult, TraderAgent
from qtrader.brokers.paper_broker import PaperBroker
from qtrader.costs import CostsConfig
from qtrader.data.universe import UniverseManager
from qtrader.execution.rate_limiter import ExecutionRateLimiter
from qtrader.execution.sanity import BrokerSanityChecker
from qtrader.ledger.sqlite import SQLiteLedger
from qtrader.portfolio.construction import PortfolioConfig, PortfolioConstructor
from qtrader.risk.config import RiskConfig
from qtrader.safety.kill_switch import HaltBackend, KillSwitch
from qtrader.safety.watchdog import Watchdog
from qtrader.strategies.momentum import CrossSectionalMomentumStrategy

if TYPE_CHECKING:
    from qtrader.core.types import Instrument

_log = logging.getLogger(__name__)

_INITIAL_CASH = Decimal("15000")   # CLAUDE.md 1: capital de simulacion
_LOOKBACK_DAYS = 900               # momentum necesita 253 barras


def business_days_until(end: date, count: int) -> list[date]:
    """Dias habiles (lun-vie) terminando en `end`, en orden ascendente."""
    days: list[date] = []
    cursor = end
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


async def run_once(
    provider: BarSource,
    phase: RunPhase,
    trading_date: date,
    *,
    mode: str = "paper",
    agent_db: str | Path = "data/agent.db",
    ledger_db: str | Path = "data/state.db",
    broker_db: str | Path = "data/paper_broker.db",
    exec_db: str | Path = "data/execution.db",
    halt_path: str | Path = "data/HALT",
    halt_backend: HaltBackend = HaltBackend.FILE,
    config_dir: Path | None = None,
    costs_config: CostsConfig | None = None,
    start_watchdog: bool = True,
) -> CycleResult:
    """Ejecuta un ciclo con el cableado real. El llamante gestiona asyncio."""
    universe_mgr = UniverseManager(config_dir=config_dir)
    universe: list[Instrument] = universe_mgr.get_universe(trading_date)
    instruments = {inst.symbol: inst for inst in universe}

    trading_days = business_days_until(trading_date, _LOOKBACK_DAYS)

    kill_switch = KillSwitch(halt_path=halt_path, mode=mode, backend=halt_backend)
    watchdog = Watchdog(kill_switch, max_silence_seconds=180.0)
    if start_watchdog:
        watchdog.start()

    if costs_config is None:
        costs_path = (config_dir or _default_config_dir()) / "costs.yaml"
        costs_config = (
            CostsConfig.from_yaml(costs_path)
            if costs_path.exists()
            else CostsConfig.zero_costs()
        )

    broker = PaperBroker(
        db_path=broker_db,
        initial_cash=_INITIAL_CASH,
        costs_config=costs_config,
        currency="EUR",
    )
    rate_limiter = ExecutionRateLimiter(db_path=exec_db)
    store = AgentStateStore(agent_db)
    ledger = SQLiteLedger(str(ledger_db))
    ensure_ledger_schema(ledger_db)

    # Precios de apertura para que advance_to() pueda rellenar las ordenes.
    # El ADV se reutiliza en el contexto: sin el, la capa 3 aplica su fallback
    # de 2000 EUR por orden y rechaza casi todo.
    adv = _inject_market_data(broker, provider, instruments, trading_date)

    ctx = AgentContext(
        broker=broker,
        ledger=ledger,
        store=store,
        kill_switch=kill_switch,
        watchdog=watchdog,
        rate_limiter=rate_limiter,
        sanity=BrokerSanityChecker(),
        strategy=CrossSectionalMomentumStrategy(
            trading_days=trading_days, strategy_id="momentum"
        ),
        portfolio_constructor=PortfolioConstructor(
            instruments=instruments, config=PortfolioConfig()
        ),
        provider=provider,
        instruments=instruments,
        trading_days=trading_days,
        risk_config=RiskConfig(),
        portfolio_config=PortfolioConfig(),
        mode=mode,
        # En paper/development el CLI inyecta activate. En production seria
        # None: el Watchdog, fuera del proceso, activa el HALT (CLAUDE.md 2.6).
        halt_requester=kill_switch.activate if mode != "production" else None,
        reconcile_divergence_is_fatal=(mode == "production"),
        adv=adv,
    )

    agent = TraderAgent(ctx)
    try:
        return await agent.run_once(phase, trading_date)
    finally:
        broker.close()
        rate_limiter.close()
        store.close()


def _default_config_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "config"


def ensure_ledger_schema(ledger_db: str | Path) -> None:
    """Crea las tablas del ledger si no existen, SIN borrar las que ya hay.

    SQLiteLedger.initialize() hace DROP de todo (sqlite.py:82-86), asi que el
    agente no puede llamarla: destruiria la cadena de auditoria existente.
    """
    import sqlite3

    from qtrader.ledger.sqlite import _SCHEMA  # noqa: PLC2701

    path = Path(ledger_db)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    with conn:
        conn.executescript(_SCHEMA)
    conn.close()


def _inject_market_data(
    broker: PaperBroker,
    provider: BarSource,
    instruments: dict[str, Instrument],
    trading_date: date,
) -> dict[str, Decimal]:
    """Inyecta precios de apertura y ADV en el broker. Devuelve el ADV."""
    from datetime import UTC, datetime

    end = datetime.combine(trading_date, datetime.min.time(), tzinfo=UTC) + timedelta(
        days=5
    )
    start = end - timedelta(days=_LOOKBACK_DAYS + 5)

    data: dict[str, dict[date, Decimal]] = {}
    adv: dict[str, Decimal] = {}

    for symbol in instruments:
        try:
            bars = provider.get_bars(symbol, start, end, as_of=end)
        except (OSError, ValueError):
            continue
        if not bars:
            continue
        data[symbol] = {vb.bar.timestamp.date(): vb.bar.open for vb in bars}
        recent = bars[-20:]
        if recent:
            avg_volume = sum((vb.bar.volume for vb in recent), Decimal("0")) / Decimal(
                len(recent)
            )
            adv[symbol] = avg_volume * recent[-1].bar.close

    broker.set_market_data(
        data=data,
        instruments=dict(instruments),
        adv=adv,
    )
    return adv


__all__ = [
    "AgentHalted",
    "RunPhase",
    "business_days_until",
    "ensure_ledger_schema",
    "run_once",
]
