"""AlertManager — alertas no bloqueantes con dos canales.

Canal 1 (siempre): logging.CRITICAL + append a data/alerts.log.
Canal 2 (opcional): POST a Telegram si TELEGRAM_BOT_TOKEN y
                    TELEGRAM_CHAT_ID están en el entorno.

Condiciones obligatorias:
    KILL_SWITCH_ACTIVATED   — siempre
    RISK_LEVEL_CHANGED      — cuando el nivel SUBE (no al bajar)
    DRAWDOWN_WARNING        — drawdown > 10 % (antes del HALT)
    DATA_FROZEN             — mismo close N días en > 20 % del universo
    BROKER_RECONCILIATION_FAILED — discrepancia de posiciones
    AGENT_ERROR             — cualquier estado ERROR

El envío es en hilo daemon para no bloquear el ciclo del agente.
Si falla, loggea y continúa.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

_DEFAULT_ALERTS_LOG = Path("data/alerts.log")
_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


class AlertCondition(StrEnum):
    KILL_SWITCH_ACTIVATED = "KILL_SWITCH_ACTIVATED"
    RISK_LEVEL_CHANGED = "RISK_LEVEL_CHANGED"
    DRAWDOWN_WARNING = "DRAWDOWN_WARNING"
    DATA_FROZEN = "DATA_FROZEN"
    BROKER_RECONCILIATION_FAILED = "BROKER_RECONCILIATION_FAILED"
    AGENT_ERROR = "AGENT_ERROR"


class AlertManager:
    """Envía alertas por los canales configurados sin bloquear el llamante."""

    def __init__(
        self,
        alerts_log: str | Path = _DEFAULT_ALERTS_LOG,
        telegram_token: str | None = None,
        telegram_chat_id: str | None = None,
    ) -> None:
        self._alerts_log = Path(alerts_log)
        self._alerts_log.parent.mkdir(parents=True, exist_ok=True)

        # Canales Telegram: parámetros explícitos o variables de entorno.
        self._token = telegram_token or os.environ.get("TELEGRAM_BOT_TOKEN")
        self._chat_id = telegram_chat_id or os.environ.get("TELEGRAM_CHAT_ID")

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    def fire(
        self,
        condition: AlertCondition,
        message: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Dispara una alerta de forma no bloqueante."""
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "condition": condition.value,
            "message": message,
            **(extra or {}),
        }
        # Siempre sincrónico: log + fichero local (no puede fallar el ciclo)
        self._write_local(payload)

        # Telegram en hilo daemon para no bloquear
        if self._token and self._chat_id:
            t = threading.Thread(
                target=self._send_telegram,
                args=(payload,),
                daemon=True,
            )
            t.start()

    # ------------------------------------------------------------------
    # Helpers de condiciones específicas
    # ------------------------------------------------------------------

    def kill_switch_activated(self) -> None:
        self.fire(AlertCondition.KILL_SWITCH_ACTIVATED, "Kill switch activado")

    def risk_level_changed(self, previous: str, current: str) -> None:
        self.fire(
            AlertCondition.RISK_LEVEL_CHANGED,
            f"Nivel de riesgo subió de {previous} a {current}",
            {"previous": previous, "current": current},
        )

    def drawdown_warning(self, drawdown: float) -> None:
        self.fire(
            AlertCondition.DRAWDOWN_WARNING,
            f"Drawdown {drawdown:.1%} supera el umbral del 10%",
            {"drawdown": drawdown},
        )

    def data_frozen(self, pct_frozen: float, n_days: int) -> None:
        self.fire(
            AlertCondition.DATA_FROZEN,
            f"{pct_frozen:.1%} del universo sin cambios en {n_days} días",
            {"pct_frozen": pct_frozen, "n_days": n_days},
        )

    def reconciliation_failed(self, detail: str) -> None:
        self.fire(
            AlertCondition.BROKER_RECONCILIATION_FAILED,
            f"Reconciliación fallida: {detail}",
            {"detail": detail},
        )

    def agent_error(self, error_msg: str) -> None:
        self.fire(
            AlertCondition.AGENT_ERROR,
            f"Error del agente: {error_msg}",
            {"error": error_msg},
        )

    # ------------------------------------------------------------------
    # Canales internos
    # ------------------------------------------------------------------

    def _write_local(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False)
        _log.critical("ALERT %s: %s", payload["condition"], payload["message"])
        try:
            with self._alerts_log.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError as exc:
            _log.error("No se pudo escribir alerts.log: %s", exc)

    def _send_telegram(self, payload: dict[str, Any]) -> None:
        if not (self._token and self._chat_id):
            return
        text = f"[QTRADER ALERT]\n{payload['condition']}\n{payload['message']}"
        url = _TELEGRAM_API.format(token=self._token)
        data = urllib.parse.urlencode({"chat_id": self._chat_id, "text": text}).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                if resp.status != 200:
                    _log.error("Telegram API devolvió %s", resp.status)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            _log.error("Envío Telegram fallido (se ignora): %s", exc)
