"""Property-based tests for the JK Modbus decoder."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from jkbms2mqtt.protocol.jk_modbus import (
    ALARM_NAMES,
    RT_BLOCK_WORDS,
    _i16,
    _i32,
    _u16,
    _u32,
    decode_realtime,
)


@given(st.integers(min_value=0, max_value=0xFFFF))
def test_u16_roundtrip(v: int) -> None:
    assert _u16([v], 0) == v


@given(st.integers(min_value=-32768, max_value=32767))
def test_i16_roundtrip(v: int) -> None:
    word = v + 0x10000 if v < 0 else v
    assert _i16([word], 0) == v


@given(st.integers(min_value=0, max_value=0xFFFFFFFF))
def test_u32_roundtrip(v: int) -> None:
    regs = [(v >> 16) & 0xFFFF, v & 0xFFFF]
    assert _u32(regs, 0) == v


@given(st.integers(min_value=-(2**31), max_value=2**31 - 1))
def test_i32_roundtrip(v: int) -> None:
    word = v + 0x1_0000_0000 if v < 0 else v
    regs = [(word >> 16) & 0xFFFF, word & 0xFFFF]
    assert _i32(regs, 0) == v


@given(st.integers(min_value=0, max_value=2**22 - 1))
def test_alarm_decode_subset_matches_bitmap(bits: int) -> None:
    regs = [0] * RT_BLOCK_WORDS
    regs[0x50] = (bits >> 16) & 0xFFFF
    regs[0x51] = bits & 0xFFFF
    result = decode_realtime(regs)
    expected = {ALARM_NAMES[i] for i in range(len(ALARM_NAMES)) if bits & (1 << i)}
    assert set(result.alarms) == expected
    assert result.alarm_bits == bits


@given(
    cell_count=st.integers(min_value=1, max_value=16),
    base_mv=st.integers(min_value=2500, max_value=4200),
)
def test_cell_count_matches_present_bitmap(cell_count: int, base_mv: int) -> None:
    """For any cell_count 1..16, the decoded cell_count must match the bitmap."""
    regs = [0] * RT_BLOCK_WORDS
    # Cells 1..cell_count present, all at base_mv mV.
    regs[0x20] = ((1 << cell_count) - 1) >> 16 & 0xFFFF
    regs[0x21] = ((1 << cell_count) - 1) & 0xFFFF
    for i in range(cell_count):
        regs[i] = base_mv
    result = decode_realtime(regs)
    assert result.cell_count == cell_count
    assert len(result.cell_voltages_v) == cell_count
