"""Modbus RTU framing for the JK-BMS RS485 dialect.

All functions are pure: inputs are bytes/ints, outputs are bytes or typed Result objects.
No I/O, no logging, no side effects. This is the layer that gets exhaustive unit + property
+ mutation testing.

The JK-BMS uses:
- Function 0x10 (Write Multiple Registers) for BOTH polls and writes.
  - Polls: 1 register written to a "trigger" address (0x161C / 0x1620 / 0x1622) — this elicits
    a multi-hundred-byte JK reply frame (see jk_frame.py).
  - Setting writes: 2 registers (4 bytes payload) at the target parameter address.
- Function 0x06 (Write Single Register) for packed-bit settings at 0x1114 with 1 byte payload.
- CRC16 Modbus polynomial 0xA001, little-endian on the wire.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

CRC16_POLY: Final = 0xA001
CRC16_INIT: Final = 0xFFFF

FUNC_READ_HOLDING: Final = 0x03
FUNC_WRITE_SINGLE: Final = 0x06
FUNC_WRITE_MULTIPLE: Final = 0x10
EXCEPTION_FLAG: Final = 0x80


def crc16_a001(data: bytes) -> int:
    """Compute the Modbus CRC16 (polynomial 0xA001) of *data*.

    Returned value is the raw 16-bit integer; serialize little-endian on the wire.
    """
    crc = CRC16_INIT
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ CRC16_POLY
            else:
                crc >>= 1
    return crc


def append_crc(frame_no_crc: bytes) -> bytes:
    """Return *frame_no_crc* with a little-endian CRC16 appended."""
    crc = crc16_a001(frame_no_crc)
    return frame_no_crc + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def verify_crc(frame: bytes) -> bool:
    """True if the final two bytes of *frame* are the valid CRC16 of the preceding bytes."""
    if len(frame) < 2:
        return False
    body = frame[:-2]
    expected = crc16_a001(body)
    got = frame[-2] | (frame[-1] << 8)
    return got == expected


def encode_poll_request(slave_addr: int, register: int) -> bytes:
    """Encode the JK-BMS poll-trigger frame.

    The BMS uses a quirky "write 1 register to a magic address" pattern as the poll trigger.
    The three known trigger registers are 0x161C, 0x1620, 0x1622 (fixed/live/setup).

    Frame layout: [addr] [0x10] [reg_hi] [reg_lo] 0x00 0x01 0x02 0x00 0x00 [crc_lo] [crc_hi]
    """
    _validate_slave(slave_addr)
    _validate_register(register)
    body = bytes(
        [
            slave_addr,
            FUNC_WRITE_MULTIPLE,
            (register >> 8) & 0xFF,
            register & 0xFF,
            0x00,
            0x01,  # quantity = 1 register
            0x02,  # byte count = 2 bytes
            0x00,
            0x00,  # payload (ignored by BMS; it's just a trigger)
        ]
    )
    return append_crc(body)


def encode_write_request(slave_addr: int, register: int, value: bytes) -> bytes:
    """Encode a Modbus function 0x10 write of two 16-bit registers (4 bytes payload).

    *value* must be exactly 4 bytes, big-endian, already scaled and encoded per the
    parameter's register definition (see protocol/registers.py).
    """
    _validate_slave(slave_addr)
    _validate_register(register)
    if len(value) != 4:
        raise ValueError(f"write payload must be 4 bytes, got {len(value)}")
    body = bytes(
        [
            slave_addr,
            FUNC_WRITE_MULTIPLE,
            (register >> 8) & 0xFF,
            register & 0xFF,
            0x00,
            0x02,  # quantity = 2 registers (uint32)
            0x04,  # byte count = 4
            value[0],
            value[1],
            value[2],
            value[3],
        ]
    )
    return append_crc(body)


def encode_write_single_register(slave_addr: int, register: int, value: int) -> bytes:
    """Encode a Modbus function 0x06 write of a single 16-bit register.

    Used for the packed-bit control register at 0x1114 (PCL disable / smart sleep / etc.).
    *value* is a 16-bit unsigned integer.
    """
    _validate_slave(slave_addr)
    _validate_register(register)
    if not 0 <= value <= 0xFFFF:
        raise ValueError(f"value must fit in 16 bits, got {value:#x}")
    body = bytes(
        [
            slave_addr,
            FUNC_WRITE_SINGLE,
            (register >> 8) & 0xFF,
            register & 0xFF,
            (value >> 8) & 0xFF,
            value & 0xFF,
        ]
    )
    return append_crc(body)


@dataclass(frozen=True, slots=True)
class WriteAck:
    """Successful Modbus write acknowledgement."""

    slave_addr: int
    function: int
    register: int
    quantity: int


@dataclass(frozen=True, slots=True)
class ModbusException:
    """Modbus exception response (function | 0x80, exception code)."""

    slave_addr: int
    function: int
    exception_code: int

    @property
    def message(self) -> str:
        codes = {
            0x01: "illegal function",
            0x02: "illegal data address",
            0x03: "illegal data value",
            0x04: "slave device failure",
            0x05: "acknowledge",
            0x06: "slave device busy",
        }
        return codes.get(self.exception_code, f"unknown exception {self.exception_code:#x}")


@dataclass(frozen=True, slots=True)
class MalformedAck:
    """The bytes received did not parse as a valid Modbus ack or exception."""

    reason: str
    raw: bytes


ParsedAck = WriteAck | ModbusException | MalformedAck


def parse_write_ack(frame: bytes, *, expected_slave: int, expected_register: int) -> ParsedAck:
    """Parse a Modbus write-ack response and validate it against expectations.

    Returns one of:
    - WriteAck if it's a valid acknowledgement matching slave + register.
    - ModbusException if the BMS returned an exception.
    - MalformedAck for short/garbled/CRC-failed input.
    """
    if len(frame) < 5:
        return MalformedAck("frame too short", frame)
    if not verify_crc(frame):
        return MalformedAck("bad CRC", frame)

    slave = frame[0]
    func = frame[1]

    if func & EXCEPTION_FLAG:
        if len(frame) != 5:
            return MalformedAck("exception frame must be 5 bytes", frame)
        return ModbusException(
            slave_addr=slave,
            function=func & ~EXCEPTION_FLAG,
            exception_code=frame[2],
        )

    if func == FUNC_WRITE_MULTIPLE:
        if len(frame) != 8:
            return MalformedAck("0x10 ack must be 8 bytes", frame)
        register = (frame[2] << 8) | frame[3]
        quantity = (frame[4] << 8) | frame[5]
        if slave != expected_slave:
            return MalformedAck(
                f"slave mismatch: expected {expected_slave}, got {slave}", frame
            )
        if register != expected_register:
            return MalformedAck(
                f"register mismatch: expected {expected_register:#x}, got {register:#x}", frame
            )
        return WriteAck(
            slave_addr=slave,
            function=func,
            register=register,
            quantity=quantity,
        )

    if func == FUNC_WRITE_SINGLE:
        if len(frame) != 8:
            return MalformedAck("0x06 ack must be 8 bytes", frame)
        register = (frame[2] << 8) | frame[3]
        value = (frame[4] << 8) | frame[5]
        if slave != expected_slave:
            return MalformedAck(
                f"slave mismatch: expected {expected_slave}, got {slave}", frame
            )
        if register != expected_register:
            return MalformedAck(
                f"register mismatch: expected {expected_register:#x}, got {register:#x}", frame
            )
        return WriteAck(
            slave_addr=slave,
            function=func,
            register=register,
            quantity=value,
        )

    return MalformedAck(f"unexpected function {func:#x}", frame)


def _validate_slave(addr: int) -> None:
    if not 0 <= addr <= 0xFF:
        raise ValueError(f"slave address must fit in a byte, got {addr}")


def _validate_register(register: int) -> None:
    if not 0 <= register <= 0xFFFF:
        raise ValueError(f"register must fit in 16 bits, got {register:#x}")
