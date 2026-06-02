"""CAN bus transport (SocketCAN via python-can).

Unlike RS485, CAN is message-oriented: each `recv()` returns one CAN frame with
an arbitration ID and up to 8 data bytes. We therefore expose a distinct
`recv_message()` API rather than shoehorning bytes into the `Transport` protocol.

`python-can` is an optional dependency — imported lazily so a TCP-gateway-only
install doesn't need it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from jkbms2mqtt.transport.backoff import connect_with_backoff as _connect_with_backoff

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CanMessage:
    """One received CAN frame."""

    arbitration_id: int
    data: bytes
    is_extended: bool


@dataclass
class CanBusTransport:
    """SocketCAN bus reader. Reads one CAN frame at a time via asyncio.

    `channel` is typically `can0`. `interface` is `socketcan` on Linux/HA.
    Recv timeout is per-call.
    """

    channel: str = "can0"
    interface: str = "socketcan"

    _bus: object = field(default=None, init=False, repr=False)
    _factory: object = field(default=None, init=False, repr=False)

    @property
    def is_connected(self) -> bool:
        return self._bus is not None

    async def connect(self) -> None:
        if self._bus is not None:
            return
        factory = self._factory or _default_factory()
        logger.info("Opening CAN bus interface=%s channel=%s", self.interface, self.channel)
        # Bus creation is synchronous in python-can; run it off the event loop.
        loop = asyncio.get_running_loop()
        self._bus = await loop.run_in_executor(
            None, lambda: factory(channel=self.channel, interface=self.interface)
        )
        logger.info("CAN bus open")

    async def aclose(self) -> None:
        bus = self._bus
        self._bus = None
        if bus is None:
            return
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, bus.shutdown)
        except Exception:  # pragma: no cover - python-can shutdown is best-effort
            pass

    async def recv_message(self, *, timeout_s: float) -> CanMessage | None:
        """Block up to *timeout_s* for the next CAN frame.

        Returns None on timeout. Raises `ConnectionError` if the bus is not open.
        """
        if self._bus is None:
            raise ConnectionError("CAN bus not connected")
        loop = asyncio.get_running_loop()
        msg = await loop.run_in_executor(None, lambda: self._bus.recv(timeout=timeout_s))
        if msg is None:
            return None
        return CanMessage(
            arbitration_id=int(msg.arbitration_id),
            data=bytes(msg.data),
            is_extended=bool(getattr(msg, "is_extended_id", False)),
        )


def _default_factory() -> object:
    """Lazy-import `can.Bus`. Skipped if `python-can` isn't installed."""
    try:
        import can
    except ImportError as exc:  # pragma: no cover - exercised only when can pkg missing
        raise RuntimeError(
            "python-can is not installed. Install with `pip install 'jkbms2mqtt[can]'`."
        ) from exc
    return can.Bus


async def connect_with_backoff(
    transport: CanBusTransport, *, max_attempts: int | None = None
) -> None:
    """Connect this CAN transport with exponential backoff."""
    await _connect_with_backoff(transport, max_attempts=max_attempts, label="CAN bus")
