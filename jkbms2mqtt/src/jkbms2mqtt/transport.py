"""Pymodbus client factory + exponential-backoff connect helper.

This is the entire transport layer of the add-on. Pymodbus handles RTU framing,
CRC, timeout, transaction serialisation, and the RTU-over-TCP "pass-through
gateway" pattern transparently — we only need to pick the right client class
and feed it the user's connection details.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from pymodbus.client import AsyncModbusSerialClient, AsyncModbusTcpClient
from pymodbus.framer import FramerType

if TYPE_CHECKING:
    from jkbms2mqtt.config import Settings

logger = logging.getLogger(__name__)

JK_BAUD_RATE = 115200
DEFAULT_TIMEOUT_S = 3.0

INITIAL_BACKOFF_S = 1.0
MAX_BACKOFF_S = 30.0


ModbusClient = AsyncModbusTcpClient | AsyncModbusSerialClient


def build_client(settings: Settings) -> ModbusClient:
    """Construct a pymodbus async client matching ``settings.transport``.

    Both client types use ``FramerType.RTU`` — RTU on raw serial, and RTU-over-
    TCP for the typical pass-through gateway (Elfin EW10/EW11, Waveshare, etc.).
    """
    if settings.transport == "tcp_gateway":
        logger.info(
            "Building TCP-gateway client (host=%s, port=%d)",
            settings.gateway_host,
            settings.gateway_port,
        )
        return AsyncModbusTcpClient(
            host=settings.gateway_host,
            port=settings.gateway_port,
            framer=FramerType.RTU,
            timeout=DEFAULT_TIMEOUT_S,
        )
    logger.info("Building USB-serial client (port=%s)", settings.jkbms_path)
    return AsyncModbusSerialClient(
        port=settings.jkbms_path,
        baudrate=JK_BAUD_RATE,
        bytesize=8,
        parity="N",
        stopbits=1,
        framer=FramerType.RTU,
        timeout=DEFAULT_TIMEOUT_S,
    )


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
