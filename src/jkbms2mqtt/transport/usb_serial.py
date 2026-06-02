"""USB serial transport using `pyserial-asyncio-fast`.

The JK-BMS RS485 line operates at 115200 baud, 8N1, with the line direction
controlled either by an auto-direction transceiver or the USB-to-RS485 adapter
firmware (FTDI / CH340 etc.). RTS / DTR are not managed explicitly — the
common JK-BMS hardware assumes auto-direction RS485 transceivers.

The transport surface matches `TcpGatewayTransport` exactly so the orchestrator
doesn't need to special-case it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from jkbms2mqtt.transport.backoff import connect_with_backoff as _connect_with_backoff

logger = logging.getLogger(__name__)

BAUD_RATE = 115200  # JK-BMS fixed baud — not user-configurable

# A serial-opener takes keyword args and returns a (reader, writer) pair.
# Both `serial_asyncio_fast.open_serial_connection` and our test fakes match.
SerialOpener = Callable[..., Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


@dataclass
class UsbSerialTransport:
    """Bidirectional USB-to-RS485 serial transport."""

    device_path: str
    baud_rate: int = BAUD_RATE

    _reader: asyncio.StreamReader | None = field(default=None, init=False, repr=False)
    _writer: asyncio.StreamWriter | None = field(default=None, init=False, repr=False)
    _lock: asyncio.Lock | None = field(default=None, init=False, repr=False)
    # Test seam: lets tests inject a fake open_serial_connection.
    _opener: SerialOpener | None = field(default=None, init=False, repr=False)

    def _lock_for(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    @property
    def is_connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> None:
        async with self._lock_for():
            if self.is_connected:
                return
            logger.info("Opening serial port %s @ %d baud", self.device_path, self.baud_rate)
            opener = self._opener or _default_opener()
            self._reader, self._writer = await opener(
                url=self.device_path,
                baudrate=self.baud_rate,
            )
            logger.info("Serial port %s open", self.device_path)

    async def aclose(self) -> None:
        async with self._lock_for():
            writer = self._writer
            self._reader = None
            self._writer = None
            if writer is None or writer.is_closing():
                return
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:  # pragma: no cover - defensive on broken serial port
                pass

    async def read_exactly(self, n: int, *, timeout_s: float) -> bytes:
        if self._reader is None:
            raise ConnectionError("transport not connected")
        return await asyncio.wait_for(self._reader.readexactly(n), timeout=timeout_s)

    async def write(self, data: bytes) -> None:
        if self._writer is None:
            raise ConnectionError("transport not connected")
        self._writer.write(data)
        await self._writer.drain()


def _default_opener() -> SerialOpener:
    """Return `serial_asyncio_fast.open_serial_connection` lazily.

    `pyserial-asyncio-fast` is an optional dependency; importing it at module
    import time would force every standalone-Docker install to also ship the
    serial wheel. Importing on first use keeps the TCP-gateway path lean.
    """
    try:
        from serial_asyncio_fast import open_serial_connection
    except ImportError as exc:  # pragma: no cover - exercised via missing-dep test only when pkg is installed
        raise RuntimeError(
            "pyserial-asyncio-fast is not installed. "
            "Install with `pip install 'jkbms2mqtt[serial]'`."
        ) from exc
    return open_serial_connection


async def connect_with_backoff(
    transport: UsbSerialTransport, *, max_attempts: int | None = None
) -> None:
    """Connect this USB serial transport with exponential backoff."""
    await _connect_with_backoff(transport, max_attempts=max_attempts, label="USB serial")
