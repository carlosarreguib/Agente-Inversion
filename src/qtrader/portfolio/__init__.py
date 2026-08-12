"""Portfolio construction module (T4.1).

Volatility targeting + equal risk contribution simplificado.
Restricciones: max_position, max_sector, max_region, min_position_size,
max_positions.
"""
from qtrader.portfolio.construction import (
    PortfolioConfig,
    PortfolioTarget,
    PortfolioConstructor,
)

__all__ = ["PortfolioConfig", "PortfolioTarget", "PortfolioConstructor"]
