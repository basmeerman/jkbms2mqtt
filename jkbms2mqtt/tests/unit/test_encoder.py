"""Encoder tests: every encoding path + per-parameter bound enforcement."""

from __future__ import annotations

import pytest

from jkbms2mqtt.protocol.encoder import EncodeError, encode_packed_bit_value, encode_value
from jkbms2mqtt.protocol.registers import (
    PACKED_BITS,
    all_registers,
    find_packed_bit,
    find_register,
)


class TestEncodeValueBOOL32:
    def test_true_encodes_to_one(self) -> None:
        reg = find_register("balance_switch")
        assert reg is not None
        assert encode_value(reg, True) == b"\x00\x00\x00\x01"

    def test_false_encodes_to_zero(self) -> None:
        reg = find_register("balance_switch")
        assert reg is not None
        assert encode_value(reg, False) == b"\x00\x00\x00\x00"

    def test_truthy_int_encodes_to_one(self) -> None:
        reg = find_register("charging_switch")
        assert reg is not None
        assert encode_value(reg, 1) == b"\x00\x00\x00\x01"


class TestEncodeValueU32_MILLI:
    def test_voltage_scales_to_millivolts(self) -> None:
        reg = find_register("cell_request_charge_voltage")
        assert reg is not None
        # 3.65 V → 3650 mV → 0x00000E42
        assert encode_value(reg, 3.65) == (3650).to_bytes(4, "big")

    def test_voltage_out_of_range_raises(self) -> None:
        reg = find_register("cell_request_charge_voltage")
        assert reg is not None
        with pytest.raises(EncodeError, match="outside"):
            encode_value(reg, 10.0)
        with pytest.raises(EncodeError, match="outside"):
            encode_value(reg, 0.5)


class TestEncodeValueU32_DECI:
    def test_current_scales_to_deci_amps(self) -> None:
        reg = find_register("max_charge_current")
        assert reg is not None
        # 50.0 A → 500 deci-A
        assert encode_value(reg, 50.0) == (500).to_bytes(4, "big")


class TestEncodeValueI32_DECI_SignedTemp:
    def test_positive_temp(self) -> None:
        reg = find_register("charge_overtemperature_protection")
        assert reg is not None
        # 70.0 °C → 700 (×10)
        assert encode_value(reg, 70.0) == (700).to_bytes(4, "big", signed=True)

    def test_negative_temp(self) -> None:
        reg = find_register("charge_undertemperature_protection")
        assert reg is not None
        # -10.0 °C → -100
        assert encode_value(reg, -10.0) == (-100).to_bytes(4, "big", signed=True)

    def test_negative_temp_first_byte_is_ff(self) -> None:
        reg = find_register("charge_undertemperature_protection")
        assert reg is not None
        # Negative two's-complement should make the high byte 0xFF.
        encoded = encode_value(reg, -1.0)
        assert encoded[0] == 0xFF


class TestEncodeValueU32_RAW:
    def test_seconds_pass_through(self) -> None:
        reg = find_register("charge_overcurrent_protection_delay")
        assert reg is not None
        # 30 s → 30
        assert encode_value(reg, 30) == (30).to_bytes(4, "big")

    def test_cell_count_pass_through(self) -> None:
        reg = find_register("cell_count")
        assert reg is not None
        assert encode_value(reg, 8) == (8).to_bytes(4, "big")


@pytest.mark.parametrize("reg", list(all_registers()))
def test_min_and_max_are_accepted(reg: object) -> None:
    """Boundary values should always encode without error."""
    from jkbms2mqtt.protocol.registers import RegisterDef

    assert isinstance(reg, RegisterDef)
    encode_value(reg, reg.min_value)
    encode_value(reg, reg.max_value)


@pytest.mark.parametrize("reg", list(all_registers()))
def test_below_min_raises(reg: object) -> None:
    from jkbms2mqtt.protocol.registers import RegisterDef

    assert isinstance(reg, RegisterDef)
    with pytest.raises(EncodeError, match="outside"):
        encode_value(reg, reg.min_value - reg.step)


@pytest.mark.parametrize("reg", list(all_registers()))
def test_above_max_raises(reg: object) -> None:
    from jkbms2mqtt.protocol.registers import RegisterDef

    assert isinstance(reg, RegisterDef)
    with pytest.raises(EncodeError, match="outside"):
        encode_value(reg, reg.max_value + reg.step)


class TestEncodePackedBit:
    def test_setting_bit_when_others_zero(self) -> None:
        bit = find_packed_bit("disable_pcl_module_switch")
        assert bit is not None
        assert encode_packed_bit_value(bit, desired_on=True, current_register_value=0) == 0x80

    def test_clearing_bit_preserves_others(self) -> None:
        bit = find_packed_bit("disable_pcl_module_switch")
        assert bit is not None
        # Current = 0xFF (all bits on). Clearing bit 7 should leave 0x7F.
        assert (
            encode_packed_bit_value(bit, desired_on=False, current_register_value=0xFF) == 0x7F
        )

    def test_setting_bit_preserves_others(self) -> None:
        bit = find_packed_bit("smart_sleep_switch")
        assert bit is not None
        # Current = 0x80 (PCL on). Setting smart sleep should give 0xC0.
        assert (
            encode_packed_bit_value(bit, desired_on=True, current_register_value=0x80) == 0xC0
        )

    def test_invalid_current_register_value(self) -> None:
        bit = PACKED_BITS[0]
        with pytest.raises(EncodeError, match="16 bits"):
            encode_packed_bit_value(bit, desired_on=True, current_register_value=0x10000)
        with pytest.raises(EncodeError, match="16 bits"):
            encode_packed_bit_value(bit, desired_on=True, current_register_value=-1)


class TestEncoderInternals:
    """Test the _u32_be / _i32_be bound guards via crafted scenarios.

    The public encoder enforces `reg.min_value <= value <= reg.max_value` *before*
    invoking the int conversion. But the inner helpers still need their own bound
    checks for defence in depth — exercise them by directly importing.
    """

    def test_u32_be_below_zero(self) -> None:
        from jkbms2mqtt.protocol.encoder import _u32_be

        with pytest.raises(EncodeError, match="u32"):
            _u32_be(-1)

    def test_u32_be_too_large(self) -> None:
        from jkbms2mqtt.protocol.encoder import _u32_be

        with pytest.raises(EncodeError, match="u32"):
            _u32_be(0x1_0000_0000)

    def test_i32_be_too_negative(self) -> None:
        from jkbms2mqtt.protocol.encoder import _i32_be

        with pytest.raises(EncodeError, match="i32"):
            _i32_be(-(2**31) - 1)

    def test_i32_be_too_positive(self) -> None:
        from jkbms2mqtt.protocol.encoder import _i32_be

        with pytest.raises(EncodeError, match="i32"):
            _i32_be(2**31)
