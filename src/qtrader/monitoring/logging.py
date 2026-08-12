"""Logging estructurado con structlog.

Formato JSON Lines en producción (QTRADER_ENV=production),
pretty-print en cualquier otro entorno.

Campos obligatorios en cada evento (enlazados en configure_logging):
    timestamp, level, logger, event,
    trading_date, cycle_id, agent_state, git_sha.

El RedactProcessor elimina strings que parezcan credenciales
(regex [A-Za-z0-9]{32,}) antes de la emisión.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import Any

import structlog
from structlog.types import EventDict, WrappedLogger

_KEY_LIKE = re.compile(r"[A-Za-z0-9]{32,}")

# Claves que NUNCA se redactan aunque sean largas (e.g., git SHA de 40 chars,
# hashes de datos, cadenas de configuración legítimas conocidas).
_SAFE_KEYS = frozenset({"git_sha", "data_hash", "previous_hash", "config_hash", "record_id"})


def _get_git_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0 and (sha := result.stdout.strip()):
            return sha
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return "unknown"


_GIT_SHA: str = _get_git_sha()


class RedactProcessor:
    """Redacta strings de ≥32 chars alfanuméricos que no sean claves seguras."""

    def __call__(
        self, _logger: WrappedLogger, _method: str, event_dict: EventDict
    ) -> EventDict:
        for key, value in list(event_dict.items()):
            if key in _SAFE_KEYS:
                continue
            if isinstance(value, str) and _KEY_LIKE.search(value):
                event_dict[key] = "[REDACTED]"
        return event_dict


def _add_git_sha(
    _logger: WrappedLogger, _method: str, event_dict: EventDict
) -> EventDict:
    event_dict.setdefault("git_sha", _GIT_SHA)
    return event_dict


def _add_defaults(
    _logger: WrappedLogger, _method: str, event_dict: EventDict
) -> EventDict:
    """Rellena campos obligatorios con placeholder si no los inyectó el llamante."""
    event_dict.setdefault("trading_date", "unknown")
    event_dict.setdefault("cycle_id", "unknown")
    event_dict.setdefault("agent_state", "unknown")
    return event_dict


def configure_logging(level: str = "INFO") -> None:
    """Configura structlog para toda la aplicación. Llamar una sola vez al arrancar.

    Producción (QTRADER_ENV=production): JSON Lines en stdout.
    Cualquier otro entorno: pretty-print con colores.
    """
    is_production = os.environ.get("QTRADER_ENV", "development") == "production"

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        _add_defaults,
        _add_git_sha,
        RedactProcessor(),
    ]

    if is_production:
        renderer: Any = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Devuelve un logger structlog enlazado al nombre dado."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
