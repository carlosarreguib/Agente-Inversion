"""Adaptadores puros entre contratos de modulos (T6).

Sin I/O, sin estado. Estas funciones existen porque los modulos de fases
anteriores tienen contratos deliberadamente desacoplados: PortfolioConstructor
emite TargetPosition, el Risk Engine consume ProposedOrder, y nadie en el repo
hacia la conversion hasta ahora.

No hay logica de negocio nueva: solo diferencia objetivo contra actual y
traduce tipos.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping  # noqa: TC003 — runtime: firma publica
from datetime import date  # noqa: TCH003 — runtime
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING

from qtrader.core.types import Instrument, Side  # noqa: TCH001 — runtime
from qtrader.execution.sanity import SanityMarketState
from qtrader.risk.types import CurrentPortfolio, PositionSnapshot, ProposedOrder

if TYPE_CHECKING:
    from qtrader.core.types import ValidatedBar
    from qtrader.portfolio.construction import PortfolioTarget

_ZERO = Decimal("0")


class AdapterWarning(Exception):
    """Problema con un simbolo concreto: se excluye, no aborta el ciclo."""


def build_proposed_orders(
    target: PortfolioTarget,
    current_positions: Mapping[str, Decimal],
    prices: Mapping[str, Decimal],
    instruments: Mapping[str, Instrument],
    trading_date: date,
) -> tuple[list[ProposedOrder], dict[str, Decimal], list[str]]:
    """Diferencia el PortfolioTarget contra las posiciones actuales.

    Returns:
        (proposed_orders, price_map por order_id, warnings)

    El price_map existe porque ApprovedOrder no lleva price: el sanity checker
    y la persistencia lo necesitan y derivarlo de notional/quantity perderia
    precision.
    """
    warnings: list[str] = []

    # GUARD CRITICO. construction.py:433-445 — cuando rebalance_needed es False,
    # targets replica current_weights con quantity=0. Diferenciar contra esos
    # ceros emitiria un SELL por cada posicion: liquidaria la cartera entera en
    # un dia tranquilo. Es la linea mas peligrosa de este modulo.
    if not target.rebalance_needed:
        return ([], {}, warnings)

    target_qty: dict[str, Decimal] = {t.symbol: t.quantity for t in target.targets}

    symbols = sorted(set(target_qty) | set(current_positions))

    buys: list[ProposedOrder] = []
    sells: list[ProposedOrder] = []
    price_map: dict[str, Decimal] = {}

    for symbol in symbols:
        held = current_positions.get(symbol, _ZERO)
        want = target_qty.get(symbol, _ZERO)
        delta = want - held

        if delta == _ZERO:
            continue

        price = prices.get(symbol)
        if price is None or price <= _ZERO:
            # Sin precio no se puede dimensionar. Excluir, nunca interpolar (3.4).
            warnings.append(f"NO_PRICE:{symbol}")
            continue

        instrument = instruments.get(symbol)
        if instrument is None:
            warnings.append(f"UNKNOWN_INSTRUMENT:{symbol}")
            continue

        side = Side.BUY if delta > _ZERO else Side.SELL
        quantity = abs(delta)
        order_id = f"{trading_date.isoformat()}:{symbol}:{side.value}"

        order = ProposedOrder(
            order_id=order_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            category=instrument.category,
        )
        price_map[order_id] = price

        if side == Side.SELL:
            sells.append(order)
        else:
            buys.append(order)

    # SELLs primero, luego BUYs, cada bloque ordenado por simbolo.
    # RiskEngine acumula contra topes diarios en orden de llegada
    # (engine.py:503-525), asi que el orden cambia el conjunto aprobado.
    # Vender primero libera headroom de notional y exposicion; ademas los SELL
    # estan exentos del multiplicador de nivel (engine.py:385), asi que no
    # deben quedar sin cuota por un BUY que consumio el presupuesto del dia.
    return (sells + buys, price_map, warnings)


def build_current_portfolio(
    positions: Mapping[str, Decimal],
    prices: Mapping[str, Decimal],
    instruments: Mapping[str, Instrument],
    nav: Decimal,
    peak_nav: Decimal,
    nav_open_today: Decimal,
    nav_open_week: Decimal,
    orders_today: int,
    notional_today: Decimal,
) -> CurrentPortfolio:
    """Construye el CurrentPortfolio que consume RiskEngine.evaluate()."""
    snapshots: list[PositionSnapshot] = []
    for symbol in sorted(positions):
        quantity = positions[symbol]
        if quantity == _ZERO:
            continue
        price = prices.get(symbol)
        instrument = instruments.get(symbol)
        if price is None or instrument is None:
            continue
        snapshots.append(
            PositionSnapshot(
                symbol=symbol,
                quantity=quantity,
                price=price,
                category=instrument.category,
                notional=quantity * price,
            )
        )

    return CurrentPortfolio(
        positions=tuple(snapshots),
        nav=nav,
        peak_nav=peak_nav,
        nav_open_today=nav_open_today,
        nav_open_week=nav_open_week,
        orders_today=orders_today,
        notional_today=notional_today,
    )


def build_sanity_market_states(
    instruments: Mapping[str, Instrument],
    last_close: Mapping[str, Decimal],
    adv: Mapping[str, Decimal],
    trading_date: date,
) -> dict[str, SanityMarketState]:
    """Un SanityMarketState por exchange.

    El universo mezcla XNYS y XNAS; pasar un solo calendario para todo el lote
    seria incorrecto porque sanity.check() consulta is_session() del exchange.
    """
    by_exchange: dict[str, set[str]] = {}
    for symbol, instrument in instruments.items():
        by_exchange.setdefault(instrument.exchange, set()).add(symbol)

    return {
        exchange: SanityMarketState(
            last_close=dict(last_close),
            adv=dict(adv),
            active_symbols=frozenset(symbols),
            trading_date=trading_date,
            exchange=exchange,
        )
        for exchange, symbols in by_exchange.items()
    }


def compute_data_hash(bars_by_symbol: Mapping[str, list[ValidatedBar]]) -> str:
    """Hash del snapshot de datos usado en el ciclo (invariante 3.10).

    Determinista: ordena por simbolo y timestamp antes de hashear.
    Cada decision referencia este hash.
    """
    hasher = hashlib.sha256()
    for symbol in sorted(bars_by_symbol):
        for vbar in bars_by_symbol[symbol]:
            bar = vbar.bar
            hasher.update(
                f"{symbol}|{bar.timestamp.isoformat()}|{bar.close}\n".encode()
            )
    return hasher.hexdigest()


def strmap(data: Mapping[str, object]) -> dict[str, str]:
    """Convierte un mapping a dict[str, str] para AuditRecord.

    payload, parameters y risk_output son dict[str, str]: un Decimal sin
    convertir lanza ValidationError de pydantic.
    """
    out: dict[str, str] = {}
    for key, value in data.items():
        out[key] = value.value if isinstance(value, Enum) else str(value)
    return out
