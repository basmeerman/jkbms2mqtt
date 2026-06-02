"""Tests for the JK Modbus V1.0/V1.1 register decoder.

The decoder is pure-functional and operates on register-word lists, so tests
can synthesize arbitrary register inputs and verify decoded fields exactly —
no real BMS required.
"""

from __future__ import annotations

import pytest

from jkbms2mqtt.protocol.jk_modbus import (
    ALARM_NAMES,
    INFO_BLOCK_WORDS,
    RT_BLOCK_WORDS,
    JkRealtime,
    JkStaticInfo,
    decode_realtime,
    decode_static_info,
    format_runtime,
)

# -- Helpers for synthesizing register inputs ----------------------------------------


def _empty_regs(size: int = RT_BLOCK_WORDS) -> list[int]:
    return [0] * size


def _set_u16(regs: list[int], off: int, value: int) -> None:
    regs[off] = value & 0xFFFF


def _set_i16(regs: list[int], off: int, value: int) -> None:
    regs[off] = (value + 0x10000) & 0xFFFF if value < 0 else value & 0xFFFF


def _set_u32(regs: list[int], off: int, value: int) -> None:
    regs[off] = (value >> 16) & 0xFFFF
    regs[off + 1] = value & 0xFFFF


def _set_i32(regs: list[int], off: int, value: int) -> None:
    if value < 0:
        value = value + 0x1_0000_0000
    _set_u32(regs, off, value)


def _set_ascii(regs: list[int], off: int, text: str, length_bytes: int) -> None:
    """Pack an ASCII string into successive registers (hi byte first)."""
    encoded = text.encode("ascii")[:length_bytes].ljust(length_bytes, b"\x00")
    for i in range(length_bytes // 2):
        hi = encoded[2 * i]
        lo = encoded[2 * i + 1]
        regs[off + i] = (hi << 8) | lo


# -- Helper-primitive tests ------------------------------------------------------------


class TestPrimitives:
    def test_u16_unsigned(self) -> None:
        from jkbms2mqtt.protocol.jk_modbus import _u16

        assert _u16([0xABCD], 0) == 0xABCD

    def test_i16_positive(self) -> None:
        from jkbms2mqtt.protocol.jk_modbus import _i16

        assert _i16([0x7FFF], 0) == 32767

    def test_i16_negative(self) -> None:
        from jkbms2mqtt.protocol.jk_modbus import _i16

        assert _i16([0x8000], 0) == -32768
        assert _i16([0xFFFF], 0) == -1

    def test_u32_hi_first(self) -> None:
        from jkbms2mqtt.protocol.jk_modbus import _u32

        # JK convention: lower-addressed register is the high word.
        assert _u32([0x0001, 0x0000], 0) == 0x00010000

    def test_i32_negative(self) -> None:
        from jkbms2mqtt.protocol.jk_modbus import _i32

        assert _i32([0xFFFF, 0xFFFF], 0) == -1

    def test_i32_max_positive(self) -> None:
        from jkbms2mqtt.protocol.jk_modbus import _i32

        assert _i32([0x7FFF, 0xFFFF], 0) == 0x7FFFFFFF

    def test_ascii_strips_trailing_nulls(self) -> None:
        from jkbms2mqtt.protocol.jk_modbus import _ascii

        regs = [0] * 8
        _set_ascii(regs, 0, "ABC", 8)
        assert _ascii(regs, 0, 8) == "ABC"

    def test_ascii_handles_high_byte_only_register(self) -> None:
        from jkbms2mqtt.protocol.jk_modbus import _ascii

        regs = [0x4100]   # 'A' in hi byte, 0 in lo byte
        # Length must cover both bytes; the lo byte is 0 and gets skipped.
        assert _ascii(regs, 0, 2) == "A"


# -- decode_realtime() tests -----------------------------------------------------------


class TestDecodeRealtime:
    def test_too_short_raises(self) -> None:
        with pytest.raises(ValueError, match="too short"):
            decode_realtime([0] * 10)

    def test_minimal_no_cells_no_alarms(self) -> None:
        regs = _empty_regs()
        # No cell-present bits set — falls back to BMS-reported avg / delta.
        _set_u16(regs, 0x22, 3300)
        _set_u16(regs, 0x23, 25)

        result = decode_realtime(regs)
        assert isinstance(result, JkRealtime)
        assert result.cell_voltages_v == ()
        assert result.cell_count == 0
        assert result.cell_voltage_avg_v == pytest.approx(3.300)
        assert result.cell_voltage_delta_v == pytest.approx(0.025)
        assert result.cell_voltage_max_v == 0.0
        assert result.cell_voltage_min_v == 0.0
        assert result.cell_voltage_max_number == 0
        assert result.cell_voltage_min_number == 0
        assert result.alarms == ()
        assert result.alarm_bits == 0

    def test_full_16_cells(self) -> None:
        regs = _empty_regs()
        # Cell-present bitmap: all 16 cells present.
        _set_u32(regs, 0x20, 0xFFFF)
        # Cell voltages: 3300, 3301, 3302, ..., 3315 mV.
        for i in range(16):
            _set_u16(regs, i, 3300 + i)

        result = decode_realtime(regs)
        assert result.cell_count == 16
        assert result.cell_voltages_v == tuple(
            pytest.approx(3.300 + i / 1000) for i in range(16)
        )
        assert result.cell_voltage_max_v == pytest.approx(3.315)
        assert result.cell_voltage_min_v == pytest.approx(3.300)
        assert result.cell_voltage_max_number == 16
        assert result.cell_voltage_min_number == 1
        assert result.cell_voltage_delta_v == pytest.approx(0.015)
        assert result.cell_voltage_avg_v == pytest.approx(3.3075)

    def test_non_16s_pack_only_8_cells(self) -> None:
        regs = _empty_regs()
        # Cell-present bitmap: cells 1..8 only (bits 0..7).
        _set_u32(regs, 0x20, 0x00FF)
        for i in range(8):
            _set_u16(regs, i, 3300)
        # Set cells 9..16 voltage too — they should be IGNORED.
        for i in range(8, 16):
            _set_u16(regs, i, 9999)

        result = decode_realtime(regs)
        assert result.cell_count == 8
        assert len(result.cell_voltages_v) == 8
        assert all(v == pytest.approx(3.300) for v in result.cell_voltages_v)
        # Min/max from populated cells only — phantom 9999 mV must NOT appear.
        assert result.cell_voltage_max_v == pytest.approx(3.300)
        assert result.cell_voltage_min_v == pytest.approx(3.300)

    def test_sparse_cell_present_bitmap(self) -> None:
        regs = _empty_regs()
        # Cells 1, 3, 5 only (bits 0, 2, 4).
        _set_u32(regs, 0x20, 0b10101)
        _set_u16(regs, 0, 3300)
        _set_u16(regs, 2, 3500)
        _set_u16(regs, 4, 3100)
        result = decode_realtime(regs)
        assert result.cell_count == 3
        assert result.cell_voltages_v == (
            pytest.approx(3.300),
            pytest.approx(3.500),
            pytest.approx(3.100),
        )
        # max is cell 3 (1-indexed within the populated set), min is cell 5.
        assert result.cell_voltage_max_number == 2
        assert result.cell_voltage_min_number == 3

    def test_total_voltage_and_signed_current(self) -> None:
        regs = _empty_regs()
        _set_u32(regs, 0x48, 53_000)        # 53.000 V (mV)
        _set_i32(regs, 0x4C, -25_500)       # −25.5 A discharge (mA)
        result = decode_realtime(regs)
        assert result.total_voltage_v == pytest.approx(53.0)
        assert result.total_current_a == pytest.approx(-25.5)
        assert result.total_power_w == pytest.approx(53.0 * -25.5)

    def test_temperatures_negative_and_positive(self) -> None:
        regs = _empty_regs()
        _set_i16(regs, 0x45, 250)           # MOS 25.0 °C
        _set_i16(regs, 0x4E, -50)           # probe 1 = −5.0 °C
        _set_i16(regs, 0x4F, 380)           # probe 2 = 38.0 °C
        _set_i16(regs, 0x7C, -100)          # probe 3 = −10.0 °C
        _set_i16(regs, 0x7D, 250)           # probe 4
        _set_i16(regs, 0x7E, 250)           # probe 5
        result = decode_realtime(regs)
        assert result.mos_temp_c == pytest.approx(25.0)
        assert result.probe_1_temp_c == pytest.approx(-5.0)
        assert result.probe_2_temp_c == pytest.approx(38.0)
        assert result.probe_3_temp_c == pytest.approx(-10.0)
        assert result.probe_4_temp_c == pytest.approx(25.0)
        assert result.probe_5_temp_c == pytest.approx(25.0)

    def test_balance_state_and_soc(self) -> None:
        regs = _empty_regs()
        # balance_state | soc — hi byte balance_state=1, lo byte soc=75
        _set_u16(regs, 0x53, (1 << 8) | 75)
        _set_i16(regs, 0x52, 500)            # 0.5 A balance
        result = decode_realtime(regs)
        assert result.balance_active is True
        assert result.balance_current_a == pytest.approx(0.5)
        assert result.soc_percentage == 75

    def test_balance_state_zero_means_inactive(self) -> None:
        regs = _empty_regs()
        _set_u16(regs, 0x53, (0 << 8) | 50)
        result = decode_realtime(regs)
        assert result.balance_active is False
        assert result.soc_percentage == 50

    def test_soh_precharge_packed(self) -> None:
        regs = _empty_regs()
        # SoH 95 | precharge 1
        _set_u16(regs, 0x5C, (95 << 8) | 1)
        result = decode_realtime(regs)
        assert result.soh_percentage == 95

    def test_charge_discharge_flags(self) -> None:
        regs = _empty_regs()
        _set_u16(regs, 0x60, (1 << 8) | 0)
        result = decode_realtime(regs)
        assert result.charge_enabled is True
        assert result.discharge_enabled is False

    def test_charge_discharge_both_on(self) -> None:
        regs = _empty_regs()
        _set_u16(regs, 0x60, (1 << 8) | 1)
        result = decode_realtime(regs)
        assert result.charge_enabled is True
        assert result.discharge_enabled is True

    def test_capacity_and_cycles(self) -> None:
        regs = _empty_regs()
        _set_i32(regs, 0x54, 80_000)         # 80 Ah remaining
        _set_u32(regs, 0x56, 100_000)        # 100 Ah nominal
        _set_u32(regs, 0x58, 42)
        _set_u32(regs, 0x5E, 86400)
        result = decode_realtime(regs)
        assert result.remaining_capacity_ah == pytest.approx(80.0)
        assert result.nominal_capacity_ah == pytest.approx(100.0)
        assert result.cycle_count == 42
        assert result.runtime_s == 86400

    def test_alarms_single_bit(self) -> None:
        regs = _empty_regs()
        # Bit 4: cell_overvoltage
        _set_u32(regs, 0x50, 1 << 4)
        result = decode_realtime(regs)
        assert "cell_overvoltage" in result.alarms
        assert len(result.alarms) == 1
        assert result.alarm_bits == 0x10

    def test_alarms_multiple_bits(self) -> None:
        regs = _empty_regs()
        # bits 4, 6, 11: overvoltage / charge_overcurrent / cell_undervoltage
        bits = (1 << 4) | (1 << 6) | (1 << 11)
        _set_u32(regs, 0x50, bits)
        result = decode_realtime(regs)
        assert "cell_overvoltage" in result.alarms
        assert "charge_overcurrent" in result.alarms
        assert "cell_undervoltage" in result.alarms
        assert result.alarm_bits == bits

    def test_alarm_unknown_bit_ignored(self) -> None:
        regs = _empty_regs()
        # bit 30 is beyond the known set (we name 22)
        _set_u32(regs, 0x50, 1 << 30)
        result = decode_realtime(regs)
        # No named alarm because there's no name for bit 30.
        assert result.alarms == ()
        assert result.alarm_bits == 1 << 30


# -- decode_static_info() tests -------------------------------------------------------


class TestDecodeStaticInfo:
    def test_too_short_raises(self) -> None:
        with pytest.raises(ValueError, match="too short"):
            decode_static_info([0] * 10)

    def test_decodes_all_fields(self) -> None:
        regs = [0] * INFO_BLOCK_WORDS
        _set_ascii(regs, 0x00, "JK-PB2A16S15P", 16)
        _set_ascii(regs, 0x08, "HW10A20H", 8)
        _set_ascii(regs, 0x0C, "SW1209HE", 8)
        _set_ascii(regs, 0x28, "JK202401012345", 16)

        result = decode_static_info(regs)
        assert isinstance(result, JkStaticInfo)
        assert result.model == "JK-PB2A16S15P"
        assert result.hw_version == "HW10A20H"
        assert result.sw_version == "SW1209HE"
        assert result.serial_number == "JK202401012345"

    def test_empty_strings_when_unpopulated(self) -> None:
        regs = [0] * INFO_BLOCK_WORDS
        result = decode_static_info(regs)
        assert result.model == ""
        assert result.hw_version == ""
        assert result.sw_version == ""
        assert result.serial_number == ""


# -- format_runtime() ----------------------------------------------------------------


class TestFormatRuntime:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0, "0d00h00m"),
            (60, "0d00h01m"),
            (3600, "0d01h00m"),
            (86400, "1d00h00m"),
            (86400 + 3600 + 60, "1d01h01m"),
            (86400 * 5 + 23 * 3600 + 59 * 60, "5d23h59m"),
        ],
    )
    def test_examples(self, seconds: int, expected: str) -> None:
        assert format_runtime(seconds) == expected

    def test_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            format_runtime(-1)


# -- Alarm bit name table -------------------------------------------------------------


def test_alarm_names_length_is_22() -> None:
    assert len(ALARM_NAMES) == 22


def test_alarm_names_all_strings() -> None:
    assert all(isinstance(n, str) and n for n in ALARM_NAMES)


def test_alarm_names_unique() -> None:
    assert len(set(ALARM_NAMES)) == len(ALARM_NAMES)
