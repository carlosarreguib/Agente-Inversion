"""Colaboradores inyectados del agente (T6).

El agente no construye nada: todo llega por AgentContext (CLAUDE.md 4:
"Sin estado global mutable. Sin singletons. La configuracion se inyecta").
El cableado concreto vive en runner.py.
"""
from __future__ import annotations

from collections.abc import Callable  # noqa: TC003 — runtime: anotacion de dataclass
from dataclasses import dataclass, field
from datetime import date, datetime  # noqa: TC003 — runtime: anotacion de dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from qtrader.agents.store import AgentStateStore
    from qtrader.core.types import Instrument, Signal, ValidatedBar
    from qtrader.execution.rate_limiter import ExecutionRateLimiter
    from qtrader.execution.sanity import BrokerSanityChecker
    from qtrader.ledger.sqlite import SQLiteLedger
    from qtrader.portfolio.construction import PortfolioConfig, PortfolioConstructor
    from qtrader.risk.config import RiskConfig
    from qtrader.safety.watchdog import Watchdog


@runtime_checkable
class KillSwitchReader(Protocol):
    """Vista de SOLO LECTURA del kill switch.

    El agente lo lee; no puede escribirlo ni borrarlo (CLAUDE.md 2.6).
    Tipar asi el colaborador hace que activate() y deactivate() no esten
    siquiera en la superficie de tipos del agente: es la garantia estructural
    de la invariante, no solo una convencion.
    """

    def is_active(self) -> bool: ...


@runtime_checkable
class SignalSource(Protocol):
    """Estrategia que emite senales para un dia.

    `data` se tipa como Any y no como object: los implementadores concretos
    (CrossSectionalMomentumStrategy) anotan un tipo mas estrecho, y un Protocol
    con `object` los rechazaria por contravarianza de parametros.
    """

    def on_bar(
        self,
        trading_day: date,
        universe: list[Instrument],
        data: Any,
    ) -> list[Signal]: ...


@runtime_checkable
class BarSource(Protocol):
    """Proveedor de barras ya validadas por simbolo."""

    def get_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        as_of: datetime,
    ) -> list[ValidatedBar]: ...


@runtime_checkable
class AgentBroker(Protocol):
    """Subconjunto de BrokerInterface + extras del PaperBroker que usa el agente.

    Los parametros y retornos van como Any por la misma razon que en
    SignalSource: PaperBroker anota tipos concretos (ApprovedOrder,
    OrderAcknowledgement, OrderStatus) y un Protocol con `object` no lo
    aceptaria como implementacion valida.
    """

    async def submit_order(self, order: Any, client_order_id: str) -> Any: ...
    async def get_order_status(self, client_order_id: str) -> Any: ...
    async def get_positions(self) -> tuple[Any, ...]: ...
    async def get_fills_since(self, since: datetime) -> tuple[Any, ...]: ...
    def reconcile(self) -> None: ...
    def advance_to(self, trading_date: date) -> None: ...
    def get_nav(self, closes: dict[str, Decimal] | None = None) -> Decimal: ...


@dataclass(frozen=True)
class AgentContext:
    """Todo lo que el agente necesita, inyectado. Frozen: el agente no lo muta."""

    broker: AgentBroker
    ledger: SQLiteLedger
    store: AgentStateStore
    kill_switch: KillSwitchReader
    watchdog: Watchdog
    rate_limiter: ExecutionRateLimiter
    sanity: BrokerSanityChecker
    strategy: SignalSource
    portfolio_constructor: PortfolioConstructor
    provider: BarSource
    instruments: dict[str, Instrument]
    trading_days: list[date]
    risk_config: RiskConfig
    portfolio_config: PortfolioConfig
    mode: str = "paper"
    strategy_id: str = "momentum"

    # En paper/development el CLI inyecta kill_switch.activate. En production
    # es None: el Watchdog, que vive FUERA del proceso del agente, es quien
    # activa el HALT al no recibir heartbeat (CLAUDE.md 2.6).
    halt_requester: Callable[[str], None] | None = None

    # En production una divergencia de posiciones es HALT, no warning
    # (CLAUDE.md 2.5). En paper el propio PaperBroker ya la trata como warning.
    reconcile_divergence_is_fatal: bool = False

    git_sha: str = "unknown"
    config_hash: str = "0" * 64
    initial_cash: Decimal = Decimal("15000")
    data_view_factory: Callable[[date], object] | None = None
    extra_parameters: dict[str, str] = field(default_factory=dict)

    # ADV estimado por simbolo en EUR, para la capa 3 (sanity). Sin esto, el
    # checker aplica su fallback conservador de 2000 EUR por orden y rechaza
    # casi todo (sanity.py: ADV_EXCEEDED).
    adv: dict[str, Decimal] = field(default_factory=dict)
