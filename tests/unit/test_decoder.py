"""Decoder tests.

Named tests capture the protocol-level invariants:
- `cell_voltage_delta = max − min` (computed from cell voltages, never echoed).
- Averaging only over populated cells in non-16S packs.
- Probe-temperature mapping (probe 4 at offset 256, probe 3 at offset 258).
"""

from __future__ import annotations

import pytest

from jkbms2mqtt.protocol.decoder import (
    FixedData,
    LiveData,
    SetupData,
    decode_fixed,
    decode_live,
    decode_setup,
    format_runtime,
)


class TestDecodeLiveBasics:
    def test_default_frame_decodes_expected_values(self, live_frame) -> None:
        data = decode_live(live_frame(), cell_count=16)
        assert isinstance(data, LiveData)
        # Default fixture: 16 cells × 3.3 V each
        assert data.cell_voltages_v == tuple([3.3] * 16)
        assert data.cell_voltage_average_v == pytest.approx(3.3)
        assert data.cell_voltage_delta_v == pytest.approx(0.0)
        assert data.total_voltage_v == pytest.approx(53.00)
        assert data.total_current_a == pytest.approx(10.0)
        assert data.total_power_w == pytest.approx(530.0)
        assert data.soc_percentage == 75
        assert data.soh_percentage == 100
        assert data.cycle_count == 42
        assert data.switch_charge is True
        assert data.switch_discharge is True
        assert data.switch_balance is False
        assert data.heating is False
        assert data.charge_status == 0


class TestRegressionIssue128CellDelta:
    def test_delta_is_max_minus_min_not_max(self, live_frame) -> None:
        # Set cell 0 to 3.500 V and cell 1 to 3.250 V → delta must be 0.250
        raw = live_frame(overrides={6: ("<H", 3500), 8: ("<H", 3250)})
        data = decode_live(raw, cell_count=16)
        assert data.cell_voltage_max_v == pytest.approx(3.500)
        assert data.cell_voltage_min_v == pytest.approx(3.250)
        assert data.cell_voltage_delta_v == pytest.approx(0.250)

    def test_delta_zero_when_all_cells_equal(self, live_frame) -> None:
        data = decode_live(live_frame(), cell_count=16)
        assert data.cell_voltage_delta_v == pytest.approx(0.0)


class TestRegressionIssue130NonSixteenCellAverage:
    def test_eight_cell_pack_averages_over_eight_only(self, live_frame) -> None:
        # 8 cells at 3.300 V, slots 8..15 are zero in the buffer.
        raw = live_frame(cell_count=8)
        data = decode_live(raw, cell_count=8)
        assert len(data.cell_voltages_v) == 8
        assert data.cell_voltage_average_v == pytest.approx(3.3)
        # Zero-padded slots must NOT pull the average down.
        assert data.cell_voltage_min_v > 3.0

    def test_invalid_cell_count_raises(self, live_frame) -> None:
        raw = live_frame()
        with pytest.raises(ValueError, match="cell_count"):
            decode_live(raw, cell_count=0)
        with pytest.raises(ValueError, match="cell_count"):
            decode_live(raw, cell_count=17)


class TestRegressionIssue38ProbeTemperatures:
    def test_probe_3_reads_distinct_byte_from_mos(self, live_frame) -> None:
        # In the JK frame layout probe 3 is at offset 258 and mos_temp at 144.
        # We give probe 3 a value distinct from mos_temp and confirm separation.
        raw = live_frame(overrides={144: ("<h", 250), 258: ("<h", 380)})
        data = decode_live(raw, cell_count=16)
        assert data.mos_temp_c == pytest.approx(25.0)
        assert data.probe_3_temp_c == pytest.approx(38.0)
        # probe_4 at offset 256 (note: precedes probe_3 in the frame layout)
        assert data.probe_4_temp_c == pytest.approx(25.0)

    def test_probe_4_can_be_independently_set(self, live_frame) -> None:
        raw = live_frame(overrides={256: ("<h", 410)})
        data = decode_live(raw, cell_count=16)
        assert data.probe_4_temp_c == pytest.approx(41.0)
        assert data.probe_3_temp_c == pytest.approx(25.0)


class TestDecodeLiveSignedFields:
    def test_negative_total_current_is_discharge(self, live_frame) -> None:
        # -25 A discharge → -25000 mA
        raw = live_frame(overrides={158: ("<i", -25000)})
        data = decode_live(raw, cell_count=16)
        assert data.total_current_a == pytest.approx(-25.0)

    def test_negative_temperature_decoded(self, live_frame) -> None:
        # -5.0 °C → -50 (×10)
        raw = live_frame(overrides={162: ("<h", -50)})
        data = decode_live(raw, cell_count=16)
        assert data.probe_1_temp_c == pytest.approx(-5.0)


class TestDecodeLiveLengthValidation:
    def test_short_frame_raises(self) -> None:
        with pytest.raises(ValueError, match="too short"):
            decode_live(b"\x00" * 100, cell_count=16)


class TestDecodeSetup:
    def test_default_setup_frame(self, setup_frame) -> None:
        data = decode_setup(setup_frame())
        assert isinstance(data, SetupData)
        assert data.smart_sleep_voltage_v == pytest.approx(2.5)
        assert data.cell_voltage_undervoltage_protection_v == pytest.approx(2.8)
        assert data.cell_voltage_overvoltage_protection_v == pytest.approx(3.65)
        assert data.max_charge_current_a == pytest.approx(50.0)
        assert data.cell_count == 16
        assert data.charging_switch is True
        assert data.discharging_switch is True
        assert data.balance_switch is True
        assert data.total_battery_capacity_ah == pytest.approx(100.0)
        # Packed-bit fields
        assert data.smart_sleep_switch is True  # bit 6 set in 0x60
        assert data.display_always_on_switch is True  # bit 5 set in 0x60
        assert data.disable_pcl_module_switch is False  # bit 7 cleared
        assert data.timed_stored_data_switch is True

    def test_all_packed_bits_off(self, setup_frame) -> None:
        data = decode_setup(setup_frame(overrides={282: ("<B", 0), 283: ("<B", 0)}))
        assert data.smart_sleep_switch is False
        assert data.display_always_on_switch is False
        assert data.disable_pcl_module_switch is False
        assert data.timed_stored_data_switch is False

    def test_pcl_bit_can_be_set(self, setup_frame) -> None:
        data = decode_setup(setup_frame(overrides={282: ("<B", 0x80)}))
        assert data.disable_pcl_module_switch is True

    def test_short_setup_raises(self) -> None:
        with pytest.raises(ValueError, match="too short"):
            decode_setup(b"\x00" * 100)


class TestDecodeFixed:
    def test_default_fixed_frame(self, fixed_frame) -> None:
        data = decode_fixed(fixed_frame())
        assert isinstance(data, FixedData)
        assert data.bms_model == "JK_PB2A16S15P"
        assert data.firmware_version == "15A6.0"
        assert data.software_version == "V19H_06"
        assert data.uptime_s == 360000
        assert data.power_on_count == 1234
        assert data.serial_number == "ABCDEF12345678"
        assert data.manufacturing_date == "240315"
        assert data.brand == "JIKONG"
        assert data.uart1_protocol_number == 2
        assert data.can_protocol_number == 6
        assert data.lcd_buzzer_trigger == 5
        assert data.lcd_buzzer_trigger_value == 80
        assert data.lcd_buzzer_release_value == 60
        assert data.request_charge_voltage_time_h == 2
        assert data.request_float_voltage_time_h == 4

    def test_short_fixed_raises(self) -> None:
        with pytest.raises(ValueError, match="too short"):
            decode_fixed(b"\x00" * 100)


class TestFormatRuntime:
    @pytest.mark.parametrize(
        ("secs", "expected"),
        [
            (0, "0d00h00m"),
            (60, "0d00h01m"),
            (3600, "0d01h00m"),
            (86400, "1d00h00m"),
            (86400 + 3600 + 60, "1d01h01m"),
            (86400 * 5 + 23 * 3600 + 59 * 60, "5d23h59m"),
        ],
    )
    def test_examples(self, secs: int, expected: str) -> None:
        assert format_runtime(secs) == expected

    def test_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            format_runtime(-1)
