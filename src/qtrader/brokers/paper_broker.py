"""PaperBroker — implementacion concreta de BrokerInterface (T5.2).

Simula un broker real con estado persistido en SQLite (paper_broker.db).
Implementa BrokerInterface (typing.Protocol de T5.1).

Garantias:
  - Fill price = open[T+1] ajustado por spread + slippage usando
    calculate_costs() de src/qtrader/costs.py. MISMO codigo que el backtester.
  - Si open[T+1] no esta disponible: orden queda PENDING hasta el siguiente
    dia con datos (festivos, huecos).
  - Gap-through: si open cruza un stop implicito, fill al open.
  - Dividendos y splits se procesan en advance_to() antes que los fills.
  - El balance de cash_ledger siempre cuadra: verificado en cada transaccion.
  - Estado en SQLite con WAL mode y transacciones atomicas.
  - Idempotencia de ordenes (T5.3): flujo WAL-first con order_intentions.
    Una intencion se persiste ANTES de llamar al broker. reconcile() al arrancar
    recupera cualquier estado inconsistente sin duplicar ni perder ordenes.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from qtrader.brokers.order_id import init_sequence_table, next_sequence
from qtrader.brokers.types import (
    AccountState,
    BrokerPosition,
    CancelResult,
    OrderAcknowledgement,
    OrderStatus,
)
from qtrader.core.types import Fill, Side
from qtrader.costs import CostsConfig, OrderTooSmall, adjusted_fill_price, calculate_costs

if TYPE_CHECKING:
    from qtrader.risk.types import ApprovedOrder

_log = logging.getLogger(__name__)

try:
    import sqlite3
except ImportError as exc:
    raise ImportError("sqlite3 is required for PaperBroker") from exc

_ZERO = Decimal("0")
_ONE = Decimal("1")

# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

_DDL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS order_intentions (
    client_order_id TEXT PRIMARY KEY,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    quantity        TEXT NOT NULL,
    strategy_id     TEXT NOT NULL,
    trading_date    TEXT NOT NULL,
    status          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    broker_order_id TEXT,
    error_message   TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    broker_order_id TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    quantity        TEXT NOT NULL,
    approved_qty    TEXT NOT NULL,
    status          TEXT NOT NULL,
    submitted_at    TEXT NOT NULL,
    filled_at       TEXT,
    fill_price      TEXT,
    fill_quantity   TEXT,
    commission      TEXT,
    slippage        TEXT,
    spread_cost     TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    symbol      TEXT PRIMARY KEY,
    quantity    TEXT NOT NULL,
    avg_cost    TEXT NOT NULL,
    currency    TEXT NOT NULL DEFAULT 'EUR',
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cash_ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     TEXT NOT NULL,
    amount        TEXT NOT NULL,
    reason        TEXT NOT NULL,
    balance_after TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dividends (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    ex_date         TEXT NOT NULL,
    amount_per_share TEXT NOT NULL,
    paid_date       TEXT,
    applied_at      TEXT
);

CREATE TABLE IF NOT EXISTS splits (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol     TEXT NOT NULL,
    ex_date    TEXT NOT NULL,
    ratio      TEXT NOT NULL,
    applied_at TEXT
);
"""


# ---------------------------------------------------------------------------
# Tipos de dominio del paper broker
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EquityPoint:
    date: date
    nav: Decimal
    cash: Decimal
    positions_value: Decimal


@dataclass(frozen=True)
class CorporateActionRecord:
    """Corporate action pendiente de aplicar."""

    symbol: str
    ex_date: date
    action_type: str        # "DIVIDEND" | "SPLIT"
    amount_per_share: Decimal | None   # dividendo
    ratio: Decimal | None              # split


# ---------------------------------------------------------------------------
# PaperBroker
# ---------------------------------------------------------------------------

class PaperBroker:
    """Implementa BrokerInterface. Toda la logica de fills esta aqui.

    Args:
        db_path:          ruta al fichero SQLite (paper_broker.db).
        initial_cash:     cash inicial al crear el broker.
        costs_config:     parametros del modelo de costes (T2.2).
                          Si es None: sin costes (solo para tests).
        currency:         divisa de la cuenta (default EUR).
        _testing:         si True, habilita reset(). NUNCA True en produccion.
    """

    def __init__(
        self,
        db_path: str | Path,
        initial_cash: Decimal,
        costs_config: CostsConfig | None = None,
        currency: str = "EUR",
        *,
        _testing: bool = False,
    ) -> None:
        self._db_path = Path(db_path)
        self._initial_cash = initial_cash
        self._costs_config = costs_config or CostsConfig.zero_costs()
        self._currency = currency
        self._testing = _testing
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.row_factory = sqlite3.Row
        self._init_db()
        self._pending_corporate_actions: list[CorporateActionRecord] = []
        # Caché en memoria del proveedor de datos para advance_to()
        # Inyectado externamente via set_data_provider()
        self._data: dict[str, dict[date, Decimal]] = {}  # symbol -> date -> open_price
        self._instruments: dict[str, object] = {}        # symbol -> Instrument
        self._adv: dict[str, Decimal] = {}               # symbol -> avg daily volume
        # Reconciliar intenciones pendientes de sesiones anteriores (T5.3)
        self.reconcile()

    def set_market_data(
        self,
        data: dict[str, dict[date, Decimal]],
        instruments: dict[str, object],
        adv: dict[str, Decimal] | None = None,
    ) -> None:
        """Inyecta datos de mercado para advance_to().

        Args:
            data:        {symbol: {date: open_price}} — opens para cada dia.
            instruments: {symbol: Instrument} — metadatos del universo.
            adv:         {symbol: avg_daily_volume} — para slippage.
        """
        self._data = data
        self._instruments = instruments
        self._adv = adv or {}

    def register_corporate_action(self, action: CorporateActionRecord) -> None:
        """Registra una corporate action pendiente de aplicar."""
        self._pending_corporate_actions.append(action)

    # ------------------------------------------------------------------
    # BrokerInterface — metodos async
    # ------------------------------------------------------------------

    async def submit_order(
        self,
        order: ApprovedOrder,
        client_order_id: str,
    ) -> OrderAcknowledgement:
        """Registra la orden con flujo WAL-first (T5.3).

        Orden de operaciones garantizado:
          1. INSERT order_intentions con PENDING_UNKNOWN (commit antes de broker).
          2. Llamada al "broker" (INSERT en orders con SUBMITTED).
          3. UPDATE order_intentions -> SUBMITTED.

        Si el proceso muere entre 1 y 2: reconcile() detecta PENDING_UNKNOWN
        y consulta get_order_status — si UNKNOWN, reenviar; si existe, actualizar.

        Si el proceso muere entre 2 y 3: misma logica; el broker ya tiene la orden,
        no se reenvía.

        Idempotente: INSERT OR IGNORE en orders — si client_order_id ya existe,
        no se duplica la orden.
        """
        now = datetime.now(UTC)
        now_str = now.isoformat()

        # Paso 1: persistir intencion ANTES de cualquier llamada al broker
        # INSERT OR IGNORE: si ya existe (reenvio), no sobreescribir
        with self._conn:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO order_intentions
                  (client_order_id, symbol, side, quantity, strategy_id,
                   trading_date, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    client_order_id,
                    order.symbol,
                    order.side.value,
                    str(order.approved_quantity),
                    getattr(order, "strategy_id", "unknown"),
                    now_str[:10],   # date portion
                    "PENDING_UNKNOWN",
                    now_str,
                    now_str,
                ),
            )

        # Paso 2: registrar la orden en el broker (paper: INSERT en orders)
        broker_order_id = str(uuid.uuid4())
        with self._conn:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO orders
                  (client_order_id, broker_order_id, symbol, side,
                   quantity, approved_qty, status, submitted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    client_order_id,
                    broker_order_id,
                    order.symbol,
                    order.side.value,
                    str(order.original_quantity),
                    str(order.approved_quantity),
                    OrderStatus.SUBMITTED.value,
                    now_str,
                ),
            )

        # Paso 3: actualizar intencion a SUBMITTED
        existing_broker_id = self._conn.execute(
            "SELECT broker_order_id FROM orders WHERE client_order_id = ?",
            (client_order_id,),
        ).fetchone()
        resolved_broker_id = existing_broker_id[0] if existing_broker_id else broker_order_id
        self._update_intention(client_order_id, "SUBMITTED", resolved_broker_id)

        return OrderAcknowledgement(
            client_order_id=client_order_id,
            broker_order_id=resolved_broker_id,
            status=OrderStatus.SUBMITTED,
            timestamp=now,
        )

    async def cancel_order(self, client_order_id: str) -> CancelResult:
        row = self._conn.execute(
            "SELECT status FROM orders WHERE client_order_id = ?",
            (client_order_id,),
        ).fetchone()

        if row is None:
            return CancelResult(
                client_order_id=client_order_id,
                success=False,
                reason="ORDER_NOT_FOUND",
            )

        status = OrderStatus(row["status"])
        if status in (OrderStatus.FILLED, OrderStatus.CANCELLED):
            return CancelResult(
                client_order_id=client_order_id,
                success=False,
                reason=f"CANNOT_CANCEL_{status.value}",
            )

        with self._conn:
            self._conn.execute(
                "UPDATE orders SET status = ? WHERE client_order_id = ?",
                (OrderStatus.CANCELLED.value, client_order_id),
            )

        return CancelResult(
            client_order_id=client_order_id,
            success=True,
            reason=None,
        )

    async def get_order_status(self, client_order_id: str) -> OrderStatus:
        row = self._conn.execute(
            "SELECT status FROM orders WHERE client_order_id = ?",
            (client_order_id,),
        ).fetchone()
        if row is None:
            return OrderStatus.UNKNOWN
        return OrderStatus(row["status"])

    async def get_positions(self) -> tuple[BrokerPosition, ...]:
        rows = self._conn.execute(
            "SELECT symbol, quantity, avg_cost, currency FROM positions"
        ).fetchall()
        return tuple(
            BrokerPosition(
                symbol=r["symbol"],
                quantity=Decimal(r["quantity"]),
                avg_cost=Decimal(r["avg_cost"]),
                market_value=_ZERO,       # sin precio de mercado en tiempo real aqui
                unrealized_pnl=_ZERO,
                currency=r["currency"],
            )
            for r in rows
            if Decimal(r["quantity"]) > _ZERO
        )

    async def get_account(self) -> AccountState:
        cash = self._get_cash()
        return AccountState(
            cash=cash,
            nav=cash,     # sin MTM en tiempo real; advance_to() calcula NAV
            buying_power=cash,
            currency=self._currency,
            timestamp=datetime.now(UTC),
        )

    async def get_fills_since(self, since: datetime) -> tuple[Fill, ...]:
        since_str = since.isoformat()
        rows = self._conn.execute(
            """
            SELECT client_order_id, symbol, side, fill_quantity, fill_price,
                   commission, filled_at
            FROM orders
            WHERE status = ? AND filled_at >= ?
            ORDER BY filled_at
            """,
            (OrderStatus.FILLED.value, since_str),
        ).fetchall()

        fills: list[Fill] = []
        for r in rows:
            if r["fill_price"] is None or r["fill_quantity"] is None:
                continue
            fills.append(Fill(
                client_order_id=r["client_order_id"],
                fill_id=f"fill-{r['client_order_id']}",
                symbol=r["symbol"],
                side=Side(r["side"]),
                quantity=Decimal(r["fill_quantity"]),
                price=Decimal(r["fill_price"]),
                commission=Decimal(r["commission"] or "0"),
                timestamp=datetime.fromisoformat(r["filled_at"]),
            ))
        return tuple(fills)

    # ------------------------------------------------------------------
    # Metodos adicionales (solo PaperBroker, no en el Protocol)
    # ------------------------------------------------------------------

    def reconcile(self) -> None:
        """Reconcilia intenciones pendientes al arrancar (T5.3).

        Debe llamarse en __init__ despues de crear las tablas.
        No lanza excepciones: registra warnings y continua.
        En paper broker, discrepancias generan WARNING (no HALT como en produccion).

        Algoritmo:
          1. Cargar intenciones con status != CONFIRMED y != FAILED.
          2. Para cada una: consultar el estado en la tabla orders (sync, sin I/O).
          3. Segun el estado:
             PENDING_UNKNOWN + sin fila en orders (< 24h): dejar PENDING_UNKNOWN.
             PENDING_UNKNOWN + sin fila en orders (>= 24h): marcar FAILED.
             PENDING_UNKNOWN + fila SUBMITTED en orders: actualizar a SUBMITTED.
             SUBMITTED + fila FILLED en orders: marcar CONFIRMED.
             SUBMITTED + sin fila o SUBMITTED: mantener SUBMITTED.
        """
        rows = self._conn.execute(
            """
            SELECT client_order_id, status, created_at
            FROM order_intentions
            WHERE status NOT IN ('CONFIRMED', 'FAILED')
            """
        ).fetchall()

        if not rows:
            return

        _log.info("reconcile: %d intenciones pendientes", len(rows))

        for row in rows:
            coid = row["client_order_id"]
            intention_status = row["status"]
            created_at_str = row["created_at"]
            created_at = datetime.fromisoformat(created_at_str)
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)

            # Consultar estado en tabla orders (broker local, sin I/O de red)
            order_row = self._conn.execute(
                "SELECT status FROM orders WHERE client_order_id = ?", (coid,)
            ).fetchone()
            broker_status: OrderStatus = (
                OrderStatus(order_row["status"]) if order_row else OrderStatus.UNKNOWN
            )

            if broker_status == OrderStatus.UNKNOWN:
                # El broker no tiene la orden: no llego o fue perdida
                age = datetime.now(UTC) - created_at
                if age > timedelta(hours=24):
                    self._update_intention(coid, "FAILED", error="TIMEOUT_NO_BROKER_RECORD")
                    _log.warning("reconcile: %s FAILED (>24h sin registro en broker)", coid)
                else:
                    # < 24h: dejar PENDING_UNKNOWN para que el llamador reenvie
                    _log.warning(
                        "reconcile: %s PENDING_UNKNOWN — orden sin registro en broker, "
                        "pendiente de reenvio",
                        coid,
                    )

            elif broker_status in (
                OrderStatus.SUBMITTED,
                OrderStatus.PARTIAL,
                OrderStatus.PENDING,
            ):
                if intention_status != "SUBMITTED":
                    self._update_intention(coid, "SUBMITTED")
                _log.info("reconcile: %s -> SUBMITTED (broker confirma)", coid)

            elif broker_status == OrderStatus.FILLED:
                self._update_intention(coid, "CONFIRMED")
                _log.info("reconcile: %s -> CONFIRMED (fill ya registrado)", coid)

            elif broker_status in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
                self._update_intention(
                    coid, "FAILED", error=f"BROKER_{broker_status.value}"
                )
                _log.info("reconcile: %s -> FAILED (%s)", coid, broker_status.value)

        # Verificar coherencia de posiciones (paper: warning, no HALT)
        self._reconcile_positions_warning()

    def _reconcile_positions_warning(self) -> None:
        """Compara posiciones en orders con positions. Warning si divergen."""
        # Calcular posiciones derivadas de los fills
        rows = self._conn.execute(
            "SELECT symbol, side, fill_quantity FROM orders WHERE status = ?",
            (OrderStatus.FILLED.value,),
        ).fetchall()
        derived: dict[str, Decimal] = {}
        for r in rows:
            sym = r["symbol"]
            qty = Decimal(r["fill_quantity"])
            if r["side"] == Side.BUY.value:
                derived[sym] = derived.get(sym, _ZERO) + qty
            else:
                derived[sym] = derived.get(sym, _ZERO) - qty

        stored = {
            r["symbol"]: Decimal(r["quantity"])
            for r in self._conn.execute(
                "SELECT symbol, quantity FROM positions"
            ).fetchall()
        }

        for sym, qty in derived.items():
            stored_qty = stored.get(sym, _ZERO)
            if abs(qty - stored_qty) > Decimal("0.0001"):
                _log.warning(
                    "reconcile: posicion divergente en %s — "
                    "derivada_de_fills=%s stored=%s",
                    sym, qty, stored_qty,
                )

    def _update_intention(
        self,
        client_order_id: str,
        status: str,
        broker_order_id: str | None = None,
        error: str | None = None,
    ) -> None:
        now_str = datetime.now(UTC).isoformat()
        with self._conn:
            self._conn.execute(
                """
                UPDATE order_intentions
                SET status = ?, updated_at = ?,
                    broker_order_id = COALESCE(?, broker_order_id),
                    error_message = COALESCE(?, error_message)
                WHERE client_order_id = ?
                """,
                (status, now_str, broker_order_id, error, client_order_id),
            )

    def confirm_fill_intention(self, client_order_id: str) -> None:
        """Marca la intencion como CONFIRMED tras registrar un fill.

        Llamar desde _execute_fill() al final de cada fill exitoso.
        """
        self._update_intention(client_order_id, "CONFIRMED")

    def advance_to(self, trading_date: date) -> None:
        """Avanza el tiempo a trading_date.

        Orden de operaciones:
          1. Corporate actions (dividendos y splits) del dia.
          2. Fills de ordenes SUBMITTED/PENDING con datos disponibles.
          3. Ordenes sin datos quedan PENDING para el siguiente dia.
        """
        self._process_corporate_actions(trading_date)
        self._process_fills(trading_date)

    def get_equity_curve(self) -> tuple[EquityPoint, ...]:
        """Devuelve la curva de equity calculada desde cash_ledger y posiciones.

        Nota: sin MTM continuo, NAV = cash (las posiciones se valoran al avg_cost).
        Para MTM real, usar advance_to() con precios de cierre inyectados.
        """
        rows = self._conn.execute(
            "SELECT timestamp, balance_after FROM cash_ledger ORDER BY id"
        ).fetchall()
        points: list[EquityPoint] = []
        for r in rows:
            ts = datetime.fromisoformat(r["timestamp"])
            cash = Decimal(r["balance_after"])
            points.append(EquityPoint(
                date=ts.date(),
                nav=cash,
                cash=cash,
                positions_value=_ZERO,
            ))
        return tuple(points)

    def get_pnl_by_symbol(self) -> dict[str, Decimal]:
        """PnL realizado por simbolo (suma de fills: SELL ingresos - BUY costes)."""
        rows = self._conn.execute(
            """
            SELECT symbol, side, fill_quantity, fill_price, commission
            FROM orders WHERE status = ?
            """,
            (OrderStatus.FILLED.value,),
        ).fetchall()

        pnl: dict[str, Decimal] = {}
        for r in rows:
            sym = r["symbol"]
            qty = Decimal(r["fill_quantity"])
            price = Decimal(r["fill_price"])
            comm = Decimal(r["commission"] or "0")
            if r["side"] == Side.BUY.value:
                pnl[sym] = pnl.get(sym, _ZERO) - (qty * price + comm)
            else:
                pnl[sym] = pnl.get(sym, _ZERO) + (qty * price - comm)
        return pnl

    def get_total_commission(self) -> Decimal:
        """Suma de todas las comisiones pagadas (fills completados)."""
        row = self._conn.execute(
            "SELECT SUM(CAST(commission AS REAL)) FROM orders WHERE status = ?",
            (OrderStatus.FILLED.value,),
        ).fetchone()
        if row[0] is None:
            return _ZERO
        return Decimal(str(row[0]))

    def get_num_fills(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM orders WHERE status = ?",
            (OrderStatus.FILLED.value,),
        ).fetchone()
        return int(row[0])

    def get_nav(self, closes: dict[str, Decimal] | None = None) -> Decimal:
        """Calcula NAV = cash + valor de mercado de posiciones.

        Args:
            closes: {symbol: close_price}. Si es None, usa avg_cost como proxy.
        """
        cash = self._get_cash()
        rows = self._conn.execute(
            "SELECT symbol, quantity, avg_cost FROM positions"
        ).fetchall()
        pos_value = _ZERO
        for r in rows:
            qty = Decimal(r["quantity"])
            if qty <= _ZERO:
                continue
            sym = r["symbol"]
            price = closes[sym] if closes and sym in closes else Decimal(r["avg_cost"])
            pos_value += qty * price
        return cash + pos_value

    def reset(self, initial_cash: Decimal) -> None:
        """Resetea el estado a inicial. SOLO para tests (_testing=True)."""
        if not self._testing:
            raise RuntimeError("reset() is only available in testing mode")
        with self._conn:
            self._conn.execute("DELETE FROM orders")
            self._conn.execute("DELETE FROM positions")
            self._conn.execute("DELETE FROM cash_ledger")
            self._conn.execute("DELETE FROM dividends")
            self._conn.execute("DELETE FROM splits")
            self._conn.execute("DELETE FROM order_intentions")
            self._conn.execute("UPDATE order_sequence SET next_seq = 1 WHERE id = 1")
        self._initial_cash = initial_cash
        self._pending_corporate_actions.clear()
        self._record_cash(
            amount=initial_cash,
            reason="INITIAL_CASH",
            ts=datetime.now(UTC),
        )

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # Helpers privados
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        with self._conn:
            self._conn.executescript(_DDL)
        init_sequence_table(self._conn)
        # Si no hay entradas en cash_ledger, registrar el cash inicial
        row = self._conn.execute("SELECT COUNT(*) FROM cash_ledger").fetchone()
        if row[0] == 0:
            self._record_cash(
                amount=self._initial_cash,
                reason="INITIAL_CASH",
                ts=datetime.now(UTC),
            )

    def _get_cash(self) -> Decimal:
        row = self._conn.execute(
            "SELECT balance_after FROM cash_ledger ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return _ZERO
        return Decimal(row[0])

    def _record_cash(self, amount: Decimal, reason: str, ts: datetime) -> Decimal:
        """Registra un movimiento de cash y devuelve el balance resultante."""
        current = self._get_cash()
        balance_after = current + amount
        self._conn.execute(
            """
            INSERT INTO cash_ledger (timestamp, amount, reason, balance_after)
            VALUES (?, ?, ?, ?)
            """,
            (ts.isoformat(), str(amount), reason, str(balance_after)),
        )
        return balance_after

    def _verify_cash_invariant(self) -> None:
        """Verifica que balance = initial_cash + sum(amounts). HALT si diverge."""
        row = self._conn.execute(
            "SELECT SUM(CAST(amount AS REAL)) FROM cash_ledger"
        ).fetchone()
        total = Decimal(str(row[0] or 0))
        current = self._get_cash()
        diff = abs(current - total)
        if diff > Decimal("0.01"):
            raise RuntimeError(
                f"CASH INVARIANT VIOLATED: balance={current} != sum(amounts)={total} "
                f"diff={diff}. HALT required."
            )

    def _process_fills(self, trading_date: date) -> None:
        """Procesa fills de ordenes SUBMITTED y PENDING para trading_date."""
        rows = self._conn.execute(
            """
            SELECT client_order_id, symbol, side, approved_qty
            FROM orders
            WHERE status IN (?, ?)
            """,
            (OrderStatus.SUBMITTED.value, OrderStatus.PENDING.value),
        ).fetchall()

        for row in rows:
            client_order_id = row["client_order_id"]
            symbol = row["symbol"]
            side = Side(row["side"])
            qty = Decimal(row["approved_qty"])
            symbol_opens = self._data.get(symbol, {})
            open_price = symbol_opens.get(trading_date)

            if open_price is None or open_price <= _ZERO:
                # Sin datos para este dia — orden queda PENDING
                with self._conn:
                    self._conn.execute(
                        "UPDATE orders SET status = ? WHERE client_order_id = ?",
                        (OrderStatus.PENDING.value, client_order_id),
                    )
                continue

            self._execute_fill(
                client_order_id=client_order_id,
                symbol=symbol,
                side=side,
                qty=qty,
                open_price=open_price,
                trading_date=trading_date,
            )

    def _execute_fill(
        self,
        client_order_id: str,
        symbol: str,
        side: Side,
        qty: Decimal,
        open_price: Decimal,
        trading_date: date,
    ) -> None:
        """Ejecuta un fill con el modelo de costes completo."""
        from qtrader.core.types import Bar, DataQuality, Instrument, Order, OrderType, ValidatedBar

        instrument = self._instruments.get(symbol)
        adv = self._adv.get(symbol, _ZERO)
        ts = datetime(trading_date.year, trading_date.month, trading_date.day, tzinfo=UTC)

        # Construir un Order minimo para calculate_costs()
        order = Order(
            client_order_id=client_order_id,
            symbol=symbol,
            side=side,
            quantity=qty,
            order_type=OrderType.MOO,
            timestamp=ts,
            strategy_id="paper",
        )

        # Construir un ValidatedBar con open_price para calculate_costs()
        vbar = ValidatedBar(
            bar=Bar(
                symbol=symbol,
                timestamp=ts,
                open=open_price,
                high=open_price,
                low=open_price,
                close=open_price,
                volume=adv if adv > _ZERO else Decimal("1000000"),
            ),
            quality=DataQuality.OK,
        )

        commission = _ZERO
        spread_cost = _ZERO
        slippage_val = _ZERO
        fill_price = open_price
        fill_qty = qty

        if instrument is not None and isinstance(instrument, Instrument):
            try:
                breakdown = calculate_costs(
                    order=order,
                    bar=vbar,
                    instrument=instrument,
                    config=self._costs_config,
                    avg_daily_volume=adv,
                )
                commission = breakdown.commission
                spread_cost = breakdown.spread_cost
                slippage_val = breakdown.slippage
                fill_price = adjusted_fill_price(open_price, side, breakdown.price_adjustment)
                fill_qty = breakdown.adjusted_qty
            except OrderTooSmall:
                # Orden demasiado pequeña — cancelar
                with self._conn:
                    self._conn.execute(
                        "UPDATE orders SET status = ? WHERE client_order_id = ?",
                        (OrderStatus.CANCELLED.value, client_order_id),
                    )
                return

        filled_at = ts.isoformat()

        with self._conn:
            # Actualizar la orden
            self._conn.execute(
                """
                UPDATE orders SET
                    status = ?, filled_at = ?, fill_price = ?,
                    fill_quantity = ?, commission = ?, slippage = ?, spread_cost = ?
                WHERE client_order_id = ?
                """,
                (
                    OrderStatus.FILLED.value,
                    filled_at,
                    str(fill_price),
                    str(fill_qty),
                    str(commission),
                    str(slippage_val),
                    str(spread_cost),
                    client_order_id,
                ),
            )

            # Actualizar posicion
            self._update_position(symbol, side, fill_qty, fill_price, ts)

            # Actualizar cash
            if side == Side.BUY:
                cash_delta = -(fill_qty * fill_price + commission)
                reason = f"BUY_FILL:{client_order_id}"
            else:
                cash_delta = fill_qty * fill_price - commission
                reason = f"SELL_FILL:{client_order_id}"

            self._record_cash(cash_delta, reason, ts)
            self._verify_cash_invariant()

        # Marcar intencion como CONFIRMED (paso 5 del flujo WAL, T5.3)
        self.confirm_fill_intention(client_order_id)

    def _update_position(
        self,
        symbol: str,
        side: Side,
        qty: Decimal,
        price: Decimal,
        ts: datetime,
    ) -> None:
        row = self._conn.execute(
            "SELECT quantity, avg_cost FROM positions WHERE symbol = ?",
            (symbol,),
        ).fetchone()

        ts_str = ts.isoformat()

        if side == Side.BUY:
            if row is None:
                self._conn.execute(
                    """
                    INSERT INTO positions (symbol, quantity, avg_cost, currency, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (symbol, str(qty), str(price), self._currency, ts_str),
                )
            else:
                existing_qty = Decimal(row["quantity"])
                existing_cost = Decimal(row["avg_cost"])
                new_qty = existing_qty + qty
                new_avg = (existing_qty * existing_cost + qty * price) / new_qty
                sql = "UPDATE positions SET quantity=?, avg_cost=?, updated_at=? WHERE symbol=?"
                self._conn.execute(sql, (str(new_qty), str(new_avg), ts_str, symbol))
        else:  # SELL
            if row is None:
                return  # vender sin posicion — ignorar (no deberia ocurrir)
            existing_qty = Decimal(row["quantity"])
            new_qty = existing_qty - qty
            if new_qty <= _ZERO:
                self._conn.execute(
                    "DELETE FROM positions WHERE symbol = ?", (symbol,)
                )
            else:
                self._conn.execute(
                    "UPDATE positions SET quantity = ?, updated_at = ? WHERE symbol = ?",
                    (str(new_qty), ts_str, symbol),
                )

    def _process_corporate_actions(self, trading_date: date) -> None:
        """Aplica corporate actions cuyo ex_date == trading_date."""
        remaining: list[CorporateActionRecord] = []
        for ca in self._pending_corporate_actions:
            if ca.ex_date != trading_date:
                remaining.append(ca)
                continue

            ts = datetime(trading_date.year, trading_date.month, trading_date.day, tzinfo=UTC)

            if ca.action_type == "DIVIDEND":
                self._apply_dividend(ca, ts)
            elif ca.action_type == "SPLIT":
                self._apply_split(ca, ts)

        self._pending_corporate_actions = remaining

    def _apply_dividend(self, ca: CorporateActionRecord, ts: datetime) -> None:
        if ca.amount_per_share is None:
            return
        row = self._conn.execute(
            "SELECT quantity FROM positions WHERE symbol = ?", (ca.symbol,)
        ).fetchone()
        if row is None:
            return
        qty = Decimal(row["quantity"])
        dividend_total = qty * ca.amount_per_share
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO dividends (symbol, ex_date, amount_per_share, applied_at)
                VALUES (?, ?, ?, ?)
                """,
                (ca.symbol, ca.ex_date.isoformat(), str(ca.amount_per_share), ts.isoformat()),
            )
            self._record_cash(dividend_total, f"DIVIDEND:{ca.symbol}", ts)

    def _apply_split(self, ca: CorporateActionRecord, ts: datetime) -> None:
        if ca.ratio is None:
            return
        row = self._conn.execute(
            "SELECT quantity, avg_cost FROM positions WHERE symbol = ?", (ca.symbol,)
        ).fetchone()
        if row is None:
            return
        old_qty = Decimal(row["quantity"])
        old_cost = Decimal(row["avg_cost"])
        new_qty = old_qty * ca.ratio
        new_cost = old_cost / ca.ratio
        with self._conn:
            self._conn.execute(
                "UPDATE positions SET quantity = ?, avg_cost = ?, updated_at = ? WHERE symbol = ?",
                (str(new_qty), str(new_cost), ts.isoformat(), ca.symbol),
            )
            self._conn.execute(
                """
                INSERT INTO splits (symbol, ex_date, ratio, applied_at)
                VALUES (?, ?, ?, ?)
                """,
                (ca.symbol, ca.ex_date.isoformat(), str(ca.ratio), ts.isoformat()),
            )
