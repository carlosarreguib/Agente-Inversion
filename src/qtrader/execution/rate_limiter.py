"""Capa 2 de defensa: ExecutionRateLimiter (T4.3).

INDEPENDIENTE del Risk Engine: tiene sus propios limites, su propio config
y su propia persistencia en SQLite.

Limites (diferentes del Risk Engine a proposito):
  max_orders_per_minute: 5
  max_orders_per_day:    30   (Risk Engine: 20)
  max_notional_per_day:  20000 EUR  (Risk Engine: 15000)

Estado: persiste en SQLite (tabla execution_log) para sobrevivir reinicios.
Al arrancar, reconstructye los contadores del dia leyendo esa tabla.

Invariante: este modulo NO importa nada de qtrader.risk.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict

_ZERO = Decimal("0")


class RateLimiterConfig(BaseModel):
    """Parametros del rate limiter. Distintos del Risk Engine (independencia)."""

    model_config = ConfigDict(frozen=True)

    max_orders_per_minute: int = 5
    max_orders_per_day: int = 30          # Risk Engine usa 20
    max_notional_per_day: Decimal = Decimal("20000")  # Risk Engine usa 15000


class RateLimitResult(BaseModel):
    """Resultado de check_and_record. Inmutable."""

    model_config = ConfigDict(frozen=True)

    allowed: bool
    reason: str | None  # None si allowed=True


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS execution_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    TEXT    NOT NULL,
    symbol      TEXT    NOT NULL,
    notional    TEXT    NOT NULL,
    recorded_at TEXT    NOT NULL
);
"""

_INSERT = """
INSERT INTO execution_log (order_id, symbol, notional, recorded_at)
VALUES (?, ?, ?, ?)
"""

_SELECT_TODAY = """
SELECT notional, recorded_at
FROM execution_log
WHERE date(recorded_at) = ?
"""

_SELECT_MINUTE = """
SELECT COUNT(*) AS cnt
FROM execution_log
WHERE recorded_at >= ?
"""


class ExecutionRateLimiter:
    """Capa 2 de defensa.

    Mantiene estado en SQLite para sobrevivir reinicios del proceso.
    check_and_record() consulta y registra atomicamente via BEGIN EXCLUSIVE.

    No importa nada de qtrader.risk — los limites son propios.
    """

    def __init__(
        self,
        db_path: str | Path,
        config: RateLimiterConfig | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._config = config or RateLimiterConfig()
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_CREATE_TABLE)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # API publica
    # ------------------------------------------------------------------

    def check_and_record(
        self,
        order_id: str,
        symbol: str,
        notional: Decimal,
        timestamp: datetime,
    ) -> RateLimitResult:
        """Comprueba los limites y, si se pasan, registra la orden.

        Si allowed=False, la orden NO se registra (no se acumula al contador).
        Operacion atomica via BEGIN EXCLUSIVE.

        Args:
            order_id:  identificador de la orden (para el log).
            symbol:    simbolo del instrumento.
            notional:  notional de la orden en EUR.
            timestamp: momento de la comprobacion (UTC).

        Returns:
            RateLimitResult(allowed, reason)
        """
        ts_str = timestamp.isoformat()
        today_str = timestamp.date().isoformat()
        one_minute_ago = (timestamp - timedelta(minutes=1)).isoformat()

        with self._conn:
            self._conn.execute("BEGIN EXCLUSIVE")

            # -- Limite por minuto --
            row = self._conn.execute(_SELECT_MINUTE, (one_minute_ago,)).fetchone()
            orders_last_minute = row[0] if row else 0
            if orders_last_minute >= self._config.max_orders_per_minute:
                self._conn.execute("ROLLBACK")
                return RateLimitResult(
                    allowed=False,
                    reason=(
                        f"RATE_LIMIT_MINUTE: {orders_last_minute} ordenes "
                        f">= {self._config.max_orders_per_minute} en el ultimo minuto"
                    ),
                )

            # -- Limites diarios --
            rows = self._conn.execute(_SELECT_TODAY, (today_str,)).fetchall()
            orders_today = len(rows)
            notional_today = sum(Decimal(r[0]) for r in rows)

            if orders_today >= self._config.max_orders_per_day:
                self._conn.execute("ROLLBACK")
                return RateLimitResult(
                    allowed=False,
                    reason=(
                        f"RATE_LIMIT_ORDERS_DAY: {orders_today} ordenes "
                        f">= {self._config.max_orders_per_day}"
                    ),
                )

            if notional_today + notional > self._config.max_notional_per_day:
                self._conn.execute("ROLLBACK")
                return RateLimitResult(
                    allowed=False,
                    reason=(
                        f"RATE_LIMIT_NOTIONAL_DAY: {notional_today + notional:.2f} EUR "
                        f"> {self._config.max_notional_per_day} EUR"
                    ),
                )

            # -- Registrar --
            self._conn.execute(_INSERT, (order_id, symbol, str(notional), ts_str))
            # COMMIT implicito en el bloque with

        return RateLimitResult(allowed=True, reason=None)

    # ------------------------------------------------------------------
    # Consultas de estado (para tests e instrumentacion)
    # ------------------------------------------------------------------

    def orders_today(self, trading_date: date) -> int:
        """Numero de ordenes registradas hoy."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM execution_log WHERE date(recorded_at) = ?",
            (trading_date.isoformat(),),
        ).fetchone()
        return row[0] if row else 0

    def notional_today(self, trading_date: date) -> Decimal:
        """Notional total registrado hoy."""
        rows = self._conn.execute(
            "SELECT notional FROM execution_log WHERE date(recorded_at) = ?",
            (trading_date.isoformat(),),
        ).fetchall()
        return sum((Decimal(r[0]) for r in rows), _ZERO)

    def orders_last_minute(self, timestamp: datetime) -> int:
        """Numero de ordenes en el ultimo minuto."""
        one_minute_ago = (timestamp - timedelta(minutes=1)).isoformat()
        row = self._conn.execute(_SELECT_MINUTE, (one_minute_ago,)).fetchone()
        return row[0] if row else 0
