"""Proveedor sintético multi-símbolo para backtesting determinista (T2.3).

Cada símbolo recibe una seed derivada de la semilla maestra:
    seed(symbol) = base_seed + (hash(symbol) % 10_000)

Esto garantiza:
  - Reproducibilidad exacta: misma semilla → misma serie en cualquier máquina.
  - Independencia entre símbolos: cada ETF sintético tiene su propia trayectoria.
  - Sin dependencia de datos externos: solo stdlib + Decimal.

Los parámetros drift y volatilidad se configuran por símbolo o se usan defaults.
"""
from __future__ import annotations

import hashlib
import random
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from qtrader.core.types import Bar, CorporateAction, DataQuality, ValidatedBar
from qtrader.data.provider import (
    MarketDataProvider,  # noqa: TCH001 — usado en verificacion de Protocol
)

_TICK = Decimal("0.01")
_ONE = Decimal("1")
_ZERO = Decimal("0")


def _d(f: float) -> Decimal:
    return Decimal(str(f))


class SyntheticMultiProvider:
    """Proveedor de datos sintéticos con series independientes por símbolo.

    Implementa MarketDataProvider. Sin I/O, sin red, sin estado global.
    Mismas entradas → misma salida (invariante §2.2).

    Args:
        base_seed:    semilla maestra. Cada símbolo deriva su propia seed.
        start_date:   fecha de inicio de las series generadas.
        symbol_params: dict symbol → dict con 'drift', 'vol', 'base_price'.
                       Símbolos sin entrada usan los defaults.
        default_drift:      drift diario por defecto (log-return medio).
        default_vol:        volatilidad diaria por defecto (desviación estándar).
        default_base_price: precio inicial por defecto.
    """

    def __init__(
        self,
        base_seed: int = 42,
        start_date: datetime | None = None,
        symbol_params: dict[str, dict[str, Any]] | None = None,
        default_drift: float = 0.0003,
        default_vol: float = 0.012,
        default_base_price: float = 100.0,
    ) -> None:
        self._base_seed = base_seed
        self._start_date = start_date or datetime(2022, 1, 3, 0, 0, 0, tzinfo=UTC)
        self._symbol_params: dict[str, dict[str, Any]] = symbol_params or {}
        self._default_drift = default_drift
        self._default_vol = default_vol
        self._default_base_price = default_base_price

    def _seed_for(self, symbol: str) -> int:
        # hashlib.md5 es deterministico entre procesos (a diferencia de hash() de Python).
        digest = int(hashlib.md5(symbol.encode(), usedforsecurity=False).hexdigest(), 16)
        return self._base_seed + (digest % 10_000)

    def get_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        as_of: datetime,  # noqa: ARG002 — requerido por Protocol; sin look-ahead en sintéticos
    ) -> list[ValidatedBar]:
        num_days = (end.date() - self._start_date.date()).days + 2
        raw_bars = self._generate_bars(symbol, num_days)
        return [
            ValidatedBar(bar=b, quality=DataQuality.OK)
            for b in raw_bars
            if start <= b.timestamp <= end
        ]

    def get_corporate_actions(
        self,
        symbol: str,  # noqa: ARG002
        start: datetime,  # noqa: ARG002
        end: datetime,  # noqa: ARG002
    ) -> list[CorporateAction]:
        return []

    def _generate_bars(self, symbol: str, num_bars: int) -> list[Bar]:
        params = self._symbol_params.get(symbol, {})
        drift = float(params.get("drift", self._default_drift))
        vol = float(params.get("vol", self._default_vol))
        base_price = Decimal(str(params.get("base_price", self._default_base_price)))

        rng = random.Random(self._seed_for(symbol))
        bars: list[Bar] = []
        prev_close = base_price

        for i in range(num_bars):
            bar = _make_bar(rng, symbol, i, prev_close, self._start_date, drift, vol)
            bars.append(bar)
            prev_close = bar.close

        return bars


def _make_bar(
    rng: random.Random,
    symbol: str,
    day_index: int,
    prev_close: Decimal,
    start_ts: datetime,
    drift: float,
    vol: float,
) -> Bar:
    ret = _d(rng.gauss(drift, vol))
    close_raw = prev_close * (_ONE + ret)
    close = max(_TICK, close_raw).quantize(_TICK, rounding=ROUND_HALF_UP)

    gap = _d(rng.gauss(0.0, vol * 0.25))
    open_raw = prev_close * (_ONE + gap)
    open_ = max(_TICK, open_raw).quantize(_TICK, rounding=ROUND_HALF_UP)

    spread = abs(_d(rng.gauss(0.0, vol * 0.6))) * prev_close
    hi_extra = spread * abs(_d(rng.gauss(0.15, 0.20)))
    lo_extra = spread * abs(_d(rng.gauss(0.15, 0.20)))

    high = (max(open_, close) + hi_extra).quantize(_TICK, rounding=ROUND_HALF_UP)
    low = max(_TICK, min(open_, close) - lo_extra).quantize(_TICK, rounding=ROUND_HALF_UP)

    if low > high:
        low, high = high, low

    volume = Decimal(str(rng.randint(500_000, 2_000_000)))
    ts = start_ts + timedelta(days=day_index)

    return Bar(
        symbol=symbol,
        timestamp=ts,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


# Verificación estática del Protocol
_: MarketDataProvider = SyntheticMultiProvider()
