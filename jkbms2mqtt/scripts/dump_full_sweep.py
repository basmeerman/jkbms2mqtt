"""Comprehensive register sweep — every spec'd block + a few empirical-alias probes.

Run with the add-on STOPPED. Reads each block in <=120-register chunks (well
under the FC 0x03 ceiling of 125), gracefully continues on per-chunk errors,
and emits both raw hex (with ASCII sidecar) and the decoder's interpretation
for known regions.

Usage:

    /Users/basmeerman/Downloads/jkbms2mqtt/.venv/bin/python -m scripts.dump_full_sweep \
        --gateway 192.168.4.156 --port 502 --slave-id 1 \
        > scripts/captures/BMS_<id>_sweep.txt 2>&1

Coverage:

  * Settings 0x1000..0x108D                 (V1.1 spec range)
  * Real-time 0x1200..0x130F                (V1.0/V1.1 spec range)
  * Info 0x1400..0x150F                     (V1.1 spec range)
  * Empirical alias probes:
      - 0x10B0..0x10F0  (gap settings → packed bit)
      - 0x10F0..0x1140  (where the old 0x1114 packed-bit alias lived)
      - 0x12A8..0x12F0  (gap between RT_B and RT_C)
      - 0x1300..0x1400  (gap between RT and info)
      - 0x1460..0x1500  (gap after info block)

Per-spec interpretations follow specifications/BMS.RS485.Modbus.V1.1.pdf.
A Modbus error on an alias probe is normal and is logged at INFO.
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
    decode_packed_bit_value,
    decode_register_value,
)


@dataclass
class Chunk:
    name: str
    address: int
    count: int
    optional: bool = False
    purpose: str = ""


# Each entry is a Modbus read of `count` registers starting at `address`.
# Count is held under 120 to leave headroom under the FC 0x03 cap of 125.
PLAN: list[Chunk] = [
    # ---- Settings 0x1000..0x108D ----
    Chunk("SETTINGS_A", 0x1000, 120, purpose="settings 0x1000..0x1077"),
    Chunk("SETTINGS_B", 0x1078, 22, purpose="settings 0x1078..0x108D incl. packed bit"),
    # ---- Real-time 0x1200..0x12FF ----
    # Block sizes match the production decoder's BLOCK_A/B/C_COUNT. Asking
    # for more than this returns Modbus illegal-data-address on PB2A16S20P
    # because the request crosses into an unmapped region.
    Chunk("RT_A", 0x1200, 120, purpose="cells, V/I/P, SoC, temps, alarms"),
    Chunk("RT_B", 0x1278, 50, purpose="RT continuation"),
    Chunk("RT_C", 0x12F0, 16, purpose="probes 3/4/5 (PB2A16S20P firmware deviation)"),
    # ---- Info 0x1400..0x150F ----
    Chunk("INFO_A", 0x1400, 120, purpose="model/fw/sw/serial + V1.1 host config"),
    Chunk("INFO_B", 0x1478, 60, purpose="info continuation incl. RCVTime/RFVTime"),
    # ---- Empirical alias probes (Modbus error = region not mapped on this firmware) ----
    Chunk("ALIAS_SETTINGS_GAP", 0x10B0, 64, optional=True,
          purpose="gap between settings and packed bit"),
    Chunk("ALIAS_OLD_PACKED",   0x10F0, 80, optional=True,
          purpose="where 0x1114 packed-bit alias lives on some firmware"),
    Chunk("ALIAS_RT_GAP",       0x12A8, 72, optional=True,
          purpose="gap between RT_B and RT_C"),
    Chunk("ALIAS_RT_INFO_GAP",  0x1310, 80, optional=True,
          purpose="gap between RT and info"),
    Chunk("ALIAS_POST_INFO",    0x14E0, 48, optional=True,
          purpose="gap after info block"),
]


def hex_dump(name: str, base: int, regs: list[int]) -> None:
    """Pretty hex dump, 8 regs per row, with ASCII sidecar."""
    print(f"\n# {name} @ 0x{base:04x}  (count={len(regs)})")
    for i in range(0, len(regs), 8):
        row = regs[i : i + 8]
        offset = base + i
        hex_str = " ".join(f"{v:04x}" for v in row)
        ascii_str = ""
        for v in row:
            for c in ((v >> 8) & 0xFF, v & 0xFF):
                ascii_str += chr(c) if 32 <= c < 127 else "."
        print(f"  0x{offset:04x}:  {hex_str:<40s}  |{ascii_str}|")


async def read_chunk(client, c: Chunk, slave_id: int) -> list[int] | None:
    try:
        resp = await client.read_holding_registers(
            address=c.address, count=c.count, device_id=slave_id
        )
    except Exception as exc:
        kind = "INFO" if c.optional else "ERROR"
        print(f"\n# {c.name} @ 0x{c.address:04x}: {kind} {type(exc).__name__}: {exc}")
        return None
    if resp.isError():
        kind = "INFO" if c.optional else "ERROR"
        print(f"\n# {c.name} @ 0x{c.address:04x}: {kind} Modbus error {resp}")
        return None
    return list(resp.registers)


def decode_realtime_summary(buf: list[int]) -> None:
    """Run the production decoder against the stitched RT buffer."""
    try:
        live = decode_realtime(buf)
    except Exception as exc:
        print(f"  decode_realtime failed: {exc}")
        return
    print(f"  cell_count               = {live.cell_count}")
    cells = tuple(round(v, 3) for v in live.cell_voltages_v)
    print(f"  cell_voltages_v          = {cells}")
    res = tuple(round(r, 3) for r in live.cell_resistances_ohm)
    print(f"  cell_resistances_ohm     = {res}")
    print(f"  cell_voltage_avg_v       = {round(live.cell_voltage_avg_v, 3)}")
    print(f"  cell_voltage_delta_v     = {round(live.cell_voltage_delta_v, 3)}")
    print(f"  total_voltage_v          = {round(live.total_voltage_v, 3)}")
    print(f"  total_current_a          = {round(live.total_current_a, 3)}")
    print(f"  total_power_w            = {round(live.total_power_w, 1)}")
    print(f"  soc_percentage           = {live.soc_percentage}")
    print(f"  soh_percentage           = {live.soh_percentage}")
    print(f"  remaining_capacity_ah    = {round(live.remaining_capacity_ah, 2)}")
    print(f"  nominal_capacity_ah      = {round(live.nominal_capacity_ah, 2)}")
    print(f"  cycle_count              = {live.cycle_count}")
    print(f"  total_cycle_capacity_ah  = {round(live.total_cycle_capacity_ah, 2)}")
    print(f"  runtime_s                = {live.runtime_s}")
    print(f"  mos_temp_c               = {round(live.mos_temp_c, 1)}")
    print(f"  probe_1_temp_c           = {round(live.probe_1_temp_c, 1)}")
    print(f"  probe_2_temp_c           = {round(live.probe_2_temp_c, 1)}")
    print(f"  probe_3_temp_c           = {round(live.probe_3_temp_c, 1)}    # FW-deviation: spec says 0x127C")
    print(f"  probe_4_temp_c           = {round(live.probe_4_temp_c, 1)}    # FW-deviation: spec says 0x127D")
    print(f"  probe_5_temp_c           = {round(live.probe_5_temp_c, 1)}    # FW-deviation: spec says 0x127E")
    print(f"  balance_current_a        = {round(live.balance_current_a, 3)}")
    print(f"  balance_active           = {live.balance_active}")
    print(f"  charge_enabled           = {live.charge_enabled}")
    print(f"  discharge_enabled        = {live.discharge_enabled}")
    print(f"  heating_active           = {live.heating_active}")
    print(f"  heating_current_a        = {round(live.heating_current_a, 3)}")
    print(f"  alarm_bits               = 0x{live.alarm_bits:08x}")
    print(f"  alarms                   = {live.alarms}")


def decode_settings_summary(buf: list[int], read_addrs: set[int]) -> None:
    """For every BASIC_REGISTERS + SAFETY_REGISTERS entry that we actually read,
    decode and print the value next to its spec field name."""
    print("\n  Numeric settings (spec V1.1 mapping):")
    for r in (*BASIC_REGISTERS, *SAFETY_REGISTERS):
        if r.address not in read_addrs or (r.address + 1) not in read_addrs:
            print(f"    {r.name:55s} = (not read)")
            continue
        try:
            value = decode_register_value(r, buf)
        except Exception as exc:
            value = f"DECODE ERROR: {exc}"
        unit = f" {r.unit}" if r.unit else ""
        spec_byte = (r.address - SETTINGS_BLOCK_BASE) * 2
        meta = f"[spec byte 0x{spec_byte:03x} reg 0x{r.address:04x} {r.encoding.value}]"
        print(f"    {r.name:55s} = {value}{unit}    {meta}")


def decode_packed_bit_summary(raw: int) -> None:
    print(f"\n  Packed-bit register 0x{PACKED_BIT_REGISTER:04x} = 0x{raw:04x}  "
          f"(binary {raw:016b})")
    for bit in PACKED_BITS:
        v = decode_packed_bit_value(bit, raw)
        print(f"    {bit.name:35s} = {v}   [mask=0x{bit.bit_mask:04x}]")


def decode_static_info_summary(buf: list[int]) -> None:
    try:
        info = decode_static_info(buf)
    except Exception as exc:
        print(f"  decode_static_info failed: {exc}")
        return
    print(f"  model         = {info.model!r}")
    print(f"  hw_version    = {info.hw_version!r}")
    print(f"  sw_version    = {info.sw_version!r}")
    print(f"  serial_number = {info.serial_number!r}")


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--gateway", required=True, help="TCP gateway IP / hostname")
    p.add_argument("--port", type=int, default=502)
    p.add_argument("--slave-id", type=int, required=True, help="Modbus slave addr 1..15")
    p.add_argument("--timeout", type=float, default=5.0)
    p.add_argument("--skip-alias", action="store_true",
                   help="Skip empirical alias probes (no Modbus errors in output)")
    args = p.parse_args()

    print("# jkbms2mqtt full register sweep")
    print(f"# gateway   = {args.gateway}:{args.port}")
    print(f"# slave_id  = {args.slave_id}")
    print(f"# skip_alias = {args.skip_alias}")

    client = AsyncModbusTcpClient(
        host=args.gateway, port=args.port,
        framer=FramerType.RTU, timeout=args.timeout,
    )
    ok = await client.connect()
    if not ok:
        print("# CONNECT FAILED")
        return 1

    # Each block's reads stitched into per-block buffers, plus the set of
    # absolute addresses we actually saw, for downstream decoding.
    rt_buf = [0] * RT_BLOCK_WORDS
    settings_buf = [0] * 0x90       # cover up to 0x108E (well past spec end)
    info_buf = [0] * 0x90
    settings_read: set[int] = set()
    packed_bit_value: int | None = None

    # -- Settings sweep --
    print("\n" + "=" * 78)
    print("SETTINGS BLOCK 0x1000..0x108D")
    print("=" * 78)
    for c in [c for c in PLAN if c.name.startswith("SETTINGS_")]:
        regs = await read_chunk(client, c, args.slave_id)
        if regs is None:
            continue
        hex_dump(f"{c.name} ({c.purpose})", c.address, regs)
        off = c.address - SETTINGS_BLOCK_BASE
        for i, v in enumerate(regs[: c.count]):
            if off + i < len(settings_buf):
                settings_buf[off + i] = v
                settings_read.add(c.address + i)

    # Packed-bit register. Probe both the spec-derived address (0x108A) AND
    # the empirical PB2A16S20P address (0x1114) so we can compare on any
    # firmware. The production code uses whichever PACKED_BIT_REGISTER points
    # at — currently the empirical address for PB2A16S20P compatibility.
    print("\n# Probing packed-bit register at spec address 0x108A and empirical 0x1114")
    spec_probe = await read_chunk(client, Chunk(
        "PACKED_BIT_SPEC", 0x108A, 1, purpose="V1.1 spec-derived address"
    ), args.slave_id)
    if spec_probe is not None:
        hex_dump("PACKED_BIT_SPEC @ 0x108A", 0x108A, spec_probe)
    empirical_probe = await read_chunk(client, Chunk(
        "PACKED_BIT_EMPIRICAL", 0x1114, 1, purpose="PB2A16S20P empirical address"
    ), args.slave_id)
    if empirical_probe is not None:
        hex_dump("PACKED_BIT_EMPIRICAL @ 0x1114", 0x1114, empirical_probe)
    # Use whichever address the production code is configured for.
    chosen = await read_chunk(client, Chunk(
        "PACKED_BIT", PACKED_BIT_REGISTER, 1, purpose="production decoder address"
    ), args.slave_id)
    if chosen is not None:
        packed_bit_value = chosen[0]

    print("\n## Decoded settings (spec V1.1)")
    decode_settings_summary(settings_buf, settings_read)
    if packed_bit_value is not None:
        decode_packed_bit_summary(packed_bit_value)

    # -- Real-time sweep --
    print("\n" + "=" * 78)
    print("REAL-TIME BLOCK 0x1200..0x130F")
    print("=" * 78)
    for c in [c for c in PLAN if c.name.startswith("RT_")]:
        regs = await read_chunk(client, c, args.slave_id)
        if regs is None:
            continue
        hex_dump(f"{c.name} ({c.purpose})", c.address, regs)
        off = c.address - BASE_RT
        for i, v in enumerate(regs[: c.count]):
            if 0 <= off + i < RT_BLOCK_WORDS:
                rt_buf[off + i] = v

    print("\n## Decoded real-time (spec V1.1 with PB2A16S20P probe deviation)")
    decode_realtime_summary(rt_buf)

    # -- Info sweep --
    print("\n" + "=" * 78)
    print("INFO BLOCK 0x1400..0x150F")
    print("=" * 78)
    for c in [c for c in PLAN if c.name.startswith("INFO_")]:
        regs = await read_chunk(client, c, args.slave_id)
        if regs is None:
            continue
        hex_dump(f"{c.name} ({c.purpose})", c.address, regs)
        off = c.address - BASE_INFO
        for i, v in enumerate(regs[: c.count]):
            if off + i < len(info_buf):
                info_buf[off + i] = v

    print("\n## Decoded static info")
    decode_static_info_summary(info_buf)

    # -- Alias probes --
    if not args.skip_alias:
        print("\n" + "=" * 78)
        print("EMPIRICAL ALIAS PROBES (Modbus errors normal for non-mirrored firmware)")
        print("=" * 78)
        for c in [c for c in PLAN if c.name.startswith("ALIAS_")]:
            regs = await read_chunk(client, c, args.slave_id)
            if regs is None:
                continue
            hex_dump(f"{c.name} ({c.purpose})", c.address, regs)

    client.close()
    print("\n# done.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
