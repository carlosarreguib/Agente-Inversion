"""Capa de ejecucion (T4.3) — capas 2 y 3 de defensa independientes.

Capa 2: ExecutionRateLimiter — limites propios, estado en SQLite.
Capa 3: BrokerSanityChecker  — validaciones contra el mundo real.

Invariante: execution/ no importa risk/ ni agents/.
"""
from qtrader.execution.rate_limiter import (
    ExecutionRateLimiter,
    RateLimiterConfig,
    RateLimitResult,
)
from qtrader.execution.sanity import (
    BrokerSanityChecker,
    SanityConfig,
    SanityMarketState,
    SanityResult,
)

__all__ = [
    "BrokerSanityChecker",
    "ExecutionRateLimiter",
    "RateLimitResult",
    "RateLimiterConfig",
    "SanityConfig",
    "SanityMarketState",
    "SanityResult",
]
