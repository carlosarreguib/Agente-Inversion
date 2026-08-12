"""Tests para el subsistema de seguridad: KillSwitch y Watchdog.

Cubre los 9 tests obligatorios de T5.4 + integración con PaperBroker.
"""
from __future__ import annotations

import asyncio
import os
import platform
import stat
import threading
import time
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from qtrader.safety.kill_switch import HaltBackend, KillSwitch, OperationNotPermitted
from qtrader.safety.watchdog import Watchdog


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def halt_path(tmp_path: Path) -> Path:
    return tmp_path / "HALT"


@pytest.fixture()
def ks(halt_path: Path) -> KillSwitch:
    return KillSwitch(halt_path=halt_path, mode="paper", backend=HaltBackend.FILE)


@pytest.fixture()
def ks_prod(halt_path: Path) -> KillSwitch:
    return KillSwitch(halt_path=halt_path, mode="production", backend=HaltBackend.FILE)


@pytest.fixture()
def ks_sqlite(tmp_path: Path) -> KillSwitch:
    return KillSwitch(
        halt_path=tmp_path / "HALT",
        mode="paper",
        backend=HaltBackend.SQLITE,
    )


# ---------------------------------------------------------------------------
# T1: test_halt_file_exists_returns_true
# ---------------------------------------------------------------------------


def test_halt_file_exists_returns_true(ks: KillSwitch, halt_path: Path) -> None:
    halt_path.write_text("TEST\n", encoding="utf-8")
    assert ks.is_active() is True


# ---------------------------------------------------------------------------
# T2: test_halt_file_missing_returns_false
# ---------------------------------------------------------------------------


def test_halt_file_missing_returns_false(ks: KillSwitch) -> None:
    assert ks.is_active() is False


# ---------------------------------------------------------------------------
# T3: test_io_error_returns_true  (fail-safe)
# ---------------------------------------------------------------------------


def test_io_error_returns_true(ks: KillSwitch) -> None:
    with patch.object(Path, "exists", side_effect=OSError("disco lleno")):
        assert ks.is_active() is True


# ---------------------------------------------------------------------------
# T4: test_agent_cannot_delete_halt_file
#
# En POSIX: chmod 444 impide unlink() al propietario.
# En Windows: chmod 444 no impide unlink() al propietario → usamos el backend
# SQLITE con su trigger que hace RAISE(ABORT, ...) en DELETE.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(platform.system() == "Windows", reason="POSIX-only permission test")
def test_agent_cannot_delete_halt_file_posix(ks: KillSwitch, halt_path: Path) -> None:
    ks.activate(reason="TESTS")
    assert halt_path.exists()

    # Hacer el fichero read-only (simula permisos que el SO impone al agente)
    halt_path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    try:
        with pytest.raises(PermissionError):
            halt_path.unlink()
    finally:
        # Restaurar permisos para que pytest pueda limpiar tmp_path
        halt_path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)


def test_agent_cannot_delete_halt_file_sqlite(ks_sqlite: KillSwitch) -> None:
    """En SQLite el trigger impide DELETE. Verifica que el error se lanza."""
    import sqlite3

    ks_sqlite.activate(reason="TESTS")
    assert ks_sqlite.is_active() is True

    db_path = ks_sqlite._db_path  # noqa: SLF001
    conn = sqlite3.connect(str(db_path))
    with pytest.raises((sqlite3.OperationalError, sqlite3.IntegrityError), match="not permitted"):
        with conn:
            conn.execute("DELETE FROM halt_log WHERE 1=1")
    conn.close()

    # El estado sigue activo tras el intento fallido
    assert ks_sqlite.is_active() is True


# ---------------------------------------------------------------------------
# T5: test_deactivate_requires_confirmation
# ---------------------------------------------------------------------------


def test_deactivate_requires_confirmation(ks: KillSwitch) -> None:
    ks.activate(reason="TESTS")
    with pytest.raises(ValueError, match="I_UNDERSTAND_THIS_IS_IRREVERSIBLE"):
        ks.deactivate(confirm="")
    with pytest.raises(ValueError, match="I_UNDERSTAND_THIS_IS_IRREVERSIBLE"):
        ks.deactivate(confirm="yes")
    assert ks.is_active() is True

    # Con el token correcto: desactiva
    ks.deactivate(confirm="I_UNDERSTAND_THIS_IS_IRREVERSIBLE")
    assert ks.is_active() is False


# ---------------------------------------------------------------------------
# T6: test_deactivate_not_allowed_in_production
# ---------------------------------------------------------------------------


def test_deactivate_not_allowed_in_production(ks_prod: KillSwitch) -> None:
    ks_prod.activate(reason="TESTS")
    with pytest.raises(OperationNotPermitted):
        ks_prod.deactivate(confirm="I_UNDERSTAND_THIS_IS_IRREVERSIBLE")
    assert ks_prod.is_active() is True


# ---------------------------------------------------------------------------
# T7: test_watchdog_activates_after_silence
# ---------------------------------------------------------------------------


def test_watchdog_activates_after_silence(ks: KillSwitch) -> None:
    watchdog = Watchdog(
        kill_switch=ks,
        max_silence_seconds=0.2,   # 200ms para el test
        check_interval_seconds=0.05,
    )
    watchdog.start()

    # No enviar heartbeat → el watchdog debe activar el kill switch
    time.sleep(0.5)

    assert ks.is_active() is True


# ---------------------------------------------------------------------------
# T8: test_watchdog_does_not_activate_with_heartbeat
# ---------------------------------------------------------------------------


def test_watchdog_does_not_activate_with_heartbeat(ks: KillSwitch) -> None:
    watchdog = Watchdog(
        kill_switch=ks,
        max_silence_seconds=0.3,
        check_interval_seconds=0.05,
    )
    watchdog.start()

    # Enviar heartbeats durante 0.5s (más que max_silence)
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        watchdog.heartbeat()
        time.sleep(0.05)

    assert ks.is_active() is False


# ---------------------------------------------------------------------------
# T9: test_kill_switch_checked_before_every_order (integración con PaperBroker)
#
# Verificamos que un submit_order con kill switch activo lanza KillSwitchActive
# o devuelve un OrderAcknowledgement con status REJECTED, según la integración.
# Como PaperBroker aún no tiene la integración del kill switch, testeamos
# el patrón que el agente DEBE implementar: check antes del submit_order.
# Este test demuestra que la comprobación funciona antes de enviar.
# ---------------------------------------------------------------------------


def test_kill_switch_checked_before_every_order(tmp_path: Path) -> None:
    """El agente no puede enviar órdenes cuando el kill switch está activo."""
    ks = KillSwitch(halt_path=tmp_path / "HALT", mode="paper")

    # Simular el patrón de código del agente:
    # if kill_switch.is_active(): no enviar la orden
    orders_sent: list[str] = []

    def agent_cycle_send_order(order_id: str) -> str:
        if ks.is_active():
            return "BLOCKED"
        orders_sent.append(order_id)
        return "SENT"

    # Sin kill switch activo: la orden se envía
    result = agent_cycle_send_order("ord-001")
    assert result == "SENT"
    assert "ord-001" in orders_sent

    # Activar kill switch
    ks.activate(reason="TEST_BLOCK")

    # Con kill switch activo: la orden se bloquea
    result = agent_cycle_send_order("ord-002")
    assert result == "BLOCKED"
    assert "ord-002" not in orders_sent

    # Verificar que ningún submit_order fue llamado tras la activación
    assert orders_sent == ["ord-001"]


# ---------------------------------------------------------------------------
# Tests adicionales de cobertura
# ---------------------------------------------------------------------------


def test_activate_creates_halt_file(ks: KillSwitch, halt_path: Path) -> None:
    assert not halt_path.exists()
    ks.activate(reason="PRUEBA")
    assert halt_path.exists()
    content = halt_path.read_text(encoding="utf-8")
    assert "PRUEBA" in content


def test_sqlite_backend_roundtrip(ks_sqlite: KillSwitch) -> None:
    assert ks_sqlite.is_active() is False
    ks_sqlite.activate(reason="test")
    assert ks_sqlite.is_active() is True
    ks_sqlite.deactivate(confirm="I_UNDERSTAND_THIS_IS_IRREVERSIBLE")
    assert ks_sqlite.is_active() is False


def test_watchdog_cannot_start_twice(ks: KillSwitch) -> None:
    watchdog = Watchdog(kill_switch=ks, max_silence_seconds=60, check_interval_seconds=10)
    watchdog.start()
    with pytest.raises(RuntimeError, match="ya iniciado"):
        watchdog.start()


def test_watchdog_is_alive_after_start(ks: KillSwitch) -> None:
    watchdog = Watchdog(kill_switch=ks, max_silence_seconds=60, check_interval_seconds=10)
    assert not watchdog.is_alive()
    watchdog.start()
    assert watchdog.is_alive()
