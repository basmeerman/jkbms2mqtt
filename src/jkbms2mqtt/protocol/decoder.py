"""Decode validated JK-BMS reply frames into typed Python values.

Byte offsets here are taken from the documented JK-BMS RS485 protocol layout for
the three reply frames (fixed / setup / live). All offsets are absolute into
the full received buffer, i.e. they include the 4-byte magic header.

Notable behaviours:
- `cell_voltage_delta = max − min`, computed over the populated cells only.
- The pack `cell_count` is honoured for averaging and min/max — no
  implicit-16 assumption that breaks non-16S packs.
- Probe 4 is stored at byte offset 256 and probe 3 at 258 (the protocol layout
  has them in that order). This is preserved exactly so historic temperature
  data stays consistent.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Final

MAX_CELLS: Final = 16


@dataclass(frozen=True, slots=True)
class LiveData:
    """Decoded Trame 3 live data.

    `cell_voltages_v` and `cell_resistances_ohm` are tuples whose length is the BMS-reported
    `cell_count` — never padded.
    """

    cell_voltages_v: tuple[float, ...]
    cell_resistances_ohm: tuple[float, ...]
    cell_voltage_average_v: float
    cell_voltage_delta_v: float
    cell_voltage_max_v: float
    cell_voltage_min_v: float
    cell_voltage_max_number: int  # 1-indexed cell index
    cell_voltage_min_number: int

    mos_temp_c: float
    probe_1_temp_c: float
    probe_2_temp_c: float
    probe_3_temp_c: float
    probe_4_temp_c: float

    total_voltage_v: float
    total_current_a: float
    total_power_w: float

    balance_current_a: float
    balance_action: int

    soc_percentage: int
    soh_percentage: int
    remaining_capacity_ah: float
    battery_capacity_ah: float
    cycle_count: int
    cycle_capacity_ah: float

    total_runtime_s: int

    switch_charge: bool
    switch_discharge: bool
    switch_balance: bool

    heating: bool
    heating_current_a: float

    charge_status: int
    charge_status_time_s: int


@dataclass(frozen=True, slots=True)
class SetupData:
    """Decoded Trame 2 configuration / settings frame.

    Each field carries the BMS's *current* setting, used to seed HA state topics and
    by the write executor for the read-modify-write of the packed-bit register at 0x1114.
    """

    smart_sleep_voltage_v: float
    cell_voltage_undervoltage_protection_v: float
    cell_voltage_undervoltage_recovery_v: float
    cell_voltage_overvoltage_protection_v: float
    cell_voltage_overvoltage_recovery_v: float
    balance_trigger_voltage_v: float
    cell_soc100_voltage_v: float
    cell_soc0_voltage_v: float
    cell_request_charge_voltage_v: float
    cell_request_float_voltage_v: float
    power_off_voltage_v: float
    max_charge_current_a: float
    charge_overcurrent_protection_delay_s: int
    charge_overcurrent_protection_recovery_time_s: int
    max_discharge_current_a: float
    discharge_overcurrent_protection_delay_s: int
    discharge_overcurrent_protection_recovery_time_s: int
    short_circuit_protection_recovery_time_s: int
    max_balance_current_a: float
    charge_overtemperature_protection_c: float
    charge_overtemperature_protection_recovery_c: float
    discharge_overtemperature_protection_c: float
    discharge_overtemperature_protection_recovery_c: float
    charge_undertemperature_protection_c: float
    charge_undertemperature_protection_recovery_c: float
    power_tube_overtemperature_protection_c: float
    power_tube_overtemperature_protection_recovery_c: float
    cell_count: int
    charging_switch: bool
    discharging_switch: bool
    balance_switch: bool
    total_battery_capacity_ah: float
    short_circuit_protection_delay_s: int
    balance_starting_voltage_v: float
    connection_wire_resistance_1_ohm: float
    device_address: int
    display_always_on_switch: bool
    smart_sleep_switch: bool
    disable_pcl_module_switch: bool
    timed_stored_data_switch: bool


@dataclass(frozen=True, slots=True)
class FixedData:
    """Decoded Trame 1 static-info frame."""

    bms_model: str
    firmware_version: str
    software_version: str
    uptime_s: int
    power_on_count: int
    serial_number: str
    manufacturing_date: str
    brand: str
    uart1_protocol_number: int
    can_protocol_number: int
    lcd_buzzer_trigger: int
    lcd_buzzer_trigger_value: int
    lcd_buzzer_release_value: int
    request_charge_voltage_time_h: int
    request_float_voltage_time_h: int


# -- decoding primitives ---------------------------------------------------------------------


def _u8(buf: bytes, off: int) -> int:
    return buf[off]


def _u16le(buf: bytes, off: int) -> int:
    return struct.unpack_from("<H", buf, off)[0]


def _i16le(buf: bytes, off: int) -> int:
    return struct.unpack_from("<h", buf, off)[0]


def _u32le(buf: bytes, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


def _i32le(buf: bytes, off: int) -> int:
    return struct.unpack_from("<i", buf, off)[0]


def _ascii(buf: bytes, off: int, length: int) -> str:
    return buf[off : off + length].rstrip(b"\x00").decode("ascii", errors="replace")


# -- Trame 3: live data ----------------------------------------------------------------------

# Byte offsets per the JK-BMS live-data ("Trame 3") frame layout:
_CELL_V_BASE = 6  # uint16le ×16 cells, scale /1000
_CELL_OHM_BASE = 80  # int16le ×16 cells, scale /1000
_OFF_MOS_TEMP = 144
_OFF_TOTAL_POWER = 154
_OFF_TOTAL_CURRENT = 158
_OFF_PROBE_1 = 162
_OFF_PROBE_2 = 164
_OFF_BALANCE_CURRENT = 170
_OFF_BALANCE_ACTION = 172
_OFF_SOC = 173
_OFF_REMAINING_CAP = 174
_OFF_BATTERY_CAP = 178
_OFF_CYCLE_COUNT = 182
_OFF_CYCLE_CAPACITY = 186
_OFF_SOH = 190
_OFF_TOTAL_RUNTIME = 194
_OFF_SWITCH_CHARGE = 198
_OFF_SWITCH_DISCHARGE = 199
_OFF_SWITCH_BALANCE = 200
_OFF_HEATING = 215
_OFF_TOTAL_VOLTAGE = 234
_OFF_HEATING_CURRENT = 236
_OFF_PROBE_4 = 256  # NOTE: probe 4 sits BEFORE probe 3 in the buffer layout
_OFF_PROBE_3 = 258
_OFF_CHARGE_STATUS_TIME = 278
_OFF_CHARGE_STATUS = 280


def decode_live(raw: bytes, cell_count: int) -> LiveData:
    """Decode a Trame 3 (live data) frame.

    *raw* must be the full received buffer including the 4-byte magic header
    (i.e. `JkFrame.raw`). *cell_count* should come from the most recent setup
    frame; if unknown at first poll, pass `MAX_CELLS` and the over-read will
    appear as zero-volt cells which the min/max ignores.
    """
    if cell_count < 1 or cell_count > MAX_CELLS:
        raise ValueError(f"cell_count must be 1..{MAX_CELLS}, got {cell_count}")
    if len(raw) <= _OFF_CHARGE_STATUS:
        raise ValueError(f"frame too short ({len(raw)}) to be a live frame")

    cells_v = tuple(
        _u16le(raw, _CELL_V_BASE + 2 * i) / 1000.0 for i in range(cell_count)
    )
    cells_ohm = tuple(
        _i16le(raw, _CELL_OHM_BASE + 2 * i) / 1000.0 for i in range(cell_count)
    )

    # Cell stats over populated cells only — closes #130 (no /16 hardcode) and #128
    # (delta = max - min, not = max).
    cell_max_v = max(cells_v)
    cell_min_v = min(cells_v)
    cell_avg_v = sum(cells_v) / cell_count
    cell_delta_v = cell_max_v - cell_min_v
    # 1-indexed cell number (matches the common MQTT convention)
    cell_max_number = cells_v.index(cell_max_v) + 1
    cell_min_number = cells_v.index(cell_min_v) + 1

    return LiveData(
        cell_voltages_v=cells_v,
        cell_resistances_ohm=cells_ohm,
        cell_voltage_average_v=cell_avg_v,
        cell_voltage_delta_v=cell_delta_v,
        cell_voltage_max_v=cell_max_v,
        cell_voltage_min_v=cell_min_v,
        cell_voltage_max_number=cell_max_number,
        cell_voltage_min_number=cell_min_number,
        mos_temp_c=_i16le(raw, _OFF_MOS_TEMP) / 10.0,
        probe_1_temp_c=_i16le(raw, _OFF_PROBE_1) / 10.0,
        probe_2_temp_c=_i16le(raw, _OFF_PROBE_2) / 10.0,
        probe_3_temp_c=_i16le(raw, _OFF_PROBE_3) / 10.0,
        probe_4_temp_c=_i16le(raw, _OFF_PROBE_4) / 10.0,
        total_voltage_v=_u16le(raw, _OFF_TOTAL_VOLTAGE) / 100.0,
        total_current_a=_i32le(raw, _OFF_TOTAL_CURRENT) / 1000.0,
        total_power_w=_u32le(raw, _OFF_TOTAL_POWER) / 1000.0,
        balance_current_a=_i16le(raw, _OFF_BALANCE_CURRENT) / 1000.0,
        balance_action=_u8(raw, _OFF_BALANCE_ACTION),
        soc_percentage=_u8(raw, _OFF_SOC),
        soh_percentage=_u8(raw, _OFF_SOH),
        remaining_capacity_ah=_i32le(raw, _OFF_REMAINING_CAP) / 1000.0,
        battery_capacity_ah=_i32le(raw, _OFF_BATTERY_CAP) / 1000.0,
        cycle_count=_i32le(raw, _OFF_CYCLE_COUNT),
        cycle_capacity_ah=_i32le(raw, _OFF_CYCLE_CAPACITY) / 1000.0,
        total_runtime_s=_u32le(raw, _OFF_TOTAL_RUNTIME),
        switch_charge=bool(_u8(raw, _OFF_SWITCH_CHARGE)),
        switch_discharge=bool(_u8(raw, _OFF_SWITCH_DISCHARGE)),
        switch_balance=bool(_u8(raw, _OFF_SWITCH_BALANCE)),
        heating=bool(_u8(raw, _OFF_HEATING)),
        heating_current_a=_i16le(raw, _OFF_HEATING_CURRENT) / 1000.0,
        charge_status=_u8(raw, _OFF_CHARGE_STATUS),
        charge_status_time_s=_u16le(raw, _OFF_CHARGE_STATUS_TIME),
    )


# -- Trame 2: setup / configuration ----------------------------------------------------------

# Byte offsets per the JK-BMS setup ("Trame 2") frame layout:
_OFF_SMART_SLEEP_V = 6
_OFF_UVP = 10
_OFF_UVPR = 14
_OFF_OVP = 18
_OFF_OVPR = 22
_OFF_BAL_TRIG = 26
_OFF_SOC100 = 30
_OFF_SOC0 = 34
_OFF_REQ_CHG = 38
_OFF_REQ_FLOAT = 42
_OFF_POWER_OFF = 46
_OFF_MAX_CHG = 50
_OFF_CHG_OCP_DELAY = 54
_OFF_CHG_OCP_RECOVERY = 58
_OFF_MAX_DCHG = 62
_OFF_DCHG_OCP_DELAY = 66
_OFF_DCHG_OCP_RECOVERY = 70
_OFF_SCP_RECOVERY = 74
_OFF_MAX_BAL = 78
_OFF_CHG_OTP = 82
_OFF_CHG_OTP_R = 86
_OFF_DCHG_OTP = 90
_OFF_DCHG_OTP_R = 94
_OFF_CHG_UTP = 98
_OFF_CHG_UTP_R = 102
_OFF_PT_OTP = 106
_OFF_PT_OTP_R = 110
_OFF_CELL_COUNT = 114
_OFF_CHG_SW = 118
_OFF_DCHG_SW = 122
_OFF_BAL_SW = 126
_OFF_TOTAL_CAP = 130
_OFF_SCP_DELAY = 134
_OFF_BAL_START_V = 138
_OFF_WIRE_R1 = 158
_OFF_DEV_ADDR = 270
_OFF_DISPLAY_ALWAYS_ON = 282  # bit 5 of the packed-flags byte
_OFF_SMART_SLEEP_SW = 282  # bit 6
_OFF_DISABLE_PCL_SW = 282  # bit 7
_OFF_TIMED_STORED_SW = 283  # lowest bit of the second flags byte


def decode_setup(raw: bytes) -> SetupData:
    """Decode a Trame 2 (setup) frame.

    The packed-bit fields at offset 282 are read by bitmask so we don't depend
    on the convention of splitting "bool at offset 282" into separate
    booleans — they all live in the same byte.
    """
    if len(raw) <= _OFF_TIMED_STORED_SW:
        raise ValueError(f"frame too short ({len(raw)}) to be a setup frame")

    packed = _u8(raw, _OFF_DISPLAY_ALWAYS_ON)

    return SetupData(
        smart_sleep_voltage_v=_i32le(raw, _OFF_SMART_SLEEP_V) / 1000.0,
        cell_voltage_undervoltage_protection_v=_i32le(raw, _OFF_UVP) / 1000.0,
        cell_voltage_undervoltage_recovery_v=_i32le(raw, _OFF_UVPR) / 1000.0,
        cell_voltage_overvoltage_protection_v=_i32le(raw, _OFF_OVP) / 1000.0,
        cell_voltage_overvoltage_recovery_v=_i32le(raw, _OFF_OVPR) / 1000.0,
        balance_trigger_voltage_v=_i32le(raw, _OFF_BAL_TRIG) / 1000.0,
        cell_soc100_voltage_v=_i32le(raw, _OFF_SOC100) / 1000.0,
        cell_soc0_voltage_v=_i32le(raw, _OFF_SOC0) / 1000.0,
        cell_request_charge_voltage_v=_i32le(raw, _OFF_REQ_CHG) / 1000.0,
        cell_request_float_voltage_v=_i32le(raw, _OFF_REQ_FLOAT) / 1000.0,
        power_off_voltage_v=_i32le(raw, _OFF_POWER_OFF) / 1000.0,
        max_charge_current_a=_i32le(raw, _OFF_MAX_CHG) / 1000.0,
        charge_overcurrent_protection_delay_s=_i32le(raw, _OFF_CHG_OCP_DELAY),
        charge_overcurrent_protection_recovery_time_s=_i32le(raw, _OFF_CHG_OCP_RECOVERY),
        max_discharge_current_a=_i32le(raw, _OFF_MAX_DCHG) / 1000.0,
        discharge_overcurrent_protection_delay_s=_i32le(raw, _OFF_DCHG_OCP_DELAY),
        discharge_overcurrent_protection_recovery_time_s=_i32le(raw, _OFF_DCHG_OCP_RECOVERY),
        short_circuit_protection_recovery_time_s=_i32le(raw, _OFF_SCP_RECOVERY),
        max_balance_current_a=_i32le(raw, _OFF_MAX_BAL) / 1000.0,
        charge_overtemperature_protection_c=_i32le(raw, _OFF_CHG_OTP) / 10.0,
        charge_overtemperature_protection_recovery_c=_i32le(raw, _OFF_CHG_OTP_R) / 10.0,
        discharge_overtemperature_protection_c=_i32le(raw, _OFF_DCHG_OTP) / 10.0,
        discharge_overtemperature_protection_recovery_c=_i32le(raw, _OFF_DCHG_OTP_R) / 10.0,
        charge_undertemperature_protection_c=_i32le(raw, _OFF_CHG_UTP) / 10.0,
        charge_undertemperature_protection_recovery_c=_i32le(raw, _OFF_CHG_UTP_R) / 10.0,
        power_tube_overtemperature_protection_c=_i32le(raw, _OFF_PT_OTP) / 10.0,
        power_tube_overtemperature_protection_recovery_c=_i32le(raw, _OFF_PT_OTP_R) / 10.0,
        cell_count=_i32le(raw, _OFF_CELL_COUNT),
        charging_switch=bool(_u8(raw, _OFF_CHG_SW)),
        discharging_switch=bool(_u8(raw, _OFF_DCHG_SW)),
        balance_switch=bool(_u8(raw, _OFF_BAL_SW)),
        total_battery_capacity_ah=_i32le(raw, _OFF_TOTAL_CAP) / 1000.0,
        short_circuit_protection_delay_s=_i32le(raw, _OFF_SCP_DELAY),
        balance_starting_voltage_v=_i32le(raw, _OFF_BAL_START_V) / 1000.0,
        connection_wire_resistance_1_ohm=_i32le(raw, _OFF_WIRE_R1) / 1000.0,
        device_address=_i32le(raw, _OFF_DEV_ADDR),
        display_always_on_switch=bool(packed & 0x20),
        smart_sleep_switch=bool(packed & 0x40),
        disable_pcl_module_switch=bool(packed & 0x80),
        timed_stored_data_switch=bool(_u8(raw, _OFF_TIMED_STORED_SW) & 0x01),
    )


# -- Trame 1: static / device info -----------------------------------------------------------

# Byte offsets per the JK-BMS device-info ("Trame 1") frame layout:
_OFF_BMS_MODEL = 6
_LEN_BMS_MODEL = 13
_OFF_FW = 22
_LEN_FW = 8
_OFF_SW = 30
_LEN_SW = 8
_OFF_UPTIME = 38
_OFF_POWER_COUNT = 42
_OFF_SERIAL = 46
_LEN_SERIAL = 16
_OFF_MANUF_DATE = 78
_LEN_MANUF_DATE = 8
_OFF_BRAND = 102
_LEN_BRAND = 16
_OFF_UART1_PROTO = 184
_OFF_CAN_PROTO = 185
_OFF_LCD_BUZ_TRIG = 234
_OFF_LCD_BUZ_TRIG_VAL = 238
_OFF_LCD_BUZ_REL_VAL = 242
_OFF_REQ_CHG_TIME = 266
_OFF_REQ_FLOAT_TIME = 267


def decode_fixed(raw: bytes) -> FixedData:
    """Decode a Trame 1 (fixed / static device info) frame."""
    if len(raw) <= _OFF_REQ_FLOAT_TIME:
        raise ValueError(f"frame too short ({len(raw)}) to be a fixed frame")

    return FixedData(
        bms_model=_ascii(raw, _OFF_BMS_MODEL, _LEN_BMS_MODEL),
        firmware_version=_ascii(raw, _OFF_FW, _LEN_FW),
        software_version=_ascii(raw, _OFF_SW, _LEN_SW),
        uptime_s=_u32le(raw, _OFF_UPTIME),
        power_on_count=_u32le(raw, _OFF_POWER_COUNT),
        serial_number=_ascii(raw, _OFF_SERIAL, _LEN_SERIAL),
        manufacturing_date=_ascii(raw, _OFF_MANUF_DATE, _LEN_MANUF_DATE),
        brand=_ascii(raw, _OFF_BRAND, _LEN_BRAND),
        uart1_protocol_number=_u8(raw, _OFF_UART1_PROTO),
        can_protocol_number=_u8(raw, _OFF_CAN_PROTO),
        lcd_buzzer_trigger=_u8(raw, _OFF_LCD_BUZ_TRIG),
        lcd_buzzer_trigger_value=_u32le(raw, _OFF_LCD_BUZ_TRIG_VAL),
        lcd_buzzer_release_value=_u32le(raw, _OFF_LCD_BUZ_REL_VAL),
        request_charge_voltage_time_h=_u8(raw, _OFF_REQ_CHG_TIME),
        request_float_voltage_time_h=_u8(raw, _OFF_REQ_FLOAT_TIME),
    )


def format_runtime(total_seconds: int) -> str:
    """Format `Total_runtime_S` as `DDDdHHhMMm` (the conventional JK display format)."""
    if total_seconds < 0:
        raise ValueError("total_seconds must be non-negative")
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    return f"{days}d{hours:02d}h{minutes:02d}m"
