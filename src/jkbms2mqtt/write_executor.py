"""Write executor — consumes WriteQueue, issues Modbus writes via BusArbiter, parses acks.

This is the heart of the safe write path. For each enqueued write the executor:

1. Verifies the write tier is enabled for the current settings (defence in depth;
   the discovery generator already filters, but this guards against direct
   `/set` MQTT messages).
2. Calls the encoder for the parameter — out-of-range values are rejected here
   and never touch the bus.
3. Acquires the BusArbiter lock, sends the Modbus frame, reads the expected ack.
4. Parses the ack via `parse_write_ack`; on success, publishes the new value to
   the parameter's state topic. On failure, publishes a structured error to
   `<bms_name>/error`.

The packed-bit register at 0x1114 uses Modbus function 0x06 (write single).
Because three different parameters live in the same 16-bit register, the
executor performs a read-modify-write: it consults the most recent SetupData
to assemble the new 16-bit value.

This module deliberately has zero MQTT-protocol knowledge — it works with an
abstract `MqttPublisher` callable, supplied by `app.py`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from jkbms2mqtt.bus_arbiter import BusArbiter
from jkbms2mqtt.config import Settings
from jkbms2mqtt.entities import WRITABLE_ENTITIES, PackedBitEntity, WritableEntity
from jkbms2mqtt.protocol.decoder import SetupData
from jkbms2mqtt.protocol.encoder import EncodeError, encode_packed_bit_value, encode_value
from jkbms2mqtt.protocol.modbus import (
    EXCEPTION_FLAG,
    ModbusException,
    WriteAck,
    encode_write_request,
    encode_write_single_register,
    parse_write_ack,
)
from jkbms2mqtt.protocol.registers import PACKED_BITS, WriteTier
from jkbms2mqtt.transport.base import Transport

logger = logging.getLogger(__name__)

PublishFn = Callable[[str, str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class WriteRequest:
    """One pending write submitted by the MQTT subscriber."""

    bms_name: str
    slave_addr: int
    object_id: str  # the parameter object_id (matches WritableEntity / PackedBitEntity)
    raw_payload: str  # the unparsed MQTT payload — encoder casts as needed


_BOOLEAN_PAYLOADS = {
    "on": True,
    "off": False,
    "true": True,
    "false": False,
    "1": True,
    "0": False,
}


def parse_boolean_payload(payload: str) -> bool:
    """Best-effort interpretation of an MQTT boolean payload."""
    lowered = payload.strip().lower()
    if lowered not in _BOOLEAN_PAYLOADS:
        raise ValueError(f"not a boolean payload: {payload!r}")
    return _BOOLEAN_PAYLOADS[lowered]


def parse_numeric_payload(payload: str) -> float:
    return float(payload.strip())


@dataclass
class WriteExecutor:
    """Drains a write queue, issues Modbus writes, publishes acks."""

    transport: Transport
    arbiter: BusArbiter
    settings: Settings
    publish: PublishFn  # async (topic, payload_str) → None
    read_timeout_s: float = 2.0

    # Set/updated by the orchestrator from the most recent Trame 2 (setup) decode.
    # Used to derive the current packed-bit register state for read-modify-write.
    latest_setup: SetupData | None = None

    async def run(self, queue: asyncio.Queue[WriteRequest]) -> None:
        """Main loop: consume requests until cancelled."""
        while True:
            req = await queue.get()
            try:
                await self._handle_one(req)
            except asyncio.CancelledError:  # pragma: no cover — propagate
                raise
            except Exception as exc:  # log any unexpected error but keep the loop alive
                logger.exception("write_executor: unhandled error for %s", req.object_id)
                await self._publish_error(req, f"internal error: {exc}")
            finally:
                queue.task_done()

    async def _handle_one(self, req: WriteRequest) -> None:
        # Find the writable entity for this object_id.
        for w in WRITABLE_ENTITIES:
            if w.object_id == req.object_id:
                await self._handle_single_register(req, w)
                return
        for b in PACKED_BITS:
            p = PackedBitEntity(object_id=b.name, topic_suffix=f"control/{b.name}", bit=b)
            if p.object_id == req.object_id:
                await self._handle_packed_bit(req, p)
                return
        await self._publish_error(req, f"unknown parameter: {req.object_id}")

    async def _handle_single_register(self, req: WriteRequest, entity: WritableEntity) -> None:
        if not self._tier_enabled(entity.register.tier):
            await self._publish_error(
                req,
                f"tier {entity.register.tier.value} writes disabled by config",
            )
            return

        # Parse payload according to encoding.
        try:
            value: float | bool
            from jkbms2mqtt.protocol.registers import Encoding

            if entity.register.encoding is Encoding.BOOL32:
                value = parse_boolean_payload(req.raw_payload)
            else:
                value = parse_numeric_payload(req.raw_payload)
        except ValueError as exc:
            await self._publish_error(req, str(exc))
            return

        # Encode the 4-byte payload (with bound checks).
        try:
            payload = encode_value(entity.register, value)
        except EncodeError as exc:
            await self._publish_error(req, str(exc))
            return

        frame = encode_write_request(
            slave_addr=req.slave_addr,
            register=entity.register.address,
            value=payload,
        )

        try:
            ack_bytes = await self._send_and_read_ack(frame)
        except (TimeoutError, ConnectionError) as exc:
            await self._publish_error(req, f"timeout waiting for ack: {exc}")
            return

        ack = parse_write_ack(
            ack_bytes,
            expected_slave=req.slave_addr,
            expected_register=entity.register.address,
        )
        if isinstance(ack, WriteAck):
            # Echo the new state to the state topic so HA reflects it immediately.
            from jkbms2mqtt.mqtt import _format

            display = "ON" if entity.register.encoding is Encoding.BOOL32 and value else (
                "OFF" if entity.register.encoding is Encoding.BOOL32 else _format(value)
            )
            await self.publish(
                f"{req.bms_name}/{entity.topic_suffix}", display
            )
        elif isinstance(ack, ModbusException):
            await self._publish_error(
                req,
                f"Modbus exception: {ack.message} (code {ack.exception_code:#x})",
            )
        else:
            await self._publish_error(req, f"malformed ack: {ack.reason}")

    async def _handle_packed_bit(
        self, req: WriteRequest, entity: PackedBitEntity
    ) -> None:
        if not self._tier_enabled(entity.bit.tier):
            await self._publish_error(
                req,
                f"tier {entity.bit.tier.value} writes disabled by config",
            )
            return

        try:
            desired_on = parse_boolean_payload(req.raw_payload)
        except ValueError as exc:
            await self._publish_error(req, str(exc))
            return

        if self.latest_setup is None:
            await self._publish_error(
                req,
                "no setup frame yet — wait for first setup poll before writing packed bits",
            )
            return

        # Reconstruct the current 16-bit register value from SetupData bits.
        current = (
            (1 << 7 if self.latest_setup.disable_pcl_module_switch else 0)
            | (1 << 6 if self.latest_setup.smart_sleep_switch else 0)
            | (1 << 5 if self.latest_setup.display_always_on_switch else 0)
        )

        try:
            new_value = encode_packed_bit_value(
                entity.bit, desired_on=desired_on, current_register_value=current
            )
        except EncodeError as exc:  # pragma: no cover - current value is constructed from bits, always valid
            await self._publish_error(req, str(exc))
            return

        frame = encode_write_single_register(
            slave_addr=req.slave_addr,
            register=entity.bit.register,
            value=new_value,
        )

        try:
            ack_bytes = await self._send_and_read_ack(frame)
        except (TimeoutError, ConnectionError) as exc:
            await self._publish_error(req, f"timeout waiting for ack: {exc}")
            return

        ack = parse_write_ack(
            ack_bytes,
            expected_slave=req.slave_addr,
            expected_register=entity.bit.register,
        )
        if isinstance(ack, WriteAck):
            await self.publish(
                f"{req.bms_name}/{entity.topic_suffix}",
                "ON" if desired_on else "OFF",
            )
        elif isinstance(ack, ModbusException):
            await self._publish_error(
                req,
                f"Modbus exception: {ack.message} (code {ack.exception_code:#x})",
            )
        else:
            await self._publish_error(req, f"malformed ack: {ack.reason}")

    async def _send_and_read_ack(self, frame: bytes) -> bytes:
        """Send a Modbus write and read the variable-length response.

        Modbus 0x10 and 0x06 acks are 8 bytes; exception responses are 5 bytes.
        We read the 2-byte header first to decide the remaining length.
        """
        async with self.arbiter.transaction():
            await self.transport.write(frame)
            header = await self.transport.read_exactly(2, timeout_s=self.read_timeout_s)
            func = header[1]
            if func & EXCEPTION_FLAG:
                rest_len = 3  # exception_code + CRC (2 bytes)
            else:
                rest_len = 6  # 2 (addr or value) + 2 (qty or value) + 2 (CRC)
            rest = await self.transport.read_exactly(rest_len, timeout_s=self.read_timeout_s)
        return header + rest

    def _tier_enabled(self, tier: WriteTier) -> bool:
        if tier is WriteTier.BASIC:
            return self.settings.enable_basic_writes
        return self.settings.enable_safety_writes

    async def _publish_error(self, req: WriteRequest, message: str) -> None:
        logger.warning("write rejected: %s/%s: %s", req.bms_name, req.object_id, message)
        await self.publish(
            f"{req.bms_name}/error",
            f'{{"param":"{req.object_id}","reason":"{message}"}}',
        )
