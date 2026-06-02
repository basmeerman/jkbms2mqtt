"""JK-BMS CAN protocol decoder.

The JK-BMS in CAN mode broadcasts a fixed set of CAN-extended-frame IDs at
~500 kbps. The IDs and byte layouts here follow the JK CAN ("Pylon-style")
protocol documented for the firmware family these decoders target.

Each decoder accepts the 8-byte CAN payload and returns a *fragment*: a
narrow dataclass with just the fields that frame carries. A higher-level
`CanFrameAccumulator` (see `can_runner.py`) merges incoming fragments into
a `LiveData` snapshot that we can publish to MQTT.

This module is pure-functional: no I/O, no python-can imports, fully
unit-testable against synthetic byte payloads.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Final

# Known CAN-arbitration IDs (extended hex, no flags).
ID_MAIN_STATUS: Final = 0x2F4
ID_CELL_MINMAX: Final = 0x4F4
ID_TEMPERATURES: Final = 0x5F4
ID_ALARM_INFO: Final = 0x7F4
ID_POWER_CURRENT: Final = 0x18F128F4
ID_MONITORING: Final = 0x01F21400
ID_CYCLE_COUNT: Final = 0x18F528F4
ID_STATUS_DATA: Final = 0x1806E5F4
ID_INDIVIDUAL_TEMPS: Final = 0x18F228F4
ID_ALT_SOC_DATA: Final = 0x18F428F4
# Cell-voltage groups: 0x18E028F4 (cells 1-4), 0x18E128F4 (5-8), 0x18E228F4 (9-12),
# 0x18E328F4 (13-16).
ID_CELL_VOLT_BASE: Final = 0x18E028F4
ID_CELL_VOLT_MASK: Final = 0xFFFCFFFF  # all-but-the-group-bits


@dataclass(frozen=True, slots=True)
class MainStatus:
    """0x2F4 — total voltage, SoC, signed current."""

    total_voltage_v: float
    soc_percentage: int
    total_current_a: float


@dataclass(frozen=True, slots=True)
class CellMinMax:
    """0x4F4 — cell delta / max / min voltage + positions."""

    cell_voltage_delta_v: float
    cell_voltage_max_v: float
    cell_voltage_max_number: int
    cell_voltage_min_v: float
    cell_voltage_min_number: int


@dataclass(frozen=True, slots=True)
class Temperatures:
    """0x5F4 — max / min / avg pack temperatures with positions."""

    max_temp_c: int
    max_temp_position: int
    min_temp_c: int
    min_temp_position: int
    avg_temp_c: int


@dataclass(frozen=True, slots=True)
class PowerCurrent:
    """0x18F128F4 — signed current, power, cycle count."""

    total_current_a: float
    total_power_w: float
    cycle_count: int


@dataclass(frozen=True, slots=True)
class Monitoring:
    """0x01F21400 — Pylon-style: voltage, signed current, temperature, cycles."""

    total_voltage_v: float
    total_current_a: float
    pack_temperature_c: float
    cycle_count: int


@dataclass(frozen=True, slots=True)
class IndividualTemps:
    """0x18F228F4 — up to 5 probe temperatures (offset -50 °C)."""

    temperatures_c: tuple[int, ...]
    mos_temp_c: int | None


@dataclass(frozen=True, slots=True)
class CellVoltages:
    """0x18E[0-3]28F4 — four cells per frame, group encoded in the ID."""

    group_index: int  # 0..3
    voltages_v: tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class AlarmInfo:
    """0x7F4 — alarm bitfield (15 alarm sources × 2 bits each)."""

    raw_bits: int
    alarms: tuple[tuple[int, str, int], ...]  # (code, name, severity 0..3)


ALARM_NAMES: Final = (
    "Cell overvoltage",
    "Cell undervoltage",
    "Total voltage overvoltage",
    "Total voltage undervoltage",
    "Large pressure difference of monomer",
    "Discharge overcurrent",
    "Charge overcurrent",
    "Temperature is too high",
    "Temperature is too low",
    "Excessive temperature difference",
    "SOC too low",
    "Insulation is too low",
    "High voltage interlock fault",
    "External communication failure",
    "Internal communication failure",
)


def _u16le(data: bytes, off: int) -> int:
    return struct.unpack_from("<H", data, off)[0]


def _u8(data: bytes, off: int) -> int:
    return data[off]


def decode_main_status(data: bytes) -> MainStatus:
    """0x2F4: 8 bytes — voltage × 0.1, SoC raw %, current (×0.1, offset −400 A)."""
    if len(data) != 8:
        raise ValueError(f"expected 8 bytes, got {len(data)}")
    total_voltage_v = _u16le(data, 0) * 0.1
    raw_current = _u16le(data, 2)
    if raw_current > 32767:
        raw_current -= 65536
    current = (raw_current * 0.1) - 400
    soc = _u8(data, 4)
    return MainStatus(
        total_voltage_v=total_voltage_v,
        soc_percentage=soc,
        total_current_a=current,
    )


def decode_cell_minmax(data: bytes) -> CellMinMax:
    """0x4F4: 8 bytes — max cell V (u16 mV) + position, min cell V (u16 mV) + position.

    Note: we compute `delta = max − min` explicitly rather than relying on a
    single-byte field — this gives the correct delta for the full 16-bit range.
    """
    if len(data) != 8:
        raise ValueError(f"expected 8 bytes, got {len(data)}")
    max_voltage = _u16le(data, 0) * 0.001
    max_position = _u8(data, 2)
    min_voltage = _u16le(data, 3) * 0.001
    min_position = _u8(data, 5)
    delta = max_voltage - min_voltage
    return CellMinMax(
        cell_voltage_delta_v=delta,
        cell_voltage_max_v=max_voltage,
        cell_voltage_max_number=max_position,
        cell_voltage_min_v=min_voltage,
        cell_voltage_min_number=min_position,
    )


def decode_temperatures(data: bytes) -> Temperatures:
    """0x5F4: temperatures with the JK-typical -50 °C offset."""
    if len(data) != 8:
        raise ValueError(f"expected 8 bytes, got {len(data)}")
    return Temperatures(
        max_temp_c=_u8(data, 0) - 50,
        max_temp_position=_u8(data, 1),
        min_temp_c=_u8(data, 2) - 50,
        min_temp_position=_u8(data, 3),
        avg_temp_c=_u8(data, 4) - 50,
    )


def decode_power_current(data: bytes) -> PowerCurrent:
    """0x18F128F4: signed current (×0.001 A), power (×0.1 W), cycle count."""
    if len(data) != 8:
        raise ValueError(f"expected 8 bytes, got {len(data)}")
    raw_current = _u16le(data, 0)
    if raw_current > 32767:
        raw_current -= 65536
    current = raw_current * 0.001
    power = _u16le(data, 2) * 0.1
    cycle = _u16le(data, 6)
    return PowerCurrent(
        total_current_a=current,
        total_power_w=power,
        cycle_count=cycle,
    )


def decode_monitoring(data: bytes) -> Monitoring:
    """0x01F21400: voltage (×0.01), signed current (×0.1), signed temp (÷10), cycles."""
    if len(data) != 8:
        raise ValueError(f"expected 8 bytes, got {len(data)}")
    voltage = _u16le(data, 0) * 0.01
    raw_current = _u16le(data, 2)
    if raw_current > 32767:
        raw_current -= 65536
    current = raw_current * 0.1
    raw_temp = _u16le(data, 4)
    if raw_temp > 32767:
        raw_temp -= 65536
    temperature = raw_temp / 10
    cycle = _u16le(data, 6)
    return Monitoring(
        total_voltage_v=voltage,
        total_current_a=current,
        pack_temperature_c=temperature,
        cycle_count=cycle,
    )


def decode_individual_temps(data: bytes) -> IndividualTemps:
    """0x18F228F4: bytes 1..5 are probe temperatures (offset −50 °C); 0 = unused.

    By convention the first non-zero probe is the MOSFET temperature.
    """
    if len(data) != 8:
        raise ValueError(f"expected 8 bytes, got {len(data)}")
    temps: list[int] = []
    for i in range(1, 6):
        if data[i] != 0:
            temps.append(data[i] - 50)
    mos = temps[0] if temps else None
    return IndividualTemps(
        temperatures_c=tuple(temps),
        mos_temp_c=mos,
    )


def decode_cell_voltages(can_id: int, data: bytes) -> CellVoltages:
    """0x18E[0-3]28F4: 4 cells (uint16le mV) per frame.

    The group index is encoded in nibble 4 of the canonical ID: e.g. `0x18E1...`
    → group 1 → cells 5..8.
    """
    if len(data) != 8:
        raise ValueError(f"expected 8 bytes, got {len(data)}")
    group = (can_id >> 16) & 0x0F  # bits 19..16 carry 0..3
    voltages = tuple(_u16le(data, 2 * i) * 0.001 for i in range(4))
    return CellVoltages(group_index=group, voltages_v=voltages)


def decode_alarm_info(data: bytes) -> AlarmInfo:
    """0x7F4: 15 alarms × 2 bits of severity (0=none, 1=serious, 2=important, 3=general)."""
    if len(data) != 8:
        raise ValueError(f"expected 8 bytes, got {len(data)}")
    raw = 0
    for i in range(4):
        raw |= data[i] << (8 * i)
    alarms: list[tuple[int, str, int]] = []
    for i, name in enumerate(ALARM_NAMES):
        level = (raw >> (i * 2)) & 0x03
        if level > 0:
            alarms.append((i + 1, name, level))
    return AlarmInfo(raw_bits=raw, alarms=tuple(alarms))


def is_cell_voltage_id(can_id: int) -> bool:
    """True if *can_id* is in the 0x18E[0-3]28F4 cell-voltage group."""
    return (can_id & ID_CELL_VOLT_MASK) == ID_CELL_VOLT_BASE and ((can_id >> 16) & 0x0F) in (
        0,
        1,
        2,
        3,
    )
