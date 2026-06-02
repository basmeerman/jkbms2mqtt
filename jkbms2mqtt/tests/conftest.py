"""Shared pytest fixtures.

The synthetic-frame builders here let us write decoder tests without depending on
captured byte fixtures. They populate the exact offsets the decoder reads, leaving
the rest of the 300-byte buffer at zero, which exercises the parser's length checks
without requiring full BMS captures.
"""

from __future__ import annotations

import struct
from typing import Final

import pytest

from jkbms2mqtt.protocol.jk_frame import (
    MAGIC,
    FrameType,
    JkFrame,
    compute_checksum,
)

FRAME_LEN: Final = 300  # all three trame types are 300 bytes in current firmwares


@pytest.fixture
def make_frame():
    """Return a builder for a 300-byte JK frame with arbitrary field overrides.

    Usage:
        raw = make_frame(FrameType.LIVE, {6: ("<H", 3300)})
        # raw is a bytes object: magic + type-byte + payload + unit_no + checksum
    """

    def _builder(
        frame_type: FrameType,
        fields: dict[int, tuple[str, int | float]] | None = None,
        *,
        unit_no: int = 0,
    ) -> bytes:
        buf = bytearray(FRAME_LEN)
        buf[0:4] = MAGIC
        buf[4] = int(frame_type)
        # Place each field at its absolute offset using struct.pack_into.
        if fields:
            for off, (fmt, value) in fields.items():
                struct.pack_into(fmt, buf, off, value)
        # Pack the 4-byte unit number at offset (len-5).
        struct.pack_into("<I", buf, FRAME_LEN - 5, unit_no)
        # Compute and place the XOR checksum at the last byte.
        buf[-1] = compute_checksum(bytes(buf[:-1]))
        return bytes(buf)

    return _builder


@pytest.fixture
def live_frame(make_frame):
    """Build a Trame 3 (live) frame populated with realistic test values.

    Defaults (in physical units):
    - 16 cells, each 3.300 V, each 1.5 mΩ
    - 53.0 V total, +10.0 A, 530 W
    - SoC 75%, SoH 100%
    - All temps 25.0 °C
    - Charge & discharge switches on; balance off; not heating
    """

    def _build(*, cell_count: int = 16, overrides: dict[int, tuple[str, int]] | None = None) -> bytes:
        fields: dict[int, tuple[str, int | float]] = {}
        # Cell voltages: uint16le ×16, mV. Real cells in slots 0..cell_count-1.
        for i in range(16):
            fields[6 + 2 * i] = ("<H", 3300 if i < cell_count else 0)
        # Cell resistances: int16le ×16, mΩ.
        for i in range(16):
            fields[80 + 2 * i] = ("<h", 1 if i < cell_count else 0)
        # Mos temp: int16le, /10 → 25.0 °C → 250
        fields[144] = ("<h", 250)
        # Total Power (uint32le, /1000 → W → mW): 530 W → 530000
        fields[154] = ("<I", 530000)
        # Total Current (int32le, /1000 → A → mA): 10 A → 10000
        fields[158] = ("<i", 10000)
        # Probe 1, 2 temps (int16le, /10) — 25.0 °C
        fields[162] = ("<h", 250)
        fields[164] = ("<h", 250)
        # Balance current (int16le, /1000) — 0
        fields[170] = ("<h", 0)
        # Balance Action (byte): 0
        fields[172] = ("<B", 0)
        # SOC (uint8): 75
        fields[173] = ("<B", 75)
        # Remaining capacity (int32le, /1000): 80 Ah → 80000 mAh
        fields[174] = ("<i", 80000)
        # Battery capacity (int32le, /1000): 100 Ah
        fields[178] = ("<i", 100000)
        # Cycle count (int32le): 42
        fields[182] = ("<i", 42)
        # Cycle capacity (int32le, /1000): 4200 Ah
        fields[186] = ("<i", 4200000)
        # SOH (uint8): 100
        fields[190] = ("<B", 100)
        # Total runtime (uint32le, seconds): 100000
        fields[194] = ("<I", 100000)
        # Switches: charge=1, discharge=1, balance=0
        fields[198] = ("<B", 1)
        fields[199] = ("<B", 1)
        fields[200] = ("<B", 0)
        # Heating (uint8): 0
        fields[215] = ("<B", 0)
        # Total Voltage (uint16le, /100): 53.00 V → 5300
        fields[234] = ("<H", 5300)
        # Heating current (int16le, /1000): 0
        fields[236] = ("<h", 0)
        # Probe 4 sits BEFORE probe 3 in the JK frame layout
        fields[256] = ("<h", 250)  # probe 4 — 25.0 °C
        fields[258] = ("<h", 250)  # probe 3 — 25.0 °C
        # charge_status_time (uint16le): 0
        fields[278] = ("<H", 0)
        # charge_status (uint8): 0 (Bulk)
        fields[280] = ("<B", 0)
        if overrides:
            fields.update(overrides)
        return make_frame(FrameType.LIVE, fields)

    return _build


@pytest.fixture
def setup_frame(make_frame):
    """Build a Trame 2 (setup) frame with sensible default values.

    Defaults match a typical 16S LiFePO4 pack configuration.
    """

    def _build(*, overrides: dict[int, tuple[str, int]] | None = None) -> bytes:
        fields: dict[int, tuple[str, int | float]] = {
            # Cell voltage protections (int32le, /1000 → mV)
            6: ("<i", 2500),  # smart_sleep_voltage 2.5 V
            10: ("<i", 2800),  # UVP 2.8
            14: ("<i", 2850),  # UVPR 2.85
            18: ("<i", 3650),  # OVP 3.65
            22: ("<i", 3600),  # OVPR 3.60
            26: ("<i", 5),  # balance trigger 0.005 V
            30: ("<i", 3400),  # SOC100 3.4
            34: ("<i", 2900),  # SOC0 2.9
            38: ("<i", 3550),  # request charge 3.55
            42: ("<i", 3400),  # request float 3.40
            46: ("<i", 2300),  # power_off 2.3
            # Currents (int32le, /1000 → mA)
            50: ("<i", 50000),  # max charge 50 A
            54: ("<i", 5),  # OCP delay 5 s
            58: ("<i", 30),  # OCP recovery 30 s
            62: ("<i", 100000),  # max discharge 100 A
            66: ("<i", 5),  # OCP delay 5 s
            70: ("<i", 30),
            74: ("<i", 5),
            78: ("<i", 1000),  # max balance 1 A
            # Temperatures (int32le, /10 → 0.1 °C)
            82: ("<i", 700),  # OTP 70.0
            86: ("<i", 650),  # OTP recovery 65.0
            90: ("<i", 700),
            94: ("<i", 650),
            98: ("<i", 0),  # UTP 0.0
            102: ("<i", 50),  # UTP recovery 5.0
            106: ("<i", 800),  # PT OTP 80.0
            110: ("<i", 750),
            # Cell count + switches
            114: ("<i", 16),
            118: ("<B", 1),
            122: ("<B", 1),
            126: ("<B", 1),
            # Capacity + SCP delay + balance starting
            130: ("<i", 100000),  # 100 Ah
            134: ("<i", 500),  # 500 μs
            138: ("<i", 3300),  # 3.3 V
            158: ("<i", 1),  # wire resistance 1 mΩ
            270: ("<i", 1),  # device address
            # Packed bits at 282: bit5=display, bit6=smart_sleep, bit7=pcl_disable
            282: ("<B", 0x60),  # smart_sleep=1, pcl_disable=0, display=1 → 0x60 (bits 5+6)
            283: ("<B", 0x01),  # timed_stored=1
        }
        if overrides:
            fields.update(overrides)
        return make_frame(FrameType.SETUP, fields)

    return _build


@pytest.fixture
def fixed_frame(make_frame):
    """Build a Trame 1 (fixed/static device info) frame with sensible defaults."""

    def _build(*, model: str = "JK_PB2A16S15P") -> bytes:
        # ASCII fields use _ascii() which strips NULs and decodes ascii.
        buf = bytearray(300)
        buf[0:4] = MAGIC
        buf[4] = int(FrameType.FIXED)
        model_b = model.encode("ascii")
        buf[6 : 6 + len(model_b)] = model_b
        fw = b"15A6.0"
        buf[22 : 22 + len(fw)] = fw
        sw = b"V19H_06"
        buf[30 : 30 + len(sw)] = sw
        # Uptime, power count
        struct.pack_into("<I", buf, 38, 360000)
        struct.pack_into("<I", buf, 42, 1234)
        # Serial
        serial = b"ABCDEF12345678"
        buf[46 : 46 + len(serial)] = serial
        # Manuf date
        date = b"240315"
        buf[78 : 78 + len(date)] = date
        # Brand
        brand = b"JIKONG"
        buf[102 : 102 + len(brand)] = brand
        # UART1 + CAN protocol numbers
        buf[184] = 2
        buf[185] = 6
        # LCD buzzer trigger / values
        buf[234] = 5
        struct.pack_into("<I", buf, 238, 80)
        struct.pack_into("<I", buf, 242, 60)
        # Request charge / float voltage times
        buf[266] = 2
        buf[267] = 4
        # Unit no + checksum
        struct.pack_into("<I", buf, 295, 0)
        buf[-1] = compute_checksum(bytes(buf[:-1]))
        return bytes(buf)

    return _build


@pytest.fixture
def parse_live_frame(live_frame):
    """Return a (raw_bytes, parsed JkFrame) pair from default live_frame."""

    def _build(**kwargs) -> tuple[bytes, JkFrame]:
        from jkbms2mqtt.protocol.jk_frame import parse_jk_frame

        raw = live_frame(**kwargs)
        result = parse_jk_frame(raw)
        assert isinstance(result, JkFrame)
        return raw, result

    return _build
