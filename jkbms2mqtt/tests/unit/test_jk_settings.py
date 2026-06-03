"""Tests for the writable register table + encoders."""

from __future__ import annotations

import pytest

from jkbms2mqtt.protocol.jk_settings import (
    BASIC_REGISTERS,
    PACKED_BIT_REGISTER,
    PACKED_BITS,
    SAFETY_REGISTERS,
    EncodeError,
    Encoding,
    PackedBitDef,
    RegisterDef,
    WriteTier,
    _i32_to_words,
    _u32_to_words,
    all_registers,
    encode_packed_bit_value,
    encode_value_to_words,
    find_packed_bit,
    find_register,
)


class TestRegisterTable:
    def test_basic_registers_all_basic_tier(self) -> None:
        for r in BASIC_REGISTERS:
            assert r.tier is WriteTier.BASIC

    def test_safety_registers_all_safety_tier(self) -> None:
        for r in SAFETY_REGISTERS:
            assert r.tier is WriteTier.SAFETY

    def test_no_duplicate_names(self) -> None:
        names = [r.name for r in all_registers()] + [b.name for b in PACKED_BITS]
        assert len(names) == len(set(names))

    def test_no_overlapping_addresses(self) -> None:
        addrs = [r.address for r in all_registers()]
        assert len(addrs) == len(set(addrs))

    def test_packed_bits_all_share_one_register(self) -> None:
        assert {b.register for b in PACKED_BITS} == {PACKED_BIT_REGISTER}

    def test_packed_bits_have_distinct_masks(self) -> None:
        masks = [b.bit_mask for b in PACKED_BITS]
        assert len(masks) == len(set(masks))

    @pytest.mark.parametrize("reg", list(all_registers()))
    def test_register_bounds_make_sense(self, reg: RegisterDef) -> None:
        assert reg.min_value < reg.max_value
        assert reg.step > 0
        assert 0 <= reg.address <= 0xFFFF
        assert reg.encoding in Encoding

    def test_find_register_hits(self) -> None:
        r = find_register("balance_switch")
        assert r is not None
        assert r.address == 0x1078

    def test_find_register_miss(self) -> None:
        assert find_register("not_a_real_param") is None

    def test_find_packed_bit_hits(self) -> None:
        b = find_packed_bit("smart_sleep_switch")
        assert b is not None
        assert b.bit_mask == 0x0040

    def test_find_packed_bit_miss(self) -> None:
        assert find_packed_bit("nonexistent") is None

    def test_critical_safety_registers_present(self) -> None:
        safety_names = {r.name for r in SAFETY_REGISTERS}
        must_be_safety = {
            "cell_voltage_overvoltage_protection",
            "cell_voltage_undervoltage_protection",
            "max_charge_current",
            "max_discharge_current",
            "charge_overtemperature_protection",
            "cell_count",
        }
        assert must_be_safety <= safety_names


class TestEncodeValueBool32:
    def test_true(self) -> None:
        reg = find_register("balance_switch")
        assert reg is not None
        assert encode_value_to_words(reg, True) == [0, 1]

    def test_false(self) -> None:
        reg = find_register("balance_switch")
        assert reg is not None
        assert encode_value_to_words(reg, False) == [0, 0]


class TestEncodeValueU32Milli:
    def test_voltage_scales_to_millivolts(self) -> None:
        reg = find_register("cell_request_charge_voltage")
        assert reg is not None
        # 3.65 V → 3650 mV → [0x0000, 0x0E42]
        assert encode_value_to_words(reg, 3.65) == [0, 3650]


class TestEncodeValueU32Deci:
    def test_current_scales_to_deci_amps(self) -> None:
        reg = find_register("max_charge_current")
        assert reg is not None
        # 50 A → 500 deci-A → [0, 500]
        assert encode_value_to_words(reg, 50.0) == [0, 500]


class TestEncodeValueI32Deci:
    def test_positive_temp(self) -> None:
        reg = find_register("charge_overtemperature_protection")
        assert reg is not None
        # 70 °C → 700 → [0, 700]
        assert encode_value_to_words(reg, 70.0) == [0, 700]

    def test_negative_temp(self) -> None:
        reg = find_register("charge_undertemperature_protection")
        assert reg is not None
        # -10 °C → -100 → 4294967196 → [0xFFFF, 0xFF9C]
        result = encode_value_to_words(reg, -10.0)
        assert result == [0xFFFF, 0xFF9C]


class TestEncodeValueU32Raw:
    def test_seconds_pass_through(self) -> None:
        reg = find_register("charge_overcurrent_protection_delay")
        assert reg is not None
        assert encode_value_to_words(reg, 30) == [0, 30]

    def test_cell_count_pass_through(self) -> None:
        reg = find_register("cell_count")
        assert reg is not None
        assert encode_value_to_words(reg, 16) == [0, 16]


@pytest.mark.parametrize("reg", list(all_registers()))
def test_min_and_max_accepted(reg: RegisterDef) -> None:
    encode_value_to_words(reg, reg.min_value)
    encode_value_to_words(reg, reg.max_value)


@pytest.mark.parametrize("reg", list(all_registers()))
def test_below_min_rejected(reg: RegisterDef) -> None:
    with pytest.raises(EncodeError, match="outside"):
        encode_value_to_words(reg, reg.min_value - reg.step)


@pytest.mark.parametrize("reg", list(all_registers()))
def test_above_max_rejected(reg: RegisterDef) -> None:
    with pytest.raises(EncodeError, match="outside"):
        encode_value_to_words(reg, reg.max_value + reg.step)


class TestPackedBit:
    def test_set_bit_when_others_zero(self) -> None:
        bit = find_packed_bit("disable_pcl_module_switch")
        assert bit is not None
        assert encode_packed_bit_value(bit, desired_on=True, current_register_value=0) == 0x80

    def test_clear_bit_preserves_others(self) -> None:
        bit = find_packed_bit("disable_pcl_module_switch")
        assert bit is not None
        # PCL on (0x80) + smart sleep on (0x40) = 0xC0. Clearing PCL → 0x40.
        assert (
            encode_packed_bit_value(bit, desired_on=False, current_register_value=0xC0) == 0x40
        )

    def test_set_bit_preserves_others(self) -> None:
        bit = find_packed_bit("smart_sleep_switch")
        assert bit is not None
        # Current 0x80 (PCL). Setting smart sleep → 0xC0.
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
    """Helper functions defended against direct out-of-range callers."""

    def test_u32_negative_rejected(self) -> None:
        with pytest.raises(EncodeError, match="u32"):
            _u32_to_words(-1)

    def test_u32_too_large_rejected(self) -> None:
        with pytest.raises(EncodeError, match="u32"):
            _u32_to_words(0x1_0000_0000)

    def test_i32_too_negative_rejected(self) -> None:
        with pytest.raises(EncodeError, match="i32"):
            _i32_to_words(-(2**31) - 1)

    def test_i32_too_positive_rejected(self) -> None:
        with pytest.raises(EncodeError, match="i32"):
            _i32_to_words(2**31)


def test_packed_bit_def_dataclass_shape() -> None:
    """Sanity check the PackedBitDef has the fields we depend on."""
    b = PackedBitDef(
        name="x", register=0x1234, bit_mask=0x0001, tier=WriteTier.BASIC, description="x"
    )
    assert b.name == "x"
    assert b.bit_mask == 1


class TestDecodeRegisterValue:
    """``decode_register_value`` reverses ``encode_value_to_words`` exactly."""

    @pytest.fixture()
    def regs(self) -> list[int]:
        from jkbms2mqtt.protocol.jk_settings import SETTINGS_BLOCK_WORDS
        return [0] * SETTINGS_BLOCK_WORDS

    def _place(self, regs: list[int], reg: RegisterDef, value: float | bool) -> None:
        from jkbms2mqtt.protocol.jk_settings import SETTINGS_BLOCK_BASE
        words = encode_value_to_words(reg, value)
        off = reg.address - SETTINGS_BLOCK_BASE
        regs[off] = words[0]
        regs[off + 1] = words[1]

    def test_u32_raw_roundtrip(self, regs: list[int]) -> None:
        from jkbms2mqtt.protocol.jk_settings import decode_register_value
        r = next(x for x in SAFETY_REGISTERS if x.encoding is Encoding.U32_RAW)
        self._place(regs, r, 30)
        assert decode_register_value(r, regs) == pytest.approx(30.0)

    def test_u32_milli_roundtrip(self, regs: list[int]) -> None:
        from jkbms2mqtt.protocol.jk_settings import decode_register_value
        r = next(x for x in BASIC_REGISTERS if x.encoding is Encoding.U32_MILLI)
        self._place(regs, r, 0.300)
        assert decode_register_value(r, regs) == pytest.approx(0.300, abs=1e-3)

    def test_u32_deci_roundtrip(self, regs: list[int]) -> None:
        from jkbms2mqtt.protocol.jk_settings import decode_register_value
        r = next(x for x in SAFETY_REGISTERS if x.encoding is Encoding.U32_DECI)
        self._place(regs, r, 50.0)
        assert decode_register_value(r, regs) == pytest.approx(50.0)

    def test_i32_deci_negative_roundtrip(self, regs: list[int]) -> None:
        from jkbms2mqtt.protocol.jk_settings import decode_register_value
        r = next(x for x in SAFETY_REGISTERS if x.encoding is Encoding.I32_DECI)
        self._place(regs, r, -20.0)
        assert decode_register_value(r, regs) == pytest.approx(-20.0)

    def test_bool32_roundtrip_on(self, regs: list[int]) -> None:
        from jkbms2mqtt.protocol.jk_settings import decode_register_value
        r = next(x for x in BASIC_REGISTERS if x.encoding is Encoding.BOOL32)
        self._place(regs, r, True)
        assert decode_register_value(r, regs) is True

    def test_bool32_roundtrip_off(self, regs: list[int]) -> None:
        from jkbms2mqtt.protocol.jk_settings import decode_register_value
        r = next(x for x in BASIC_REGISTERS if x.encoding is Encoding.BOOL32)
        self._place(regs, r, False)
        assert decode_register_value(r, regs) is False

    def test_out_of_block_register_rejected(self) -> None:
        from jkbms2mqtt.protocol.jk_settings import decode_register_value
        r = RegisterDef(
            name="bogus", address=0x9000, encoding=Encoding.U32_RAW,
            min_value=0, max_value=1, step=1, unit=None,
            tier=WriteTier.BASIC, description="off-block",
        )
        with pytest.raises(EncodeError, match="outside settings block"):
            decode_register_value(r, [0] * 4)


class TestDecodePackedBit:
    def test_bit_set(self) -> None:
        from jkbms2mqtt.protocol.jk_settings import decode_packed_bit_value
        bit = PACKED_BITS[0]
        assert decode_packed_bit_value(bit, bit.bit_mask) is True

    def test_bit_unset(self) -> None:
        from jkbms2mqtt.protocol.jk_settings import decode_packed_bit_value
        bit = PACKED_BITS[0]
        assert decode_packed_bit_value(bit, 0x0000) is False

    def test_other_bits_ignored(self) -> None:
        from jkbms2mqtt.protocol.jk_settings import decode_packed_bit_value
        bit = PACKED_BITS[0]
        # All other bits set, but mask bit cleared.
        assert decode_packed_bit_value(bit, 0xFFFF ^ bit.bit_mask) is False
