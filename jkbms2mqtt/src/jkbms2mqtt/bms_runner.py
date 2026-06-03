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
    state_messages_from_settings,
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
from jkbms2mqtt.protocol.jk_settings import (
    BASIC_REGISTERS,
    PACKED_BIT_REGISTER,
    PACKED_BITS,
    SAFETY_REGISTERS,
    SETTINGS_BLOCK_BASE,
    SETTINGS_BLOCK_CHUNKS,
    SETTINGS_BLOCK_WORDS,
    EncodeError,
    decode_packed_bit_value,
    decode_register_value,
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
    _settings_first_log_done: bool = field(default=False, init=False)

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
        for topic, payload in state_messages_from_live(
            live, self.bms_name,
            debug_unverified=self.settings.debug_unverified_fields,
        ):
            await self.publish(topic, payload, 0, False)
        await self._poll_static_info_if_needed()
        await self._poll_settings()

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
            self.slave_addr,
            info.model, info.hw_version, info.sw_version, info.serial_number,
        )
        for topic, payload in state_messages_from_static(info, self.bms_name):
            await self.publish(topic, payload, 0, True)
        self._static_info_published = True

    async def _poll_settings(self) -> None:
        """Read the writable-settings block + packed-bit register, publish state.

        The 0x1000..0x1085 settings range exceeds the 125-register Modbus 0x03
        ceiling, so we read it as multiple chunks defined in
        ``SETTINGS_BLOCK_CHUNKS`` and stitch into a single buffer. Only
        registers whose entire word range was actually read get decoded —
        partial failures don't produce phantom zeros.

        Settings rarely change, so reading every cycle is fine. The write
        executor always echoes new values to the same topic on success.
        """
        regs = [0] * SETTINGS_BLOCK_WORDS
        read_addresses: set[int] = set()
        failures: list[str] = []
        for chunk_addr, chunk_count in SETTINGS_BLOCK_CHUNKS:
            try:
                resp = await self.client.read_holding_registers(
                    address=chunk_addr,
                    count=chunk_count,
                    device_id=self.slave_addr,
                )
            except (TimeoutError, ConnectionError) as exc:
                failures.append(f"chunk @ {chunk_addr:#06x}: {exc}")
                continue
            if resp.isError():
                failures.append(f"chunk @ {chunk_addr:#06x}: Modbus error {resp}")
                continue
            off = chunk_addr - SETTINGS_BLOCK_BASE
            # A pymodbus stub / gateway may return more registers than asked;
            # only honour the count we requested.
            for i, v in enumerate(resp.registers[:chunk_count]):
                regs[off + i] = v
                read_addresses.add(chunk_addr + i)

        # Log first outcome at INFO/WARNING — silent failure here is the most
        # common reason settings show as "unknown" in HA, so the user needs to
        # see it once on startup. Subsequent failures stay at DEBUG to avoid
        # log spam.
        if not self._settings_first_log_done:
            if failures:
                logger.warning(
                    "BMS %d: settings readback failed (%s); HA will show 'unknown' "
                    "for the affected settings",
                    self.slave_addr, "; ".join(failures),
                )
            elif read_addresses:  # pragma: no branch - empty chunks list is unreachable
                logger.info(
                    "BMS %d: settings readback OK (%d registers)",
                    self.slave_addr, len(read_addresses),
                )
            self._settings_first_log_done = True
        elif failures:
            logger.debug(
                "BMS %d: settings readback failures: %s",
                self.slave_addr, "; ".join(failures),
            )

        register_values: dict[object, float | bool] = {}
        for r in (*BASIC_REGISTERS, *SAFETY_REGISTERS):
            # Only decode if both words of this 32-bit setting were actually read.
            if r.address not in read_addresses or (r.address + 1) not in read_addresses:
                continue
            try:
                register_values[r] = decode_register_value(r, regs)
            except EncodeError as exc:  # pragma: no cover - defensive
                logger.debug("BMS %d: cannot decode %s: %s", self.slave_addr, r.name, exc)

        # Packed-bit register lives outside the contiguous settings block.
        packed_values: dict[object, bool] = {}
        try:
            packed_resp = await self.client.read_holding_registers(
                address=PACKED_BIT_REGISTER, count=1, device_id=self.slave_addr,
            )
        except (TimeoutError, ConnectionError) as exc:
            logger.debug(
                "BMS %d: packed-bit register read failed: %s", self.slave_addr, exc
            )
        else:
            if not packed_resp.isError() and packed_resp.registers:
                raw = packed_resp.registers[0]
                for bit in PACKED_BITS:
                    packed_values[bit] = decode_packed_bit_value(bit, raw)

        for topic, payload in state_messages_from_settings(
            register_values=register_values,  # type: ignore[arg-type]
            packed_values=packed_values,       # type: ignore[arg-type]
            bms_name=self.bms_name,
            debug_unverified=self.settings.debug_unverified_fields,
        ):
            await self.publish(topic, payload, 0, True)
