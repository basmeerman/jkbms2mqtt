"""One-shot dump of every register block this bridge touches, for one BMS.

Run with the add-on STOPPED (avoid bus contention).

Usage:

    python -m scripts.dump_registers \
        --gateway 192.168.1.100 --port 502 --slave-id 1

The output has two sections per block:

1. Raw hex, 8 registers per row, prefixed with the absolute register address.
2. The current decoder's interpretation of fields in that block.

Pipe to a file and paste the file contents back to the chat so we can audit
every offset against actual hardware data:

    python -m scripts.dump_registers --gateway 192.168.1.100 --slave-id 1 \
        > scripts/captures/BMS_1.txt 2>&1
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.framer import FramerType

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
    PACKED_BITS,
    SAFETY_REGISTERS,
    SETTINGS_BLOCK_BASE,
    SETTINGS_BLOCK_CHUNKS,
    SETTINGS_BLOCK_WORDS,
    decode_packed_bit_value,
    decode_register_value,
)


@dataclass
class Block:
    name: str
    address: int
    count: int
    optional: bool = False


# Real-time block A + B + C (stitched into RT_BLOCK_WORDS-sized buffer for decode).
RT_BLOCKS = [
    Block("RT_A", BASE_RT, 120),
    Block("RT_B", BASE_RT + 0x78, 50, optional=True),
    Block("RT_C", BASE_RT + 0xF0, 16, optional=True),
]

# Settings block split into <=125-register chunks (Modbus FC 0x03 limit).
SETTINGS_BLOCKS = [
    Block(f"SETTINGS_{i + 1}", addr, count) for i, (addr, count) in enumerate(SETTINGS_BLOCK_CHUNKS)
]


def hex_dump(name: str, base: int, regs: list[int]) -> None:
    print(f"\n# {name} @ 0x{base:04x}  (count={len(regs)})")
    for i in range(0, len(regs), 8):
        row = regs[i : i + 8]
        offset = base + i
        hex_str = " ".join(f"{v:04x}" for v in row)
        ascii_str = ""
        for v in row:
            hi = (v >> 8) & 0xFF
            lo = v & 0xFF
            for c in (hi, lo):
                ascii_str += chr(c) if 32 <= c < 127 else "."
        print(f"  0x{offset:04x}:  {hex_str}    |{ascii_str}|")


async def read_block(client, block: Block, slave_id: int) -> list[int] | None:
    try:
        resp = await client.read_holding_registers(
            address=block.address, count=block.count, device_id=slave_id
        )
    except Exception as exc:
        print(f"\n# {block.name} @ 0x{block.address:04x}: EXCEPTION {type(exc).__name__}: {exc}")
        return None
    if resp.isError():
        print(f"\n# {block.name} @ 0x{block.address:04x}: MODBUS ERROR {resp}")
        return None
    return list(resp.registers)


def decode_realtime_summary(rt: list[int]) -> None:
    """Run the production decoder against the stitched RT buffer and print every field."""
    try:
        live = decode_realtime(rt)
    except Exception as exc:
        print(f"  decode_realtime failed: {exc}")
        return
    print(f"  cell_count               = {live.cell_count}")
    print(f"  cell_voltages_v          = {tuple(round(v, 3) for v in live.cell_voltages_v)}")
    print(f"  cell_resistances_ohm     = {tuple(round(r, 3) for r in live.cell_resistances_ohm)}")
    print(f"  cell_voltage_avg_v       = {round(live.cell_voltage_avg_v, 3)}")
    print(f"  cell_voltage_delta_v     = {round(live.cell_voltage_delta_v, 3)}")
    max_v = round(live.cell_voltage_max_v, 3)
    min_v = round(live.cell_voltage_min_v, 3)
    print(f"  cell_voltage_max_v       = {max_v}  (cell {live.cell_voltage_max_number})")
    print(f"  cell_voltage_min_v       = {min_v}  (cell {live.cell_voltage_min_number})")
    print(f"  total_voltage_v          = {round(live.total_voltage_v, 3)}")
    print(f"  total_current_a          = {round(live.total_current_a, 3)}")
    print(f"  total_power_w            = {round(live.total_power_w, 1)}")
    print(f"  soc_percentage           = {live.soc_percentage}")
    print(f"  soh_percentage           = {live.soh_percentage}")
    print(f"  remaining_capacity_ah    = {round(live.remaining_capacity_ah, 2)}")
    print(f"  nominal_capacity_ah      = {round(live.nominal_capacity_ah, 2)}")
    print(f"  cycle_count              = {live.cycle_count}")
    print(f"  total_cycle_capacity_ah  = {round(live.total_cycle_capacity_ah, 2)}   # SPECULATIVE @ 0x125A")
    print(f"  runtime_s                = {live.runtime_s}")
    print(f"  mos_temp_c               = {round(live.mos_temp_c, 1)}")
    print(f"  probe_1_temp_c           = {round(live.probe_1_temp_c, 1)}")
    print(f"  probe_2_temp_c           = {round(live.probe_2_temp_c, 1)}")
    print(f"  probe_3_temp_c           = {round(live.probe_3_temp_c, 1)}")
    print(f"  probe_4_temp_c           = {round(live.probe_4_temp_c, 1)}")
    print(f"  probe_5_temp_c           = {round(live.probe_5_temp_c, 1)}")
    print(f"  balance_current_a        = {round(live.balance_current_a, 3)}")
    print(f"  balance_active           = {live.balance_active}")
    print(f"  charge_enabled           = {live.charge_enabled}")
    print(f"  discharge_enabled        = {live.discharge_enabled}")
    print(f"  heating_active           = {live.heating_active}             # SPECULATIVE @ 0x1265")
    print(f"  heating_current_a        = {round(live.heating_current_a, 3)}   # SPECULATIVE @ 0x1264")
    print(f"  charge_status_id         = {live.charge_status_id}            # SPECULATIVE @ 0x126C")
    print(f"  charge_status            = {live.charge_status!r}             # SPECULATIVE")
    print(f"  charge_status_time_s     = {live.charge_status_time_s}        # SPECULATIVE @ 0x126D")
    print(f"  alarm_bits               = 0x{live.alarm_bits:08x}")
    print(f"  alarms                   = {live.alarms}")


def decode_settings_summary(stitched: list[int], read_addresses: set[int]) -> None:
    """Show every writable register's decoded value."""
    print("\n  Numeric registers:")
    for r in (*BASIC_REGISTERS, *SAFETY_REGISTERS):
        if r.address not in read_addresses or (r.address + 1) not in read_addresses:
            print(f"    {r.name:55s} = (not read)")
            continue
        try:
            value = decode_register_value(r, stitched)
        except Exception as exc:
            value = f"DECODE ERROR: {exc}"
        unit = f" {r.unit}" if r.unit else ""
        meta = f"[{r.encoding.value} @ 0x{r.address:04x}, tier={r.tier.value}]"
        print(f"    {r.name:55s} = {value}{unit}    {meta}")


def decode_packed_bit_summary(raw: int) -> None:
    print(f"\n  Packed-bit register 0x{PACKED_BIT_REGISTER:04x} = 0x{raw:04x}")
    for bit in PACKED_BITS:
        v = decode_packed_bit_value(bit, raw)
        print(f"    {bit.name:35s} = {v}   [mask=0x{bit.bit_mask:04x}]")


def decode_static_info_summary(regs: list[int]) -> None:
    try:
        info = decode_static_info(regs)
    except Exception as exc:
        print(f"  decode_static_info failed: {exc}")
        return
    print(f"  model         = {info.model!r}")
    print(f"  hw_version    = {info.hw_version!r}")
    print(f"  sw_version    = {info.sw_version!r}")
    print(f"  serial_number = {info.serial_number!r}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", required=True, help="TCP gateway IP / hostname")
    parser.add_argument("--port", type=int, default=502)
    parser.add_argument("--slave-id", type=int, required=True, help="Modbus slave addr (1..15)")
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()

    print("# jkbms2mqtt register dump")
    print(f"# gateway = {args.gateway}:{args.port}, slave_id = {args.slave_id}")

    client = AsyncModbusTcpClient(
        host=args.gateway,
        port=args.port,
        framer=FramerType.RTU,
        timeout=args.timeout,
    )
    ok = await client.connect()
    if not ok:
        print("# CONNECT FAILED")
        return 1

    # -- Real-time -----------------------------------------------------------------
    print("\n" + "=" * 78)
    print("REAL-TIME BLOCKS (base 0x1200)")
    print("=" * 78)
    rt_buf = [0] * RT_BLOCK_WORDS
    for blk in RT_BLOCKS:
        regs = await read_block(client, blk, args.slave_id)
        if regs is None:
            continue
        hex_dump(blk.name, blk.address, regs)
        off = blk.address - BASE_RT
        for i, v in enumerate(regs[: blk.count]):
            rt_buf[off + i] = v
    print("\n## Decoder interpretation")
    decode_realtime_summary(rt_buf)

    # -- Settings ------------------------------------------------------------------
    print("\n" + "=" * 78)
    print("SETTINGS BLOCKS (base 0x1000)")
    print("=" * 78)
    settings_buf = [0] * SETTINGS_BLOCK_WORDS
    read_addresses: set[int] = set()
    for blk in SETTINGS_BLOCKS:
        regs = await read_block(client, blk, args.slave_id)
        if regs is None:
            continue
        hex_dump(blk.name, blk.address, regs)
        off = blk.address - SETTINGS_BLOCK_BASE
        for i, v in enumerate(regs[: blk.count]):
            settings_buf[off + i] = v
            read_addresses.add(blk.address + i)
    print("\n## Decoder interpretation")
    decode_settings_summary(settings_buf, read_addresses)

    # -- Packed bit ----------------------------------------------------------------
    print("\n" + "=" * 78)
    print(f"PACKED BIT REGISTER 0x{PACKED_BIT_REGISTER:04x}")
    print("=" * 78)
    regs = await read_block(
        client, Block("PACKED_BIT", PACKED_BIT_REGISTER, 1), args.slave_id
    )
    if regs is not None:
        hex_dump("PACKED_BIT", PACKED_BIT_REGISTER, regs)
        decode_packed_bit_summary(regs[0])

    # -- Static info ---------------------------------------------------------------
    print("\n" + "=" * 78)
    print(f"STATIC INFO BLOCK @ 0x{BASE_INFO:04x}")
    print("=" * 78)
    regs = await read_block(
        client, Block("INFO", BASE_INFO, INFO_BLOCK_WORDS), args.slave_id
    )
    if regs is not None:
        hex_dump("INFO", BASE_INFO, regs)
        print("\n## Decoder interpretation")
        decode_static_info_summary(regs)

    # -- Extra: also dump the gap between 0x1085 (last known setting) and 0x10F0,
    # and 0x12A8 (end of RT_B) to 0x12F0 — useful to spot fields the bridge isn't
    # decoding yet.
    print("\n" + "=" * 78)
    print("EXTRA: UNMAPPED REGIONS (for forensic offset discovery)")
    print("=" * 78)
    extras = [
        Block("EXTRA_AFTER_SETTINGS", 0x1086, 124),
        Block("EXTRA_AFTER_RT_B", 0x12AA, 70),
    ]
    for blk in extras:
        regs = await read_block(client, blk, args.slave_id)
        if regs is not None:
            hex_dump(blk.name, blk.address, regs)

    client.close()
    print("\n# done.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
