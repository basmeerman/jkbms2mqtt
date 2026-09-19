"""Tests for scripts/hw_testbench.py — the real-hardware read/write/restore bench.

The bench writes to physical battery packs, so every decision it makes has to be
correct before it ever opens a socket: a wrong address writes an unrelated
setting, and a broken restore leaves a pack misconfigured.

The fake BMS below models the firmware's *byte* addressing directly — it stores
a byte array and a read at ``base + byte_offset`` returns the words starting at
that byte. That is precisely the hypothesis issue #32 rests on, so if the bench
computes addresses the old (word-index) way, these tests fail.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import ClassVar

import pytest

from jkbms2mqtt.protocol.jk_settings import (
    PACKED_BIT_REGISTER,
    PACKED_BITS,
    SETTINGS_BLOCK_BASE,
    Encoding,
    RegisterDef,
    WriteTier,
    encode_value_to_words,
    find_register,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from hw_testbench import (  # noqa: E402
    BusError,
    PackReport,
    Snapshot,
    Status,
    all_test_registers,
    check_packed_bit,
    check_register,
    collision_report,
    diff_words,
    owned_indices,
    plan_perturbation,
    save_snapshot,
    selected_bits,
    selected_registers,
    side_effects,
    spec_byte,
    summarise,
    take_snapshot,
    word_index,
    write_address,
)

# -- Fake BMS ---------------------------------------------------------------------------

MEMORY_BYTES = 0x200


class FakeResponse:
    """Mimics pymodbus' response surface."""

    def __init__(
        self,
        registers: list[int] | None = None,
        *,
        error: bool = False,
        exception_code: int | None = None,
    ) -> None:
        self.registers = registers or []
        self._error = error
        self.exception_code = exception_code

    def isError(self) -> bool:
        return self._error

    def __str__(self) -> str:
        return f"FakeResponse(error={self._error}, exception_code={self.exception_code})"


class FakeBms:
    """A JK BMS whose settings window is addressed as ``base + byte_offset``.

    ``corrupt`` optionally writes a stray byte pair on every write, to exercise
    the side-effect detector. ``ignore_writes`` models a BMS that ACKs but does
    nothing. ``break_restore`` corrupts the second write to a given address, so
    the restore-verification path can be tested.
    """

    def __init__(
        self,
        *,
        corrupt_at_byte: int | None = None,
        transient_corrupt_at_byte: int | None = None,
        ignore_writes: bool = False,
        break_restore: bool = False,
    ) -> None:
        self.mem = bytearray(MEMORY_BYTES)
        self.corrupt_at_byte = corrupt_at_byte
        self.transient_corrupt_at_byte = transient_corrupt_at_byte
        self.ignore_writes = ignore_writes
        self.break_restore = break_restore
        self.writes: list[tuple[int, list[int]]] = []
        self._write_counts: dict[int, int] = {}
        self._stray_original: int | None = None

    def _apply_transient(self) -> None:
        """Model a BMS that recomputes a derived register, then settles back.

        The first write disturbs an unrelated word; the following write (the
        bench's restore) puts it back. This is what makes a genuine side effect
        observable in the mid-test snapshot without also breaking the restore.
        """
        if self.transient_corrupt_at_byte is None:
            return
        address = SETTINGS_BLOCK_BASE + self.transient_corrupt_at_byte
        if self._stray_original is None:
            self._stray_original = self.peek(address, 1)[0]
            self.poke(address, [0xDEAD])
        else:
            self.poke(address, [self._stray_original])

    # -- raw helpers ---------------------------------------------------------------
    def poke(self, address: int, values: list[int]) -> None:
        """Seed memory without going through the recorded write path."""
        offset = address - SETTINGS_BLOCK_BASE
        for i, word in enumerate(values):
            self.mem[offset + 2 * i] = (word >> 8) & 0xFF
            self.mem[offset + 2 * i + 1] = word & 0xFF

    def peek(self, address: int, count: int) -> list[int]:
        offset = address - SETTINGS_BLOCK_BASE
        return [
            (self.mem[offset + 2 * i] << 8) | self.mem[offset + 2 * i + 1] for i in range(count)
        ]

    # -- pymodbus surface ----------------------------------------------------------
    async def read_holding_registers(self, *, address: int, count: int, device_id: int) -> FakeResponse:
        offset = address - SETTINGS_BLOCK_BASE
        if offset < 0 or offset + 2 * count > MEMORY_BYTES:
            return FakeResponse(error=True)
        return FakeResponse(self.peek(address, count))

    async def write_registers(self, *, address: int, values: list[int], device_id: int) -> FakeResponse:
        self.writes.append((address, list(values)))
        seen = self._write_counts.get(address, 0)
        self._write_counts[address] = seen + 1
        if self.ignore_writes:
            return FakeResponse()
        if self.break_restore and seen >= 1:
            # The restore write silently does nothing.
            return FakeResponse()
        self.poke(address, values)
        if self.corrupt_at_byte is not None:
            stray = SETTINGS_BLOCK_BASE + self.corrupt_at_byte
            self.poke(stray, [0xDEAD])
        self._apply_transient()
        return FakeResponse()

    async def write_register(self, *, address: int, value: int, device_id: int) -> FakeResponse:
        self.writes.append((address, [value]))
        seen = self._write_counts.get(address, 0)
        self._write_counts[address] = seen + 1
        if self.ignore_writes:
            return FakeResponse()
        if self.break_restore and seen >= 1:
            return FakeResponse()
        self.poke(address, [value])
        if self.corrupt_at_byte is not None:
            self.poke(SETTINGS_BLOCK_BASE + self.corrupt_at_byte, [0xDEAD])
        return FakeResponse()


def _mid(reg: RegisterDef) -> float | bool:
    """A value comfortably inside the range AND exactly representable on the wire.

    Quantised to the encoding's own resolution, so seeding a fake pack with it
    and reading it back is lossless. Without this, an integer-encoded parameter
    seeded at e.g. 16.5 reads back as 16 and every equality check drifts.
    """
    if reg.encoding is Encoding.BOOL32:
        return True
    raw = reg.min_value + (reg.max_value - reg.min_value) / 2
    if reg.encoding is Encoding.U32_RAW:
        return float(round(raw))
    if reg.encoding is Encoding.U32_MILLI:
        return round(raw, 3)
    # U32_DECI and I32_DECI both carry one decimal place.
    return round(raw, 1)


def seeded_bms(**kwargs: object) -> FakeBms:
    """A fake pack with every parameter set to a mid-range value."""
    bms = FakeBms(**kwargs)  # type: ignore[arg-type]
    for reg in all_test_registers():
        bms.poke(write_address(reg), encode_value_to_words(reg, _mid(reg)))
    bms.poke(PACKED_BIT_REGISTER, [0x3200])
    return bms


# -- Addressing ---------------------------------------------------------------------


class TestAddressing:
    def test_spec_byte_is_double_the_word_index(self) -> None:
        reg = find_register("smart_sleep_voltage")
        assert reg is not None
        assert word_index(reg) == 0
        assert spec_byte(reg) == 0x00
        assert write_address(reg) == 0x1000

    def test_switches_match_the_spec_byte_offsets(self) -> None:
        """Spec V1.1: BatChargeEN 0x70, BatDisChargeEN 0x74, BalanEN 0x78.

        phinix writes 0x1070 / 0x1074 / 0x1078 for these (docs/FIELD_MATRIX.md).
        """
        expected = {
            "charging_switch": 0x1070,
            "discharging_switch": 0x1074,
            "balance_switch": 0x1078,
        }
        for name, address in expected.items():
            reg = find_register(name)
            assert reg is not None
            assert write_address(reg) == address

    def test_balance_starting_voltage(self) -> None:
        """Spec byte 0x84 — the same offset jean-luc's Trame 2 parses at 138-6."""
        reg = find_register("balance_starting_voltage")
        assert reg is not None
        assert spec_byte(reg) == 0x84
        assert write_address(reg) == 0x1084

    def test_no_two_parameters_share_a_write_address(self) -> None:
        addresses = [write_address(r) for r in all_test_registers()]
        assert len(addresses) == len(set(addresses))

    def test_write_addresses_stay_inside_the_snapshot_window(self) -> None:
        for reg in all_test_registers():
            assert write_address(reg) + 1 < SETTINGS_BLOCK_BASE + 2 * 120

    def test_agrees_with_the_production_helper(self) -> None:
        """The bench computes addresses independently of production on purpose.

        Keeping the two implementations separate means the bench can validate
        production rather than inherit its bugs; this test pins them together
        so a regression in either is caught.
        """
        from jkbms2mqtt.protocol.jk_settings import write_address as production

        for reg in all_test_registers():
            assert write_address(reg) == production(reg), reg.name

    def test_owned_indices_covers_both_words(self) -> None:
        reg = find_register("max_charge_current")
        assert reg is not None
        assert owned_indices(reg) == frozenset({word_index(reg), word_index(reg) + 1})


class TestCollisionReport:
    def test_reports_where_uncorrected_writes_land(self) -> None:
        lines = collision_report()
        body = "\n".join(lines)
        assert "parameter" in lines[0]
        # balance_starting_voltage's table address 0x1042 denotes byte 0x42 to the
        # firmware, which belongs to a different parameter entirely.
        assert "balance_starting_voltage" in body
        # Only the parameter at byte 0 is unaffected, because 0 halves to itself.
        assert body.count("<-- same") == 1


# -- Perturbation planning -----------------------------------------------------------


class TestPlanPerturbation:
    def test_moves_up_by_one_step(self) -> None:
        reg = find_register("cell_request_charge_voltage")
        assert reg is not None
        plan = plan_perturbation(reg, 3.40)
        assert plan is not None
        assert plan.value == pytest.approx(3.41)
        assert plan.words == encode_value_to_words(reg, 3.41)

    def test_moves_down_when_up_would_exceed_max(self) -> None:
        reg = find_register("cell_request_charge_voltage")
        assert reg is not None
        plan = plan_perturbation(reg, reg.max_value)
        assert plan is not None
        assert plan.value < reg.max_value

    def test_flips_a_boolean(self) -> None:
        reg = find_register("charging_switch")
        assert reg is not None
        plan = plan_perturbation(reg, True)
        assert plan is not None
        assert plan.value is False
        assert plan.words == [0, 0]

    def test_descending_prefers_the_lower_value(self) -> None:
        reg = find_register("cell_request_charge_voltage")
        assert reg is not None
        plan = plan_perturbation(reg, 3.40, descending=True)
        assert plan is not None
        assert plan.value == pytest.approx(3.39)

    def test_refuses_a_value_outside_the_declared_range(self) -> None:
        reg = find_register("cell_request_charge_voltage")
        assert reg is not None
        assert plan_perturbation(reg, 99.0) is None

    def test_widens_until_the_encoded_words_differ(self) -> None:
        """A step finer than the encoding must not produce a no-op write."""
        reg = RegisterDef(
            name="too_fine", address=0x1000, encoding=Encoding.U32_RAW,
            min_value=0, max_value=100, step=0.01, unit=None,
            tier=WriteTier.BASIC, description="step below encoding resolution",
        )
        plan = plan_perturbation(reg, 50.0)
        assert plan is not None
        assert plan.words != encode_value_to_words(reg, 50.0)

    def test_returns_none_when_no_multiple_fits(self) -> None:
        reg = RegisterDef(
            name="pinned", address=0x1000, encoding=Encoding.U32_RAW,
            min_value=5, max_value=5, step=1, unit=None,
            tier=WriteTier.BASIC, description="single legal value",
        )
        assert plan_perturbation(reg, 5.0) is None

    def test_every_real_parameter_is_perturbable_at_midrange(self) -> None:
        for reg in all_test_registers():
            assert plan_perturbation(reg, _mid(reg)) is not None, reg.name


# -- Diffing -------------------------------------------------------------------------


class TestDiffing:
    def test_diff_words_reports_index_and_byte(self) -> None:
        diffs = diff_words((1, 2, 3), (1, 9, 3))
        assert len(diffs) == 1
        assert diffs[0].index == 1
        assert diffs[0].byte_offset == 2
        assert "0x0002 -> 0x0009" in str(diffs[0])

    def test_diff_words_tolerates_short_reads(self) -> None:
        assert diff_words((1, 2, 3), (1, 2)) == []

    def test_side_effects_ignore_the_parameter_under_test(self) -> None:
        reg = find_register("smart_sleep_voltage")
        assert reg is not None
        before = (0, 0, 0, 0)
        after = (1, 1, 0, 0)  # both words of the target
        assert side_effects(before, after, reg) == []

    def test_side_effects_catch_collateral_damage(self) -> None:
        reg = find_register("smart_sleep_voltage")
        assert reg is not None
        before = (0, 0, 0, 0)
        after = (1, 1, 0, 7)
        strays = side_effects(before, after, reg)
        assert len(strays) == 1
        assert strays[0].index == 3


# -- Snapshots ------------------------------------------------------------------------


class TestSnapshot:
    async def test_round_trips_through_json(self) -> None:
        bms = seeded_bms()
        snap = await take_snapshot(bms, slave_id=1)
        restored = Snapshot.from_json(json.loads(json.dumps(snap.to_json())))
        assert restored.settings_words == snap.settings_words
        assert restored.packed_bit == snap.packed_bit
        assert restored.slave_id == 1

    async def test_decodes_every_seeded_parameter(self) -> None:
        bms = seeded_bms()
        snap = await take_snapshot(bms, slave_id=1)
        decoded = snap.decoded()
        for reg in all_test_registers():
            expected = _mid(reg)
            if reg.encoding is Encoding.BOOL32:
                assert decoded[reg.name] is expected
            else:
                assert decoded[reg.name] == pytest.approx(expected, abs=1e-3)

    async def test_packed_bits_decode_from_the_seeded_register(self) -> None:
        bms = seeded_bms()
        snap = await take_snapshot(bms, slave_id=1)
        decoded = snap.decoded()
        # 0x3200 = bits 9, 12, 13. smart_sleep (0x40) is off.
        assert decoded["smart_sleep_switch"] is False

    async def test_read_error_becomes_a_bus_error(self) -> None:
        class Rejecting(FakeBms):
            async def read_holding_registers(
                self, *, address: int, count: int, device_id: int
            ) -> FakeResponse:
                return FakeResponse(error=True)

        with pytest.raises(BusError, match="rejected"):
            await take_snapshot(Rejecting(), slave_id=1)

    async def test_read_exception_becomes_a_bus_error(self) -> None:
        class Dropping(FakeBms):
            async def read_holding_registers(
                self, *, address: int, count: int, device_id: int
            ) -> FakeResponse:
                raise ConnectionError("bus dropped")

        with pytest.raises(BusError, match="failed"):
            await take_snapshot(Dropping(), slave_id=1)

    def test_save_snapshot_writes_the_record(self, tmp_path: Path) -> None:
        snap = Snapshot(
            slave_id=3, taken_at="2026-01-01T00:00:00+00:00",
            settings_words=(1, 2, 3), packed_bit=0x40,
        )
        path = save_snapshot(snap, tmp_path, "stamp")
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["slave_id"] == 3
        assert data["settings_words_hex"] == ["0001", "0002", "0003"]


# -- The write/verify/restore cycle ---------------------------------------------------


class TestRegisterCycle:
    async def test_passes_against_byte_addressed_firmware(self) -> None:
        bms = seeded_bms()
        snap = await take_snapshot(bms, slave_id=1)
        reg = find_register("cell_request_charge_voltage")
        assert reg is not None
        result = await check_register(bms, reg, slave_id=1, baseline=snap)
        assert result.status is Status.PASS
        assert result.write_addr == write_address(reg)

    async def test_restores_the_original_words(self) -> None:
        bms = seeded_bms()
        snap = await take_snapshot(bms, slave_id=1)
        reg = find_register("cell_request_charge_voltage")
        assert reg is not None
        await check_register(bms, reg, slave_id=1, baseline=snap)
        after = await take_snapshot(bms, slave_id=1)
        assert after.settings_words == snap.settings_words
        assert after.packed_bit == snap.packed_bit

    async def test_detects_a_bms_that_ignores_writes(self) -> None:
        bms = seeded_bms(ignore_writes=True)
        snap = await take_snapshot(bms, slave_id=1)
        reg = find_register("cell_request_charge_voltage")
        assert reg is not None
        result = await check_register(bms, reg, slave_id=1, baseline=snap)
        assert result.status is Status.FAIL_NO_CHANGE

    async def test_reports_a_transient_side_effect(self) -> None:
        """A collateral change that settles back is reported, not raised.

        Byte 0xC0 is inside the snapshot window but past the last parameter
        (which ends at byte 0x87), so nothing else claims it.
        """
        bms = seeded_bms(transient_corrupt_at_byte=0xC0)
        snap = await take_snapshot(bms, slave_id=1)
        reg = find_register("cell_request_charge_voltage")
        assert reg is not None
        result = await check_register(bms, reg, slave_id=1, baseline=snap)
        assert result.status is Status.FAIL_SIDE_EFFECT
        assert "spec byte 0x0c0" in result.detail

    async def test_persistent_collateral_damage_aborts(self) -> None:
        """Damage that survives the restore must abort the run, not be reported."""
        bms = seeded_bms(corrupt_at_byte=0xC0)
        snap = await take_snapshot(bms, slave_id=1)
        reg = find_register("cell_request_charge_voltage")
        assert reg is not None
        with pytest.raises(BusError, match="RESTORE FAILED"):
            await check_register(bms, reg, slave_id=1, baseline=snap)

    async def test_restore_failure_raises(self) -> None:
        bms = seeded_bms(break_restore=True)
        snap = await take_snapshot(bms, slave_id=1)
        reg = find_register("cell_request_charge_voltage")
        assert reg is not None
        with pytest.raises(BusError, match="RESTORE FAILED"):
            await check_register(bms, reg, slave_id=1, baseline=snap)

    async def test_skips_a_parameter_it_cannot_perturb(self) -> None:
        bms = seeded_bms()
        snap = await take_snapshot(bms, slave_id=1)
        reg = find_register("cell_request_charge_voltage")
        assert reg is not None
        # Drive the stored value out of range so planning refuses.
        bms.poke(write_address(reg), encode_value_to_words(
            RegisterDef(
                name="x", address=reg.address, encoding=Encoding.U32_RAW,
                min_value=0, max_value=1_000_000, step=1, unit=None,
                tier=reg.tier, description="",
            ),
            999_999,
        ))
        snap = await take_snapshot(bms, slave_id=1)
        result = await check_register(bms, reg, slave_id=1, baseline=snap)
        assert result.status is Status.SKIPPED

    async def test_write_rejection_is_reported_not_raised(self) -> None:
        class Rejecting(FakeBms):
            async def write_registers(self, *, address: int, values: list[int], device_id: int):
                return FakeResponse(error=True)

        bms = Rejecting()
        for reg in all_test_registers():
            bms.poke(write_address(reg), encode_value_to_words(reg, _mid(reg)))
        bms.poke(PACKED_BIT_REGISTER, [0x3200])
        snap = await take_snapshot(bms, slave_id=1)
        reg = find_register("cell_request_charge_voltage")
        assert reg is not None
        result = await check_register(bms, reg, slave_id=1, baseline=snap)
        assert result.status is Status.FAIL_WRITE

    async def test_every_parameter_round_trips(self) -> None:
        """The full bank of parameters, end to end, against a faithful fake."""
        bms = seeded_bms()
        snap = await take_snapshot(bms, slave_id=1)
        for reg in all_test_registers():
            result = await check_register(bms, reg, slave_id=1, baseline=snap)
            assert result.status is Status.PASS, f"{reg.name}: {result.detail}"


class TestPackedBitCycle:
    async def test_flips_and_restores(self) -> None:
        bms = seeded_bms()
        snap = await take_snapshot(bms, slave_id=1)
        result = await check_packed_bit(bms, PACKED_BITS[0], slave_id=1, baseline=snap)
        assert result.status is Status.PASS
        after = await take_snapshot(bms, slave_id=1)
        assert after.packed_bit == snap.packed_bit

    async def test_preserves_sibling_bits(self) -> None:
        bms = seeded_bms()
        snap = await take_snapshot(bms, slave_id=1)
        await check_packed_bit(bms, PACKED_BITS[0], slave_id=1, baseline=snap)
        # 0x3200's bits 9/12/13 must be intact after flip + restore.
        after = await take_snapshot(bms, slave_id=1)
        assert after.packed_bit == 0x3200

    async def test_detects_a_bms_that_ignores_the_flip(self) -> None:
        bms = seeded_bms(ignore_writes=True)
        snap = await take_snapshot(bms, slave_id=1)
        result = await check_packed_bit(bms, PACKED_BITS[0], slave_id=1, baseline=snap)
        assert result.status is Status.FAIL_NO_CHANGE

    async def test_write_rejection_is_reported(self) -> None:
        class Rejecting(FakeBms):
            async def write_register(self, *, address: int, value: int, device_id: int):
                return FakeResponse(error=True)

        bms = Rejecting()
        bms.poke(PACKED_BIT_REGISTER, [0x3200])
        for reg in all_test_registers():
            bms.poke(write_address(reg), encode_value_to_words(reg, _mid(reg)))
        snap = await take_snapshot(bms, slave_id=1)
        result = await check_packed_bit(bms, PACKED_BITS[0], slave_id=1, baseline=snap)
        assert result.status is Status.FAIL_WRITE


# -- Firmware quirks observed on PB2A16S20P 15.41 -------------------------------------


class TestFirmwareQuirks:
    """Behaviours the BMS 1 sweep actually exhibited, reproduced as fakes.

    Both were real failures in the first full sweep: two currents rejected with
    illegal-data-value because they already sat at the firmware ceiling, and
    all three packed bits rejected with illegal-data-address under FC06.
    """

    async def test_retries_downward_when_the_value_hits_a_ceiling(self) -> None:
        """max_balance_current sat at 2.000 A on a 2 A balancer; up was refused."""

        class Ceilinged(FakeBms):
            ceilings: ClassVar[dict[int, int]] = {}

            async def write_registers(
                self, *, address: int, values: list[int], device_id: int
            ) -> FakeResponse:
                raw = (values[0] << 16) | values[1] if len(values) == 2 else values[0]
                if address in self.ceilings and raw > self.ceilings[address]:
                    return FakeResponse(error=True, exception_code=3)
                return await super().write_registers(
                    address=address, values=values, device_id=device_id
                )

        bms = Ceilinged()
        for reg in all_test_registers():
            bms.poke(write_address(reg), encode_value_to_words(reg, _mid(reg)))
        bms.poke(PACKED_BIT_REGISTER, [0x3200])
        target = find_register("max_balance_current")
        assert target is not None
        bms.poke(write_address(target), encode_value_to_words(target, 2.0))
        bms.ceilings = {write_address(target): 2000}

        snap = await take_snapshot(bms, slave_id=1)
        result = await check_register(bms, target, slave_id=1, baseline=snap)
        assert result.status is Status.PASS
        assert result.written == pytest.approx(1.999)

    async def test_reports_failure_when_both_directions_are_refused(self) -> None:
        class AlwaysIllegalValue(FakeBms):
            async def write_registers(
                self, *, address: int, values: list[int], device_id: int
            ) -> FakeResponse:
                return FakeResponse(error=True, exception_code=3)

        bms = AlwaysIllegalValue()
        for reg in all_test_registers():
            bms.poke(write_address(reg), encode_value_to_words(reg, _mid(reg)))
        bms.poke(PACKED_BIT_REGISTER, [0x3200])
        target = find_register("max_balance_current")
        assert target is not None
        snap = await take_snapshot(bms, slave_id=1)
        result = await check_register(bms, target, slave_id=1, baseline=snap)
        assert result.status is Status.FAIL_WRITE
        assert "both directions rejected" in result.detail

    async def test_packed_bit_falls_back_to_fc16_when_fc06_is_unsupported(self) -> None:
        """FC03 reads 0x1114 fine but FC06 returns illegal-data-address."""

        class NoFc06(FakeBms):
            async def write_register(
                self, *, address: int, value: int, device_id: int
            ) -> FakeResponse:
                return FakeResponse(error=True, exception_code=2)

        bms = NoFc06()
        for reg in all_test_registers():
            bms.poke(write_address(reg), encode_value_to_words(reg, _mid(reg)))
        bms.poke(PACKED_BIT_REGISTER, [0x3200])
        snap = await take_snapshot(bms, slave_id=1)
        result = await check_packed_bit(bms, PACKED_BITS[0], slave_id=1, baseline=snap)
        assert result.status is Status.PASS
        assert "via FC16" in result.detail
        after = await take_snapshot(bms, slave_id=1)
        assert after.packed_bit == 0x3200

    async def test_packed_bit_does_not_fall_back_on_other_exceptions(self) -> None:
        """Only illegal-data-address justifies the FC16 retry."""

        class RefusesValue(FakeBms):
            async def write_register(
                self, *, address: int, value: int, device_id: int
            ) -> FakeResponse:
                return FakeResponse(error=True, exception_code=3)

        bms = RefusesValue()
        for reg in all_test_registers():
            bms.poke(write_address(reg), encode_value_to_words(reg, _mid(reg)))
        bms.poke(PACKED_BIT_REGISTER, [0x3200])
        snap = await take_snapshot(bms, slave_id=1)
        result = await check_packed_bit(bms, PACKED_BITS[0], slave_id=1, baseline=snap)
        assert result.status is Status.FAIL_WRITE

    async def test_packed_bit_records_fc06_when_it_works(self) -> None:
        bms = seeded_bms()
        snap = await take_snapshot(bms, slave_id=1)
        result = await check_packed_bit(bms, PACKED_BITS[0], slave_id=1, baseline=snap)
        assert result.status is Status.PASS
        assert "via FC06" in result.detail


# -- Selection and summary ------------------------------------------------------------


class TestSelection:
    def test_structural_parameters_are_excluded_by_default(self) -> None:
        names = {r.name for r in selected_registers(only=None, skip=set(), include_structural=False)}
        assert "cell_count" not in names
        assert "pack_capacity_setting" not in names

    def test_structural_parameters_can_be_opted_in(self) -> None:
        names = {r.name for r in selected_registers(only=None, skip=set(), include_structural=True)}
        assert "cell_count" in names

    def test_only_filter(self) -> None:
        regs = selected_registers(
            only={"max_charge_current"}, skip=set(), include_structural=False
        )
        assert [r.name for r in regs] == ["max_charge_current"]

    def test_skip_filter(self) -> None:
        regs = selected_registers(
            only=None, skip={"max_charge_current"}, include_structural=False
        )
        assert "max_charge_current" not in {r.name for r in regs}

    def test_switches_are_ordered_last(self) -> None:
        names = [r.name for r in all_test_registers()]
        switch_positions = [names.index(n) for n in
                            ("charging_switch", "discharging_switch", "balance_switch")]
        non_switch = [i for i, n in enumerate(names)
                      if n not in ("charging_switch", "discharging_switch", "balance_switch")]
        assert min(switch_positions) > max(non_switch)

    def test_bit_selection(self) -> None:
        assert selected_bits(only={"smart_sleep_switch"}, skip=set())[0].name == "smart_sleep_switch"
        assert selected_bits(only=None, skip={"smart_sleep_switch"}) == tuple(
            b for b in PACKED_BITS if b.name != "smart_sleep_switch"
        )


class TestSummary:
    def test_clean_run_exits_zero(self) -> None:
        report = PackReport(slave_id=1)
        report.results = [
            FieldResultStub("a", Status.PASS),
            FieldResultStub("b", Status.SKIPPED),
        ]  # type: ignore[assignment]
        lines, code = summarise([report])
        assert code == 0
        assert "ALL GOOD" in "\n".join(lines)

    def test_failure_exits_nonzero(self) -> None:
        report = PackReport(slave_id=1)
        report.results = [FieldResultStub("a", Status.FAIL_SIDE_EFFECT)]  # type: ignore[assignment]
        lines, code = summarise([report])
        assert code == 1
        assert "fail_side_effect" in "\n".join(lines)

    def test_abort_is_reported(self) -> None:
        report = PackReport(slave_id=2, aborted="restore failed")
        lines, code = summarise([report])
        assert code == 1
        assert "ABORTED" in "\n".join(lines)

    def test_read_only_run_is_summarised(self) -> None:
        report = PackReport(
            slave_id=1,
            baseline=Snapshot(slave_id=1, taken_at="t", settings_words=(0,) * 8, packed_bit=0),
        )
        lines, code = summarise([report])
        assert code == 0
        assert "read-only" in "\n".join(lines)


class FieldResultStub:
    """Minimal stand-in for FieldResult in summary tests."""

    def __init__(self, parameter: str, status: Status) -> None:
        self.parameter = parameter
        self.status = status
        self.detail = "stub"

    @property
    def ok(self) -> bool:
        return self.status in (Status.PASS, Status.SKIPPED)
