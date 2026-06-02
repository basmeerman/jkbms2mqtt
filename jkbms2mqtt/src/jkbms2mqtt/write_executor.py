"""Write executor — consumes MQTT ``/set`` commands, sends Modbus writes.

For each enqueued write the executor:

1. Verifies the write tier is enabled for the current settings.
2. Calls the encoder for the parameter — out-of-range values are rejected here
   and never touch the bus.
3. Calls ``client.write_registers`` (function 0x10) for a normal setting, or
   read-modify-write of register 0x1114 via ``client.write_register`` (function
   0x06) for the packed-bit booleans.
4. Echoes the new value to the parameter's state topic on success, or publishes
   a structured error to ``<bms_name>/error`` on failure.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from jkbms2mqtt.entities import PACKED_BIT_ENTITIES, WRITABLE_ENTITIES, PackedBitEntity
from jkbms2mqtt.protocol.jk_settings import (
    EncodeError,
    Encoding,
    WriteTier,
    encode_packed_bit_value,
    encode_value_to_words,
)

if TYPE_CHECKING:
    from jkbms2mqtt.config import Settings
    from jkbms2mqtt.entities import WritableEntity
    from jkbms2mqtt.transport import ModbusClient

logger = logging.getLogger(__name__)

PublishFn = Callable[[str, str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class WriteRequest:
    """One pending write from an MQTT ``/set`` topic."""

    bms_name: str
    slave_addr: int
    object_id: str
    raw_payload: str


_BOOLEAN_PAYLOADS = {
    "on": True, "off": False, "true": True, "false": False, "1": True, "0": False,
}


def parse_boolean_payload(payload: str) -> bool:
    lowered = payload.strip().lower()
    if lowered not in _BOOLEAN_PAYLOADS:
        raise ValueError(f"not a boolean payload: {payload!r}")
    return _BOOLEAN_PAYLOADS[lowered]


def parse_numeric_payload(payload: str) -> float:
    return float(payload.strip())


@dataclass
class WriteExecutor:
    """Drains a write queue; calls into pymodbus; publishes outcomes."""

    client: ModbusClient
    settings: Settings
    publish: PublishFn

    async def run(self, queue: asyncio.Queue[WriteRequest]) -> None:
        """Main loop: consume queue until cancelled."""
        while True:
            req = await queue.get()
            try:
                await self._handle_one(req)
            except asyncio.CancelledError:  # pragma: no cover
                raise
            except Exception as exc:
                logger.exception("write_executor: unhandled error for %s", req.object_id)
                await self._publish_error(req, f"internal error: {exc}")
            finally:
                queue.task_done()

    async def _handle_one(self, req: WriteRequest) -> None:
        for w in WRITABLE_ENTITIES:
            if w.object_id == req.object_id:
                await self._handle_register(req, w)
                return
        for b in PACKED_BIT_ENTITIES:
            if b.object_id == req.object_id:
                await self._handle_packed_bit(req, b)
                return
        await self._publish_error(req, f"unknown parameter: {req.object_id}")

    async def _handle_register(self, req: WriteRequest, entity: WritableEntity) -> None:
        if not self._tier_enabled(entity.register.tier):
            await self._publish_error(
                req,
                f"tier {entity.register.tier.value} writes disabled by config",
            )
            return

        try:
            value: float | bool
            if entity.register.encoding is Encoding.BOOL32:
                value = parse_boolean_payload(req.raw_payload)
            else:
                value = parse_numeric_payload(req.raw_payload)
        except ValueError as exc:
            await self._publish_error(req, str(exc))
            return

        try:
            words = encode_value_to_words(entity.register, value)
        except EncodeError as exc:
            await self._publish_error(req, str(exc))
            return

        response = await self._safe_write_registers(
            req, address=entity.register.address, values=words
        )
        if response is None:
            return  # error already published

        from jkbms2mqtt.mqtt import _format

        if entity.register.encoding is Encoding.BOOL32:
            display = "ON" if value else "OFF"
        else:
            display = _format(value)
        await self.publish(f"{req.bms_name}/{entity.topic_suffix}", display)

    async def _handle_packed_bit(self, req: WriteRequest, entity: PackedBitEntity) -> None:
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

        current = await self._safe_read_register(req, address=entity.bit.register)
        if current is None:
            return  # error already published

        try:
            new_value = encode_packed_bit_value(
                entity.bit, desired_on=desired_on, current_register_value=current
            )
        except EncodeError as exc:  # pragma: no cover - register read is always u16
            await self._publish_error(req, str(exc))
            return

        response = await self._safe_write_register(
            req, address=entity.bit.register, value=new_value
        )
        if response is None:
            return

        await self.publish(
            f"{req.bms_name}/{entity.topic_suffix}", "ON" if desired_on else "OFF"
        )

    # -- pymodbus wrappers that consolidate error handling -----------------------------

    async def _safe_write_registers(
        self, req: WriteRequest, *, address: int, values: list[int]
    ) -> Any | None:
        try:
            response = await self.client.write_registers(
                address=address, values=values, device_id=req.slave_addr
            )
        except (TimeoutError, ConnectionError) as exc:
            await self._publish_error(req, f"write failed: {exc}")
            return None
        if response.isError():
            await self._publish_error(req, f"BMS rejected write: {response}")
            return None
        return response

    async def _safe_write_register(
        self, req: WriteRequest, *, address: int, value: int
    ) -> Any | None:
        try:
            response = await self.client.write_register(
                address=address, value=value, device_id=req.slave_addr
            )
        except (TimeoutError, ConnectionError) as exc:
            await self._publish_error(req, f"write failed: {exc}")
            return None
        if response.isError():
            await self._publish_error(req, f"BMS rejected write: {response}")
            return None
        return response

    async def _safe_read_register(
        self, req: WriteRequest, *, address: int
    ) -> int | None:
        try:
            response = await self.client.read_holding_registers(
                address=address, count=1, device_id=req.slave_addr
            )
        except (TimeoutError, ConnectionError) as exc:
            await self._publish_error(req, f"read-modify-write read failed: {exc}")
            return None
        if response.isError():
            await self._publish_error(req, f"BMS rejected RMW read: {response}")
            return None
        return int(response.registers[0])

    def _tier_enabled(self, tier: WriteTier) -> bool:
        if tier is WriteTier.BASIC:
            return self.settings.enable_basic_writes
        return self.settings.enable_safety_writes

    async def _publish_error(self, req: WriteRequest, message: str) -> None:
        logger.warning("write rejected: %s/%s: %s", req.bms_name, req.object_id, message)
        await self.publish(
            f"{req.bms_name}/error",
            json.dumps({"param": req.object_id, "reason": message}),
        )
