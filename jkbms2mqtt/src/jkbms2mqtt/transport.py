"""Pymodbus client factory + exponential-backoff connect helper.

This is the entire transport layer of the add-on. Pymodbus handles RTU framing,
CRC, timeout, transaction serialisation, and the RTU-over-TCP "pass-through
gateway" pattern transparently — we only need to pick the right client class
and feed it the user's connection details.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from pymodbus.client import AsyncModbusSerialClient, AsyncModbusTcpClient
from pymodbus.framer import FramerType

if TYPE_CHECKING:
    from jkbms2mqtt.config import Settings

logger = logging.getLogger(__name__)

JK_BAUD_RATE = 115200
DEFAULT_TIMEOUT_S = 3.0

INITIAL_BACKOFF_S = 1.0
MAX_BACKOFF_S = 30.0


class FrameGapClient:
    """Serialises Modbus transactions and enforces a minimum silent interval.

    RTU requires a quiet period between frames. On a JK PB2A16S20P behind an
    RTU-over-TCP gateway, back-to-back requests are simply ignored: the request
    gets no reply, pymodbus times out after ``DEFAULT_TIMEOUT_S`` and the
    *retry* succeeds. Because the retry always works, this is invisible —
    nothing is logged and no read is recorded as failed — but it costs ~3 s per
    transaction instead of ~25 ms.

    Measured on that hardware, per read, over a full production poll shape::

        gap      slow reads   median read   one pack cycle
          0 ms      14/20         3018 ms        10.70 s
         20 ms       2/20           23 ms         1.77 s
         35 ms       0/20           25 ms         0.37 s

    Every runner and the write executor share one client, so a ``sleep`` inside
    each caller would not help: runner A's frame can still follow runner B's
    with no gap. The spacing has to be enforced here, once, around the shared
    connection. The lock also guarantees the ordering the gap assumes.
    """

    def __init__(
        self,
        inner: AsyncModbusTcpClient | AsyncModbusSerialClient,
        *,
        min_gap_s: float,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._inner = inner
        self._min_gap_s = min_gap_s
        self._sleeper = sleeper
        self._clock = clock
        self._lock = asyncio.Lock()
        self._last_frame = 0.0

    @property
    def inner(self) -> AsyncModbusTcpClient | AsyncModbusSerialClient:
        """The wrapped pymodbus client."""
        return self._inner

    async def _spaced(
        self, call: Callable[..., Awaitable[Any]], /, **kwargs: Any
    ) -> Any:
        """Run one transaction, holding off until the bus has been quiet."""
        async with self._lock:
            idle = self._clock() - self._last_frame
            if idle < self._min_gap_s:
                await self._sleeper(self._min_gap_s - idle)
            try:
                return await call(**kwargs)
            finally:
                # Measured from the end of the exchange: the device needs the
                # silence after the last byte, not after we asked for it.
                self._last_frame = self._clock()

    async def read_holding_registers(
        self, *, address: int, count: int, device_id: int
    ) -> Any:
        return await self._spaced(
            self._inner.read_holding_registers,
            address=address, count=count, device_id=device_id,
        )

    async def write_registers(
        self, *, address: int, values: list[int], device_id: int
    ) -> Any:
        return await self._spaced(
            self._inner.write_registers,
            address=address, values=values, device_id=device_id,
        )

    async def write_register(self, *, address: int, value: int, device_id: int) -> Any:
        return await self._spaced(
            self._inner.write_register,
            address=address, value=value, device_id=device_id,
        )

    async def connect(self) -> bool:
        return bool(await self._inner.connect())

    def close(self) -> None:
        self._inner.close()


ModbusClient = AsyncModbusTcpClient | AsyncModbusSerialClient | FrameGapClient


def build_client(settings: Settings) -> ModbusClient:
    """Construct a pymodbus async client matching ``settings.transport``.

    Both client types use ``FramerType.RTU`` — RTU on raw serial, and RTU-over-
    TCP for the typical pass-through gateway (Elfin EW10/EW11, Waveshare, etc.).
    """
    inner: AsyncModbusTcpClient | AsyncModbusSerialClient
    if settings.transport == "tcp_gateway":
        logger.info(
            "Building TCP-gateway client (host=%s, port=%d)",
            settings.gateway_host,
            settings.gateway_port,
        )
        inner = AsyncModbusTcpClient(
            host=settings.gateway_host,
            port=settings.gateway_port,
            framer=FramerType.RTU,
            timeout=DEFAULT_TIMEOUT_S,
        )
    else:
        logger.info("Building USB-serial client (port=%s)", settings.jkbms_path)
        inner = AsyncModbusSerialClient(
            port=settings.jkbms_path,
            baudrate=JK_BAUD_RATE,
            bytesize=8,
            parity="N",
            stopbits=1,
            framer=FramerType.RTU,
            timeout=DEFAULT_TIMEOUT_S,
        )
    logger.info("Enforcing a %d ms minimum gap between Modbus frames",
                settings.min_frame_gap_ms)
    return FrameGapClient(inner, min_gap_s=settings.min_frame_gap_ms / 1000.0)


async def connect_with_backoff(
    client: ModbusClient,
    *,
    max_attempts: int | None = None,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Call ``client.connect()`` with exponential backoff.

    ``max_attempts=None`` retries forever (production). Tests pass a finite
    count. The ``sleeper`` is injectable so tests can swap in a no-op without
    monkey-patching the entire :mod:`asyncio` module.
    """
    attempt = 0
    backoff = INITIAL_BACKOFF_S
    while True:
        attempt += 1
        try:
            ok = await client.connect()
        except (TimeoutError, OSError) as exc:
            ok = False
            err: Exception | None = exc
        else:
            err = None
        if ok:
            return
        logger.warning(
            "Modbus connect attempt %d failed%s — retrying in %.1fs",
            attempt,
            f": {err}" if err else "",
            backoff,
        )
        if max_attempts is not None and attempt >= max_attempts:
            raise ConnectionError(
                f"giving up after {attempt} attempts; last error: {err}"
            )
        await sleeper(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF_S)
