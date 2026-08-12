"""Tests de independencia del Risk Engine (T4.2).

Verifica que el modulo qtrader.risk.engine:
  1. No importa qtrader.portfolio (aislamiento de capas).
  2. No importa qtrader.brokers, qtrader.agents, etc.
  3. Puede evaluarse incluso si el constructor de portfolios esta roto.
  4. El test del subprocess es el test mas importante del fichero.
"""
from __future__ import annotations

import subprocess
import sys
from decimal import Decimal

from qtrader.core.types import InstrumentCategory, Side
from qtrader.risk import (
    CurrentPortfolio,
    MarketState,
    ProposedOrder,
    RiskConfig,
    RiskEngine,
    RiskLevel,
)

# ---------------------------------------------------------------------------
# Test mas importante: subprocess limpio — assert "portfolio" not in sys.modules
# ---------------------------------------------------------------------------

def test_import_risk_engine_does_not_import_portfolio() -> None:
    """El modulo risk.engine no puede importar portfolio en ningun escenario.

    Se ejecuta en un subprocess limpio para garantizar que no hay estado
    heredado del proceso de test padre. Si 'portfolio' aparece en sys.modules
    tras importar qtrader.risk.engine, el test falla con un error explicito.
    """
    code = (
        "import qtrader.risk.engine; "
        "import sys; "
        "assert 'qtrader.portfolio' not in sys.modules, "
        "f'INVARIANT VIOLATED: portfolio imported by risk engine. "
        "Modules: {[m for m in sys.modules if \"portfolio\" in m]}'"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"Risk engine importo portfolio.\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )


def test_import_risk_engine_does_not_import_brokers() -> None:
    """risk.engine no puede importar brokers."""
    code = (
        "import qtrader.risk.engine; "
        "import sys; "
        "assert 'qtrader.brokers' not in sys.modules, "
        "'INVARIANT VIOLATED: brokers imported by risk engine'"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"Risk engine importo brokers.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_import_risk_engine_does_not_import_agents() -> None:
    """risk.engine no puede importar agents."""
    code = (
        "import qtrader.risk.engine; "
        "import sys; "
        "assert 'qtrader.agents' not in sys.modules, "
        "'INVARIANT VIOLATED: agents imported by risk engine'"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"Risk engine importo agents.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_import_risk_engine_does_not_import_execution() -> None:
    """risk.engine no puede importar execution."""
    code = (
        "import qtrader.risk.engine; "
        "import sys; "
        "assert 'qtrader.execution' not in sys.modules, "
        "'INVARIANT VIOLATED: execution imported by risk engine'"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"Risk engine importo execution.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


# ---------------------------------------------------------------------------
# Test: Risk Engine funciona aunque el portfolio constructor este roto
# ---------------------------------------------------------------------------

def test_risk_engine_evaluates_without_portfolio_constructor() -> None:
    """RiskEngine.evaluate() no depende de PortfolioConstructor.

    Se verifica que el Risk Engine puede evaluar ordenes usando solo
    sus propios tipos (CurrentPortfolio), sin importar portfolio.construction.
    La verificacion de ausencia de portfolio se hace via subprocess (test separado);
    aqui verificamos que el evaluate funciona correctamente.
    """
    portfolio = CurrentPortfolio(
        positions=(),
        nav=Decimal("15000"),
        peak_nav=Decimal("15000"),
        nav_open_today=Decimal("15000"),
        nav_open_week=Decimal("15000"),
        orders_today=0,
        notional_today=Decimal("0"),
    )
    orders = [
        ProposedOrder(
            order_id="ind-001",
            symbol="SPY",
            side=Side.BUY,
            quantity=Decimal("10"),
            price=Decimal("100"),
            category=InstrumentCategory.REGION,
        )
    ]
    market = MarketState(
        trading_date=__import__("datetime").date(2024, 1, 15),
        days_below_threshold=0,
        is_market_open=True,
    )
    result = RiskEngine.evaluate(portfolio, orders, market, RiskConfig())
    assert result.level == RiskLevel.NORMAL
    # El subprocess test garantiza la ausencia de portfolio en un proceso limpio.
    # Aqui solo verificamos que RiskEngine.evaluate devuelve un resultado valido
    # sin requerir ninguna clase de portfolio.construction.
    assert result.approved_orders or result.rejected_orders  # una de las dos


def test_risk_config_independent_of_portfolio_config() -> None:
    """RiskConfig y PortfolioConfig son tipos distintos e independientes.

    RiskConfig no hereda ni importa PortfolioConfig.
    """
    from qtrader.risk.config import RiskConfig as RC
    cfg = RC()
    # RiskConfig tiene sus propios parametros — no es un alias de PortfolioConfig
    assert hasattr(cfg, "max_order_notional")
    assert hasattr(cfg, "max_daily_loss")
    assert hasattr(cfg, "recovery_days")
    # PortfolioConfig tiene estos; RiskConfig NO (son capas separadas)
    assert not hasattr(cfg, "vol_fallback")
    assert not hasattr(cfg, "rebalance_threshold")
