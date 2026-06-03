"""Register-offset decoder for the JK BMS RS485 Modbus V1.0 / V1.1 protocol.

Pure-functional: every function takes a ``list[int]`` of register words (as
returned by ``pymodbus.ReadHoldingRegistersResponse.registers``) and returns a
frozen dataclass. No I/O, no logging, no side effects — exhaustive unit
+ property + mutation testing targets this module.

Register base addresses (from the BMS RS485 Modbus V1.0 / V1.1 spec, mirrored
in ``docs/protocol/``):

- ``0x1000`` writable settings (function 0x03 reads, function 0x10 writes).
- ``0x1200`` real-time block (read-only): cells, V/I/P, SoC, temperatures,
  alarms.
- ``0x1400`` static device info (read-only): model, FW, SW, serial.

All offsets in this module are **word offsets from the block base** (1 word =
2 bytes). The reply data on the wire is big-endian; multi-register integers
compose as ``(hi << 16) | lo`` where ``hi`` is the lower-addressed word — i.e.
register-big-endian (the JK convention, matching SEH / ciciban / the official
spec).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# -- Block bases ----------------------------------------------------------------------

BASE_CFG: Final = 0x1000
BASE_RT: Final = 0x1200
BASE_INFO: Final = 0x1400


# -- Real-time block (offsets in words from ``BASE_RT``) -----------------------------

_OFF_CELL_VOLT_0: Final = 0x00       # uint16 ×16, mV
_OFF_CELL_PRESENT: Final = 0x20      # uint32 — bitmap, bit N = cell N+1 present
_OFF_CELL_AVG_V: Final = 0x22        # uint16, mV
_OFF_CELL_DELTA: Final = 0x23        # uint16, mV
_OFF_MOS_TEMP: Final = 0x45          # int16  × 0.1 °C
_OFF_TOTAL_V: Final = 0x48           # uint32, mV
_OFF_TOTAL_POWER: Final = 0x4A       # uint32, mW (magnitude)
_OFF_TOTAL_CURRENT: Final = 0x4C     # int32,  mA  (+ = charge, − = discharge)
_OFF_PROBE_1_TEMP: Final = 0x4E      # int16  × 0.1 °C
_OFF_PROBE_2_TEMP: Final = 0x4F      # int16  × 0.1 °C
_OFF_ALARM_BITS: Final = 0x50        # uint32 — alarm bitmap
_OFF_BALANCE_CURRENT: Final = 0x52   # int16,  mA
_OFF_BALANCE_STATE_SOC: Final = 0x53 # u8 balance_state | u8 SOC %
_OFF_REMAINING_CAP: Final = 0x54     # int32,  mAh
_OFF_NOMINAL_CAP: Final = 0x56       # uint32, mAh
_OFF_CYCLE_COUNT: Final = 0x58       # uint32
_OFF_TOTAL_CYCLE_CAP: Final = 0x5A   # uint32, mAh (lifetime accumulated)
_OFF_SOH_PRECHARGE: Final = 0x5C     # u8 SoH | u8 precharge
_OFF_RUNTIME: Final = 0x5E           # uint32, seconds
_OFF_CHARGE_DISCHARGE: Final = 0x60  # u8 charge_enabled | u8 discharge_enabled
_OFF_HEATING_CURRENT: Final = 0x64   # int16,  mA (PB-series heating element)
_OFF_HEATING_STATE: Final = 0x65     # u16 — non-zero = heater on
_OFF_CHARGE_STATUS: Final = 0x6C     # u16 — charge FSM id (stand-by / bulk / abs / float)
_OFF_CHARGE_STATUS_TIME: Final = 0x6D # u16 — seconds spent in current FSM state
_OFF_PROBE_3_TEMP: Final = 0x7C      # int16  × 0.1 °C
_OFF_PROBE_4_TEMP: Final = 0x7D      # int16  × 0.1 °C
_OFF_PROBE_5_TEMP: Final = 0x7E      # int16  × 0.1 °C
_OFF_CELL_RES_0: Final = 0x80        # uint16 ×16, mΩ — per-cell internal resistance

MAX_CELLS: Final = 16
RT_BLOCK_WORDS: Final = 0x110        # total real-time block size we expect
INFO_BLOCK_WORDS: Final = 0x50       # static info block size


# -- Static-info block (offsets in words from ``BASE_INFO``) -------------------------

_OFF_MODEL: Final = 0x00            # ASCII 16 bytes (8 words)
_OFF_HW_VERSION: Final = 0x08       # ASCII  8 bytes (4 words)
_OFF_SW_VERSION: Final = 0x0C       # ASCII  8 bytes (4 words)
_OFF_SERIAL: Final = 0x28           # ASCII 16 bytes (8 words)


# -- Alarm bit names (from the V1.0 spec) --------------------------------------------

ALARM_NAMES: Final = (
    "wire_resistance_too_high",        # bit 0
    "mos_overtemperature",             # bit 1
    "cell_count_mismatch",             # bit 2
    "current_sensor_fault",            # bit 3
    "cell_overvoltage",                # bit 4
    "battery_overvoltage",             # bit 5
    "charge_overcurrent",              # bit 6
    "charge_short_circuit",            # bit 7
    "charge_overtemperature",          # bit 8
    "charge_undertemperature",         # bit 9
    "internal_communication_error",    # bit 10
    "cell_undervoltage",               # bit 11
    "battery_undervoltage",            # bit 12
    "discharge_overcurrent",           # bit 13
    "discharge_short_circuit",         # bit 14
    "discharge_overtemperature",       # bit 15
    "charge_mosfet_fault",             # bit 16
    "discharge_mosfet_fault",          # bit 17
    "gps_disconnected",                # bit 18
    "modify_password",                 # bit 19
    "discharge_startup_failure",       # bit 20
    "battery_overheat_alarm",          # bit 21
)


# Charge-FSM state codes as published in the real-time block.
CHARGE_STATUS_NAMES: Final = {
    0: "standby",
    1: "bulk",
    2: "absorption",
    3: "float",
    4: "request_full_charge",
}


# -- Decoded dataclasses --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JkRealtime:
    """Decoded contents of the real-time block at ``0x1200``.

    ``cell_voltages_v`` and the matching min/max statistics only count cells the
    BMS reports as present via the cell-present bitmap at offset ``0x20``.
    """

    cell_voltages_v: tuple[float, ...]
    cell_resistances_ohm: tuple[float, ...]   # per-cell internal resistance, populated cells only
    cell_voltage_avg_v: float
    cell_voltage_delta_v: float
    cell_voltage_max_v: float
    cell_voltage_min_v: float
    cell_voltage_max_number: int       # 1-indexed
    cell_voltage_min_number: int
    cell_count: int

    total_voltage_v: float
    total_current_a: float             # signed: + = charge, − = discharge
    total_power_w: float               # signed (V × I)

    mos_temp_c: float
    probe_1_temp_c: float
    probe_2_temp_c: float
    probe_3_temp_c: float
    probe_4_temp_c: float
    probe_5_temp_c: float

    balance_current_a: float
    balance_active: bool

    soc_percentage: int
    soh_percentage: int
    remaining_capacity_ah: float
    nominal_capacity_ah: float
    cycle_count: int
    total_cycle_capacity_ah: float
    runtime_s: int

    charge_enabled: bool
    discharge_enabled: bool

    heating_active: bool
    heating_current_a: float

    charge_status_id: int
    charge_status: str                 # decoded name, or empty if unknown id
    charge_status_time_s: int

    alarm_bits: int
    alarms: tuple[str, ...]
    alarms_csv: str                    # alarms joined with ',' — friendlier for HA dashboards


@dataclass(frozen=True, slots=True)
class JkStaticInfo:
    """Decoded contents of the static-info block at ``0x1400``."""

    model: str
    hw_version: str
    sw_version: str
    serial_number: str


# -- Primitive helpers ----------------------------------------------------------------


def _u16(regs: list[int], off: int) -> int:
    """Unsigned 16-bit register word."""
    return regs[off] & 0xFFFF


def _i16(regs: list[int], off: int) -> int:
    """Signed 16-bit register word (two's complement)."""
    v = regs[off] & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _u32(regs: list[int], off: int) -> int:
    """Unsigned 32-bit composed from two consecutive registers (hi first)."""
    return ((regs[off] & 0xFFFF) << 16) | (regs[off + 1] & 0xFFFF)


def _i32(regs: list[int], off: int) -> int:
    """Signed 32-bit composed from two consecutive registers (hi first)."""
    v = _u32(regs, off)
    return v - 0x1_0000_0000 if v >= 0x8000_0000 else v


def _ascii(regs: list[int], off: int, length_bytes: int) -> str:
    """Decode an ASCII string spanning ``length_bytes // 2`` registers.

    Each register word carries two ASCII bytes (hi then lo). NULs and trailing
    whitespace are stripped — the BMS pads with NULs.
    """
    out: list[str] = []
    for i in range(length_bytes // 2):
        w = regs[off + i] & 0xFFFF
        hi = (w >> 8) & 0xFF
        lo = w & 0xFF
        if hi:
            out.append(chr(hi))
        if lo:
            out.append(chr(lo))
    return "".join(out).rstrip("\x00").strip()


# -- Top-level decoders ---------------------------------------------------------------


def decode_realtime(regs: list[int]) -> JkRealtime:
    """Decode the real-time block starting at register ``0x1200``.

    ``regs`` should be a list of at least ``RT_BLOCK_WORDS`` register words
    (zero-fill is fine for words the BMS didn't return; the runner stitches
    blocks A/B/C and zeros the gaps). Raises ``ValueError`` if the list is
    too short for the minimum data we always need (cells through SoC).
    """
    if len(regs) < RT_BLOCK_WORDS:
        raise ValueError(
            f"regs list too short: got {len(regs)} words, need {RT_BLOCK_WORDS}"
        )

    # Cell voltages — gated by the present-bitmap so non-16S packs don't report
    # phantom 0 V cells. Bit N of the bitmap = cell (N+1) is enabled.
    present = _u32(regs, _OFF_CELL_PRESENT)
    cells: list[float] = []
    resistances: list[float] = []
    for i in range(MAX_CELLS):
        if present & (1 << i):
            mv = _u16(regs, _OFF_CELL_VOLT_0 + i)
            cells.append(mv / 1000.0)
            mohm = _u16(regs, _OFF_CELL_RES_0 + i)
            resistances.append(mohm / 1000.0)

    cell_count = len(cells)
    if cells:
        cell_max_v = max(cells)
        cell_min_v = min(cells)
        cell_max_number = cells.index(cell_max_v) + 1
        cell_min_number = cells.index(cell_min_v) + 1
        cell_avg_v = sum(cells) / cell_count
        cell_delta_v = cell_max_v - cell_min_v
    else:
        # No cells reported as present — fall back to the BMS's own averages.
        cell_max_v = cell_min_v = 0.0
        cell_max_number = cell_min_number = 0
        cell_avg_v = _u16(regs, _OFF_CELL_AVG_V) / 1000.0
        cell_delta_v = _u16(regs, _OFF_CELL_DELTA) / 1000.0

    total_v = _u32(regs, _OFF_TOTAL_V) / 1000.0
    total_current_a = _i32(regs, _OFF_TOTAL_CURRENT) / 1000.0
    total_power_w = total_v * total_current_a  # signed by current

    bal_state_soc = _u16(regs, _OFF_BALANCE_STATE_SOC)
    balance_state = (bal_state_soc >> 8) & 0xFF
    soc = bal_state_soc & 0xFF

    soh_precharge = _u16(regs, _OFF_SOH_PRECHARGE)
    soh = (soh_precharge >> 8) & 0xFF

    chg_dis = _u16(regs, _OFF_CHARGE_DISCHARGE)
    charge_enabled = bool((chg_dis >> 8) & 0xFF)
    discharge_enabled = bool(chg_dis & 0xFF)

    alarm_bits = _u32(regs, _OFF_ALARM_BITS)
    alarms = tuple(
        ALARM_NAMES[i]
        for i in range(len(ALARM_NAMES))
        if alarm_bits & (1 << i)
    )

    charge_status_id = _u16(regs, _OFF_CHARGE_STATUS)
    charge_status_name = CHARGE_STATUS_NAMES.get(charge_status_id, "")

    return JkRealtime(
        cell_voltages_v=tuple(cells),
        cell_resistances_ohm=tuple(resistances),
        cell_voltage_avg_v=cell_avg_v,
        cell_voltage_delta_v=cell_delta_v,
        cell_voltage_max_v=cell_max_v,
        cell_voltage_min_v=cell_min_v,
        cell_voltage_max_number=cell_max_number,
        cell_voltage_min_number=cell_min_number,
        cell_count=cell_count,
        total_voltage_v=total_v,
        total_current_a=total_current_a,
        total_power_w=total_power_w,
        mos_temp_c=_i16(regs, _OFF_MOS_TEMP) / 10.0,
        probe_1_temp_c=_i16(regs, _OFF_PROBE_1_TEMP) / 10.0,
        probe_2_temp_c=_i16(regs, _OFF_PROBE_2_TEMP) / 10.0,
        probe_3_temp_c=_i16(regs, _OFF_PROBE_3_TEMP) / 10.0,
        probe_4_temp_c=_i16(regs, _OFF_PROBE_4_TEMP) / 10.0,
        probe_5_temp_c=_i16(regs, _OFF_PROBE_5_TEMP) / 10.0,
        balance_current_a=_i16(regs, _OFF_BALANCE_CURRENT) / 1000.0,
        balance_active=balance_state != 0,
        soc_percentage=soc,
        soh_percentage=soh,
        remaining_capacity_ah=_i32(regs, _OFF_REMAINING_CAP) / 1000.0,
        nominal_capacity_ah=_u32(regs, _OFF_NOMINAL_CAP) / 1000.0,
        cycle_count=_u32(regs, _OFF_CYCLE_COUNT),
        total_cycle_capacity_ah=_u32(regs, _OFF_TOTAL_CYCLE_CAP) / 1000.0,
        runtime_s=_u32(regs, _OFF_RUNTIME),
        charge_enabled=charge_enabled,
        discharge_enabled=discharge_enabled,
        heating_active=_u16(regs, _OFF_HEATING_STATE) != 0,
        heating_current_a=_i16(regs, _OFF_HEATING_CURRENT) / 1000.0,
        charge_status_id=charge_status_id,
        charge_status=charge_status_name,
        charge_status_time_s=_u16(regs, _OFF_CHARGE_STATUS_TIME),
        alarm_bits=alarm_bits,
        alarms=alarms,
        alarms_csv=",".join(alarms),
    )


def decode_static_info(regs: list[int]) -> JkStaticInfo:
    """Decode the static-info block starting at register ``0x1400``."""
    if len(regs) < INFO_BLOCK_WORDS:
        raise ValueError(
            f"regs list too short: got {len(regs)} words, need {INFO_BLOCK_WORDS}"
        )
    return JkStaticInfo(
        model=_ascii(regs, _OFF_MODEL, 16),
        hw_version=_ascii(regs, _OFF_HW_VERSION, 8),
        sw_version=_ascii(regs, _OFF_SW_VERSION, 8),
        serial_number=_ascii(regs, _OFF_SERIAL, 16),
    )


def format_runtime(total_seconds: int) -> str:
    """Format runtime seconds as ``DDDdHHhMMm`` for HA display."""
    if total_seconds < 0:
        raise ValueError("total_seconds must be non-negative")
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    return f"{days}d{hours:02d}h{minutes:02d}m"
