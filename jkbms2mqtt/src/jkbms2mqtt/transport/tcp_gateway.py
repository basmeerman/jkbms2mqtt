"""TCP-gateway transport.

The typical hardware path is JK-BMS → RS485 transceiver → USR-W630 (or similar)
TCP-to-serial gateway → LAN. The gateway is a transparent byte bridge: anything we
write goes onto the RS485 bus, and the RS485 transceiver handles half-duplex
turnaround automatically. Reads come back the same way.

Auto-reconnect with exponential backoff (capped at 30 s) recovers from the
typical TCP gateway stall after long sessions.

The transport is bidirectional: we use the same `asyncio.StreamWriter` for both
the periodic polls and the user-initiated writes, with the BusArbiter
serialising transactions so they never overlap on the wire.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from jkbms2mqtt.transport.backoff import connect_with_backoff as _connect_with_backoff

logger = logging.getLogger(__name__)


@dataclass
class TcpGatewayTransport:
    """Bidirectional TCP-to-serial gateway transport."""

    host: str
    port: int
    connect_timeout_s: float = 5.0

    _reader: asyncio.StreamReader | None = None
    _writer: asyncio.StreamWriter | None = None
    _lock: asyncio.Lock | None = None

    def _lock_for(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    @property
    def is_connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> None:
        """Open a TCP connection to the gateway."""
        async with self._lock_for():
            if self.is_connected:
                return
            logger.info("Opening TCP gateway %s:%d", self.host, self.port)
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=self.connect_timeout_s,
            )
            logger.info("Connected to gateway %s:%d", self.host, self.port)

    async def aclose(self) -> None:
        async with self._lock_for():
            await self._close_unlocked()

    async def _close_unlocked(self) -> None:
        writer = self._writer
        self._reader = None
        self._writer = None
        if writer is None or writer.is_closing():
            return
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass

    async def read_exactly(self, n: int, *, timeout_s: float) -> bytes:
        """Read exactly *n* bytes. Caller must hold the bus arbiter lock."""
        if self._reader is None:
            raise ConnectionError("transport not connected")
        return await asyncio.wait_for(self._reader.readexactly(n), timeout=timeout_s)

    async def write(self, data: bytes) -> None:
        if self._writer is None:
            raise ConnectionError("transport not connected")
        self._writer.write(data)
        await self._writer.drain()


async def connect_with_backoff(
    transport: TcpGatewayTransport, *, max_attempts: int | None = None
) -> None:
    """Connect this TCP gateway transport with exponential backoff."""
    await _connect_with_backoff(transport, max_attempts=max_attempts, label="TCP gateway")
