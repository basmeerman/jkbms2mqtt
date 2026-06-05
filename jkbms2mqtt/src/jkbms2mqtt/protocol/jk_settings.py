"""Writable register table for the JK BMS RS485 Modbus V1.0 / V1.1 protocol.

Two surfaces, each carried verbatim from the V1.0 spec and a write tier tag for
the add-on's two-tier safety gating:

- ``BASIC_REGISTERS`` / ``SAFETY_REGISTERS`` — full 32-bit settings written via
  Modbus function 0x10 (Write Multiple Registers) as two consecutive
  big-endian register words.
- ``PACKED_BITS`` — single-register bit flags sharing register ``0x1114``.
  Written via Modbus function 0x06 (Write Single Register) with a
  read-modify-write so other bits in the register are preserved.

Each parameter carries a ``WriteTier``:

- ``BASIC``: operational tuning. Wrong values cause sub-optimal behaviour but
  cannot damage cells or pose a fire risk.
- ``SAFETY``: protection thresholds. Wrong values can damage cells, allow
  overcurrent, or cause fire. Off by default.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, unique
from typing import Final


@unique
class WriteTier(str, Enum):
    """Two-tier safety gating for writes."""

    BASIC = "basic"
    SAFETY = "safety"


@unique
class Encoding(str, Enum):
    """Wire encodings the BMS firmware accepts for 32-bit setting writes.

    Two consecutive Modbus register words carry the value, high word first
    (the JK / Modbus convention).
    """

    U32_RAW = "u32_raw"        # raw uint32
    U32_MILLI = "u32_milli"    # value × 1000  (e.g. volts → millivolts)
    U32_DECI = "u32_deci"      # value × 10    (e.g. amps → deci-amps)
    I32_DECI = "i32_deci"      # value × 10 signed (temperatures 0.1 °C)
    BOOL32 = "bool32"          # 0 or 1, padded with zero high bytes


@dataclass(frozen=True, slots=True)
class RegisterDef:
    """One writable parameter (function 0x10, two register words)."""

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
    """One bit within the packed register at ``0x1114``."""

    name: str
    register: int
    bit_mask: int
    tier: WriteTier
    description: str


# All addresses and encodings below verified against scripts/captures/BMS_1.txt
# and the BMS app screenshots in /docs/*.jpeg (firmware 15.41). The JK V1.0
# settings layout is one u32 parameter every 2 register words; the previous
# table assumed 4-word spacing and was off-by-N for every entry.
#
# charging_switch / discharging_switch / balance_switch — REMOVED. No verified
# Modbus address for them; read-only state is still available via the
# Switch_Charge / Switch_Discharge / Switch_Balance binary sensors derived
# from the real-time block.


# -- BASIC tier ------------------------------------------------------------------------

BASIC_REGISTERS: Final[tuple[RegisterDef, ...]] = (
    RegisterDef(name="smart_sleep_voltage", address=0x1000, encoding=Encoding.U32_MILLI, min_value=0.0, max_value=5.0, step=0.01, unit="V", tier=WriteTier.BASIC, description="Cell voltage below which the BMS enters smart sleep."),
    RegisterDef(name="balance_trigger_voltage", address=0x100A, encoding=Encoding.U32_MILLI, min_value=0.003, max_value=1.000, step=0.001, unit="V", tier=WriteTier.BASIC, description="Cell delta voltage at which balancing kicks in."),
    RegisterDef(name="cell_soc100_voltage", address=0x100C, encoding=Encoding.U32_MILLI, min_value=1.200, max_value=4.500, step=0.001, unit="V", tier=WriteTier.BASIC, description="Cell voltage that represents 100% SoC (display only)."),
    RegisterDef(name="cell_soc0_voltage", address=0x100E, encoding=Encoding.U32_MILLI, min_value=1.000, max_value=4.500, step=0.001, unit="V", tier=WriteTier.BASIC, description="Cell voltage that represents 0% SoC (display only)."),
    RegisterDef(name="cell_request_charge_voltage", address=0x1010, encoding=Encoding.U32_MILLI, min_value=1.20, max_value=5.00, step=0.01, unit="V", tier=WriteTier.BASIC, description="Cell voltage the BMS requests from the charger."),
    RegisterDef(name="cell_request_float_voltage", address=0x1012, encoding=Encoding.U32_MILLI, min_value=1.20, max_value=5.00, step=0.01, unit="V", tier=WriteTier.BASIC, description="Cell float voltage the BMS requests from the charger."),
    RegisterDef(name="max_balance_current", address=0x1024, encoding=Encoding.U32_MILLI, min_value=0.0, max_value=10.0, step=0.001, unit="A", tier=WriteTier.BASIC, description="Maximum balance current (hardware-capped at 10 A)."),
    # BOOL switches at spec V1.0 / V1.1 byte offsets 0x70 / 0x74 / 0x78.
    # PR #3 removed these on the wrong assumption that no Modbus address
    # existed; the spec and the BMS_1 capture both confirm them.
    RegisterDef(name="charging_switch", address=0x1038, encoding=Encoding.BOOL32, min_value=0, max_value=1, step=1, unit=None, tier=WriteTier.BASIC, description="Enable / disable the charge MOSFET."),
    RegisterDef(name="discharging_switch", address=0x103A, encoding=Encoding.BOOL32, min_value=0, max_value=1, step=1, unit=None, tier=WriteTier.BASIC, description="Enable / disable the discharge MOSFET."),
    RegisterDef(name="balance_switch", address=0x103C, encoding=Encoding.BOOL32, min_value=0, max_value=1, step=1, unit=None, tier=WriteTier.BASIC, description="Enable / disable active cell balancing."),
    RegisterDef(name="balance_starting_voltage", address=0x1042, encoding=Encoding.U32_MILLI, min_value=1.200, max_value=4.250, step=0.010, unit="V", tier=WriteTier.BASIC, description="Minimum cell voltage before balancing is enabled."),
)


# -- SAFETY tier -----------------------------------------------------------------------

SAFETY_REGISTERS: Final[tuple[RegisterDef, ...]] = (
    RegisterDef(name="cell_voltage_undervoltage_protection", address=0x1002, encoding=Encoding.U32_MILLI, min_value=1.20, max_value=4.50, step=0.001, unit="V", tier=WriteTier.SAFETY, description="Under-voltage protection threshold."),
    RegisterDef(name="cell_voltage_undervoltage_recovery", address=0x1004, encoding=Encoding.U32_MILLI, min_value=1.20, max_value=4.50, step=0.001, unit="V", tier=WriteTier.SAFETY, description="UVP recovery threshold (must be above UVP)."),
    RegisterDef(name="cell_voltage_overvoltage_protection", address=0x1006, encoding=Encoding.U32_MILLI, min_value=1.20, max_value=4.50, step=0.001, unit="V", tier=WriteTier.SAFETY, description="Over-voltage protection threshold. Set too high → fire risk."),
    RegisterDef(name="cell_voltage_overvoltage_recovery", address=0x1008, encoding=Encoding.U32_MILLI, min_value=1.20, max_value=4.50, step=0.001, unit="V", tier=WriteTier.SAFETY, description="OVP recovery threshold (must be below OVP)."),
    RegisterDef(name="power_off_voltage", address=0x1014, encoding=Encoding.U32_MILLI, min_value=1.20, max_value=4.50, step=0.01, unit="V", tier=WriteTier.SAFETY, description="Cell voltage at which the BMS powers off (battery preservation)."),
    RegisterDef(name="max_charge_current", address=0x1016, encoding=Encoding.U32_MILLI, min_value=0, max_value=600, step=0.001, unit="A", tier=WriteTier.SAFETY, description="Maximum charge current."),
    RegisterDef(name="charge_overcurrent_protection_delay", address=0x1018, encoding=Encoding.U32_RAW, min_value=1, max_value=600, step=1, unit="s", tier=WriteTier.SAFETY, description="Delay before charge over-current protection trips."),
    RegisterDef(name="charge_overcurrent_protection_recovery_time", address=0x101A, encoding=Encoding.U32_RAW, min_value=2, max_value=3600, step=1, unit="s", tier=WriteTier.SAFETY, description="Time before charge OCP can be cleared."),
    RegisterDef(name="max_discharge_current", address=0x101C, encoding=Encoding.U32_MILLI, min_value=0, max_value=600, step=0.001, unit="A", tier=WriteTier.SAFETY, description="Maximum discharge current."),
    RegisterDef(name="discharge_overcurrent_protection_delay", address=0x101E, encoding=Encoding.U32_RAW, min_value=1, max_value=600, step=1, unit="s", tier=WriteTier.SAFETY, description="Delay before discharge over-current protection trips."),
    RegisterDef(name="discharge_overcurrent_protection_recovery_time", address=0x1020, encoding=Encoding.U32_RAW, min_value=2, max_value=3600, step=1, unit="s", tier=WriteTier.SAFETY, description="Time before discharge OCP can be cleared."),
    RegisterDef(name="short_circuit_protection_recovery_time", address=0x1022, encoding=Encoding.U32_RAW, min_value=1, max_value=3600, step=1, unit="s", tier=WriteTier.SAFETY, description="Time before short-circuit protection can be cleared."),
    # Spec V1.1 byte 0x4C → reg 0x1026 is TMPBatCOT (Charge OTP); discharge
    # variants follow at 0x102A/0x102C. The previous table had charge/discharge
    # labels swapped, which would have written discharge-OTP values to the
    # charge-OTP register when both tiers were enabled.
    RegisterDef(name="charge_overtemperature_protection", address=0x1026, encoding=Encoding.I32_DECI, min_value=-40, max_value=150, step=0.5, unit="°C", tier=WriteTier.SAFETY, description="Charge over-temperature protection."),
    RegisterDef(name="charge_overtemperature_protection_recovery", address=0x1028, encoding=Encoding.I32_DECI, min_value=-40, max_value=150, step=0.5, unit="°C", tier=WriteTier.SAFETY, description="Charge OTP recovery threshold."),
    RegisterDef(name="discharge_overtemperature_protection", address=0x102A, encoding=Encoding.I32_DECI, min_value=-40, max_value=150, step=0.5, unit="°C", tier=WriteTier.SAFETY, description="Discharge over-temperature protection."),
    RegisterDef(name="discharge_overtemperature_protection_recovery", address=0x102C, encoding=Encoding.I32_DECI, min_value=-40, max_value=150, step=0.5, unit="°C", tier=WriteTier.SAFETY, description="Discharge OTP recovery threshold."),
    RegisterDef(name="charge_undertemperature_protection", address=0x102E, encoding=Encoding.I32_DECI, min_value=-40, max_value=50, step=0.5, unit="°C", tier=WriteTier.SAFETY, description="Charge under-temperature protection (lithium plating risk)."),
    RegisterDef(name="charge_undertemperature_protection_recovery", address=0x1030, encoding=Encoding.I32_DECI, min_value=-40, max_value=50, step=0.5, unit="°C", tier=WriteTier.SAFETY, description="Charge UTP recovery threshold."),
    RegisterDef(name="power_tube_overtemperature_protection", address=0x1032, encoding=Encoding.I32_DECI, min_value=30, max_value=100, step=0.5, unit="°C", tier=WriteTier.SAFETY, description="MOSFET over-temperature protection."),
    RegisterDef(name="power_tube_overtemperature_protection_recovery", address=0x1034, encoding=Encoding.I32_DECI, min_value=30, max_value=100, step=0.5, unit="°C", tier=WriteTier.SAFETY, description="MOSFET OTP recovery threshold."),
    RegisterDef(name="cell_count", address=0x1036, encoding=Encoding.U32_RAW, min_value=1, max_value=32, step=1, unit=None, tier=WriteTier.SAFETY, description="Number of cells in the pack."),
    RegisterDef(name="pack_capacity_setting", address=0x103E, encoding=Encoding.U32_MILLI, min_value=0, max_value=10000, step=0.001, unit="Ah", tier=WriteTier.SAFETY, description="Configured pack capacity (drives SoC scaling)."),
    RegisterDef(name="short_circuit_protection_delay_us", address=0x1040, encoding=Encoding.U32_RAW, min_value=1, max_value=10000, step=1, unit="µs", tier=WriteTier.SAFETY, description="Short-circuit protection trip delay."),
)




# -- Packed-bit register — SPEC-DEVIATION on PB2A16S20P --------------------------------
# Spec V1.1 places this UINT16 at byte offset 0x114 in the settings block
# under the spec's byte→register convention, which maps to Modbus reg 0x108A
# (= 0x1000 + 0x114/2). On PB2A16S20P firmware 15.41 we verified that
# 0x108A reads 0x0000 (no bits set) while the BMS app's Control tab shows
# ChargingFloatMode / Discharge OCP 2 / Discharge OCP 3 ON. The empirical
# register 0x1114 (where the spec byte offset 0x114 is treated as a register
# index instead of a byte index — same direct-address mapping the firmware
# also uses for TempBat3/4/5 in the RT block) reads 0x3200 = bits 9, 12, 13,
# which matches the three ON toggles. So on this firmware the packed-bit
# register lives at 0x1114; we use that address with a SPEC-DEVIATION
# annotation.
#
# Bit positions per V1.1 spec page 3:
#   BIT0 HeatEN            BIT5 SpecialCharger
#   BIT1 DisableTempSensor BIT6 SmartSleep
#   BIT2 GPSHeartbeat      BIT7 DisablePCLModule  (V1.1)
#   BIT3 PortSwitch        BIT8 TimedStoredData   (V1.1)
#   BIT4 LCDAlwaysOn       BIT9 ChargingFloatMode (V1.1)
# Bits 10+ are not documented in the V1.1 PDF but bits 12 and 13 are
# observed on PB2A16S20P, corresponding to the app's Discharge OCP 2 / 3.
# V1.0 only documents BIT0–6.

PACKED_BIT_REGISTER: Final = 0x1114    # SPEC-DEVIATION — spec says 0x108A

PACKED_BITS: Final[tuple[PackedBitDef, ...]] = (
    PackedBitDef(name="smart_sleep_switch", register=PACKED_BIT_REGISTER, bit_mask=0x0040, tier=WriteTier.BASIC, description="Enable smart-sleep behaviour."),
    PackedBitDef(name="disable_pcl_module_switch", register=PACKED_BIT_REGISTER, bit_mask=0x0080, tier=WriteTier.BASIC, description="Disable the pre-charge limit module (V1.1)."),
    PackedBitDef(name="timed_stored_data_switch", register=PACKED_BIT_REGISTER, bit_mask=0x0100, tier=WriteTier.BASIC, description="Enable periodic data storage in BMS RAM (V1.1)."),
)


# -- Public helpers --------------------------------------------------------------------


def all_registers() -> tuple[RegisterDef, ...]:
    """Return BASIC + SAFETY registers as one tuple."""
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


# -- Encoders --------------------------------------------------------------------------


class EncodeError(ValueError):
    """Raised when a value cannot be encoded for the given parameter."""


def encode_value_to_words(reg: RegisterDef, value: float | int | bool) -> list[int]:
    """Encode *value* into two 16-bit register words for ``client.write_registers``.

    The two-element list is ``[hi_word, lo_word]`` (the JK / Modbus convention).
    Range and width are enforced here so a malformed MQTT command never reaches
    the BMS.
    """
    numeric = float(value)
    if not reg.min_value <= numeric <= reg.max_value:
        raise EncodeError(
            f"{reg.name}: value {numeric} outside [{reg.min_value}, {reg.max_value}]"
        )

    if reg.encoding is Encoding.U32_RAW:
        scaled = int(round(numeric))
        return _u32_to_words(scaled)
    if reg.encoding is Encoding.U32_MILLI:
        return _u32_to_words(int(round(numeric * 1000)))
    if reg.encoding is Encoding.U32_DECI:
        return _u32_to_words(int(round(numeric * 10)))
    if reg.encoding is Encoding.I32_DECI:
        return _i32_to_words(int(round(numeric * 10)))
    # BOOL32: high words zero, low word = 0 / 1
    return [0, 1 if numeric else 0]


def encode_packed_bit_value(
    bit_def: PackedBitDef, *, desired_on: bool, current_register_value: int
) -> int:
    """Return the new 16-bit value to write to the packed-bit register.

    The caller (write executor) reads the current register value, calls this
    helper, then writes back via Modbus function 0x06 — preserving other bits.
    """
    if not 0 <= current_register_value <= 0xFFFF:
        raise EncodeError(
            f"current_register_value must fit in 16 bits, got {current_register_value:#x}"
        )
    if desired_on:
        return current_register_value | bit_def.bit_mask
    return current_register_value & ~bit_def.bit_mask & 0xFFFF


def _u32_to_words(value: int) -> list[int]:
    if not 0 <= value <= 0xFFFFFFFF:
        raise EncodeError(f"value {value} does not fit in u32")
    return [(value >> 16) & 0xFFFF, value & 0xFFFF]


def _i32_to_words(value: int) -> list[int]:
    if not -(2**31) <= value <= 2**31 - 1:
        raise EncodeError(f"value {value} does not fit in i32")
    if value < 0:
        value += 0x1_0000_0000
    return [(value >> 16) & 0xFFFF, value & 0xFFFF]


# -- Decoders (settings readback) -----------------------------------------------------

# Verified writable register addresses span 0x1000..0x1043. Read as a single
# Modbus 0x03 — well under the 125-register protocol ceiling.
SETTINGS_BLOCK_BASE: Final = 0x1000
SETTINGS_BLOCK_WORDS: Final = 0x44     # 0x1000..0x1043 covers every BASIC + SAFETY reg
SETTINGS_BLOCK_CHUNKS: Final = (
    (0x1000, SETTINGS_BLOCK_WORDS),
)

# The packed-bit register lives well above the settings block — read it separately.
# The settings-block decoder below uses ``len(regs)`` to bounds-check, so an
# in-block packed-bit register would still decode correctly if a firmware
# variant relocated it.


def decode_register_value(reg: RegisterDef, regs: list[int]) -> float | bool:
    """Reverse ``encode_value_to_words``: read two register words back to a value.

    ``regs`` is the full settings block (indexed from ``SETTINGS_BLOCK_BASE``);
    we only read the two words at ``reg.address - SETTINGS_BLOCK_BASE``.
    """
    off = reg.address - SETTINGS_BLOCK_BASE
    if off < 0 or off + 1 >= len(regs):
        raise EncodeError(
            f"{reg.name}: address {reg.address:#06x} outside settings block"
        )
    hi = regs[off] & 0xFFFF
    lo = regs[off + 1] & 0xFFFF
    raw32 = (hi << 16) | lo

    if reg.encoding is Encoding.U32_RAW:
        return float(raw32)
    if reg.encoding is Encoding.U32_MILLI:
        return raw32 / 1000.0
    if reg.encoding is Encoding.U32_DECI:
        return raw32 / 10.0
    if reg.encoding is Encoding.I32_DECI:
        signed = raw32 - 0x1_0000_0000 if raw32 >= 0x8000_0000 else raw32
        return signed / 10.0
    # BOOL32 — non-zero anywhere counts as on (firmware variants differ on which
    # word carries the bit).
    return raw32 != 0


def decode_packed_bit_value(bit: PackedBitDef, register_value: int) -> bool:
    """Decode a single packed bit given the current value of register 0x1114."""
    return bool(register_value & bit.bit_mask)
