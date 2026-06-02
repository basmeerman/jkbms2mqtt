"""Tests for the JK CAN protocol decoders.

Each test uses a synthetic 8-byte payload that matches the documented JK CAN
frame layout, so any regression in scaling / sign-extension is caught.
"""

from __future__ import annotations

import pytest

from jkbms2mqtt.protocol.can_protocol import (
    ALARM_NAMES,
    ID_CELL_MINMAX,
    ID_CELL_VOLT_BASE,
    ID_MAIN_STATUS,
    AlarmInfo,
    CellMinMax,
    CellVoltages,
    IndividualTemps,
    MainStatus,
    Monitoring,
    PowerCurrent,
    Temperatures,
    decode_alarm_info,
    decode_cell_minmax,
    decode_cell_voltages,
    decode_individual_temps,
    decode_main_status,
    decode_monitoring,
    decode_power_current,
    decode_temperatures,
    is_cell_voltage_id,
)


class TestMainStatus:
    def test_zero_current_offset(self) -> None:
        # raw_current = 4000 (0x0FA0) → (4000 * 0.1) − 400 = 0
        data = bytes([
            0x14, 0x02,  # 532 * 0.1 = 53.2 V
            0xA0, 0x0F,  # raw_current 4000 → 0 A
            0x55,        # SOC 85
            0, 0, 0,
        ])
        result = decode_main_status(data)
        assert isinstance(result, MainStatus)
        assert result.total_voltage_v == pytest.approx(53.2)
        assert result.total_current_a == pytest.approx(0.0)
        assert result.soc_percentage == 0x55

    def test_negative_current(self) -> None:
        # raw_current 2000 → (200 * 0.1) - 400 = -200 → A → -200 A discharge
        data = bytes([0x00, 0x02, 0xD0, 0x07, 50, 0, 0, 0])  # raw=2000, soc=50
        result = decode_main_status(data)
        assert result.total_current_a == pytest.approx(-200.0)

    def test_positive_current(self) -> None:
        # raw_current 5000 → 500 - 400 = +100 A
        data = bytes([0x00, 0x02, 0x88, 0x13, 80, 0, 0, 0])  # raw=5000
        result = decode_main_status(data)
        assert result.total_current_a == pytest.approx(100.0)

    def test_signed_two_complement(self) -> None:
        # raw_current 0xFFFE → −2 (signed) → ((−2) * 0.1) - 400 = −400.2
        data = bytes([0x00, 0x02, 0xFE, 0xFF, 0, 0, 0, 0])
        result = decode_main_status(data)
        assert result.total_current_a == pytest.approx(-400.2)

    def test_wrong_length_raises(self) -> None:
        with pytest.raises(ValueError, match="8 bytes"):
            decode_main_status(b"\x00" * 7)


class TestCellMinMax:
    def test_basic(self) -> None:
        # max = 0x0E42 = 3650 mV = 3.65 V at position 3
        # min = 0x0DEF = 3567 mV = 3.567 V at position 7
        data = bytes([0x42, 0x0E, 3, 0xEF, 0x0D, 7, 0, 0])
        result = decode_cell_minmax(data)
        assert isinstance(result, CellMinMax)
        assert result.cell_voltage_max_v == pytest.approx(3.650)
        assert result.cell_voltage_max_number == 3
        assert result.cell_voltage_min_v == pytest.approx(3.567)
        assert result.cell_voltage_min_number == 7
        assert result.cell_voltage_delta_v == pytest.approx(0.083)

    def test_wrong_length_raises(self) -> None:
        with pytest.raises(ValueError, match="8 bytes"):
            decode_cell_minmax(b"\x00" * 4)


class TestTemperatures:
    def test_offsets(self) -> None:
        # All bytes offset by -50 °C
        data = bytes([70, 1, 30, 4, 50, 0, 0, 0])
        result = decode_temperatures(data)
        assert isinstance(result, Temperatures)
        assert result.max_temp_c == 20  # 70 - 50
        assert result.max_temp_position == 1
        assert result.min_temp_c == -20  # 30 - 50
        assert result.min_temp_position == 4
        assert result.avg_temp_c == 0  # 50 - 50

    def test_wrong_length_raises(self) -> None:
        with pytest.raises(ValueError, match="8 bytes"):
            decode_temperatures(b"\x00" * 3)


class TestPowerCurrent:
    def test_positive_values(self) -> None:
        # current 1500 mA = 1.5 A, power 100 = 10 W, cycles 42
        data = bytes([0xDC, 0x05, 0x64, 0x00, 0, 0, 0x2A, 0x00])
        result = decode_power_current(data)
        assert isinstance(result, PowerCurrent)
        assert result.total_current_a == pytest.approx(1.5)
        assert result.total_power_w == pytest.approx(10.0)
        assert result.cycle_count == 42

    def test_negative_current(self) -> None:
        # raw_current = 0xFFFF = -1 → -0.001 A
        data = bytes([0xFF, 0xFF, 0, 0, 0, 0, 0, 0])
        result = decode_power_current(data)
        assert result.total_current_a == pytest.approx(-0.001)

    def test_wrong_length_raises(self) -> None:
        with pytest.raises(ValueError, match="8 bytes"):
            decode_power_current(b"\x00" * 2)


class TestMonitoring:
    def test_basic(self) -> None:
        # V = 5300 (0x14B4) * 0.01 = 53.00 V
        # current = 100 (0x0064) * 0.1 = 10.0 A
        # temp = 250 / 10 = 25.0 °C
        # cycles = 12
        data = bytes([0xB4, 0x14, 0x64, 0x00, 0xFA, 0x00, 0x0C, 0x00])
        result = decode_monitoring(data)
        assert isinstance(result, Monitoring)
        assert result.total_voltage_v == pytest.approx(53.00)
        assert result.total_current_a == pytest.approx(10.0)
        assert result.pack_temperature_c == pytest.approx(25.0)
        assert result.cycle_count == 12

    def test_negative_current_and_temp(self) -> None:
        # current = 0xFFFF (-1) * 0.1 = -0.1
        # temp = 0xFFEC (-20) / 10 = -2.0
        data = bytes([0x00, 0x02, 0xFF, 0xFF, 0xEC, 0xFF, 0, 0])
        result = decode_monitoring(data)
        assert result.total_current_a == pytest.approx(-0.1)
        assert result.pack_temperature_c == pytest.approx(-2.0)

    def test_wrong_length_raises(self) -> None:
        with pytest.raises(ValueError, match="8 bytes"):
            decode_monitoring(b"\x00" * 5)


class TestIndividualTemps:
    def test_all_five_probes(self) -> None:
        data = bytes([0, 70, 75, 80, 65, 60, 0, 0])
        result = decode_individual_temps(data)
        assert isinstance(result, IndividualTemps)
        assert result.temperatures_c == (20, 25, 30, 15, 10)
        assert result.mos_temp_c == 20

    def test_skips_zero_probes(self) -> None:
        data = bytes([0, 70, 0, 80, 0, 0, 0, 0])
        result = decode_individual_temps(data)
        assert result.temperatures_c == (20, 30)
        assert result.mos_temp_c == 20

    def test_all_zero_no_mos(self) -> None:
        data = bytes(8)
        result = decode_individual_temps(data)
        assert result.temperatures_c == ()
        assert result.mos_temp_c is None

    def test_wrong_length_raises(self) -> None:
        with pytest.raises(ValueError, match="8 bytes"):
            decode_individual_temps(b"\x00" * 4)


class TestCellVoltages:
    @pytest.mark.parametrize("group", [0, 1, 2, 3])
    def test_group_index_decoded(self, group: int) -> None:
        can_id = ID_CELL_VOLT_BASE | (group << 16)
        # Four cells: 3.500, 3.501, 3.502, 3.503 V → mV 3500..3503
        data = b""
        for i in range(4):
            data += int(3500 + i).to_bytes(2, "little")
        assert len(data) == 8
        result = decode_cell_voltages(can_id, data)
        assert isinstance(result, CellVoltages)
        assert result.group_index == group
        assert result.voltages_v == pytest.approx((3.500, 3.501, 3.502, 3.503))

    def test_wrong_length_raises(self) -> None:
        with pytest.raises(ValueError, match="8 bytes"):
            decode_cell_voltages(ID_CELL_VOLT_BASE, b"\x00" * 7)


class TestIsCellVoltageId:
    @pytest.mark.parametrize("group", [0, 1, 2, 3])
    def test_recognises_valid_groups(self, group: int) -> None:
        assert is_cell_voltage_id(ID_CELL_VOLT_BASE | (group << 16))

    def test_rejects_other_ids(self) -> None:
        assert not is_cell_voltage_id(ID_MAIN_STATUS)
        assert not is_cell_voltage_id(ID_CELL_MINMAX)
        # Group 4 doesn't exist (group bits 0100 — fails the membership check)
        assert not is_cell_voltage_id(ID_CELL_VOLT_BASE | (4 << 16))

    def test_rejects_id_with_correct_mask_but_invalid_group(self) -> None:
        # The mask matches but the group nibble is out of range.
        # Take a base ID and set group = 5 (0101) explicitly.
        bad_id = (ID_CELL_VOLT_BASE & ~(0x0F << 16)) | (5 << 16)
        # The mask check still passes (we clear the group bits before comparing),
        # but the group must be in {0,1,2,3}.
        assert not is_cell_voltage_id(bad_id)


class TestAlarmInfo:
    def test_no_alarms(self) -> None:
        result = decode_alarm_info(bytes(8))
        assert isinstance(result, AlarmInfo)
        assert result.raw_bits == 0
        assert result.alarms == ()

    def test_single_alarm_serious(self) -> None:
        # bit 0..1 = 01 (serious) → first alarm "Cell overvoltage" at code 1
        data = bytes([0x01, 0, 0, 0, 0, 0, 0, 0])
        result = decode_alarm_info(data)
        assert len(result.alarms) == 1
        code, name, level = result.alarms[0]
        assert code == 1
        assert name == "Cell overvoltage"
        assert level == 1

    def test_multiple_alarms(self) -> None:
        # bits 0..1 = 11 (general) → "Cell overvoltage"
        # bits 2..3 = 10 (important) → "Cell undervoltage"
        data = bytes([0b00001011, 0, 0, 0, 0, 0, 0, 0])
        result = decode_alarm_info(data)
        assert len(result.alarms) == 2
        assert result.alarms[0] == (1, ALARM_NAMES[0], 3)
        assert result.alarms[1] == (2, ALARM_NAMES[1], 2)

    def test_wrong_length_raises(self) -> None:
        with pytest.raises(ValueError, match="8 bytes"):
            decode_alarm_info(b"\x00" * 4)
