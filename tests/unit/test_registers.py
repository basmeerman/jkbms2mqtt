"""Tests for the register table: lookups, tier consistency, no accidental duplicates."""

from __future__ import annotations

import pytest

from jkbms2mqtt.protocol.registers import (
    BASIC_REGISTERS,
    PACKED_BITS,
    POLL_TRIGGER_FIXED,
    POLL_TRIGGER_LIVE,
    POLL_TRIGGER_SETUP,
    SAFETY_REGISTERS,
    Encoding,
    WriteTier,
    all_registers,
    find_packed_bit,
    find_register,
)


def test_basic_registers_all_tagged_basic() -> None:
    for r in BASIC_REGISTERS:
        assert r.tier is WriteTier.BASIC


def test_safety_registers_all_tagged_safety() -> None:
    for r in SAFETY_REGISTERS:
        assert r.tier is WriteTier.SAFETY


def test_no_duplicate_parameter_names() -> None:
    names = [r.name for r in all_registers()] + [b.name for b in PACKED_BITS]
    assert len(names) == len(set(names)), f"duplicate parameter names: {names}"


def test_no_overlapping_addresses_across_function_10_registers() -> None:
    addresses = [r.address for r in all_registers()]
    assert len(addresses) == len(set(addresses)), f"duplicate addresses: {addresses}"


def test_packed_bits_share_register_address() -> None:
    # All packed bits live in 0x1114
    assert {b.register for b in PACKED_BITS} == {0x1114}


def test_packed_bits_have_distinct_masks() -> None:
    masks = [b.bit_mask for b in PACKED_BITS]
    assert len(masks) == len(set(masks))


@pytest.mark.parametrize("reg", list(all_registers()))
def test_register_bounds_make_sense(reg: object) -> None:
    # min < max, step > 0, address in valid range
    from jkbms2mqtt.protocol.registers import RegisterDef

    assert isinstance(reg, RegisterDef)
    assert reg.min_value < reg.max_value
    assert reg.step > 0
    assert 0 <= reg.address <= 0xFFFF
    assert reg.encoding in Encoding


def test_find_register_hits() -> None:
    r = find_register("balance_switch")
    assert r is not None
    assert r.address == 0x1078
    assert r.tier is WriteTier.BASIC


def test_find_register_miss() -> None:
    assert find_register("not_a_real_param") is None


def test_find_packed_bit_hits() -> None:
    b = find_packed_bit("smart_sleep_switch")
    assert b is not None
    assert b.bit_mask == 0x0040


def test_find_packed_bit_miss() -> None:
    assert find_packed_bit("nonexistent") is None


def test_all_registers_returns_union() -> None:
    union = all_registers()
    assert set(union) == set(BASIC_REGISTERS) | set(SAFETY_REGISTERS)


def test_poll_trigger_addresses_are_distinct() -> None:
    triggers = {POLL_TRIGGER_FIXED, POLL_TRIGGER_LIVE, POLL_TRIGGER_SETUP}
    assert len(triggers) == 3


def test_safety_registers_include_critical_thresholds() -> None:
    # Spot-check a handful: if any of these slip out of SAFETY, the safety gate is broken.
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
