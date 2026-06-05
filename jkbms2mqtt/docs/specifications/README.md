# JK BMS protocol specifications

Primary-source documents used to ground every register address, encoding, and
bit position in this add-on. The implementation is calibrated against these
PDFs plus the on-hardware capture in
[`../../scripts/captures/BMS_1.txt`](../../scripts/captures/BMS_1.txt); the
audit in [`../FIELD_AUDIT.md`](../FIELD_AUDIT.md) records every
implementation-vs-spec deviation.

| File | Source | Purpose |
|---|---|---|
| `BMS.RS485.Modbus.V1.0.pdf` | [ciciban/jkbms-PB2A16S20P](https://github.com/ciciban/jkbms-PB2A16S20P/blob/main/BMS.RS485.Modbus.V1.0.pdf) | Original JIKONG RS485 Modbus protocol, **version 1.0**. Mostly Chinese with English field names interleaved. Authoritative for the early JK-PB / JK-B series register map. |
| `BMS.RS485.Modbus.V1.1.pdf` | [syssi/esphome-jk-bms](https://github.com/syssi/esphome-jk-bms/blob/main/docs/pb2a16s20p/BMS%20RS485%20Modbus%20V1.1.pdf) | JIKONG RS485 Modbus protocol, **version 1.1**. Shipped with the **PB2A16S20P** family (the firmware this add-on is calibrated against). Superset of V1.0: adds three packed-bit toggles (BITs 7-9), reorganises the special-command block at `0x1600`, and adds several info-block fields (LCD buzzer, dry-contacts, RCV/RFV timers). **Authoritative for current PB-series firmware.** |
| `BMS.CAN.protocol.pdf` | [syssi/esphome-jk-bms](https://github.com/syssi/esphome-jk-bms/blob/main/docs/BMS-CAN%20Communication%20protocol.pdf) | JIKONG **CAN-bus** protocol. Out of scope for this add-on (we only speak RS485 Modbus); included as reference for the inverter-facing side of the JK ecosystem. |

## How to read the spec tables

The register tables in V1.0 / V1.1 use **byte offsets** from the indicated block
base (`0x1000` settings, `0x1200` real-time, `0x1400` info, `0x1600` commands).
To convert a spec byte offset to a Modbus register address:

```
modbus_register = block_base + (byte_offset / 2)
```

For example: `VolCellUV` at byte offset `0x0004` in the `0x1000` block →
Modbus register `0x1000 + 0x0004/2 = 0x1002`. Each `UINT32` field then spans
two consecutive Modbus registers (high word first, low word second).

The `R/W` column is per-bit for the packed-bit register at byte `0x0114`;
elsewhere it indicates whether the whole field is read-only (`R`), writable
(`W`), or read-write (`RW`).

## Firmware-specific deviations from the spec

The PB2A16S20P firmware (used by the user during this project) deviates from
the V1.1 spec for these fields — see `FIELD_AUDIT.md` for the evidence:

- **`TempBat 3 / 4 / 5`** — spec says Modbus regs `0x127C / 0x127D / 0x127E`
  (byte offsets `0xF8 / 0xFA / 0xFC`); on real hardware those words read zero
  and the actual temperatures appear at `0x12F4 / 0x12F5 / 0x12F6`. The
  decoder uses the empirical addresses with a `# SPEC-DEVIATION` comment.
- **Packed-bit register** — spec V1.1 places this at byte offset `0x114` in
  the settings block, which maps to Modbus reg `0x108A` under the spec's
  byte→register convention. On PB2A16S20P firmware 15.41, reg `0x108A`
  returns `0x0000` (no bits set) while the BMS app's Control tab shows
  `Charging Float Mode`, `Discharge OCP 2`, `Discharge OCP 3` all ON. Reg
  `0x1114` returns `0x3200` — bits 9 / 12 / 13 — which matches the three
  ON toggles. The decoder uses the empirical address `0x1114` with a
  `# SPEC-DEVIATION` annotation. Bits 12 / 13 are not documented in V1.1
  but consistently map to `Discharge OCP 2 / 3`.
- **`charge_status` FSM state** (Stand-by / Bulk / Absorption / Float) shown by
  the BMS app is **not** documented in V1.1 or V1.0 RS485 Modbus. It is
  reachable via the BLE / UART-TTL proprietary protocol but not exposed at any
  Modbus register address the audit could verify. Removed from the entity
  table. Confirmed again in the BMS_1 full sweep (capture `BMS_1_sweep.txt`,
  BLE app showed `Charge Status: Abs`, `Charge Status Time: 320s`; neither
  value appears in any Modbus register read).

- **Heating bit** at spec V1.1 reg `0x1268` is the **low** byte (Modbus
  big-endian); the high byte is `TempSensorAbsent`. The full sweep confirmed
  reg `0x1268 = 0xFF00`, with the app's `Heating Status: OFF` matching the
  low byte `0x00`. The previous decoder used the high byte and consequently
  reported `heating_active = True` even when the heater was off.

## Licensing

These PDFs are © JIKONG Electronic Technology Co., Ltd. They are mirrored here
for documentation purposes — to ground the implementation in a citable primary
source and to make field audits reproducible. If JIKONG asks us to remove
them, we will and will revert to linking the upstream mirrors only.
