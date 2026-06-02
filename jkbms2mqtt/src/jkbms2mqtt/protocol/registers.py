"""Declarative register table for every writable JK-BMS parameter.

Each entry is the register address, encoding, and value range that the BMS
firmware accepts via Modbus function 0x10 (or 0x06 for packed-bit settings).
The encoder layer in `encoder.py` consults this table to convert Python values
into the 4-byte big-endian payload the BMS expects.

Each parameter carries a *tier* tag (`BASIC` / `SAFETY`). The MQTT publisher
and write executor honour this tag: a write of a SAFETY-tier parameter is
refused unless `enable_safety_writes` is true. See `MIGRATION.md` for the
policy rationale.

Three packed-bit settings (`disable_pcl_module_switch`, `smart_sleep_switch`,
`timed_stored_data_switch`) all live inside the single 16-bit register 0x1114
as bit flags. These use Modbus function 0x06 (write single register) with a
read-modify-write strategy implemented by the write executor.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, unique
from typing import Final


@unique
class WriteTier(str, Enum):
    """Two-tier safety gating for writes.

    - `BASIC`: operational tuning. Wrong values cause sub-optimal behaviour but
      cannot damage cells or pose a fire risk.
    - `SAFETY`: protection thresholds. Wrong values can damage cells, allow
      overcurrent, or cause fire. Off by default.
    """

    BASIC = "basic"
    SAFETY = "safety"


@unique
class Encoding(str, Enum):
    """Wire encodings used by the JK BMS for 4-byte register payloads.

    All multi-byte values are BIG-endian on the wire (note: Modbus convention)
    even though the *read* path uses little-endian. This asymmetry comes from
    the BMS firmware: the write path is big-endian, the read path is little-endian.
    """

    # u32 big-endian, raw value
    U32_RAW = "u32_raw"
    # u32 big-endian, value × 1000 (e.g. volts → millivolts)
    U32_MILLI = "u32_milli"
    # u32 big-endian, value × 10 (e.g. amps → deci-amps)
    U32_DECI = "u32_deci"
    # u32 big-endian, value × 10 then signed-extend interpretation (temperature 0.1 °C)
    I32_DECI = "i32_deci"
    # u32 big-endian, last byte = 0 or 1
    BOOL32 = "bool32"


@dataclass(frozen=True, slots=True)
class RegisterDef:
    """One writable parameter.

    `address`: 16-bit Modbus register address.
    `encoding`: how to convert a Python number/bool into the 4-byte payload.
    `min_value` / `max_value`: human-units bounds the encoder enforces before transmitting.
    `step`: granularity for HA `number` entity discovery.
    `unit`: HA `unit_of_measurement`.
    `tier`: BASIC or SAFETY — write gating tier.
    `description`: human-readable explanation (used in HA discovery friendly name).
    """

    name: str
    address: int
    encoding: Encoding
    min_value: float
    max_value: float
    step: float
    unit: str | None
    tier: WriteTier
    description: str


@dataclass(frozen=True, slots=True)
class PackedBitDef:
    """One bit within the packed register at 0x1114.

    The BMS accepts function-0x06 single-register writes here. To preserve other
    bits we read-modify-write: the executor fetches the current value via the
    setup (Trame 2) frame field, applies the bitmask, and writes back.
    """

    name: str
    register: int
    bit_mask: int  # the bit(s) this parameter controls, e.g. 0x0080 for PCL
    tier: WriteTier
    description: str


# -- BASIC tier: operational tuning ----------------------------------------------------------

BASIC_REGISTERS: Final[tuple[RegisterDef, ...]] = (
    RegisterDef(
        name="charging_switch",
        address=0x1070,
        encoding=Encoding.BOOL32,
        min_value=0,
        max_value=1,
        step=1,
        unit=None,
        tier=WriteTier.BASIC,
        description="Enable / disable the charge MOSFET.",
    ),
    RegisterDef(
        name="discharging_switch",
        address=0x1074,
        encoding=Encoding.BOOL32,
        min_value=0,
        max_value=1,
        step=1,
        unit=None,
        tier=WriteTier.BASIC,
        description="Enable / disable the discharge MOSFET.",
    ),
    RegisterDef(
        name="balance_switch",
        address=0x1078,
        encoding=Encoding.BOOL32,
        min_value=0,
        max_value=1,
        step=1,
        unit=None,
        tier=WriteTier.BASIC,
        description="Enable / disable active cell balancing.",
    ),
    RegisterDef(
        name="balance_trigger_voltage",
        address=0x1014,
        encoding=Encoding.U32_MILLI,
        min_value=0.003,
        max_value=1.000,
        step=0.001,
        unit="V",
        tier=WriteTier.BASIC,
        description="Cell delta voltage at which balancing kicks in.",
    ),
    RegisterDef(
        name="balance_starting_voltage",
        address=0x1084,
        encoding=Encoding.U32_MILLI,
        min_value=1.200,
        max_value=4.250,
        step=0.010,
        unit="V",
        tier=WriteTier.BASIC,
        description="Minimum cell voltage before balancing is enabled.",
    ),
    RegisterDef(
        name="max_balance_current",
        address=0x1048,
        encoding=Encoding.U32_DECI,
        min_value=0.0,
        max_value=10.0,
        step=0.1,
        unit="A",
        tier=WriteTier.BASIC,
        description="Maximum balance current (hardware-capped at 10 A).",
    ),
    RegisterDef(
        name="cell_soc100_voltage",
        address=0x1018,
        encoding=Encoding.U32_MILLI,
        min_value=1.200,
        max_value=4.500,
        step=0.001,
        unit="V",
        tier=WriteTier.BASIC,
        description="Cell voltage that represents 100% SoC (display only).",
    ),
    RegisterDef(
        name="cell_soc0_voltage",
        address=0x101C,
        encoding=Encoding.U32_MILLI,
        min_value=1.000,
        max_value=4.500,
        step=0.001,
        unit="V",
        tier=WriteTier.BASIC,
        description="Cell voltage that represents 0% SoC (display only).",
    ),
    RegisterDef(
        name="cell_request_charge_voltage",
        address=0x1020,
        encoding=Encoding.U32_MILLI,
        min_value=1.20,
        max_value=5.00,
        step=0.01,
        unit="V",
        tier=WriteTier.BASIC,
        description="Cell voltage the BMS requests from the charger.",
    ),
    RegisterDef(
        name="cell_request_float_voltage",
        address=0x1024,
        encoding=Encoding.U32_MILLI,
        min_value=1.20,
        max_value=5.00,
        step=0.01,
        unit="V",
        tier=WriteTier.BASIC,
        description="Cell float voltage the BMS requests from the charger.",
    ),
    RegisterDef(
        name="smart_sleep_voltage",
        address=0x1000,
        encoding=Encoding.U32_MILLI,
        min_value=0.0,
        max_value=5.0,
        step=0.01,
        unit="V",
        tier=WriteTier.BASIC,
        description="Cell voltage below which the BMS enters smart sleep.",
    ),
)


# -- SAFETY tier: protection thresholds, current/temperature limits, topology ----------------

SAFETY_REGISTERS: Final[tuple[RegisterDef, ...]] = (
    RegisterDef(
        name="cell_voltage_undervoltage_protection",
        address=0x1004,
        encoding=Encoding.U32_MILLI,
        min_value=1.20,
        max_value=4.50,
        step=0.001,
        unit="V",
        tier=WriteTier.SAFETY,
        description="Under-voltage protection (UVP) threshold.",
    ),
    RegisterDef(
        name="cell_voltage_undervoltage_recovery",
        address=0x1008,
        encoding=Encoding.U32_MILLI,
        min_value=1.20,
        max_value=4.50,
        step=0.001,
        unit="V",
        tier=WriteTier.SAFETY,
        description="UVP recovery threshold (must be above UVP).",
    ),
    RegisterDef(
        name="cell_voltage_overvoltage_protection",
        address=0x100C,
        encoding=Encoding.U32_MILLI,
        min_value=1.20,
        max_value=4.50,
        step=0.001,
        unit="V",
        tier=WriteTier.SAFETY,
        description="Over-voltage protection (OVP) threshold. Set too high → fire risk.",
    ),
    RegisterDef(
        name="cell_voltage_overvoltage_recovery",
        address=0x1010,
        encoding=Encoding.U32_MILLI,
        min_value=1.20,
        max_value=4.50,
        step=0.001,
        unit="V",
        tier=WriteTier.SAFETY,
        description="OVP recovery threshold (must be below OVP).",
    ),
    RegisterDef(
        name="power_off_voltage",
        address=0x1028,
        encoding=Encoding.U32_MILLI,
        min_value=1.20,
        max_value=4.50,
        step=0.01,
        unit="V",
        tier=WriteTier.SAFETY,
        description="Cell voltage at which the BMS powers off (battery preservation).",
    ),
    RegisterDef(
        name="max_charge_current",
        address=0x102C,
        encoding=Encoding.U32_DECI,
        min_value=0,
        max_value=600,
        step=0.1,
        unit="A",
        tier=WriteTier.SAFETY,
        description="Maximum charge current. Set too high → wire fire / cell damage.",
    ),
    RegisterDef(
        name="charge_overcurrent_protection_delay",
        address=0x1030,
        encoding=Encoding.U32_RAW,
        min_value=2,
        max_value=600,
        step=1,
        unit="s",
        tier=WriteTier.SAFETY,
        description="Delay before charge over-current protection trips.",
    ),
    RegisterDef(
        name="charge_overcurrent_protection_recovery_time",
        address=0x1034,
        encoding=Encoding.U32_RAW,
        min_value=2,
        max_value=3600,
        step=1,
        unit="s",
        tier=WriteTier.SAFETY,
        description="Time before charge OCP can be cleared.",
    ),
    RegisterDef(
        name="max_discharge_current",
        address=0x1038,
        encoding=Encoding.U32_DECI,
        min_value=0,
        max_value=600,
        step=0.1,
        unit="A",
        tier=WriteTier.SAFETY,
        description="Maximum discharge current.",
    ),
    RegisterDef(
        name="charge_overtemperature_protection",
        address=0x104C,
        encoding=Encoding.I32_DECI,
        min_value=-40,
        max_value=150,
        step=0.5,
        unit="°C",
        tier=WriteTier.SAFETY,
        description="Charge over-temperature protection.",
    ),
    RegisterDef(
        name="charge_overtemperature_protection_recovery",
        address=0x1050,
        encoding=Encoding.I32_DECI,
        min_value=-40,
        max_value=150,
        step=0.5,
        unit="°C",
        tier=WriteTier.SAFETY,
        description="Charge OTP recovery threshold.",
    ),
    RegisterDef(
        name="discharge_overtemperature_protection",
        address=0x1054,
        encoding=Encoding.I32_DECI,
        min_value=-40,
        max_value=150,
        step=0.5,
        unit="°C",
        tier=WriteTier.SAFETY,
        description="Discharge over-temperature protection.",
    ),
    RegisterDef(
        name="discharge_overtemperature_protection_recovery",
        address=0x1058,
        encoding=Encoding.I32_DECI,
        min_value=-40,
        max_value=150,
        step=0.5,
        unit="°C",
        tier=WriteTier.SAFETY,
        description="Discharge OTP recovery threshold.",
    ),
    RegisterDef(
        name="charge_undertemperature_protection",
        address=0x105C,
        encoding=Encoding.I32_DECI,
        min_value=-40,
        max_value=50,
        step=0.5,
        unit="°C",
        tier=WriteTier.SAFETY,
        description="Charge under-temperature protection (lithium plating risk).",
    ),
    RegisterDef(
        name="charge_undertemperature_protection_recovery",
        address=0x1060,
        encoding=Encoding.I32_DECI,
        min_value=-40,
        max_value=50,
        step=0.5,
        unit="°C",
        tier=WriteTier.SAFETY,
        description="Charge UTP recovery threshold.",
    ),
    RegisterDef(
        name="power_tube_overtemperature_protection",
        address=0x1064,
        encoding=Encoding.I32_DECI,
        min_value=30,
        max_value=100,
        step=0.5,
        unit="°C",
        tier=WriteTier.SAFETY,
        description="MOSFET over-temperature protection.",
    ),
    RegisterDef(
        name="power_tube_overtemperature_protection_recovery",
        address=0x1068,
        encoding=Encoding.I32_DECI,
        min_value=30,
        max_value=100,
        step=0.5,
        unit="°C",
        tier=WriteTier.SAFETY,
        description="MOSFET OTP recovery threshold.",
    ),
    RegisterDef(
        name="cell_count",
        address=0x106C,
        encoding=Encoding.U32_RAW,
        min_value=1,
        max_value=32,
        step=1,
        unit=None,
        tier=WriteTier.SAFETY,
        description="Number of cells in the pack. Wrong value → BMS misreads pack.",
    ),
)


# -- Packed-bit register at 0x1114 ----------------------------------------------------------

PACKED_BITS: Final[tuple[PackedBitDef, ...]] = (
    PackedBitDef(
        name="disable_pcl_module_switch",
        register=0x1114,
        bit_mask=0x0080,  # bit 7
        tier=WriteTier.BASIC,
        description="Disable the pre-charge limit module.",
    ),
    PackedBitDef(
        name="smart_sleep_switch",
        register=0x1114,
        bit_mask=0x0040,  # bit 6
        tier=WriteTier.BASIC,
        description="Enable smart-sleep behaviour.",
    ),
    PackedBitDef(
        name="timed_stored_data_switch",
        register=0x1114,
        bit_mask=0x0020,  # bit 5
        tier=WriteTier.BASIC,
        description="Enable periodic data storage in BMS RAM.",
    ),
)


def all_registers() -> tuple[RegisterDef, ...]:
    """Return BASIC + SAFETY registers as one tuple, useful for iteration."""
    return BASIC_REGISTERS + SAFETY_REGISTERS


def find_register(name: str) -> RegisterDef | None:
    """Look up a register by parameter name, or None if not found."""
    for r in all_registers():
        if r.name == name:
            return r
    return None


def find_packed_bit(name: str) -> PackedBitDef | None:
    """Look up a packed-bit parameter by name."""
    for b in PACKED_BITS:
        if b.name == name:
            return b
    return None


# Poll-trigger register addresses (used by the read path).
POLL_TRIGGER_FIXED: Final = 0x161C  # Trame 1 — static info
POLL_TRIGGER_LIVE: Final = 0x1620  # Trame 3 — live data
POLL_TRIGGER_SETUP: Final = 0x1622  # Trame 2 — setup / configuration
