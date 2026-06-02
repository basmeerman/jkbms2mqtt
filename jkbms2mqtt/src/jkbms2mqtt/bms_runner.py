"""Per-BMS poll loop: reads three Modbus blocks, decodes, publishes to MQTT.

One ``BmsRunner`` per slave address. Every cycle:

1. Block A: ``read_holding_registers(0x1200, 120)`` — the bulk of the
   real-time telemetry.
2. Block B: ``read_holding_registers(0x1278, 50)`` — extra real-time fields.
   Failures here are non-fatal (some firmware versions have a memory hole here).
3. Block C: ``read_holding_registers(0x12F0, 16)`` — probes 3/4/5 temps.
4. First cycle only: ``read_holding_registers(0x1400, 80)`` — static info.

Blocks A/B/C are stitched into a single 0x110-word buffer (zero-fill on
failure) and decoded via ``jk_modbus.decode_realtime``.

All runners share a single pymodbus client. ``pymodbus`` serialises requests
internally on a single asyncio task — no BusArbiter / asyncio.Lock needed at
this layer.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from jkbms2mqtt.mqtt import (
    build_discovery_messages,
    render,
    state_messages_from_live,
    state_messages_from_static,
)
from jkbms2mqtt.protocol.jk_modbus import (
    BASE_INFO,
    BASE_RT,
    INFO_BLOCK_WORDS,
    RT_BLOCK_WORDS,
    decode_realtime,
    decode_static_info,
)

if TYPE_CHECKING:
    from jkbms2mqtt.config import Settings
    from jkbms2mqtt.transport import ModbusClient

logger = logging.getLogger(__name__)

# Block sizes (in registers / words).
BLOCK_A_COUNT = 120
BLOCK_B_OFFSET = 0x78
BLOCK_B_COUNT = 50
BLOCK_C_OFFSET = 0xF0
BLOCK_C_COUNT = 16

PublishFn = Callable[[str, str, int, bool], Awaitable[None]]
"""``(topic, payload, qos, retain) -> None``."""


@dataclass
class BmsRunner:
    """One BMS, polled periodically; publishes to MQTT."""

    client: ModbusClient
    settings: Settings
    slave_addr: int
    bms_name: str
    publish: PublishFn

    _cell_count: int = field(default=16, init=False)
    _discovery_announced: bool = field(default=False, init=False)
    _static_info_published: bool = field(default=False, init=False)

    async def announce_discovery(self) -> None:
        """Publish retained HA Discovery for every appropriate entity."""
        if self._discovery_announced:
            return
        for msg in build_discovery_messages(
            settings=self.settings, bms_name=self.bms_name, cell_count=self._cell_count
        ):
            topic, payload = render(msg)
            await self.publish(topic, payload.decode(), 1, True)
        self._discovery_announced = True

    async def poll_loop(self) -> None:
        """Forever: poll, decode, publish.

        ``_poll_once`` catches and logs every expected ConnectionError /
        TimeoutError internally, so this loop just keeps cycling on a
        timer.
        """
        await self.announce_discovery()
        while True:
            await self._poll_once()
            await asyncio.sleep(self.settings.poll_interval_s)

    async def _poll_once(self) -> None:
        regs = await self._read_realtime_blocks()
        if regs is None:
            return
        live = decode_realtime(regs)
        # Re-publish discovery if the BMS-reported cell count changes
        # (e.g. first poll updates from default 16 to actual N).
        if live.cell_count and live.cell_count != self._cell_count:
            self._cell_count = live.cell_count
            self._discovery_announced = False
            await self.announce_discovery()
        for topic, payload in state_messages_from_live(live, self.bms_name):
            await self.publish(topic, payload, 0, False)
        await self._poll_static_info_if_needed()

    async def _read_realtime_blocks(self) -> list[int] | None:
        """Read blocks A/B/C; return a stitched buffer or None on critical failure."""
        regs = [0] * RT_BLOCK_WORDS

        # Block A is critical — without it we have no useful data.
        try:
            resp = await self.client.read_holding_registers(
                address=BASE_RT, count=BLOCK_A_COUNT, device_id=self.slave_addr
            )
        except (TimeoutError, ConnectionError) as exc:
            logger.warning(
                "BMS %d: block A read failed: %s", self.slave_addr, exc
            )
            return None
        if resp.isError():
            logger.warning(
                "BMS %d: block A returned Modbus error: %s", self.slave_addr, resp
            )
            return None
        for i, v in enumerate(resp.registers):
            regs[i] = v

        # Block B is optional — graceful degradation if it fails.
        try:
            resp = await self.client.read_holding_registers(
                address=BASE_RT + BLOCK_B_OFFSET,
                count=BLOCK_B_COUNT,
                device_id=self.slave_addr,
            )
        except (TimeoutError, ConnectionError) as exc:
            logger.debug(
                "BMS %d: block B read failed: %s", self.slave_addr, exc
            )
        else:
            if resp.isError():
                logger.debug(
                    "BMS %d: block B returned Modbus error: %s",
                    self.slave_addr,
                    resp,
                )
            else:
                for i, v in enumerate(resp.registers):
                    regs[BLOCK_B_OFFSET + i] = v

        # Block C is optional — temps 3/4/5 only.
        try:
            resp = await self.client.read_holding_registers(
                address=BASE_RT + BLOCK_C_OFFSET,
                count=BLOCK_C_COUNT,
                device_id=self.slave_addr,
            )
        except (TimeoutError, ConnectionError) as exc:
            logger.debug(
                "BMS %d: block C read failed: %s", self.slave_addr, exc
            )
        else:
            if resp.isError():
                logger.debug(
                    "BMS %d: block C returned Modbus error: %s",
                    self.slave_addr,
                    resp,
                )
            else:
                for i, v in enumerate(resp.registers):
                    regs[BLOCK_C_OFFSET + i] = v

        return regs

    async def _poll_static_info_if_needed(self) -> None:
        if self._static_info_published:
            return
        try:
            resp = await self.client.read_holding_registers(
                address=BASE_INFO, count=INFO_BLOCK_WORDS, device_id=self.slave_addr
            )
        except (TimeoutError, ConnectionError) as exc:
            logger.debug("BMS %d: static-info read failed: %s", self.slave_addr, exc)
            return
        if resp.isError():
            logger.debug(
                "BMS %d: static-info returned Modbus error: %s", self.slave_addr, resp
            )
            return
        if len(resp.registers) < INFO_BLOCK_WORDS:
            logger.debug(
                "BMS %d: static-info short reply (%d/%d regs); skipping",
                self.slave_addr, len(resp.registers), INFO_BLOCK_WORDS,
            )
            return
        info = decode_static_info(resp.registers)
        logger.info(
            "BMS %d: model=%r hw=%r sw=%r serial=%r",
            self.slave_addr, info.model, info.hw_version, info.sw_version, info.serial_number,
        )
        for topic, payload in state_messages_from_static(info, self.bms_name):
            await self.publish(topic, payload, 0, True)
        self._static_info_published = True
