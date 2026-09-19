"""Hardware test bench — read/write/restore every setting against a real BMS.

Run with the add-on STOPPED. Nothing else may hold the RS485 bus.

WHY THIS EXISTS
---------------
Issue #32: the ``address`` field in ``jk_settings.RegisterDef`` is a *word
index* (``0x1000 + spec_byte/2``), but ``write_executor`` passes it to
``write_registers()`` as a literal Modbus address. The firmware interprets a
start address as ``block_base + spec_byte_offset``, so every numeric write
currently lands at half the intended offset.

Evidence for the byte rule (all primary, none assumed):

* ``scripts/captures/BMS_1_sweep.txt``: ``SETTINGS_B`` requested at 0x1078
  returns different data than the same region of ``SETTINGS_A``; ``INFO_B``
  requested at 0x1478 returns the bytes at offset 0x78 of the 0x1400 window
  (the ``4MXr`` marker), i.e. start = base + byte offset.
* jean-luc1203/jkbms-rs485-addon ``flows.json``, node ``function 4``: FC06 to
  ``0x1504`` for RCVTime/RFVTime, whose spec byte offset in the 0x1400 block
  is 0x104. ``0x1400 + 0x104 = 0x1504``.
* ``docs/FIELD_MATRIX.md``: phinix writes ``0x1070``/``0x1074``/``0x1078`` for
  BatChargeEN/BatDisChargeEN/BalanEN, spec bytes 0x70/0x74/0x78.

Reads are unaffected: ``decode_register_value`` slices a bulk read that starts
at byte 0, and its word index happens to land on the right byte. Only writes
are wrong. The packed-bit register is already byte-direct
(``0x1114 = 0x1000 + 0x114``), so bit flips are believed correct today — the
bench checks that claim rather than assuming it.

This bench NEVER writes at the un-corrected address. That address lands on an
unidentified register, and corrupting an unknown setting is not an acceptable
cost of proving a bug we can prove arithmetically (see ``--collision-report``).

WHAT IT DOES
------------
Per parameter, in order (numerics first, MOSFET switches last):

1. Snapshot the whole settings window + the packed-bit register.
2. Perturb the parameter by ONE declared step (its own ``step`` field — the
   smallest increment the parameter admits), or flip it if it is a boolean.
3. Re-snapshot. Assert the target's words changed to exactly the encoded
   value, and that NO other word in the window changed (side-effect check).
4. Restore the captured raw words verbatim.
5. Re-snapshot. Assert the window is byte-identical to the baseline.

A failed restore aborts the entire run immediately and loudly.

USAGE
-----
Always start read-only, one pack::

    .venv/bin/python -m scripts.hw_testbench --gateway 192.168.8.153 --slave-id 1

Then writes on one pack::

    .venv/bin/python -m scripts.hw_testbench --gateway 192.168.8.153 --slave-id 1 \
        --write --confirm-writes

Then the whole bank::

    .venv/bin/python -m scripts.hw_testbench --gateway 192.168.8.153 \
        --slave-ids 1,2,3,4,5,6 --write --confirm-writes

Emergency restore from a snapshot file::

    .venv/bin/python -m scripts.hw_testbench --gateway 192.168.8.153 \
        --restore-from scripts/captures/hw_snapshots/<file>.json --confirm-writes
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum, unique
from pathlib import Path
from typing import Any, Protocol

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.framer import FramerType

from jkbms2mqtt.protocol.jk_modbus import BASE_RT, RT_BLOCK_WORDS, decode_realtime
from jkbms2mqtt.protocol.jk_settings import (
    BASIC_REGISTERS,
    PACKED_BIT_REGISTER,
    PACKED_BITS,
    SAFETY_REGISTERS,
    SETTINGS_BLOCK_BASE,
    EncodeError,
    Encoding,
    PackedBitDef,
    RegisterDef,
    decode_packed_bit_value,
    decode_register_value,
    encode_packed_bit_value,
    encode_value_to_words,
)

# -- Constants -------------------------------------------------------------------------

# Words to pull for a snapshot. 120 is the largest safe FC03 count (protocol cap
# is 125) and covers spec bytes 0x00..0xEF — every settings parameter (which end
# at byte 0x87) plus a margin of unmapped space, so a stray write landing just
# past the table is still caught by the side-effect check.
SNAPSHOT_WORDS = 120

# Real-time read used only by the safety interlock.
RT_PROBE_WORDS = 120

# The interlock refuses to write if more than this flows through the pack.
# The user's stated condition is "nothing connected"; this enforces it.
MAX_SAFE_CURRENT_A = 1.0

# Structural parameters. Changing these reconfigures the pack (cell count) or
# the SoC model (capacity) even transiently. Excluded from write tests unless
# the operator opts in explicitly.
STRUCTURAL_PARAMETERS = frozenset({"cell_count", "pack_capacity_setting"})

# MOSFET switches. Safe to flip with nothing connected, but tested last so a
# failure cannot leave a pack mid-run with an output disabled.
SWITCH_PARAMETERS = frozenset({"charging_switch", "discharging_switch", "balance_switch"})

# How many step multiples to try before declaring a parameter unperturbable.
MAX_STEP_MULTIPLE = 10

# Modbus exception codes we react to specifically.
#   2 — illegal data address: the register cannot be written this way. Observed
#       on PB2A16S20P for FC06 against 0x1114, which FC03 reads happily; the
#       firmware appears to implement FC16 only for writes.
#   3 — illegal data value: the address was fine, the value was not. Observed
#       where a parameter already sits at its firmware ceiling (max_balance_
#       current at 2.000 A on a "PB2A" = 2 A balancer) and we perturbed upward.
ILLEGAL_DATA_ADDRESS = 2
ILLEGAL_DATA_VALUE = 3

# Smallest change each wire encoding can represent. A parameter whose declared
# `step` is finer than this would otherwise encode to the same words it already
# holds, making a successful write indistinguishable from the BMS ignoring us.
ENCODING_RESOLUTION = {
    Encoding.U32_RAW: 1.0,
    Encoding.U32_MILLI: 0.001,
    Encoding.U32_DECI: 0.1,
    Encoding.I32_DECI: 0.1,
}


def encoding_resolution(reg: RegisterDef) -> float:
    """The smallest value change *reg*'s encoding can actually carry."""
    return ENCODING_RESOLUTION.get(reg.encoding, 1.0)


@unique
class Status(str, Enum):
    """Outcome of one parameter's test."""

    PASS = "pass"
    FAIL_NO_CHANGE = "fail_no_change"
    FAIL_WRONG_VALUE = "fail_wrong_value"
    FAIL_SIDE_EFFECT = "fail_side_effect"
    FAIL_RESTORE = "fail_restore"
    FAIL_WRITE = "fail_write"
    SKIPPED = "skipped"


# -- Addressing ------------------------------------------------------------------------


def word_index(reg: RegisterDef) -> int:
    """Index of *reg* within a settings snapshot (a bulk read from byte 0)."""
    return reg.address - SETTINGS_BLOCK_BASE


def spec_byte(reg: RegisterDef) -> int:
    """The parameter's byte offset as printed in the V1.1 spec's index column."""
    return 2 * word_index(reg)


def write_address(reg: RegisterDef) -> int:
    """The Modbus start address the firmware actually wants for a write.

    ``SETTINGS_BLOCK_BASE + spec_byte``. See the module docstring for the
    three independent sources behind this rule.
    """
    return SETTINGS_BLOCK_BASE + spec_byte(reg)


def all_test_registers() -> tuple[RegisterDef, ...]:
    """Every writable numeric parameter, ordered least-invasive first."""
    regs = [*BASIC_REGISTERS, *SAFETY_REGISTERS]
    return tuple(sorted(regs, key=lambda r: (r.name in SWITCH_PARAMETERS, r.address)))


def collision_report() -> list[str]:
    """Explain, without touching hardware, where un-corrected writes land.

    For each parameter: the address the table holds (what the buggy code puts
    on the wire), the byte offset that address denotes to the firmware, and
    which parameter — if any — actually owns that byte.
    """
    owner_by_byte: dict[int, str] = {}
    for reg in all_test_registers():
        # A u32 parameter owns four bytes.
        for offset in range(4):
            owner_by_byte[spec_byte(reg) + offset] = reg.name

    lines = [
        f"{'parameter':<50s} {'table':<6s} {'wire byte':>9s} {'correct':<7s} lands on",
        "-" * 108,
    ]
    for reg in all_test_registers():
        stray_byte = reg.address - SETTINGS_BLOCK_BASE
        victim = owner_by_byte.get(stray_byte, "(unmapped)")
        marker = "  <-- same" if victim == reg.name else ""
        lines.append(
            f"{reg.name:<50s} {reg.address:#06x} {stray_byte:>9d} "
            f"{write_address(reg):#06x}  {victim}{marker}"
        )
    return lines


# -- Client protocol -------------------------------------------------------------------


class ModbusLike(Protocol):
    """The three pymodbus calls this bench uses (kwargs-only, as pymodbus 3.x)."""

    async def read_holding_registers(
        self, *, address: int, count: int, device_id: int
    ) -> Any: ...

    async def write_registers(
        self, *, address: int, values: list[int], device_id: int
    ) -> Any: ...

    async def write_register(self, *, address: int, value: int, device_id: int) -> Any: ...


class BusError(RuntimeError):
    """A Modbus read or write failed or was rejected by the BMS.

    ``exception_code`` carries the Modbus exception when the BMS returned one,
    so callers can tell "wrong address" (2) from "wrong value" (3) instead of
    string-matching the response.
    """

    def __init__(self, message: str, *, exception_code: int | None = None) -> None:
        super().__init__(message)
        self.exception_code = exception_code


# -- Snapshots -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Snapshot:
    """One complete capture of a pack's writable state."""

    slave_id: int
    taken_at: str
    settings_words: tuple[int, ...]
    packed_bit: int

    def decoded(self) -> dict[str, float | bool]:
        """Every parameter decoded, for the human-readable safety record."""
        out: dict[str, float | bool] = {}
        buf = list(self.settings_words)
        for reg in all_test_registers():
            try:
                out[reg.name] = decode_register_value(reg, buf)
            except EncodeError:  # pragma: no cover - snapshot is always wide enough
                continue
        for bit in PACKED_BITS:
            out[bit.name] = decode_packed_bit_value(bit, self.packed_bit)
        return out

    def to_json(self) -> dict[str, Any]:
        return {
            "slave_id": self.slave_id,
            "taken_at": self.taken_at,
            "settings_base": SETTINGS_BLOCK_BASE,
            "settings_words_hex": [f"{w:04x}" for w in self.settings_words],
            "packed_bit_register": PACKED_BIT_REGISTER,
            "packed_bit_hex": f"{self.packed_bit:04x}",
            "decoded": self.decoded(),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Snapshot:
        return cls(
            slave_id=int(data["slave_id"]),
            taken_at=str(data["taken_at"]),
            settings_words=tuple(int(w, 16) for w in data["settings_words_hex"]),
            packed_bit=int(data["packed_bit_hex"], 16),
        )


@dataclass(frozen=True, slots=True)
class WordDiff:
    """One 16-bit word that changed between two snapshots."""

    index: int
    byte_offset: int
    before: int
    after: int

    def __str__(self) -> str:
        return (
            f"word[{self.index}] (spec byte 0x{self.byte_offset:03x}): "
            f"0x{self.before:04x} -> 0x{self.after:04x}"
        )


def diff_words(before: tuple[int, ...], after: tuple[int, ...]) -> list[WordDiff]:
    """Every differing word. Lengths may differ if a read was short."""
    out: list[WordDiff] = []
    for i in range(min(len(before), len(after))):
        if before[i] != after[i]:
            out.append(WordDiff(index=i, byte_offset=2 * i, before=before[i], after=after[i]))
    return out


def owned_indices(reg: RegisterDef) -> frozenset[int]:
    """The word indices a parameter legitimately occupies (u32 = two words)."""
    base = word_index(reg)
    return frozenset({base, base + 1})


def side_effects(
    before: tuple[int, ...], after: tuple[int, ...], reg: RegisterDef
) -> list[WordDiff]:
    """Word changes outside the parameter under test."""
    allowed = owned_indices(reg)
    return [d for d in diff_words(before, after) if d.index not in allowed]


# -- Perturbation planning -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Perturbation:
    """A planned change: the new value and the words it must produce."""

    value: float | bool
    words: list[int]


def plan_perturbation(
    reg: RegisterDef, current: float | bool, *, descending: bool = False
) -> Perturbation | None:
    """Pick the smallest safe change to *reg*, or None if none is possible.

    Booleans flip. Numerics move by one declared ``step``, preferring upward;
    if that would exceed ``max_value`` we go down instead. Where ``step`` is
    finer than the wire encoding can carry, the encoding's own resolution is
    used instead — otherwise a "successful" write would be indistinguishable
    from the BMS ignoring us. Every parameter in the real table already has a
    step at or above its resolution, so this only guards synthetic cases.

    Returns None when the current value sits outside the declared range (we do
    not touch a parameter we cannot reason about) or when no multiple of the
    step stays in range.
    """
    if reg.encoding is Encoding.BOOL32:
        new_bool = not bool(current)
        return Perturbation(value=new_bool, words=encode_value_to_words(reg, new_bool))

    numeric = float(current)
    if not reg.min_value <= numeric <= reg.max_value:
        return None

    try:
        current_words = encode_value_to_words(reg, numeric)
    except EncodeError:  # pragma: no cover - guarded by the range check above
        return None

    base_delta = max(reg.step, encoding_resolution(reg))
    for multiple in range(1, MAX_STEP_MULTIPLE + 1):
        delta = base_delta * multiple
        ordered = (
            (numeric - delta, numeric + delta)
            if descending
            else (numeric + delta, numeric - delta)
        )
        for candidate in ordered:
            rounded = round(candidate, 6)
            if not reg.min_value <= rounded <= reg.max_value:
                continue
            try:
                words = encode_value_to_words(reg, rounded)
            except EncodeError:  # pragma: no cover - range already checked
                continue
            if words != current_words:
                return Perturbation(value=rounded, words=words)
    return None


# -- Results ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FieldResult:
    """Outcome of testing one parameter."""

    parameter: str
    status: Status
    detail: str
    original: float | bool | None = None
    written: float | bool | None = None
    observed: float | bool | None = None
    write_addr: int | None = None

    @property
    def ok(self) -> bool:
        return self.status in (Status.PASS, Status.SKIPPED)

    def to_json(self) -> dict[str, Any]:
        return {
            "parameter": self.parameter,
            "status": self.status.value,
            "detail": self.detail,
            "original": self.original,
            "written": self.written,
            "observed": self.observed,
            "write_addr": None if self.write_addr is None else f"{self.write_addr:#06x}",
        }


@dataclass
class PackReport:
    """Everything the bench learned about one pack."""

    slave_id: int
    baseline: Snapshot | None = None
    results: list[FieldResult] = field(default_factory=list)
    aborted: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "slave_id": self.slave_id,
            "baseline": None if self.baseline is None else self.baseline.to_json(),
            "results": [r.to_json() for r in self.results],
            "aborted": self.aborted,
        }


# -- Bus helpers -----------------------------------------------------------------------


async def read_words(client: ModbusLike, *, address: int, count: int, slave_id: int) -> list[int]:
    """FC03 with errors raised rather than returned."""
    try:
        resp = await client.read_holding_registers(
            address=address, count=count, device_id=slave_id
        )
    except Exception as exc:
        # Any transport failure is surfaced identically; the caller aborts.
        raise BusError(f"read {address:#06x}+{count} failed: {exc}") from exc
    if resp.isError():
        raise BusError(
            f"read {address:#06x}+{count} rejected: {resp}",
            exception_code=getattr(resp, "exception_code", None),
        )
    return list(resp.registers)


async def write_words(
    client: ModbusLike, *, address: int, values: list[int], slave_id: int
) -> None:
    """FC16 with errors raised rather than returned."""
    try:
        resp = await client.write_registers(address=address, values=values, device_id=slave_id)
    except Exception as exc:
        raise BusError(f"write {address:#06x} failed: {exc}") from exc
    if resp.isError():
        raise BusError(
            f"write {address:#06x} rejected: {resp}",
            exception_code=getattr(resp, "exception_code", None),
        )


async def write_single(client: ModbusLike, *, address: int, value: int, slave_id: int) -> None:
    """FC06 with errors raised rather than returned."""
    try:
        resp = await client.write_register(address=address, value=value, device_id=slave_id)
    except Exception as exc:
        raise BusError(f"write {address:#06x} failed: {exc}") from exc
    if resp.isError():
        raise BusError(
            f"write {address:#06x} rejected: {resp}",
            exception_code=getattr(resp, "exception_code", None),
        )


async def write_one_register(
    client: ModbusLike, *, address: int, value: int, slave_id: int
) -> str:
    """Write a single register, falling back from FC06 to FC16.

    PB2A16S20P firmware 15.41 rejects FC06 against 0x1114 with illegal-data-
    address while FC03 reads that same register fine, and every FC16 write in
    the BMS 1 sweep succeeded. So a single-register write is retried as a
    one-register FC16 write before being called a failure.

    Returns the function code that succeeded, for the record.
    """
    try:
        await write_single(client, address=address, value=value, slave_id=slave_id)
    except BusError as exc:
        if exc.exception_code != ILLEGAL_DATA_ADDRESS:
            raise
        await write_words(client, address=address, values=[value], slave_id=slave_id)
        return "FC16"
    return "FC06"


async def take_snapshot(client: ModbusLike, *, slave_id: int) -> Snapshot:
    """Capture the settings window and the packed-bit register."""
    words = await read_words(
        client, address=SETTINGS_BLOCK_BASE, count=SNAPSHOT_WORDS, slave_id=slave_id
    )
    packed = await read_words(client, address=PACKED_BIT_REGISTER, count=1, slave_id=slave_id)
    return Snapshot(
        slave_id=slave_id,
        taken_at=datetime.now(UTC).isoformat(timespec="seconds"),
        settings_words=tuple(words),
        packed_bit=packed[0],
    )


async def measured_current(client: ModbusLike, *, slave_id: int) -> float:
    """Pack current from the real-time block, for the write interlock."""
    words = await read_words(client, address=BASE_RT, count=RT_PROBE_WORDS, slave_id=slave_id)
    buf = [0] * RT_BLOCK_WORDS
    for i, value in enumerate(words[:RT_BLOCK_WORDS]):
        buf[i] = value
    return float(decode_realtime(buf).total_current_a)


# -- The test itself -------------------------------------------------------------------


async def check_register(
    client: ModbusLike, reg: RegisterDef, *, slave_id: int, baseline: Snapshot
) -> FieldResult:
    """Perturb one numeric parameter, verify, restore, verify the restore.

    Raises BusError if the restore itself fails — the caller must abort.
    """
    current = decode_register_value(reg, list(baseline.settings_words))
    addr = write_address(reg)
    original_words = list(baseline.settings_words[word_index(reg) : word_index(reg) + 2])

    plan = plan_perturbation(reg, current)
    if plan is None:
        return FieldResult(
            parameter=reg.name,
            status=Status.SKIPPED,
            detail=f"no in-range perturbation (current={current}, "
            f"range=[{reg.min_value}, {reg.max_value}])",
            original=current,
        )

    try:
        await write_words(client, address=addr, values=plan.words, slave_id=slave_id)
    except BusError as exc:
        # Illegal-data-value means the address was accepted and the value was
        # not — typically the parameter already sits at its firmware ceiling
        # and we perturbed upward. Try the other direction before giving up,
        # so a ceiling is not misreported as a broken address.
        retry = (
            plan_perturbation(reg, current, descending=True)
            if exc.exception_code == ILLEGAL_DATA_VALUE
            else None
        )
        if retry is None or retry.words == plan.words:
            return FieldResult(
                parameter=reg.name,
                status=Status.FAIL_WRITE,
                detail=str(exc),
                original=current,
                written=plan.value,
                write_addr=addr,
            )
        first_value = plan.value
        plan = retry
        try:
            await write_words(client, address=addr, values=plan.words, slave_id=slave_id)
        except BusError as retry_exc:
            return FieldResult(
                parameter=reg.name,
                status=Status.FAIL_WRITE,
                detail=f"both directions rejected: up={first_value} ({exc}); "
                f"down={plan.value} ({retry_exc})",
                original=current,
                written=plan.value,
                write_addr=addr,
            )

    after = await take_snapshot(client, slave_id=slave_id)
    observed = decode_register_value(reg, list(after.settings_words))
    actual_words = list(after.settings_words[word_index(reg) : word_index(reg) + 2])
    strays = side_effects(baseline.settings_words, after.settings_words, reg)
    bit_changed = after.packed_bit != baseline.packed_bit

    # Restore before judging, so a verdict never leaves the pack modified.
    await write_words(client, address=addr, values=original_words, slave_id=slave_id)
    restored = await take_snapshot(client, slave_id=slave_id)
    if restored.settings_words != baseline.settings_words or (
        restored.packed_bit != baseline.packed_bit
    ):
        drift = diff_words(baseline.settings_words, restored.settings_words)
        raise BusError(
            f"RESTORE FAILED for {reg.name}: pack not back at baseline. "
            f"diffs={[str(d) for d in drift]} "
            f"packed_bit 0x{baseline.packed_bit:04x} -> 0x{restored.packed_bit:04x}"
        )

    if actual_words == original_words:
        return FieldResult(
            parameter=reg.name,
            status=Status.FAIL_NO_CHANGE,
            detail=f"wrote {plan.words} to {addr:#06x}; words unchanged — BMS ignored it",
            original=current,
            written=plan.value,
            observed=observed,
            write_addr=addr,
        )
    if strays or bit_changed:
        notes = [str(d) for d in strays]
        if bit_changed:
            notes.append(
                f"packed bit 0x{baseline.packed_bit:04x} -> 0x{after.packed_bit:04x}"
            )
        return FieldResult(
            parameter=reg.name,
            status=Status.FAIL_SIDE_EFFECT,
            detail=f"collateral change: {notes}",
            original=current,
            written=plan.value,
            observed=observed,
            write_addr=addr,
        )
    if actual_words != plan.words:
        return FieldResult(
            parameter=reg.name,
            status=Status.FAIL_WRONG_VALUE,
            detail=f"expected words {plan.words}, read back {actual_words} "
            f"(BMS may clamp or round)",
            original=current,
            written=plan.value,
            observed=observed,
            write_addr=addr,
        )
    return FieldResult(
        parameter=reg.name,
        status=Status.PASS,
        detail=f"{current} -> {plan.value} -> restored",
        original=current,
        written=plan.value,
        observed=observed,
        write_addr=addr,
    )


async def check_packed_bit(
    client: ModbusLike, bit: PackedBitDef, *, slave_id: int, baseline: Snapshot
) -> FieldResult:
    """Flip one packed bit via read-modify-write, verify, restore.

    Raises BusError if the restore fails.
    """
    current = decode_packed_bit_value(bit, baseline.packed_bit)
    desired = not current
    new_value = encode_packed_bit_value(
        bit, desired_on=desired, current_register_value=baseline.packed_bit
    )

    try:
        function_code = await write_one_register(
            client, address=bit.register, value=new_value, slave_id=slave_id
        )
    except BusError as exc:
        return FieldResult(
            parameter=bit.name,
            status=Status.FAIL_WRITE,
            detail=str(exc),
            original=current,
            written=desired,
            write_addr=bit.register,
        )

    after = await take_snapshot(client, slave_id=slave_id)
    observed = decode_packed_bit_value(bit, after.packed_bit)
    settings_drift = diff_words(baseline.settings_words, after.settings_words)
    # Every bit except ours must survive the read-modify-write.
    other_bits_before = baseline.packed_bit & ~bit.bit_mask & 0xFFFF
    other_bits_after = after.packed_bit & ~bit.bit_mask & 0xFFFF

    await write_one_register(
        client, address=bit.register, value=baseline.packed_bit, slave_id=slave_id
    )
    restored = await take_snapshot(client, slave_id=slave_id)
    if restored.packed_bit != baseline.packed_bit or (
        restored.settings_words != baseline.settings_words
    ):
        raise BusError(
            f"RESTORE FAILED for {bit.name}: packed bit "
            f"0x{baseline.packed_bit:04x} -> 0x{restored.packed_bit:04x}"
        )

    if after.packed_bit == baseline.packed_bit:
        return FieldResult(
            parameter=bit.name,
            status=Status.FAIL_NO_CHANGE,
            detail=f"wrote 0x{new_value:04x} to {bit.register:#06x}; register unchanged",
            original=current,
            written=desired,
            observed=observed,
            write_addr=bit.register,
        )
    if settings_drift or other_bits_before != other_bits_after:
        notes = [str(d) for d in settings_drift]
        if other_bits_before != other_bits_after:
            notes.append(
                f"sibling bits 0x{other_bits_before:04x} -> 0x{other_bits_after:04x}"
            )
        return FieldResult(
            parameter=bit.name,
            status=Status.FAIL_SIDE_EFFECT,
            detail=f"collateral change: {notes}",
            original=current,
            written=desired,
            observed=observed,
            write_addr=bit.register,
        )
    if observed is not desired:
        return FieldResult(
            parameter=bit.name,
            status=Status.FAIL_WRONG_VALUE,
            detail=f"expected {desired}, read back {observed}",
            original=current,
            written=desired,
            observed=observed,
            write_addr=bit.register,
        )
    return FieldResult(
        parameter=bit.name,
        status=Status.PASS,
        detail=f"{current} -> {desired} -> restored (via {function_code})",
        original=current,
        written=desired,
        observed=observed,
        write_addr=bit.register,
    )


def selected_registers(
    *, only: set[str] | None, skip: set[str], include_structural: bool
) -> tuple[RegisterDef, ...]:
    """Apply the operator's filters to the ordered register list."""
    out: list[RegisterDef] = []
    for reg in all_test_registers():
        if only is not None and reg.name not in only:
            continue
        if reg.name in skip:
            continue
        if reg.name in STRUCTURAL_PARAMETERS and not include_structural:
            continue
        out.append(reg)
    return tuple(out)


def selected_bits(*, only: set[str] | None, skip: set[str]) -> tuple[PackedBitDef, ...]:
    return tuple(
        b
        for b in PACKED_BITS
        if (only is None or b.name in only) and b.name not in skip
    )


# -- Orchestration ---------------------------------------------------------------------


def snapshot_path(directory: Path, slave_id: int, stamp: str) -> Path:
    return directory / f"BMS_{slave_id}_settings_{stamp}.json"


def save_snapshot(snapshot: Snapshot, directory: Path, stamp: str) -> Path:
    """Persist the safety record. Written before any write is attempted."""
    directory.mkdir(parents=True, exist_ok=True)
    path = snapshot_path(directory, snapshot.slave_id, stamp)
    path.write_text(json.dumps(snapshot.to_json(), indent=2, sort_keys=True))
    return path


async def run_pack(
    client: ModbusLike,
    slave_id: int,
    *,
    do_writes: bool,
    registers: tuple[RegisterDef, ...],
    bits: tuple[PackedBitDef, ...],
    snapshot_dir: Path,
    stamp: str,
    force: bool,
) -> PackReport:
    """Snapshot, optionally test every selected parameter, report."""
    report = PackReport(slave_id=slave_id)
    print(f"\n{'=' * 78}\nBMS {slave_id}\n{'=' * 78}")

    baseline = await take_snapshot(client, slave_id=slave_id)
    report.baseline = baseline
    path = save_snapshot(baseline, snapshot_dir, stamp)
    print(f"  baseline recorded: {path}")
    print(f"  packed-bit 0x{PACKED_BIT_REGISTER:04x} = 0x{baseline.packed_bit:04x}")
    for name, value in baseline.decoded().items():
        print(f"    {name:<50s} = {value}")

    if not do_writes:
        print("  read-only mode — no writes attempted")
        return report

    current = await measured_current(client, slave_id=slave_id)
    print(f"  pack current = {current:.3f} A")
    if abs(current) > MAX_SAFE_CURRENT_A and not force:
        report.aborted = (
            f"interlock: {current:.3f} A flowing (limit {MAX_SAFE_CURRENT_A} A). "
            "Disconnect loads/chargers, or pass --force if this reading is wrong."
        )
        print(f"  ABORT — {report.aborted}")
        return report

    for reg in registers:
        try:
            result = await check_register(client, reg, slave_id=slave_id, baseline=baseline)
        except BusError as exc:
            report.aborted = str(exc)
            print(f"  !! {exc}")
            print("  !! ABORTING — pack may be modified. Restore from the snapshot above.")
            return report
        report.results.append(result)
        print(f"  [{result.status.value:<18s}] {result.parameter:<50s} {result.detail}")

    for bit in bits:
        try:
            result = await check_packed_bit(client, bit, slave_id=slave_id, baseline=baseline)
        except BusError as exc:
            report.aborted = str(exc)
            print(f"  !! {exc}")
            print("  !! ABORTING — pack may be modified. Restore from the snapshot above.")
            return report
        report.results.append(result)
        print(f"  [{result.status.value:<18s}] {result.parameter:<50s} {result.detail}")

    return report


async def restore_from_file(client: ModbusLike, path: Path) -> int:
    """Write a saved snapshot's settings back, word pair by word pair."""
    raw = await asyncio.to_thread(path.read_text)
    snapshot = Snapshot.from_json(json.loads(raw))
    print(f"Restoring BMS {snapshot.slave_id} from {path} (captured {snapshot.taken_at})")
    for reg in all_test_registers():
        words = list(snapshot.settings_words[word_index(reg) : word_index(reg) + 2])
        await write_words(
            client, address=write_address(reg), values=words, slave_id=snapshot.slave_id
        )
        print(f"  restored {reg.name:<50s} {words}")
    await write_single(
        client,
        address=PACKED_BIT_REGISTER,
        value=snapshot.packed_bit,
        slave_id=snapshot.slave_id,
    )
    print(f"  restored packed bit 0x{snapshot.packed_bit:04x}")

    verify = await take_snapshot(client, slave_id=snapshot.slave_id)
    drift = diff_words(snapshot.settings_words, verify.settings_words)
    if drift or verify.packed_bit != snapshot.packed_bit:
        print(f"  RESTORE INCOMPLETE: {[str(d) for d in drift]}")
        return 1
    print("  restore verified — pack matches the snapshot")
    return 0


def summarise(reports: list[PackReport]) -> tuple[list[str], int]:
    """Human summary plus the process exit code."""
    lines = ["", "=" * 78, "SUMMARY", "=" * 78]
    failures = 0
    for report in reports:
        if report.aborted:
            failures += 1
            lines.append(f"BMS {report.slave_id}: ABORTED — {report.aborted}")
            continue
        if not report.results:
            words = len(report.baseline.settings_words) if report.baseline else 0
            lines.append(f"BMS {report.slave_id}: read-only, {words} words recorded")
            continue
        counts: dict[str, int] = {}
        for result in report.results:
            counts[result.status.value] = counts.get(result.status.value, 0) + 1
            if not result.ok:
                failures += 1
        detail = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        lines.append(f"BMS {report.slave_id}: {detail}")
        for result in report.results:
            if not result.ok:
                lines.append(f"    {result.status.value}: {result.parameter} — {result.detail}")
    lines.append("")
    lines.append("ALL GOOD" if failures == 0 else f"{failures} problem(s) — see above")
    return lines, (0 if failures == 0 else 1)


def parse_ids(raw: str) -> list[int]:
    return [int(part.strip()) for part in raw.split(",") if part.strip()]


def parse_ids_names(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--gateway", help="TCP gateway IP / hostname")
    p.add_argument("--port", type=int, default=502)
    p.add_argument("--slave-id", type=int, help="Test a single pack")
    p.add_argument("--slave-ids", help="Comma-separated pack list, e.g. 1,2,3,4,5,6")
    p.add_argument("--timeout", type=float, default=5.0)
    p.add_argument("--write", action="store_true", help="Perform write tests (default: read-only)")
    p.add_argument(
        "--confirm-writes",
        action="store_true",
        help="Required alongside --write. Confirms loads and chargers are disconnected.",
    )
    p.add_argument("--force", action="store_true", help="Bypass the pack-current interlock")
    p.add_argument("--only", help="Comma-separated parameter names to test exclusively")
    p.add_argument("--skip", default="", help="Comma-separated parameter names to skip")
    p.add_argument(
        "--include-structural",
        action="store_true",
        help=f"Also test {sorted(STRUCTURAL_PARAMETERS)} (excluded by default)",
    )
    p.add_argument(
        "--snapshot-dir",
        type=Path,
        default=Path("scripts/captures/hw_snapshots"),
        help="Where the safety snapshots are written",
    )
    p.add_argument("--report", type=Path, help="Write the full JSON report here")
    p.add_argument("--restore-from", type=Path, help="Restore a pack from a snapshot file")
    p.add_argument(
        "--collision-report",
        action="store_true",
        help="Print where un-corrected writes would land (no hardware needed) and exit",
    )
    return p


async def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.collision_report:
        for line in collision_report():
            print(line)
        return 0

    if not args.gateway:
        print("--gateway is required")
        return 2

    if args.write and not args.confirm_writes:
        print(
            "Refusing to write without --confirm-writes.\n"
            "Writes perturb each setting by one step and restore it. Confirm that\n"
            "loads and chargers are disconnected, then re-run with --confirm-writes."
        )
        return 2

    if args.restore_from and not args.confirm_writes:
        print("Refusing to restore without --confirm-writes (a restore writes to the BMS).")
        return 2

    ids: list[int] = []
    if args.slave_ids:
        ids = parse_ids(args.slave_ids)
    elif args.slave_id is not None:
        ids = [args.slave_id]
    elif not args.restore_from:
        print("Give --slave-id or --slave-ids")
        return 2

    client = AsyncModbusTcpClient(
        host=args.gateway, port=args.port, framer=FramerType.RTU, timeout=args.timeout
    )
    if not await client.connect():
        print(f"CONNECT FAILED: {args.gateway}:{args.port}")
        return 1

    try:
        if args.restore_from:
            return await restore_from_file(client, args.restore_from)

        only = set(parse_ids_names(args.only)) if args.only else None
        skip = set(parse_ids_names(args.skip))
        registers = selected_registers(
            only=only, skip=skip, include_structural=args.include_structural
        )
        bits = selected_bits(only=only, skip=skip)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")

        print("# jkbms2mqtt hardware test bench")
        print(f"# gateway   = {args.gateway}:{args.port}")
        print(f"# packs     = {ids}")
        print(f"# mode      = {'WRITE' if args.write else 'READ-ONLY'}")
        print(f"# registers = {len(registers)}, packed bits = {len(bits)}")

        reports: list[PackReport] = []
        for slave_id in ids:
            report = await run_pack(
                client,
                slave_id,
                do_writes=args.write,
                registers=registers,
                bits=bits,
                snapshot_dir=args.snapshot_dir,
                stamp=stamp,
                force=args.force,
            )
            reports.append(report)
            if report.aborted:
                print("Stopping: a pack aborted. Fix that before continuing to the rest.")
                break

        lines, code = summarise(reports)
        for line in lines:
            print(line)

        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(
                json.dumps([r.to_json() for r in reports], indent=2, sort_keys=True)
            )
            print(f"report written: {args.report}")
        return code
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
