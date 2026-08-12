"""Capa 3 de defensa: BrokerSanityChecker (T4.3).

INDEPENDIENTE del Risk Engine Y del rate limiter.
No conoce RiskConfig ni RateLimiterConfig.

Validaciones (en orden):
  a. Simbolo en el universo activo.
     Simbolo desconocido → rechazar siempre.
  b. Precio dentro de ±10 % del ultimo close conocido.
     Sin close disponible → rechazar (no asumir nada).
  c. Tamano <= 10 % del ADV estimado del instrumento.
     Sin ADV disponible → fallback conservador: max 2000 EUR.
  d. Mercado abierto para ese simbolo en ese momento.
     Usa exchange_calendars.

Invariante: este modulo NO importa nada de qtrader.risk.
"""
from __future__ import annotations

from datetime import date  # noqa: TCH003
from decimal import Decimal

import exchange_calendars as xcals  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict

_ZERO = Decimal("0")
_ONE = Decimal("1")

# Fallback notional maximo cuando no hay ADV disponible
_ADV_FALLBACK_EUR = Decimal("2000")


class SanityConfig(BaseModel):
    """Parametros del sanity checker. Propios e independientes."""

    model_config = ConfigDict(frozen=True)

    max_price_deviation: Decimal = Decimal("0.10")  # ±10 % del ultimo close
    max_adv_fraction: Decimal = Decimal("0.10")     # 10 % del ADV
    adv_fallback_eur: Decimal = _ADV_FALLBACK_EUR   # si no hay ADV


class SanityMarketState(BaseModel):
    """Estado del mercado necesario para las validaciones de sanidad.

    Inmutable. Se construye fuera de la capa 3; el checker lo consume.

    Campos:
        last_close:     {symbol: precio} — cierre del dia anterior.
        adv:            {symbol: EUR} — ADV estimado en EUR. Puede estar vacio.
        active_symbols: simbolos del universo activos en trading_date.
        trading_date:   fecha de la sesion actual.
        exchange:       calendario de exchange_calendars (ej. "XNYS").
    """

    model_config = ConfigDict(frozen=True)

    last_close: dict[str, Decimal]
    adv: dict[str, Decimal]
    active_symbols: frozenset[str]
    trading_date: date
    exchange: str = "XNYS"


class SanityResult(BaseModel):
    """Resultado de check. Inmutable."""

    model_config = ConfigDict(frozen=True)

    passed: bool
    reason: str | None  # None si passed=True


class BrokerSanityChecker:
    """Capa 3 de defensa.

    Sin estado mutable. check() es una funcion casi pura:
    el unico estado externo que lee es exchange_calendars (datos estaticos).

    No importa nada de qtrader.risk ni de qtrader.execution.rate_limiter.
    """

    def __init__(self, config: SanityConfig | None = None) -> None:
        self._config = config or SanityConfig()

    def check(
        self,
        order_id: str,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        notional: Decimal,
        market_state: SanityMarketState,
    ) -> SanityResult:
        """Valida la orden contra la realidad del mercado.

        Las validaciones son secuenciales en el orden de la especificacion.
        El primer fallo retorna inmediatamente.

        Args:
            order_id:     identificador de la orden (para logs).
            symbol:       simbolo del instrumento.
            side:         "BUY" | "SELL".
            quantity:     cantidad de acciones/participaciones.
            price:        precio de la orden.
            notional:     quantity * price.
            market_state: estado del mercado en este momento.

        Returns:
            SanityResult(passed, reason)
        """
        cfg = self._config

        # a. Simbolo en universo
        if symbol not in market_state.active_symbols:
            return SanityResult(
                passed=False,
                reason=f"UNKNOWN_SYMBOL: {symbol!r} no esta en el universo activo",
            )

        # b. Precio dentro de ±10 % del ultimo close
        last_close = market_state.last_close.get(symbol)
        if last_close is None or last_close <= _ZERO:
            return SanityResult(
                passed=False,
                reason=f"NO_CLOSE_PRICE: no hay cierre disponible para {symbol!r}",
            )
        deviation = abs(price - last_close) / last_close
        if deviation > cfg.max_price_deviation:
            return SanityResult(
                passed=False,
                reason=(
                    f"PRICE_DEVIATION: {symbol!r} precio={price} cierre={last_close} "
                    f"desviacion={deviation:.4f} > {cfg.max_price_deviation}"
                ),
            )

        # c. Tamano <= 10 % del ADV (solo para BUY — no bloqueamos cierres por ADV)
        if side == "BUY":
            adv = market_state.adv.get(symbol)
            max_notional = (
                adv * cfg.max_adv_fraction if adv is not None and adv > _ZERO
                else cfg.adv_fallback_eur
            )
            if notional > max_notional:
                return SanityResult(
                    passed=False,
                    reason=(
                        f"ADV_EXCEEDED: {symbol!r} notional={notional:.2f} EUR "
                        f"> max={max_notional:.2f} EUR "
                        f"(ADV={'N/A' if adv is None else str(adv)}, "
                        f"fallback={'si' if adv is None else 'no'})"
                    ),
                )

        # d. Mercado abierto
        try:
            cal = xcals.get_calendar(market_state.exchange)
            if not cal.is_session(market_state.trading_date):
                return SanityResult(
                    passed=False,
                    reason=(
                        f"MARKET_CLOSED: {market_state.trading_date} no es sesion "
                        f"en {market_state.exchange!r}"
                    ),
                )
        except Exception as exc:
            return SanityResult(
                passed=False,
                reason=f"CALENDAR_ERROR: {exc}",
            )

        return SanityResult(passed=True, reason=None)
