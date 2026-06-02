"""Encode Python values into JK-BMS register payloads.

Pure functions. Each encoder reads the parameter's `RegisterDef` and produces
the 4-byte big-endian payload the BMS expects (function 0x10 / 2 registers).
Out-of-range values are rejected here — defence in depth even when the
HA `number` entity's min/max are honoured by the dashboard.

The 1-byte packed-bits encoder is separate: see `encode_packed_bit_value`,
which produces the 16-bit value to use with Modbus function 0x06 (write single
register) at register 0x1114.
"""

from __future__ import annotations

from jkbms2mqtt.protocol.registers import Encoding, PackedBitDef, RegisterDef


class EncodeError(ValueError):
    """Raised when a value cannot be encoded for the given parameter."""


def encode_value(reg: RegisterDef, value: float | int | bool) -> bytes:
    """Encode *value* for a single-register write (function 0x10).

    Returns a 4-byte big-endian payload. Raises `EncodeError` if *value* is
    outside the parameter's documented `[min_value, max_value]` range.
    """
    numeric = float(value)  # bool→float is fine; int→float lossless
    if not reg.min_value <= numeric <= reg.max_value:
        raise EncodeError(
            f"{reg.name}: value {numeric} outside [{reg.min_value}, {reg.max_value}]"
        )

    if reg.encoding is Encoding.U32_RAW:
        scaled = int(round(numeric))
        return _u32_be(scaled)

    if reg.encoding is Encoding.U32_MILLI:
        scaled = int(round(numeric * 1000))
        return _u32_be(scaled)

    if reg.encoding is Encoding.U32_DECI:
        scaled = int(round(numeric * 10))
        return _u32_be(scaled)

    if reg.encoding is Encoding.I32_DECI:
        scaled = int(round(numeric * 10))
        return _i32_be(scaled)

    if reg.encoding is Encoding.BOOL32:
        # last byte = 1/0, leading three bytes = 0
        return b"\x00\x00\x00" + bytes([1 if numeric else 0])

    raise EncodeError(f"unknown encoding {reg.encoding!r}")  # pragma: no cover


def encode_packed_bit_value(
    bit_def: PackedBitDef, *, desired_on: bool, current_register_value: int
) -> int:
    """Compute the new 16-bit value for the packed-bit register at 0x1114.

    Uses read-modify-write: the executor first reads `current_register_value`
    from the BMS (typically from the most recent Trame 2 / setup frame), passes
    it here along with the desired bit state, and we return the new 16-bit
    value to write back via Modbus function 0x06.
    """
    if not 0 <= current_register_value <= 0xFFFF:
        raise EncodeError(
            f"current_register_value must fit in 16 bits, got {current_register_value:#x}"
        )
    if desired_on:
        return current_register_value | bit_def.bit_mask
    return current_register_value & ~bit_def.bit_mask & 0xFFFF


def _u32_be(value: int) -> bytes:
    if not 0 <= value <= 0xFFFFFFFF:
        raise EncodeError(f"value {value} does not fit in u32")
    return value.to_bytes(4, "big", signed=False)


def _i32_be(value: int) -> bytes:
    if not -(2**31) <= value <= 2**31 - 1:
        raise EncodeError(f"value {value} does not fit in i32")
    return value.to_bytes(4, "big", signed=True)
