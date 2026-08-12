"""Script auxiliar para el test de crash real del agente (T6).

Uso:
    python _agent_crash_helper.py <tmp_dir> submit [signal_file]
    python _agent_crash_helper.py <tmp_dir> --recovery

Protocolo (identico al de tests/brokers/_crash_helper.py):
    Imprime "READY:<step>" a stdout en el punto de sincronizacion. El test lee
    esa linea y mata el proceso con process.kill() (TerminateProcess en Windows),
    simulando una muerte subita a mitad de SUBMITTING_ORDERS.

    Con --recovery, el agente arranca de nuevo sobre las mismas BD y debe
    reanudar en RECONCILING, nunca reintentando submits a ciegas.

El escenario 'submit' envia la primera orden, imprime READY y se queda
esperando: cuando el test lo mata, hay ordenes ya enviadas y otras no, que es
exactamente la particion desconocida que obliga a reconciliar.
"""
from __future__ import annotations

import asyncio
import sys
import time
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

_repo_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_repo_root / "src"))
sys.path.insert(0, str(_repo_root))

from qtrader.agents.context import AgentContext  # noqa: E402
from qtrader.agents.runner import ensure_ledger_schema  # noqa: E402
from qtrader.agents.states import RunPhase  # noqa: E402
from qtrader.agents.store import AgentStateStore  # noqa: E402
from qtrader.agents.trader import TraderAgent  # noqa: E402
from qtrader.brokers.paper_broker import PaperBroker  # noqa: E402
from qtrader.costs import CostsConfig  # noqa: E402
from qtrader.execution.rate_limiter import (  # noqa: E402
    ExecutionRateLimiter,
    RateLimiterConfig,
)
from qtrader.execution.sanity import BrokerSanityChecker, SanityConfig  # noqa: E402
from qtrader.ledger.sqlite import SQLiteLedger  # noqa: E402
from qtrader.portfolio.construction import (  # noqa: E402
    PortfolioConfig,
    PortfolioConstructor,
)
from qtrader.risk.config import RiskConfig  # noqa: E402
from tests.agents.conftest import (  # noqa: E402
    SYMBOLS,
    TRADING_DATE,
    FakeKillSwitch,
    FakeProvider,
    FakeStrategy,
    FakeWatchdog,
    make_instrument,
)

_EXEC_DATE = date(2024, 6, 17)   # lunes: sesion real de mercado


class SlowBroker(PaperBroker):
    """PaperBroker que senaliza tras el primer submit y luego se cuelga.

    Deja el lote a medias a proposito: unas ordenes han llegado al broker y
    otras no. Esa es la particion que el agente no puede resolver por si mismo
    y que obliga a pasar por RECONCILING antes de reintentar nada.
    """

    signal_after_first = False
    _submits = 0

    async def submit_order(self, order: object, client_order_id: str) -> object:  # type: ignore[override]
        ack = await super().submit_order(order, client_order_id)  # type: ignore[arg-type]
        SlowBroker._submits += 1
        if SlowBroker.signal_after_first and SlowBroker._submits == 1:
            print("READY:after_first_submit", flush=True)
            # Esperar a que el test nos mate. Sin timeout infinito para que el
            # helper no quede colgado si el test falla antes.
            time.sleep(30)
        return ack


def build_context(tmp: Path, *, slow: bool) -> tuple[AgentContext, list[object]]:
    instruments = {s: make_instrument(s) for s in SYMBOLS}

    broker_cls = SlowBroker if slow else PaperBroker
    broker = broker_cls(
        db_path=tmp / "paper.db",
        initial_cash=Decimal("15000"),
        costs_config=CostsConfig.zero_costs(),
    )
    opens = {
        s: {TRADING_DATE + timedelta(days=o): Decimal("100") for o in range(-5, 8)}
        for s in instruments
    }
    broker.set_market_data(
        data=opens,
        instruments=dict(instruments),
        adv={s: Decimal("10000000") for s in instruments},
    )

    ledger = SQLiteLedger(str(tmp / "state.db"))
    # Crear el esquema sin borrar nada: en recovery la cadena de auditoria
    # anterior debe sobrevivir, asi que NO se puede llamar a initialize().
    ensure_ledger_schema(tmp / "state.db")
    store = AgentStateStore(tmp / "agent.db")
    rate_limiter = ExecutionRateLimiter(
        db_path=tmp / "exec.db",
        config=RateLimiterConfig(
            max_orders_per_minute=100,
            max_orders_per_day=100,
            max_notional_per_day=Decimal("1000000"),
        ),
    )

    ctx = AgentContext(
        broker=broker,
        ledger=ledger,
        store=store,
        kill_switch=FakeKillSwitch(),
        watchdog=FakeWatchdog(),
        rate_limiter=rate_limiter,
        sanity=BrokerSanityChecker(
            config=SanityConfig(
                max_price_deviation=Decimal("0.50"), max_adv_fraction=Decimal("1")
            )
        ),
        strategy=FakeStrategy(),
        portfolio_constructor=PortfolioConstructor(
            instruments=instruments,
            config=PortfolioConfig(min_position_size_eur=Decimal("100")),
        ),
        provider=FakeProvider(),
        instruments=instruments,
        trading_days=[TRADING_DATE - timedelta(days=i) for i in range(300, 0, -1)],
        risk_config=RiskConfig(),
        portfolio_config=PortfolioConfig(min_position_size_eur=Decimal("100")),
        mode="paper",
        strategy_id="test",
        adv={s: Decimal("10000000") for s in instruments},
    )
    return ctx, [broker, rate_limiter, store]


def main() -> None:
    tmp = Path(sys.argv[1])
    scenario = sys.argv[2]

    if scenario == "--recovery":
        ctx, closeables = build_context(tmp, slow=False)
        try:
            result = asyncio.run(
                TraderAgent(ctx).run_once(RunPhase.PRE_OPEN, _EXEC_DATE)
            )
            # El test lee esta linea para saber por donde reanudo.
            print(
                "RESUMED:" + ",".join(s.value for s in result.states_entered),
                flush=True,
            )
        finally:
            for c in closeables:
                c.close()  # type: ignore[attr-defined]
        return

    # Escenario 'submit': preparar ordenes y morir a mitad del envio.
    SlowBroker.signal_after_first = True
    ctx, closeables = build_context(tmp, slow=True)
    try:
        asyncio.run(TraderAgent(ctx).run_once(RunPhase.POST_CLOSE, TRADING_DATE))
        asyncio.run(TraderAgent(ctx).run_once(RunPhase.PRE_OPEN, _EXEC_DATE))
    finally:
        for c in closeables:
            c.close()  # type: ignore[attr-defined]


if __name__ == "__main__":
    main()
