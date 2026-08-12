"""Watchdog — dead man's switch independiente del proceso del agente.

Invariantes (CLAUDE.md §2.6):
  - Hilo daemon separado: no puede ser detenido por el agente.
  - Si no recibe heartbeat en max_silence_seconds → activa KillSwitch.
  - El agente llama heartbeat() ≤ cada heartbeat_interval_seconds.
  - Una vez iniciado, start() no puede llamarse dos veces.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime

from qtrader.safety.kill_switch import KillSwitch

_log = logging.getLogger(__name__)


class Watchdog:
    """Dead man's switch para el proceso del agente.

    Args:
        kill_switch:           instancia de KillSwitch a activar en timeout.
        max_silence_seconds:   tiempo máximo sin heartbeat antes de activar (default 180s).
        check_interval_seconds: con qué frecuencia comprueba el watchdog (default 10s).
    """

    def __init__(
        self,
        kill_switch: KillSwitch,
        max_silence_seconds: float = 180.0,
        check_interval_seconds: float = 10.0,
    ) -> None:
        self._kill_switch = kill_switch
        self._max_silence = max_silence_seconds
        self._check_interval = check_interval_seconds
        self._last_heartbeat: float = time.monotonic()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._started = False

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Arranca el hilo watchdog. Solo puede llamarse una vez."""
        if self._started:
            raise RuntimeError("Watchdog ya iniciado. No puede iniciarse dos veces.")
        self._started = True
        self._last_heartbeat = time.monotonic()
        self._thread = threading.Thread(
            target=self._run,
            name="watchdog",
            daemon=True,   # muere con el proceso principal; no puede detenerse desde el agente
        )
        self._thread.start()
        _log.info(
            "Watchdog iniciado (max_silence=%.0fs, check_interval=%.0fs)",
            self._max_silence,
            self._check_interval,
        )

    def heartbeat(self) -> None:
        """El agente llama este método ≤ cada heartbeat_interval_seconds para señalar que vive."""
        with self._lock:
            self._last_heartbeat = time.monotonic()

    def is_alive(self) -> bool:
        """True si el hilo watchdog está en ejecución."""
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # Hilo interno
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """Bucle principal del watchdog. Corre en hilo daemon."""
        while True:
            time.sleep(self._check_interval)
            with self._lock:
                elapsed = time.monotonic() - self._last_heartbeat
            if elapsed > self._max_silence:
                _log.critical(
                    "WATCHDOG: silencio de %.1fs > %.0fs — activando KillSwitch",
                    elapsed,
                    self._max_silence,
                )
                try:
                    self._kill_switch.activate(reason="WATCHDOG_TIMEOUT")
                except Exception as exc:  # noqa: BLE001
                    _log.critical("WATCHDOG: error activando KillSwitch: %s", exc)
                # Continuar el bucle; el agente debe leer is_active() en su ciclo
