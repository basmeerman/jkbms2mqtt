"""Decode the real-hardware capture in scripts/captures/BMS_1.txt and assert
every value matches the anchor table documented in docs/FIELD_AUDIT.md.

If anyone moves a register offset without updating the capture and the audit,
this test breaks. That's the point — the test is the ratchet that prevents the
class of speculative-offset bugs we shipped (and then had to revert) in #1/#2.

The capture file format is the one ``scripts/dump_registers.py`` produces:

    # COMMENT lines start with '#'
    # SECTION HEADERS are bare lines like '## decoder ...' (ignored)
    # ANCHOR VALUES are an annotation block we read in a second pass

    # RT_A @ 0x1200  (count=120)
      0x1200:  0c6f 0c6f 0c6f ...

We only need the raw hex rows. We pull them by their block label so a
hypothetical future change to the dump format doesn't silently re-tier them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from jkbms2mqtt.protocol.jk_modbus import (
    BASE_INFO,
    BASE_RT,
    INFO_BLOCK_WORDS,
    RT_BLOCK_WORDS,
    decode_realtime,
    decode_static_info,
)
from jkbms2mqtt.protocol.jk_settings import (
    BASIC_REGISTERS,
    PACKED_BIT_REGISTER,
    SAFETY_REGISTERS,
    SETTINGS_BLOCK_BASE,
    SETTINGS_BLOCK_WORDS,
    decode_register_value,
    find_register,
)

CAPTURE = Path(__file__).resolve().parents[2] / "scripts" / "captures" / "BMS_1.txt"


def _parse_hex_rows(capture: str, header_prefix: str) -> dict[int, int]:
    """Return ``{absolute_address: 16-bit value}`` for one named block.

    ``header_prefix`` is something like ``"# RT_A @ 0x1200"`` — the parser
    starts collecting rows that follow it until the next ``# `` header or a
    section divider (``===``).
    """
    out: dict[int, int] = {}
    lines = capture.splitlines()
    started = False
    for line in lines:
        stripped = line.strip()
        if not started:
            if stripped.startswith(header_prefix):
                started = True
            continue
        if not stripped:
            continue
        if stripped.startswith("===") or stripped.startswith("# "):
            break
        # Row format: "0x1200:  0c6f 0c6f 0c6f ..." (optional ascii sidecar after |..|)
        m = re.match(r"0x([0-9a-fA-F]+):\s+([0-9a-fA-F ]+?)(?:\s*\|.*\|)?\s*$", stripped)
        if not m:
            continue
        base_addr = int(m.group(1), 16)
        values = [int(tok, 16) for tok in m.group(2).split()]
        for i, v in enumerate(values):
            out[base_addr + i] = v
    return out


@pytest.fixture(scope="module")
def capture_text() -> str:
    return CAPTURE.read_text()


@pytest.fixture(scope="module")
def rt_buffer(capture_text: str) -> list[int]:
    """Stitched real-time buffer keyed from offset 0 = address 0x1200."""
    regs = [0] * RT_BLOCK_WORDS
    for header in ("# RT_A @ 0x1200", "# RT_B @ 0x1278", "# RT_C @ 0x12f0"):
        for addr, v in _parse_hex_rows(capture_text, header).items():
            off = addr - BASE_RT
            if 0 <= off < RT_BLOCK_WORDS:
                regs[off] = v
    return regs


@pytest.fixture(scope="module")
def settings_buffer(capture_text: str) -> tuple[list[int], set[int]]:
    """Stitched settings buffer + the set of addresses actually present."""
    regs = [0] * SETTINGS_BLOCK_WORDS
    read_addrs: set[int] = set()
    for header in ("# SETTINGS_1 @ 0x1000", "# SETTINGS_2 @ 0x1064"):
        for addr, v in _parse_hex_rows(capture_text, header).items():
            off = addr - SETTINGS_BLOCK_BASE
            if 0 <= off < SETTINGS_BLOCK_WORDS:
                regs[off] = v
                read_addrs.add(addr)
    return regs, read_addrs


@pytest.fixture(scope="module")
def info_buffer(capture_text: str) -> list[int]:
    regs = [0] * INFO_BLOCK_WORDS
    for addr, v in _parse_hex_rows(capture_text, "# INFO @ 0x1400").items():
        off = addr - BASE_INFO
        if 0 <= off < INFO_BLOCK_WORDS:
            regs[off] = v
    return regs


@pytest.fixture(scope="module")
def packed_bit_value(capture_text: str) -> int:
    rows = _parse_hex_rows(capture_text, "# PACKED BIT")
    # Fallback to scanning by absolute address — header text is non-standard.
    if PACKED_BIT_REGISTER in rows:
        return rows[PACKED_BIT_REGISTER]
    # Manual scrape: the file lists "  0x1114:  3200" on its own line.
    for line in capture_text.splitlines():
        m = re.match(r"\s*0x1114:\s+([0-9a-fA-F]+)", line)
        if m:
            return int(m.group(1), 16)
    raise AssertionError("packed-bit register 0x1114 not found in capture")


# -- Anchor values (from docs/FIELD_AUDIT.md / BMS_1.txt anchor section) ----------------

class TestRealtimeAnchors:
    def test_cell_voltages_all_3_183(self, rt_buffer: list[int]) -> None:
        live = decode_realtime(rt_buffer)
        assert len(live.cell_voltages_v) == 16
        for v in live.cell_voltages_v:
            assert v == pytest.approx(3.183, abs=1e-3)

    def test_cell_resistances_match_app(self, rt_buffer: list[int]) -> None:
        """The anchor that put the original 0x80 → 0x25 fix on the map."""
        live = decode_realtime(rt_buffer)
        expected = [
            0.062, 0.066, 0.076, 0.078, 0.092, 0.099,
            0.114, 0.120, 0.102, 0.084, 0.091, 0.076,
            0.073, 0.070, 0.067, 0.059,
        ]
        assert len(live.cell_resistances_ohm) == 16
        for got, want in zip(live.cell_resistances_ohm, expected, strict=True):
            assert got == pytest.approx(want, abs=1e-3)

    def test_total_voltage(self, rt_buffer: list[int]) -> None:
        assert decode_realtime(rt_buffer).total_voltage_v == pytest.approx(50.923, abs=1e-3)

    def test_total_current_zero(self, rt_buffer: list[int]) -> None:
        assert decode_realtime(rt_buffer).total_current_a == pytest.approx(0.0)

    def test_soc(self, rt_buffer: list[int]) -> None:
        assert decode_realtime(rt_buffer).soc_percentage == 59

    def test_soh(self, rt_buffer: list[int]) -> None:
        assert decode_realtime(rt_buffer).soh_percentage == 100

    def test_remaining_capacity(self, rt_buffer: list[int]) -> None:
        assert decode_realtime(rt_buffer).remaining_capacity_ah == pytest.approx(184.041, abs=1e-2)

    def test_nominal_capacity(self, rt_buffer: list[int]) -> None:
        assert decode_realtime(rt_buffer).nominal_capacity_ah == pytest.approx(314.0, abs=1e-2)

    def test_cycle_count(self, rt_buffer: list[int]) -> None:
        assert decode_realtime(rt_buffer).cycle_count == 0

    def test_total_cycle_capacity(self, rt_buffer: list[int]) -> None:
        # ⭐ My speculative offset that turned out to be right.
        assert decode_realtime(rt_buffer).total_cycle_capacity_ah == pytest.approx(24.888, abs=1e-2)

    def test_runtime(self, rt_buffer: list[int]) -> None:
        assert decode_realtime(rt_buffer).runtime_s == 12_232_624

    def test_mos_temp(self, rt_buffer: list[int]) -> None:
        assert decode_realtime(rt_buffer).mos_temp_c == pytest.approx(23.1, abs=1e-1)

    def test_probe_1_and_2(self, rt_buffer: list[int]) -> None:
        live = decode_realtime(rt_buffer)
        assert live.probe_1_temp_c == pytest.approx(21.6, abs=1e-1)
        assert live.probe_2_temp_c == pytest.approx(21.6, abs=1e-1)

    def test_probes_3_4_5_block_c(self, rt_buffer: list[int]) -> None:
        """The block-C fix — the previous 0x7C/D/E offsets read mirror noise."""
        live = decode_realtime(rt_buffer)
        # Probe 3: BMS app didn't display T3 but raw word is 0x00E7 = 23.1.
        assert live.probe_3_temp_c == pytest.approx(23.1, abs=1e-1)
        assert live.probe_4_temp_c == pytest.approx(21.6, abs=1e-1)  # T4 in app
        assert live.probe_5_temp_c == pytest.approx(21.8, abs=1e-1)  # T5 in app

    def test_charge_and_discharge_enabled(self, rt_buffer: list[int]) -> None:
        live = decode_realtime(rt_buffer)
        assert live.charge_enabled is True
        assert live.discharge_enabled is True

    def test_no_alarms(self, rt_buffer: list[int]) -> None:
        live = decode_realtime(rt_buffer)
        assert live.alarm_bits == 0
        assert live.alarms == ()


class TestStaticInfoAnchors:
    def test_model(self, info_buffer: list[int]) -> None:
        assert decode_static_info(info_buffer).model == "JK_PB2A16S20P"

    def test_hw_version(self, info_buffer: list[int]) -> None:
        assert decode_static_info(info_buffer).hw_version == "15A"

    def test_sw_version(self, info_buffer: list[int]) -> None:
        assert decode_static_info(info_buffer).sw_version == "15.41"

    def test_serial_number_starts_with_BMS_specific_prefix(self, info_buffer: list[int]) -> None:
        # Serial in the capture is "50314490295" + trailing pad; just sanity check.
        assert decode_static_info(info_buffer).serial_number.startswith("50314490295")


# -- Settings anchors — these are the addresses that were wrong in PR #1/#2 ------------

@pytest.mark.parametrize(("name", "expected"), [
    ("smart_sleep_voltage", 3.500),
    ("balance_trigger_voltage", 0.010),
    ("cell_soc100_voltage", 3.590),
    ("cell_soc0_voltage", 2.600),
    ("cell_request_charge_voltage", 3.600),
    ("cell_request_float_voltage", 3.500),
    ("max_balance_current", 2.000),
    ("balance_starting_voltage", 3.000),
    # Spec-restored BOOL switches at 0x1038/0x103A/0x103C.
    ("charging_switch", True),
    ("discharging_switch", True),
    ("balance_switch", True),
])
def test_basic_settings_match_app(
    name: str, expected: float | bool, settings_buffer: tuple[list[int], set[int]]
) -> None:
    """Every basic-tier address verified against the BMS app's Settings tab."""
    regs, _ = settings_buffer
    reg = find_register(name)
    assert reg is not None, f"{name} not in BASIC_REGISTERS"
    assert reg in BASIC_REGISTERS
    if isinstance(expected, bool):
        assert decode_register_value(reg, regs) is expected
    else:
        assert decode_register_value(reg, regs) == pytest.approx(expected, abs=1e-3)


@pytest.mark.parametrize(("name", "expected"), [
    ("cell_voltage_undervoltage_protection", 2.580),
    ("cell_voltage_undervoltage_recovery", 2.620),
    ("cell_voltage_overvoltage_protection", 3.650),
    ("cell_voltage_overvoltage_recovery", 3.580),
    ("power_off_voltage", 2.500),
    ("max_charge_current", 40.000),
    ("charge_overcurrent_protection_delay", 3.0),
    ("charge_overcurrent_protection_recovery_time", 60.0),
    ("max_discharge_current", 200.000),
    ("discharge_overcurrent_protection_delay", 300.0),
    ("discharge_overcurrent_protection_recovery_time", 60.0),
    ("short_circuit_protection_recovery_time", 5.0),
    ("discharge_overtemperature_protection", 70.0),
    ("discharge_overtemperature_protection_recovery", 60.0),
    ("charge_overtemperature_protection", 70.0),
    ("charge_overtemperature_protection_recovery", 60.0),
    ("charge_undertemperature_protection", 2.0),
    ("charge_undertemperature_protection_recovery", 7.0),
    ("power_tube_overtemperature_protection", 80.0),
    ("power_tube_overtemperature_protection_recovery", 70.0),
    ("cell_count", 16),
    ("pack_capacity_setting", 314.0),
    ("short_circuit_protection_delay_us", 1500),
])
def test_safety_settings_match_app(
    name: str, expected: float, settings_buffer: tuple[list[int], set[int]]
) -> None:
    regs, _ = settings_buffer
    reg = find_register(name)
    assert reg is not None, f"{name} not in SAFETY_REGISTERS"
    assert reg in SAFETY_REGISTERS
    assert decode_register_value(reg, regs) == pytest.approx(expected, abs=1e-2)


# -- Spec compliance: address-name binding --------------------------------------------

def test_otp_register_addresses_match_spec() -> None:
    """Charge / discharge OTP labels must point at the spec'd addresses.

    Both values are 70 °C on this BMS so a value-only check can't detect a
    label swap. This test asserts that ``charge_overtemperature_protection``
    maps to spec byte 0x4C → reg 0x1026 and discharge variants to 0x102A —
    closing the swap regression introduced earlier.
    """
    by_name = {r.name: r.address for r in SAFETY_REGISTERS}
    assert by_name["charge_overtemperature_protection"] == 0x1026
    assert by_name["charge_overtemperature_protection_recovery"] == 0x1028
    assert by_name["discharge_overtemperature_protection"] == 0x102A
    assert by_name["discharge_overtemperature_protection_recovery"] == 0x102C


# -- Packed-bit register --------------------------------------------------------------

def test_packed_bit_register_value(packed_bit_value: int) -> None:
    """The packed-bit register held 0x3200 in the captured firmware.

    Bit 9 (ChargingFloatMode) is set, matching the BMS app's
    "Charging Float Mode: ON". Bits 12 / 13 are also set, corresponding to
    "Discharge OCP 2" / "Discharge OCP 3" — not documented in V1.1 spec.
    """
    assert packed_bit_value == 0x3200


def test_packed_bit_decodes_per_spec_v11_positions() -> None:
    """The three exposed packed bits decode according to spec V1.1 masks."""
    from jkbms2mqtt.protocol.jk_settings import (
        PACKED_BITS,
        decode_packed_bit_value,
    )

    bits = {b.name: b for b in PACKED_BITS}
    # Spec V1.1: BIT6 SmartSleep, BIT7 DisablePCLModule, BIT8 TimedStoredData.
    assert bits["smart_sleep_switch"].bit_mask == 0x0040
    assert bits["disable_pcl_module_switch"].bit_mask == 0x0080
    assert bits["timed_stored_data_switch"].bit_mask == 0x0100

    # In the captured 0x3200 register value, none of those three are set —
    # matches app Control-tab where SmartSleep/Par-Limiter/TimedStored are OFF.
    raw = 0x3200
    assert decode_packed_bit_value(bits["smart_sleep_switch"], raw) is False
    assert decode_packed_bit_value(bits["disable_pcl_module_switch"], raw) is False
    assert decode_packed_bit_value(bits["timed_stored_data_switch"], raw) is False
