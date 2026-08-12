"""Test de capa rota — el mas importante de T4.3.

Verifica que las capas 2 y 3 BLOQUEAN ordenes absurdas incluso cuando
el Risk Engine (capa 1) esta deliberadamente roto y aprueba todo.

Orden absurda:
  symbol="AAAA" (no en universo)
  price=Decimal("99999")
  quantity=Decimal("10000")
  notional=Decimal("999990000") EUR (casi mil millones)

Verificaciones:
  1. BrokenRiskEngine aprueba la orden (lo hacemos explicitamente).
  2. Capa 2 (RateLimiter) la bloquea por max_notional_per_day.
  3. Capa 3 (SanityChecker) la bloquea por simbolo desconocido.
  4. Verificacion de que execution/ no importa risk/ en subprocess limpio.
"""
from __future__ import annotations

import subprocess
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path  # noqa: TCH003

from qtrader.execution.rate_limiter import ExecutionRateLimiter, RateLimiterConfig
from qtrader.execution.sanity import BrokerSanityChecker, SanityMarketState

# ---------------------------------------------------------------------------
# Orden absurda — constantes
# ---------------------------------------------------------------------------

_ABSURD_SYMBOL = "AAAA"   # no esta en el universo
_ABSURD_PRICE = Decimal("99999")
_ABSURD_QTY = Decimal("10000")
_ABSURD_NOTIONAL = _ABSURD_PRICE * _ABSURD_QTY  # 999_990_000 EUR

_ORDER_ID = "absurd-001"
_SESSION_DATE = date(2024, 1, 16)   # martes — sesion NYSE normal
_TS = datetime(2024, 1, 16, 9, 30, 0, tzinfo=UTC)

# Universo activo — AAAA NO esta
_ACTIVE_SYMBOLS = frozenset({"SPY", "QQQ", "XLK", "IEF", "GLD"})
_CLOSES = {s: Decimal("100") for s in _ACTIVE_SYMBOLS}
_ADV = {s: Decimal("500000") for s in _ACTIVE_SYMBOLS}


# ---------------------------------------------------------------------------
# BrokenRiskEngine — aprueba TODO sin evaluar nada
# ---------------------------------------------------------------------------

class BrokenRiskEngine:
    """Riskengine deliberadamente roto: aprueba cualquier orden sin restriccion.

    Simula una vulnerabilidad critica en la capa 1 para demostrar que
    las capas 2 y 3 son independientes y siguen protegiendo.
    """

    @staticmethod
    def approve_everything(
        order_id: str,
        symbol: str,
        quantity: Decimal,
        price: Decimal,
        notional: Decimal,
    ) -> bool:
        """Siempre devuelve True. Capa 1 completamente rota."""
        return True  # Bug critico deliberado: no valida nada


# ---------------------------------------------------------------------------
# Test principal
# ---------------------------------------------------------------------------

def test_broken_risk_engine_capa1_aprueba_orden_absurda() -> None:
    """Confirmar que el BrokenRiskEngine aprueba la orden absurda."""
    approved = BrokenRiskEngine.approve_everything(
        order_id=_ORDER_ID,
        symbol=_ABSURD_SYMBOL,
        quantity=_ABSURD_QTY,
        price=_ABSURD_PRICE,
        notional=_ABSURD_NOTIONAL,
    )
    assert approved is True, "El BrokenRiskEngine debe aprobar todo para que el test sea valido"


def test_capa2_bloquea_orden_absurda_incluso_si_capa1_rota(tmp_path: Path) -> None:
    """Capa 2 bloquea la orden absurda por max_notional_per_day.

    Aunque el BrokenRiskEngine la aprueba, el RateLimiter la rechaza
    porque el notional (999.990.000 EUR) supera el maximo diario (20.000 EUR).
    """
    # Confirmar que capa 1 aprueba
    assert BrokenRiskEngine.approve_everything(
        _ORDER_ID, _ABSURD_SYMBOL, _ABSURD_QTY, _ABSURD_PRICE, _ABSURD_NOTIONAL,
    )

    # Capa 2: el limite es 20.000 EUR/dia — 999.990.000 >> 20.000
    cfg = RateLimiterConfig(
        max_notional_per_day=Decimal("20000"),
        max_orders_per_day=30,
        max_orders_per_minute=5,
    )
    limiter = ExecutionRateLimiter(tmp_path / "exec.db", cfg)
    result = limiter.check_and_record(_ORDER_ID, _ABSURD_SYMBOL, _ABSURD_NOTIONAL, _TS)
    limiter.close()

    assert not result.allowed, (
        "INVARIANT VIOLATED: Capa 2 no bloqueo la orden absurda. "
        f"reason={result.reason}"
    )
    assert result.reason is not None
    assert "RATE_LIMIT_NOTIONAL_DAY" in result.reason


def test_capa3_bloquea_orden_absurda_incluso_si_capa1_rota() -> None:
    """Capa 3 bloquea la orden absurda por simbolo desconocido.

    Aunque el BrokenRiskEngine la aprueba, el SanityChecker la rechaza
    porque AAAA no esta en el universo activo.
    """
    # Confirmar que capa 1 aprueba
    assert BrokenRiskEngine.approve_everything(
        _ORDER_ID, _ABSURD_SYMBOL, _ABSURD_QTY, _ABSURD_PRICE, _ABSURD_NOTIONAL,
    )

    market_state = SanityMarketState(
        last_close=_CLOSES,
        adv=_ADV,
        active_symbols=_ACTIVE_SYMBOLS,
        trading_date=_SESSION_DATE,
        exchange="XNYS",
    )

    checker = BrokerSanityChecker()
    result = checker.check(
        order_id=_ORDER_ID,
        symbol=_ABSURD_SYMBOL,
        side="BUY",
        quantity=_ABSURD_QTY,
        price=_ABSURD_PRICE,
        notional=_ABSURD_NOTIONAL,
        market_state=market_state,
    )

    assert not result.passed, (
        "INVARIANT VIOLATED: Capa 3 no bloqueo la orden absurda. "
        f"reason={result.reason}"
    )
    assert result.reason is not None
    assert "UNKNOWN_SYMBOL" in result.reason


def test_capas_2_y_3_bloquean_independientemente(tmp_path: Path) -> None:
    """Capa 2 y Capa 3 bloquean la misma orden absurda de forma independiente.

    Se verifica que:
    - Capa 2 bloquea ANTES de que llegue a capa 3 (en el flujo normal).
    - Capa 3 bloquea INCLUSO si se bypasea la capa 2 directamente.
    """
    cfg = RateLimiterConfig(
        max_notional_per_day=Decimal("20000"),
        max_orders_per_day=30,
        max_orders_per_minute=5,
    )
    limiter = ExecutionRateLimiter(tmp_path / "exec.db", cfg)
    rate_result = limiter.check_and_record(_ORDER_ID, _ABSURD_SYMBOL, _ABSURD_NOTIONAL, _TS)
    limiter.close()

    market_state = SanityMarketState(
        last_close=_CLOSES,
        adv=_ADV,
        active_symbols=_ACTIVE_SYMBOLS,
        trading_date=_SESSION_DATE,
        exchange="XNYS",
    )
    checker = BrokerSanityChecker()
    sanity_result = checker.check(
        order_id=_ORDER_ID,
        symbol=_ABSURD_SYMBOL,
        side="BUY",
        quantity=_ABSURD_QTY,
        price=_ABSURD_PRICE,
        notional=_ABSURD_NOTIONAL,
        market_state=market_state,
    )

    # Ambas capas bloquean independientemente
    assert not rate_result.allowed, "Capa 2 debe bloquear"
    assert not sanity_result.passed, "Capa 3 debe bloquear"

    # Motivos distintos — cada capa tiene su propia razon
    assert rate_result.reason != sanity_result.reason


def test_capa3_bloquea_si_capa2_bypaseada() -> None:
    """Si alguien bypasea capa 2, capa 3 sigue bloqueando.

    Este test simula el peor caso: un atacante que evita el rate limiter
    y llega directamente a sanity. La capa 3 debe ser suficiente por si sola.
    """
    market_state = SanityMarketState(
        last_close=_CLOSES,
        adv=_ADV,
        active_symbols=_ACTIVE_SYMBOLS,
        trading_date=_SESSION_DATE,
        exchange="XNYS",
    )
    checker = BrokerSanityChecker()
    result = checker.check(
        order_id=_ORDER_ID,
        symbol=_ABSURD_SYMBOL,
        side="BUY",
        quantity=_ABSURD_QTY,
        price=_ABSURD_PRICE,
        notional=_ABSURD_NOTIONAL,
        market_state=market_state,
    )
    assert not result.passed


def test_capa2_bloquea_si_capa3_bypaseada(tmp_path: Path) -> None:
    """Si alguien bypasea capa 3, capa 2 sigue bloqueando."""
    cfg = RateLimiterConfig(max_notional_per_day=Decimal("20000"))
    limiter = ExecutionRateLimiter(tmp_path / "exec.db", cfg)
    result = limiter.check_and_record(_ORDER_ID, _ABSURD_SYMBOL, _ABSURD_NOTIONAL, _TS)
    limiter.close()
    assert not result.allowed


# ---------------------------------------------------------------------------
# Subprocess: execution/ no importa risk/
# ---------------------------------------------------------------------------

def test_execution_rate_limiter_does_not_import_risk() -> None:
    """execution.rate_limiter no puede importar qtrader.risk en ningun escenario."""
    code = (
        "import qtrader.execution.rate_limiter; "
        "import sys; "
        "assert 'qtrader.risk' not in sys.modules, "
        "f'INVARIANT VIOLATED: risk imported by execution.rate_limiter. "
        "Modules: {[m for m in sys.modules if \"risk\" in m]}'"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"execution.rate_limiter importo risk.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_execution_sanity_does_not_import_risk() -> None:
    """execution.sanity no puede importar qtrader.risk en ningun escenario."""
    code = (
        "import qtrader.execution.sanity; "
        "import sys; "
        "assert 'qtrader.risk' not in sys.modules, "
        "f'INVARIANT VIOLATED: risk imported by execution.sanity. "
        "Modules: {[m for m in sys.modules if \"risk\" in m]}'"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"execution.sanity importo risk.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_execution_sanity_does_not_import_agents() -> None:
    """execution.sanity no puede importar qtrader.agents."""
    code = (
        "import qtrader.execution.sanity; "
        "import sys; "
        "assert 'qtrader.agents' not in sys.modules, "
        "'INVARIANT VIOLATED: agents imported by execution.sanity'"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"execution.sanity importo agents.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
