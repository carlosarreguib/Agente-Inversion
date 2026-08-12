"""HealthServer — endpoint HTTP simple en 127.0.0.1:8765.

GET /health →
    200 {"status": "ok",  "state": "MONITORING", "nav": 15234.50, "kill_switch": false}
    503 {"status": "halt", ...}  si kill switch activo o estado ERROR

POST /halt →
    Escribe el fichero HALT en halt_path. Requiere body JSON {"reason": "..."}.
    200 {"ok": true}  si el fichero se creó.
    409 {"ok": false, "detail": "ya activo"}  si ya estaba activo.
    501 {"ok": false, "detail": "sin halt_path"}  si halt_path no está configurado.

Usa http.server de stdlib. No FastAPI, no dependencias extras.
Corre en hilo daemon; no bloquea el ciclo del agente.
Solo escucha en loopback (CLAUDE.md §2.8).
"""
from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)


class HealthHandler(BaseHTTPRequestHandler):
    """Maneja GET /health y POST /halt."""

    _status_fn: Callable[[], dict[str, Any]] = lambda: {}
    _halt_path: Path | None = None

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/health":
            self.send_response(HTTPStatus.NOT_FOUND)
            self.end_headers()
            return

        try:
            status = self.__class__._status_fn()
        except Exception:
            _log.exception("health status_fn falló")
            status = {"status": "error", "state": "ERROR", "nav": 0.0, "kill_switch": True}

        is_ok = (
            not status.get("kill_switch", False)
            and status.get("state") not in ("ERROR",)
        )
        code = HTTPStatus.OK if is_ok else HTTPStatus.SERVICE_UNAVAILABLE
        self._send_json(code, status)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/halt":
            self.send_response(HTTPStatus.NOT_FOUND)
            self.end_headers()
            return

        halt_path = self.__class__._halt_path
        if halt_path is None:
            self._send_json(HTTPStatus.NOT_IMPLEMENTED, {"ok": False, "detail": "sin halt_path"})
            return

        if halt_path.exists():
            self._send_json(HTTPStatus.CONFLICT, {"ok": False, "detail": "ya activo"})
            return

        try:
            content_length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(content_length) if content_length else b"{}"
            body = json.loads(raw or b"{}")
            reason = str(body.get("reason", "dashboard-halt"))
        except (json.JSONDecodeError, ValueError):
            reason = "dashboard-halt"

        now = datetime.now(UTC).isoformat()
        try:
            halt_path.parent.mkdir(parents=True, exist_ok=True)
            halt_path.write_text(f"{now}\n{reason}\n", encoding="utf-8")
            _log.critical("POST /halt: fichero HALT creado en %s (razón: %s)", halt_path, reason)
            self._send_json(HTTPStatus.OK, {"ok": True})
        except OSError as exc:
            _log.error("POST /halt: no se pudo escribir HALT: %s", exc)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "detail": str(exc)})

    def _send_json(self, code: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: ANN401
        pass


class HealthServer:
    """Servidor HTTP de health check corriendo en hilo daemon.

    Args:
        status_fn:  callable sin argumentos que devuelve el dict de estado.
                    Debe ser thread-safe.
        halt_path:  ruta al fichero HALT. Si se pasa, POST /halt lo crea.
                    Si es None, POST /halt devuelve 501.
        host:       dirección de bind. Siempre 127.0.0.1.
        port:       puerto de escucha (default 8765).
    """

    def __init__(
        self,
        status_fn: Callable[[], dict[str, Any]],
        halt_path: str | Path | None = None,
        host: str = "127.0.0.1",
        port: int = 8765,
    ) -> None:
        if host != "127.0.0.1":
            raise ValueError(
                "HealthServer solo puede escuchar en 127.0.0.1 (CLAUDE.md §2.8)"
            )

        resolved_halt = Path(halt_path) if halt_path is not None else None

        handler_class = type(
            "_BoundHealthHandler",
            (HealthHandler,),
            {
                "_status_fn": staticmethod(status_fn),
                "_halt_path": resolved_halt,
            },
        )
        self._server = HTTPServer((host, port), handler_class)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        addr = self._server.server_address
        return int(addr[1])

    def start(self) -> None:
        """Arranca el servidor en un hilo daemon."""
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="health-server",
        )
        self._thread.start()
        _log.info(
            "HealthServer escuchando en %s:%s",
            *self._server.server_address,
        )

    def stop(self) -> None:
        """Detiene el servidor."""
        self._server.shutdown()
        if self._thread:
            self._thread.join(timeout=5)
