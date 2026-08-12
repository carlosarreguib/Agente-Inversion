"""KillSwitch — fichero HALT fuera del control del proceso del agente.

Invariantes (CLAUDE.md §2):
  - El agente SOLO lee. Nunca llama activate() ni escribe el fichero.
  - is_active() no cachea: cada llamada va al disco.
  - Error de I/O → True (fail-safe, no fail-open).
  - deactivate() bloqueado en production.
  - El fichero HALT (o la tabla SQLite) es la única fuente de verdad.

Backends:
  FILE  — default. Fichero en halt_path. El SO impone permisos read-only
          al proceso del agente mediante chmod (POSIX) o ACL (Windows).
          En tests el agente intenta borrar el fichero y recibe PermissionError.
  SQLITE — fallback para Windows sin permisos de admin. Tabla halt_log con
           trigger que impide DELETE desde cualquier conexión.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

_log = logging.getLogger(__name__)

_CONFIRMATION_TOKEN = "I_UNDERSTAND_THIS_IS_IRREVERSIBLE"


class OperationNotPermitted(Exception):
    """Raised when deactivate() is called in production mode."""


class HaltBackend(str, Enum):
    FILE = "file"
    SQLITE = "sqlite"


class KillSwitch:
    """Gestiona el estado HALT del sistema.

    Args:
        halt_path:  ruta al fichero HALT (default ``data/HALT``).
        mode:       ``"paper"`` | ``"development"`` | ``"production"``.
                    Solo en paper y development se puede desactivar.
        backend:    ``HaltBackend.FILE`` (default) o ``HaltBackend.SQLITE``.
                    SQLITE se usa en Windows cuando no se pueden cambiar permisos.
    """

    def __init__(
        self,
        halt_path: str | Path = "data/HALT",
        mode: str = "paper",
        backend: HaltBackend = HaltBackend.FILE,
    ) -> None:
        self._halt_path = Path(halt_path)
        self._mode = mode
        self._backend = backend
        self._db_path = self._halt_path.with_suffix(".halt.db")

        if backend == HaltBackend.SQLITE:
            self._init_sqlite()

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    def is_active(self) -> bool:
        """Comprueba si el kill switch está activo. Sin caché. Fail-safe."""
        try:
            if self._backend == HaltBackend.FILE:
                return self._halt_path.exists()
            else:
                return self._sqlite_is_active()
        except PermissionError:
            # No podemos leer → conservador: tratar como activo
            _log.critical("KillSwitch: PermissionError leyendo HALT — tratando como activo")
            return True
        except OSError as exc:
            _log.critical("KillSwitch: I/O error leyendo HALT (%s) — tratando como activo", exc)
            return True

    def activate(self, reason: str) -> None:
        """Activa el kill switch. Puede llamarse desde cualquier componente excepto el agente.

        El agente NUNCA debe llamar a este método.
        """
        now = datetime.now(UTC).isoformat()
        if self._backend == HaltBackend.FILE:
            self._halt_path.parent.mkdir(parents=True, exist_ok=True)
            self._halt_path.write_text(f"{now}\n{reason}\n", encoding="utf-8")
        else:
            self._sqlite_activate(reason, now)
        _log.critical("KillSwitch ACTIVADO: %s (reason=%s)", now, reason)

    def deactivate(self, confirm: str = "") -> None:
        """Desactiva el kill switch.

        Solo disponible en modo paper y development.
        En production: raise OperationNotPermitted.
        Requiere confirm == _CONFIRMATION_TOKEN para ejecutar.

        Args:
            confirm: debe ser exactamente ``"I_UNDERSTAND_THIS_IS_IRREVERSIBLE"``.
        """
        if self._mode == "production":
            raise OperationNotPermitted(
                "deactivate() no está permitido en modo production. "
                "Requiere intervención humana directa sobre el fichero HALT."
            )
        if confirm != _CONFIRMATION_TOKEN:
            raise ValueError(
                f"Se requiere confirm='{_CONFIRMATION_TOKEN}' para desactivar el kill switch."
            )
        if self._backend == HaltBackend.FILE:
            if self._halt_path.exists():
                self._halt_path.unlink()
        else:
            self._sqlite_deactivate()
        _log.warning("KillSwitch DESACTIVADO en modo %s", self._mode)

    # ------------------------------------------------------------------
    # Backend SQLite (Windows sin permisos de admin)
    # ------------------------------------------------------------------

    def _init_sqlite(self) -> None:
        """Inicializa la tabla halt_log con trigger que impide DELETE."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path))
        with conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS halt_log (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    activated_at TEXT NOT NULL,
                    reason       TEXT NOT NULL,
                    active       INTEGER NOT NULL DEFAULT 1
                );

                -- Trigger que impide DELETE desde cualquier conexion.
                -- El agente no puede borrar registros; solo puede INSERT (is_active/activate).
                CREATE TRIGGER IF NOT EXISTS no_delete_halt_log
                BEFORE DELETE ON halt_log
                BEGIN
                    SELECT RAISE(ABORT, 'DELETE from halt_log is not permitted');
                END;
                """
            )
        conn.close()

    def _sqlite_is_active(self) -> bool:
        conn = sqlite3.connect(str(self._db_path))
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM halt_log WHERE active = 1"
            ).fetchone()
            return bool(row[0] > 0)
        finally:
            conn.close()

    def _sqlite_activate(self, reason: str, now: str) -> None:
        conn = sqlite3.connect(str(self._db_path))
        with conn:
            conn.execute(
                "INSERT INTO halt_log (activated_at, reason, active) VALUES (?, ?, 1)",
                (now, reason),
            )
        conn.close()

    def _sqlite_deactivate(self) -> None:
        conn = sqlite3.connect(str(self._db_path))
        with conn:
            conn.execute("UPDATE halt_log SET active = 0 WHERE active = 1")
        conn.close()
