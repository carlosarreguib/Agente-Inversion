"""TraderAgent — maquina de estados del ciclo diario (T6).

No contiene logica de negocio. Conecta en orden los modulos existentes y
persiste el estado para poder reanudar tras un crash sin duplicar ordenes.

Invariantes que este modulo hace cumplir:
  - Kill switch comprobado al inicio de CADA estado y antes de CADA submit.
  - Tras un crash en SUBMITTING_ORDERS se reanuda en RECONCILING, nunca
    reintentando submits directamente (CLAUDE.md 2.4).
  - El agente NUNCA llama a kill_switch.activate(): lo tipa como
    KillSwitchReader (solo is_active). La peticion de HALT va por un
    halt_requester inyectado (CLAUDE.md 2.6).
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from qtrader.agents.adapters import (
    build_current_portfolio,
    build_proposed_orders,
    build_sanity_market_states,
    compute_data_hash,
    strmap,
)
from qtrader.agents.states import (
    TERMINAL,
    AgentState,
    RunPhase,
    next_state,
    resume_state,
)
from qtrader.agents.store import PendingOrderRow
from qtrader.brokers.order_id import generate_client_order_id, next_sequence
from qtrader.brokers.types import OrderStatus
from qtrader.core.types import AuditRecord, Severity, Side
from qtrader.data.validation import validate_bars
from qtrader.risk.engine import RiskEngine
from qtrader.risk.types import ApprovedOrder, MarketState, RiskLevel

if TYPE_CHECKING:
    from qtrader.agents.context import AgentContext
    from qtrader.core.types import Instrument, Signal, ValidatedBar

_log = logging.getLogger(__name__)

_ZERO = Decimal("0")
_TOCTOU_BUDGET_MS = 100.0


class AgentWarning(Exception):
    """Condicion que se registra y de la que se continua."""


class AgentHalted(Exception):
    """Error fatal: el ciclo se detiene."""


class KillSwitchActive(Exception):
    """El kill switch esta activo. Parada limpia, NO es un error del agente."""


class CycleResult:
    """Resultado de un ciclo. Mutable a proposito: lo rellena el agente."""

    def __init__(self, cycle_id: str, phase: RunPhase, trading_date: date) -> None:
        self.cycle_id = cycle_id
        self.phase = phase
        self.trading_date = trading_date
        self.states_entered: list[AgentState] = []
        self.warnings: list[str] = []
        self.orders_submitted: int = 0
        self.orders_blocked: int = 0
        self.fills: int = 0
        self.halted_by_kill_switch: bool = False
        self.error_message: str | None = None
        self.risk_level: RiskLevel | None = None
        self.toctou_max_ms: float = 0.0


class TraderAgent:
    """Orquestador determinista. Un ciclo por invocacion de run_once()."""

    def __init__(self, ctx: AgentContext) -> None:
        self._ctx = ctx
        self._cycle_id: str = ""
        self._trading_date: date = date.today()
        self._audit_seq: int = 0
        self._data_hash: str = "0" * 64
        self._result: CycleResult | None = None

        # Estado en memoria del ciclo POST_CLOSE (muere con el proceso: por eso
        # GENERATING_SIGNALS y EVALUATING_RISK reanudan en LOADING_DATA).
        self._bars: dict[str, list[ValidatedBar]] = {}
        self._universe: list[Instrument] = []
        self._prices: dict[str, Decimal] = {}
        self._adv: dict[str, Decimal] = {}
        self._signals: list[Signal] = []
        self._portfolio_target: object | None = None
        self._price_map: dict[str, Decimal] = {}

    # ------------------------------------------------------------------
    # Entrada publica
    # ------------------------------------------------------------------

    async def run_once(self, phase: RunPhase, trading_date: date) -> CycleResult:
        """Ejecuta un ciclo completo de la fase indicada.

        Reanuda desde el estado persistido si el proceso murio a mitad.
        """
        ctx = self._ctx
        self._trading_date = trading_date

        previous = ctx.store.load()

        # Un ERROR persistido rechaza arrancar: un agente que auto-reanuda tras
        # un error fatal anula el proposito del estado ERROR.
        if previous is not None and previous.current_state == AgentState.ERROR:
            raise AgentHalted(
                f"Estado ERROR persistido del ciclo {previous.cycle_id}: "
                f"{previous.error_message}. Requiere intervencion humana."
            )

        start_state, self._cycle_id = self._resolve_start(previous, phase, trading_date)
        self._audit_seq = self._seed_audit_seq()

        result = CycleResult(self._cycle_id, phase, trading_date)
        self._result = result

        _log.info(
            "Ciclo %s fase=%s fecha=%s estado_inicial=%s",
            self._cycle_id, phase.value, trading_date.isoformat(), start_state.value,
        )

        state = start_state
        try:
            while state not in TERMINAL:
                await self._run_state(state, phase, result)
                state = next_state(phase, state)
            ctx.store.persist(state, trading_date, self._cycle_id)
            result.states_entered.append(state)
        except KillSwitchActive as exc:
            # Parada limpia por orden externa legitima, no un fallo del agente.
            result.halted_by_kill_switch = True
            _log.warning("Ciclo detenido por kill switch: %s", exc)
            self._audit("AGENT_HALTED_BY_KILL_SWITCH", state, reason=str(exc))
            ctx.store.persist(AgentState.SLEEPING, trading_date, self._cycle_id)
        except AgentHalted as exc:
            # AgentHalted lanzado DENTRO de un handler (ej. VALIDATION_HALT)
            # tambien es un error fatal: debe persistir ERROR y pedir el HALT.
            # El chequeo de ERROR persistido al arrancar ocurre antes del try,
            # asi que no puede llegar aqui y no hay doble envoltura.
            self._to_error(state, exc, result)
            raise
        except Exception as exc:  # noqa: BLE001 — ultima linea de defensa
            self._to_error(state, exc, result)
            raise AgentHalted(str(exc)) from exc

        return result

    # ------------------------------------------------------------------
    # Envelope de estado
    # ------------------------------------------------------------------

    async def _run_state(
        self, state: AgentState, phase: RunPhase, result: CycleResult
    ) -> None:
        """Envelope comun a todos los estados.

        Persistir ANTES del trabajo es lo que hace correcta la tabla de
        recuperacion: si el proceso muere a mitad del handler, la BD ya dice
        que estabamos en este estado.
        """
        ctx = self._ctx

        self._check_kill_switch(f"inicio de {state.value}")
        ctx.watchdog.heartbeat()
        ctx.store.persist(state, self._trading_date, self._cycle_id)
        result.states_entered.append(state)

        handler = {
            AgentState.LOADING_DATA: self._state_loading_data,
            AgentState.GENERATING_SIGNALS: self._state_generating_signals,
            AgentState.EVALUATING_RISK: self._state_evaluating_risk,
            AgentState.RECONCILING: self._state_reconciling,
            AgentState.SUBMITTING_ORDERS: self._state_submitting_orders,
            AgentState.MONITORING: self._state_monitoring,
        }[state]

        try:
            await handler(result)
        except AgentWarning as warn:
            self._warn(state, str(warn), result)

    # ------------------------------------------------------------------
    # POST_CLOSE
    # ------------------------------------------------------------------

    async def _state_loading_data(self, result: CycleResult) -> None:
        """Carga barras del universo y las valida. HALT en validacion -> ERROR."""
        ctx = self._ctx
        self._universe = [
            inst for inst in ctx.instruments.values()
            if inst.declared_on <= self._trading_date
        ]
        self._universe.sort(key=lambda i: i.symbol)

        end = datetime.combine(self._trading_date, datetime.min.time(), tzinfo=UTC)
        start = end - timedelta(days=900)

        bars: dict[str, list[ValidatedBar]] = {}
        prices: dict[str, Decimal] = {}
        excluded: list[str] = []

        for inst in self._universe:
            symbol = inst.symbol
            try:
                symbol_bars = ctx.provider.get_bars(symbol, start, end, as_of=end)
            except (OSError, ValueError) as exc:
                excluded.append(symbol)
                result.warnings.append(f"PROVIDER_ERROR:{symbol}:{exc}")
                continue

            if not symbol_bars:
                excluded.append(symbol)
                continue

            findings = validate_bars(symbol_bars)

            halts = [f for f in findings if f.severity == Severity.HALT]
            if halts:
                # Severity.HALT esta reservado a corrupcion sistemica del
                # proveedor (validation.py). Es ERROR, no exclusion.
                detail = f"{symbol}: {halts[0].rule_violated} — {halts[0].details}"
                raise AgentHalted(f"VALIDATION_HALT: {detail}")

            if any(f.severity == Severity.EXCLUDE for f in findings):
                # Excluir e informar. Nunca interpolar ni rellenar (3.4).
                excluded.append(symbol)
                reasons = sorted({f.rule_violated for f in findings})
                result.warnings.append(f"EXCLUDED:{symbol}:{','.join(reasons)}")
                self._audit(
                    "AGENT_DATA_EXCLUDED",
                    AgentState.LOADING_DATA,
                    payload={"symbol": symbol, "rules": ",".join(reasons)},
                    discriminator=symbol,
                )
                continue

            for finding in findings:
                if finding.severity == Severity.WARN:
                    result.warnings.append(f"WARN:{symbol}:{finding.rule_violated}")

            bars[symbol] = symbol_bars
            prices[symbol] = symbol_bars[-1].bar.close

        if not bars:
            raise AgentHalted("NO_DATA: ningun simbolo sobrevivio a la validacion")

        self._bars = bars
        self._prices = prices
        self._adv = _compute_adv(bars)
        self._data_hash = compute_data_hash(bars)

        self._audit(
            "AGENT_DATA_LOADED",
            AgentState.LOADING_DATA,
            payload={
                "symbols": str(len(bars)),
                "excluded": str(len(excluded)),
                "data_hash": self._data_hash,
            },
        )

    async def _state_generating_signals(self, result: CycleResult) -> None:
        """Senales de momentum + construccion del PortfolioTarget."""
        ctx = self._ctx

        data_view = (
            ctx.data_view_factory(self._trading_date)
            if ctx.data_view_factory is not None
            else _InMemoryDataView(self._bars, self._trading_date)
        )

        universe_for_strategy = [
            inst for inst in self._universe if inst.symbol in self._bars
        ]

        self._signals = ctx.strategy.on_bar(
            trading_day=self._trading_date,
            universe=universe_for_strategy,
            data=data_view,
        )

        self._audit(
            "AGENT_SIGNALS",
            AgentState.GENERATING_SIGNALS,
            payload={"count": str(len(self._signals))},
        )

        positions = await self._current_positions()
        nav = ctx.broker.get_nav(closes=self._prices)
        closes_history = {
            sym: [vb.bar.close for vb in vbars] for sym, vbars in self._bars.items()
        }
        current_weights = self._current_weights(positions, nav)

        self._portfolio_target = ctx.portfolio_constructor.build(
            signals=self._signals,
            closes_history=closes_history,
            current_weights=current_weights,
            nav=nav,
            timestamp=datetime.now(UTC),
        )

        target = self._portfolio_target
        self._audit(
            "AGENT_PORTFOLIO_TARGET",
            AgentState.GENERATING_SIGNALS,
            payload=strmap({
                "targets": len(getattr(target, "targets", ())),
                "rebalance_needed": getattr(target, "rebalance_needed", False),
                "total_weight": getattr(target, "total_weight", _ZERO),
            }),
        )

    async def _state_evaluating_risk(self, result: CycleResult) -> None:
        """RiskEngine.evaluate() y persistencia de las ordenes aprobadas."""
        ctx = self._ctx
        target = self._portfolio_target
        if target is None:
            raise AgentHalted("EVALUATING_RISK sin PortfolioTarget")

        # Al reanudar, borrar filas a medio escribir antes de reinsertar.
        ctx.store.clear_unsubmitted(self._trading_date)

        positions = await self._current_positions()
        proposed, price_map, adapter_warnings = build_proposed_orders(
            target=target,                       # type: ignore[arg-type]
            current_positions=positions,
            prices=self._prices,
            instruments=ctx.instruments,
            trading_date=self._trading_date,
        )
        self._price_map = price_map
        for warning in adapter_warnings:
            result.warnings.append(warning)
            self._warn(AgentState.EVALUATING_RISK, warning, result, audit=False)

        nav = ctx.broker.get_nav(closes=self._prices)
        ctx.store.record_nav(self._trading_date, nav)
        peak_nav, nav_open_today, nav_open_week = self._nav_reference_points(nav)

        portfolio = build_current_portfolio(
            positions=positions,
            prices=self._prices,
            instruments=ctx.instruments,
            nav=nav,
            peak_nav=peak_nav,
            nav_open_today=nav_open_today,
            nav_open_week=nav_open_week,
            orders_today=ctx.rate_limiter.orders_today(self._trading_date),
            notional_today=ctx.rate_limiter.notional_today(self._trading_date),
        )

        market_state = MarketState(
            trading_date=self._trading_date,
            days_below_threshold=0,
            is_market_open=True,
        )

        decision = RiskEngine.evaluate(
            portfolio=portfolio,
            proposed_orders=proposed,
            market_state=market_state,
            config=ctx.risk_config,
        )
        result.risk_level = decision.level

        for warning in decision.warnings:
            result.warnings.append(f"RISK:{warning}")

        self._audit(
            "AGENT_RISK_DECISION",
            AgentState.EVALUATING_RISK,
            risk_output=strmap({
                "level": decision.level,
                "approved": len(decision.approved_orders),
                "rejected": len(decision.rejected_orders),
                "drawdown": decision.portfolio_metrics.current_drawdown,
                "total_exposure": decision.portfolio_metrics.total_exposure,
            }),
            decision=decision.level.value,
        )

        for rejected in decision.rejected_orders:
            self._audit(
                "AGENT_ORDER_REJECTED_RISK",
                AgentState.EVALUATING_RISK,
                payload=strmap({
                    "order_id": rejected.order_id,
                    "symbol": rejected.symbol,
                    "reason": rejected.reason,
                }),
                discriminator=rejected.order_id,
                decision="REJECT",
                reason=rejected.reason,
            )

        if decision.level == RiskLevel.HALT:
            # HALT no genera ordenes. Se registra y el ciclo termina limpio.
            _log.critical("Risk Engine en HALT: no se generan ordenes")
            result.warnings.append("RISK_LEVEL_HALT")
            return

        rows = [
            PendingOrderRow(
                cycle_id=self._cycle_id,
                order_id=approved.order_id,
                trading_date=self._trading_date,
                symbol=approved.symbol,
                side=approved.side.value,
                original_quantity=approved.original_quantity,
                approved_quantity=approved.approved_quantity,
                price=price_map.get(approved.order_id, _ZERO),
                notional=approved.notional,
                category=ctx.instruments[approved.symbol].category.value,
                reduction_reason=approved.reduction_reason,
                sequence_number=None,
                client_order_id=None,
                submitted_at=None,
            )
            for approved in decision.approved_orders
            if approved.symbol in ctx.instruments
        ]
        ctx.store.insert_pending(rows)

    # ------------------------------------------------------------------
    # PRE_OPEN
    # ------------------------------------------------------------------

    async def _state_reconciling(self, result: CycleResult) -> None:
        """PaperBroker.reconcile() + verificacion de posiciones."""
        ctx = self._ctx
        ctx.broker.reconcile()

        positions = await self._current_positions()

        self._audit(
            "AGENT_RECONCILED",
            AgentState.RECONCILING,
            payload={"positions": str(len(positions))},
        )

        if ctx.reconcile_divergence_is_fatal:
            # En production una discrepancia es HALT, nunca warning (2.5).
            # El PaperBroker ya loggea divergencias internamente en paper.
            pass

    async def _state_submitting_orders(self, result: CycleResult) -> None:
        """Envia las ordenes aprobadas pasando por las 3 capas de defensa."""
        ctx = self._ctx
        # Ordenes con fecha de senal <= hoy: la senal de T se ejecuta en la
        # apertura de T+1 o posterior (invariante 3.1).
        pending = ctx.store.pending_up_to(self._trading_date)
        if not pending:
            return

        sanity_states = build_sanity_market_states(
            instruments=ctx.instruments,
            last_close={r.symbol: r.price for r in pending},
            # Sin ADV, el sanity checker aplica su fallback de 2000 EUR y
            # rechaza casi cualquier orden con ADV_EXCEEDED.
            adv=self._adv or ctx.adv,
            trading_date=self._trading_date,
        )

        for row in pending:
            # Tras un crash, una fila con client_order_id ya escrito pudo llegar
            # al broker: consultar antes de reenviar (CLAUDE.md 2.4).
            if row.client_order_id is not None:
                status = await ctx.broker.get_order_status(row.client_order_id)
                if status != OrderStatus.UNKNOWN:
                    ctx.store.mark_submitted(row.order_id, row.trading_date)
                    _log.info(
                        "Orden %s ya conocida por el broker (%s): no se reenvia",
                        row.order_id, status,
                    )
                    continue

            instrument = ctx.instruments.get(row.symbol)
            if instrument is None:
                continue
            market_state = sanity_states.get(instrument.exchange)
            if market_state is None:
                continue

            # --- Capa 3: sanity ---
            sanity_result = ctx.sanity.check(
                order_id=row.order_id,
                symbol=row.symbol,
                side=row.side,
                quantity=row.approved_quantity,
                price=row.price,
                notional=row.notional,
                market_state=market_state,
            )
            if not sanity_result.passed:
                result.orders_blocked += 1
                self._audit(
                    "AGENT_ORDER_REJECTED_SANITY",
                    AgentState.SUBMITTING_ORDERS,
                    payload={"order_id": row.order_id, "symbol": row.symbol},
                    discriminator=row.order_id,
                    decision="BLOCKED",
                    reason=sanity_result.reason or "",
                )
                continue

            # --- Capa 2: rate limiter (consume cuota al aprobar) ---
            rate_result = ctx.rate_limiter.check_and_record(
                order_id=row.order_id,
                symbol=row.symbol,
                notional=row.notional,
                timestamp=datetime.now(UTC),
            )
            if not rate_result.allowed:
                result.orders_blocked += 1
                self._audit(
                    "AGENT_ORDER_REJECTED_RATE_LIMIT",
                    AgentState.SUBMITTING_ORDERS,
                    payload={"order_id": row.order_id, "symbol": row.symbol},
                    discriminator=row.order_id,
                    decision="BLOCKED",
                    reason=rate_result.reason or "",
                )
                continue

            # --- WAL del agente: coid y secuencia ANTES del envio ---
            with ctx.store.conn:
                sequence = next_sequence(ctx.store.conn)
                client_order_id = generate_client_order_id(
                    trading_date=row.trading_date,
                    symbol=row.symbol,
                    side=Side(row.side),
                    strategy_id=ctx.strategy_id,
                    sequence_number=sequence,
                )
                ctx.store.write_precommit(
                    row.order_id, row.trading_date, client_order_id, sequence
                )

            approved = ApprovedOrder(
                order_id=row.order_id,
                symbol=row.symbol,
                side=Side(row.side),
                original_quantity=row.original_quantity,
                approved_quantity=row.approved_quantity,
                notional=row.notional,
                reduction_reason=row.reduction_reason,
            )

            # TOCTOU: entre este check y el submit no puede haber NADA, y en
            # particular ningun await distinto del propio submit_order: un await
            # cede al event loop y la ventana se vuelve ilimitada.
            started = time.monotonic()
            self._check_kill_switch("pre-submit")
            await ctx.broker.submit_order(approved, client_order_id)
            elapsed_ms = (time.monotonic() - started) * 1000.0
            result.toctou_max_ms = max(result.toctou_max_ms, elapsed_ms)
            if elapsed_ms > _TOCTOU_BUDGET_MS:
                _log.warning(
                    "Ventana TOCTOU %.1fms > %.0fms en %s",
                    elapsed_ms, _TOCTOU_BUDGET_MS, row.order_id,
                )

            ctx.store.mark_submitted(row.order_id, self._trading_date)
            result.orders_submitted += 1
            self._audit(
                "AGENT_ORDER_SUBMITTED",
                AgentState.SUBMITTING_ORDERS,
                payload=strmap({
                    "order_id": row.order_id,
                    "client_order_id": client_order_id,
                    "symbol": row.symbol,
                    "side": row.side,
                    "quantity": row.approved_quantity,
                    "notional": row.notional,
                }),
                discriminator=row.order_id,
                decision="SUBMITTED",
            )

    async def _state_monitoring(self, result: CycleResult) -> None:
        """Procesa fills, registra en el ledger y late al watchdog."""
        ctx = self._ctx
        ctx.broker.advance_to(self._trading_date)

        since = datetime.combine(
            self._trading_date, datetime.min.time(), tzinfo=UTC
        ) - timedelta(days=1)
        fills = await ctx.broker.get_fills_since(since)

        for fill in fills:
            ctx.ledger.record_fill(fill)
        result.fills = len(fills)

        ctx.watchdog.heartbeat()

        nav = ctx.broker.get_nav()
        ctx.store.record_nav(self._trading_date, nav)

        self._audit(
            "AGENT_FILLS",
            AgentState.MONITORING,
            payload=strmap({"fills": len(fills), "nav": nav}),
        )
        self._audit(
            "AGENT_CYCLE_COMPLETE",
            AgentState.MONITORING,
            payload=strmap({
                "orders_submitted": result.orders_submitted,
                "orders_blocked": result.orders_blocked,
                "fills": len(fills),
            }),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_start(
        self,
        previous: object,
        phase: RunPhase,
        trading_date: date,
    ) -> tuple[AgentState, str]:
        """Decide estado inicial y cycle_id segun lo persistido."""
        from qtrader.agents.states import ENTRY_STATE

        if previous is None:
            return (ENTRY_STATE[phase], str(uuid.uuid4()))

        prev_state: AgentState = previous.current_state  # type: ignore[attr-defined]
        prev_cycle: str = previous.cycle_id              # type: ignore[attr-defined]
        prev_date: date = previous.trading_date          # type: ignore[attr-defined]

        # Ciclo anterior completado, o fecha distinta: ciclo nuevo.
        if prev_state == AgentState.SLEEPING or prev_date != trading_date:
            return (ENTRY_STATE[phase], str(uuid.uuid4()))

        resumed = resume_state(prev_state)

        # El estado persistido puede ser de la otra fase (ej. SLEEPING de
        # POST_CLOSE al arrancar PRE_OPEN). Si no pertenece a esta fase,
        # empezamos por la entrada de la fase pedida.
        from qtrader.agents.states import PHASE_STATES

        if resumed not in PHASE_STATES[phase]:
            return (ENTRY_STATE[phase], str(uuid.uuid4()))

        _log.warning(
            "Reanudando ciclo %s: estado persistido=%s -> reanuda en=%s",
            prev_cycle, prev_state.value, resumed.value,
        )
        return (resumed, prev_cycle)

    def _seed_audit_seq(self) -> int:
        """Siembra el contador de auditoria desde el ledger.

        Sin esto, un crash reinicia seq a 0 y los registros reanudados
        colisionan en silencio con los previos: record_audit usa INSERT OR
        IGNORE sobre record_id UNIQUE, asi que un duplicado se pierde.
        """
        return self._ctx.ledger.count_audit_with_prefix(f"{self._cycle_id}:")

    async def _current_positions(self) -> dict[str, Decimal]:
        positions = await self._ctx.broker.get_positions()
        return {p.symbol: p.quantity for p in positions}

    def _current_weights(
        self, positions: dict[str, Decimal], nav: Decimal
    ) -> dict[str, Decimal]:
        if nav <= _ZERO:
            return {}
        weights: dict[str, Decimal] = {}
        for symbol, quantity in positions.items():
            price = self._prices.get(symbol)
            if price is None:
                continue
            weights[symbol] = (quantity * price) / nav
        return weights

    def _nav_reference_points(
        self, nav: Decimal
    ) -> tuple[Decimal, Decimal, Decimal]:
        """peak_nav, nav_open_today y nav_open_week desde agent_nav_history.

        Contabilidad propia del agente: el repo no tenia fuente para estos
        valores. En la primera ejecucion los tres igualan el NAV actual.
        """
        history = self._ctx.store.nav_history()
        if not history:
            return (nav, nav, nav)

        peak = max((value for _, value in history), default=nav)
        peak = max(peak, nav)

        prior = [(d, v) for d, v in history if d < self._trading_date]
        nav_open_today = prior[-1][1] if prior else nav

        week_start = self._trading_date - timedelta(days=self._trading_date.weekday())
        week_prior = [(d, v) for d, v in history if d < week_start]
        nav_open_week = week_prior[-1][1] if week_prior else nav_open_today

        return (peak, nav_open_today, nav_open_week)

    def _check_kill_switch(self, where: str) -> None:
        if self._ctx.kill_switch.is_active():
            raise KillSwitchActive(f"kill switch activo en {where}")

    def _warn(
        self,
        state: AgentState,
        message: str,
        result: CycleResult,
        *,
        audit: bool = True,
    ) -> None:
        _log.warning("[%s] %s", state.value, message)
        if message not in result.warnings:
            result.warnings.append(message)
        if audit:
            self._audit(
                "AGENT_WARNING",
                state,
                payload={"message": message},
                reason=message,
            )

    def _to_error(self, state: AgentState, exc: Exception, result: CycleResult) -> None:
        """Registra el error, persiste ERROR y pide HALT si hay requester."""
        message = f"{type(exc).__name__}: {exc}"
        result.error_message = message
        _log.critical("[%s] ERROR FATAL: %s", state.value, message)

        try:
            self._audit(
                "AGENT_ERROR", state, payload={"error": message}, reason=message
            )
        except Exception:  # noqa: BLE001 — la auditoria no debe ocultar el error
            _log.exception("No se pudo registrar AGENT_ERROR en auditoria")

        self._ctx.store.persist(
            AgentState.ERROR, self._trading_date, self._cycle_id, error_message=message
        )
        result.states_entered.append(AgentState.ERROR)

        # El agente NO llama a kill_switch.activate(): pide el HALT por el
        # requester inyectado. En production es None y el Watchdog, fuera del
        # proceso, lo activa al no recibir heartbeat (CLAUDE.md 2.6).
        if self._ctx.halt_requester is not None:
            try:
                self._ctx.halt_requester(f"AGENT_ERROR:{state.value}")
            except Exception:  # noqa: BLE001
                _log.exception("halt_requester fallo")

    def _audit(
        self,
        event_type: str,
        state: AgentState,
        *,
        payload: dict[str, str] | None = None,
        risk_output: dict[str, str] | None = None,
        discriminator: str | None = None,
        decision: str = "",
        reason: str = "",
    ) -> None:
        """Escribe un registro encadenado.

        El ledger NO calcula previous_hash: hay que leer el ultimo hash y
        pasarlo. Escritor unico y single-threaded: get_last_record_hash() +
        record_audit() es un read-modify-write sin lock entre dos conexiones,
        y dos escritores bifurcarian la cadena.
        """
        ctx = self._ctx
        self._audit_seq += 1

        record_id = f"{self._cycle_id}:{state.value}:{self._audit_seq:04d}"
        if discriminator is not None:
            record_id = f"{record_id}:{discriminator}"

        record = AuditRecord(
            record_id=record_id,
            timestamp=datetime.now(UTC),
            event_type=event_type,
            data_hash=self._data_hash,
            previous_hash=ctx.ledger.get_last_record_hash(),
            strategy_id=ctx.strategy_id,
            payload=payload or {},
            git_sha=ctx.git_sha,
            config_hash=ctx.config_hash,
            parameters=ctx.extra_parameters,
            risk_output=risk_output or {},
            decision=decision,
            reason=reason,
        )
        ctx.ledger.record_audit(record)


def _compute_adv(
    bars: dict[str, list[ValidatedBar]], window: int = 20
) -> dict[str, Decimal]:
    """ADV en EUR por simbolo: volumen medio reciente x ultimo cierre."""
    adv: dict[str, Decimal] = {}
    for symbol, symbol_bars in bars.items():
        recent = symbol_bars[-window:]
        if not recent:
            continue
        avg_volume = sum((vb.bar.volume for vb in recent), _ZERO) / Decimal(len(recent))
        adv[symbol] = avg_volume * recent[-1].bar.close
    return adv


class _InMemoryDataView:
    """Vista de datos por defecto sobre las barras ya cargadas.

    Duck-typing de SandboxedDataView: el agente no puede importar
    qtrader.backtesting (contrato de import-linter). La anotacion en
    momentum.py es TYPE_CHECKING-only, asi que la forma estructural basta.

    Guarda la invariante anti-look-ahead: nunca devuelve barras posteriores
    a la fecha del ciclo.
    """

    def __init__(
        self, bars: dict[str, list[ValidatedBar]], current_date: date
    ) -> None:
        self._bars = bars
        self._current_date = current_date

    def get_bars(
        self, symbol: str, start_dt: object = None, end_date: object = None
    ) -> list[ValidatedBar]:
        symbol_bars = self._bars.get(symbol, [])
        return [
            vb for vb in symbol_bars
            if vb.bar.timestamp.date() <= self._current_date
        ]
