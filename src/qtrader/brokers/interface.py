"""BrokerInterface — contrato estructural común a todos los brokers (T5.1).

Paper broker (T5.2) e IBKR (fase 11) implementan este Protocol.
No hay lógica aquí: solo firmas y docstrings de contrato.

Invariante de idempotencia (CLAUDE.md §2.4):
    El llamador genera client_order_id ANTES de llamar a submit_order.
    El WAL persiste la intención de la orden antes de la llamada.
    Para reenviar, consultar primero get_order_status(client_order_id):
    si ya existe en el broker, no reenviar.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from datetime import datetime

    from qtrader.brokers.types import (
        AccountState,
        BrokerPosition,
        CancelResult,
        OrderAcknowledgement,
        OrderStatus,
    )
    from qtrader.core.types import Fill
    from qtrader.risk.types import ApprovedOrder


@runtime_checkable
class BrokerInterface(Protocol):
    """Contrato estructural para implementaciones de broker.

    `@runtime_checkable` permite isinstance() checks en tests y en el
    kill-switch (verifica que el objeto inyectado cumple el contrato
    antes de aceptar órdenes reales).

    Todas las operaciones son async: la latencia de red no es cero
    ni siquiera en paper trading simulado con asyncio.
    """

    async def submit_order(
        self,
        order: ApprovedOrder,
        client_order_id: str,
    ) -> OrderAcknowledgement:
        """Envía una orden al broker y devuelve el acuse de recibo.

        El llamador DEBE:
          1. Generar client_order_id de forma determinista antes de llamar.
          2. Persistir la intención en el WAL antes de llamar.
          3. Consultar get_order_status(client_order_id) si hay duda de
             si la orden ya fue enviada — nunca reenviar sin consultar.

        El broker asocia client_order_id con su broker_order_id interno.
        broker_order_id puede ser None si el broker no lo confirma de forma
        síncrona; se resuelve con get_order_status en el siguiente ciclo.

        Args:
            order:            Orden aprobada por el Risk Engine (capa 1).
            client_order_id:  ID determinista generado por el llamador.

        Returns:
            OrderAcknowledgement con el estado inicial de la orden.
        """
        ...

    async def cancel_order(
        self,
        client_order_id: str,
    ) -> CancelResult:
        """Solicita la cancelación de una orden pendiente.

        El resultado es best-effort: si la orden ya fue ejecutada
        (FILLED), success=False y reason indica el motivo.

        Args:
            client_order_id: ID de la orden a cancelar.

        Returns:
            CancelResult(success, reason).
        """
        ...

    async def get_order_status(
        self,
        client_order_id: str,
    ) -> OrderStatus:
        """Consulta el estado actual de una orden por client_order_id.

        El broker indexa por client_order_id, no por broker_order_id.
        Devuelve UNKNOWN si el broker no tiene registro de esa orden.

        Args:
            client_order_id: ID generado por el llamador en submit_order.

        Returns:
            OrderStatus actual.
        """
        ...

    async def get_positions(self) -> tuple[BrokerPosition, ...]:
        """Devuelve todas las posiciones abiertas según el broker.

        Esta es la fuente de verdad para reconciliación (CLAUDE.md §2.5).
        Discrepancia con la caché local > tolerancia → HALT.

        Returns:
            Tuple inmutable de BrokerPosition.
        """
        ...

    async def get_account(self) -> AccountState:
        """Devuelve el estado de la cuenta según el broker.

        Returns:
            AccountState con cash, NAV y buying_power actuales.
        """
        ...

    async def get_fills_since(
        self,
        since: datetime,
    ) -> tuple[Fill, ...]:
        """Devuelve todos los fills ocurridos desde `since` (inclusive).

        Usado en reconciliación al arrancar y periódicamente.

        Args:
            since: Timestamp UTC de inicio del intervalo.

        Returns:
            Tuple inmutable de Fill, ordenado por timestamp ascendente.
        """
        ...
