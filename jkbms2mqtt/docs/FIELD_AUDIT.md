# Field audit — spec vs implementation vs hardware

Cross-checked against three sources:

1. **Spec** — [`specifications/BMS.RS485.Modbus.V1.1.pdf`](specifications/BMS.RS485.Modbus.V1.1.pdf) (authoritative for PB2A16S20P firmware), with [`V1.0.pdf`](specifications/BMS.RS485.Modbus.V1.0.pdf) as historical reference.
2. **Hardware capture** — [`../scripts/captures/BMS_1.txt`](../scripts/captures/BMS_1.txt) (PB2A16S20P, fw 15.41).
3. **BMS app values** — recorded textually in the capture's anchor section; original phone screenshots removed from the repo.

The spec uses **byte offsets** from the block base; Modbus register address =
`block_base + (byte_offset / 2)`. Each `UINT32` field spans two consecutive
Modbus registers (high word first).

Verdict legend:

- **MATCH** — implementation address/encoding equals spec, and hardware agrees.
- **FW-DEVIATION** — implementation matches hardware, but hardware deviates from spec for this field (firmware bug or undocumented behaviour).
- **FIXED** — bug found in this audit and corrected by the same PR.

---

## Real-time block @ `0x1200`

| Field | Spec byte | Spec reg | Impl offset | Verdict |
|---|---|---|---|---|
| `CellVol[0..15]` | `0x00..0x1E` | `0x1200..0x120F` | `0x00..0x0F` | **MATCH** |
| `CellSta` (present bitmap) | `0x40..0x43` | `0x1220..0x1221` | `0x20..0x21` | **MATCH** |
| `CellVolAve` | `0x44` | `0x1222` | `0x22` | **MATCH** |
| `CellVdifMax` | `0x46` | `0x1223` | `0x23` | **MATCH** |
| `CellWireRes[0..15]` (mΩ) | `0x4A..0x68` | `0x1225..0x1234` | `0x25..0x34` | **FIXED** in PR #3 from speculative `0x80` |
| `TempMos` | `0x8A` | `0x1245` | `0x45` | **MATCH** |
| `BatVol` | `0x90..0x93` | `0x1248..0x1249` | `0x48..0x49` | **MATCH** |
| `BatWatt` (mW) | `0x94..0x97` | `0x124A..0x124B` | — (derived as V × I) | **OK** (computed not read) |
| `BatCurrent` | `0x98..0x9B` | `0x124C..0x124D` | `0x4C..0x4D` | **MATCH** |
| `TempBat 1` | `0x9C` | `0x124E` | `0x4E` | **MATCH** |
| `TempBat 2` | `0x9E` | `0x124F` | `0x4F` | **MATCH** |
| alarms (UINT32) | `0xA0..0xA3` | `0x1250..0x1251` | `0x50..0x51` | **MATCH** |
| `BalanCurrent` | `0xA4` | `0x1252` | `0x52` | **MATCH** |
| `BalanSta\|SOCStateOfcharge` | `0xA6` | `0x1253` | `0x53` | **MATCH** |
| `SOCCapRemain` | `0xA8..0xAB` | `0x1254..0x1255` | `0x54..0x55` | **MATCH** |
| `SOCFullChargeCap` | `0xAC..0xAF` | `0x1256..0x1257` | `0x56..0x57` | **MATCH** |
| `SOCCycleCount` | `0xB0..0xB3` | `0x1258..0x1259` | `0x58..0x59` | **MATCH** |
| `SOCCycleCap` | `0xB4..0xB7` | `0x125A..0x125B` | `0x5A..0x5B` | **MATCH** |
| `SOCSOH\|Precharge` | `0xB8` | `0x125C` | `0x5C` | **MATCH** |
| `RunTime` | `0xBC..0xBF` | `0x125E..0x125F` | `0x5E..0x5F` | **MATCH** |
| `Charge\|Discharge` | `0xC0` | `0x1260` | `0x60` | **MATCH** |
| `Heating` (high byte of) | `0xD0..0xD1` | `0x1268` | **FIXED** — was at speculative `0x65` |
| `HeatCurrent` | `0xE6` | `0x1273` | **FIXED** — was at speculative `0x64` (which is actually `TimeCOCPR`) |
| `TempBat 3` | `0xF8` | `0x127C` | empirical `0xF4` | **FW-DEVIATION** — spec'd reg reads zero on PB2A16S20P; data lives at `0x12F4` |
| `TempBat 4` | `0xFA` | `0x127D` | empirical `0xF5` | **FW-DEVIATION** |
| `TempBat 5` | `0xFC` | `0x127E` | empirical `0xF6` | **FW-DEVIATION** |
| `charge_status_id` | — | — | `0x6C` | **REMOVED** — not in V1.0/V1.1 spec; was a speculative offset |
| `charge_status_time_s` | — | — | `0x6D` | **REMOVED** — not in V1.0/V1.1 spec |

## Settings block @ `0x1000`

| Field | Spec byte | Spec reg | Impl reg | Verdict |
|---|---|---|---|---|
| `VolSmartSleep` | `0x00` | `0x1000` | `0x1000` | **MATCH** |
| `VolCellUV` | `0x04` | `0x1002` | `0x1002` | **MATCH** |
| `VolCellUVPR` | `0x08` | `0x1004` | `0x1004` | **MATCH** |
| `VolCellOV` | `0x0C` | `0x1006` | `0x1006` | **MATCH** |
| `VolCellOVPR` | `0x10` | `0x1008` | `0x1008` | **MATCH** |
| `VolBalanTrig` | `0x14` | `0x100A` | `0x100A` | **MATCH** |
| `VolSOC100%` | `0x18` | `0x100C` | `0x100C` | **MATCH** |
| `VolSOC0%` | `0x1C` | `0x100E` | `0x100E` | **MATCH** |
| `VolCellRCV` | `0x20` | `0x1010` | `0x1010` | **MATCH** |
| `VolCellRFV` | `0x24` | `0x1012` | `0x1012` | **MATCH** |
| `VolSysPwrOff` (= `power_off_voltage`) | `0x28` | `0x1014` | `0x1014` | **MATCH** |
| `CurBatCOC` (= `max_charge_current`) | `0x2C` | `0x1016` | `0x1016` | **MATCH** |
| `TIMBatCOCPDly` | `0x30` | `0x1018` | `0x1018` | **MATCH** |
| `TIMBatCOCPRDly` | `0x34` | `0x101A` | `0x101A` | **MATCH** |
| `CurBatDcOC` (= `max_discharge_current`) | `0x38` | `0x101C` | `0x101C` | **MATCH** |
| `TIMBatDcOCPDly` | `0x3C` | `0x101E` | `0x101E` | **MATCH** |
| `TIMBatDcOCPRDly` | `0x40` | `0x1020` | `0x1020` | **MATCH** |
| `TIMBatSCPRDly` | `0x44` | `0x1022` | `0x1022` | **MATCH** |
| `CurBalanMax` | `0x48` | `0x1024` | `0x1024` | **MATCH** |
| **`TMPBatCOT` (Charge OTP)** | `0x4C` | **`0x1026`** | was `0x102A` | **FIXED** — labels were swapped with discharge |
| **`TMPBatCOTPR` (Charge OTPR)** | `0x50` | **`0x1028`** | was `0x102C` | **FIXED** |
| **`TMPBatDcOT` (Discharge OTP)** | `0x54` | **`0x102A`** | was `0x1026` | **FIXED** |
| **`TMPBatDcOTPR` (Discharge OTPR)** | `0x58` | **`0x102C`** | was `0x1028` | **FIXED** |
| `TMPBatCUT` (Charge UTP) | `0x5C` | `0x102E` | `0x102E` | **MATCH** |
| `TMPBatCUTPR` (Charge UTPR) | `0x60` | `0x1030` | `0x1030` | **MATCH** |
| `TMPMosOT` | `0x64` | `0x1032` | `0x1032` | **MATCH** |
| `TMPMosOTPR` | `0x68` | `0x1034` | `0x1034` | **MATCH** |
| `CellCount` | `0x6C` | `0x1036` | `0x1036` | **MATCH** |
| `BatChargeEN` (BOOL) | `0x70` | `0x1038` | **RESTORED** — wrongly removed in PR #3 |
| `BatDisChargeEN` (BOOL) | `0x74` | `0x103A` | **RESTORED** |
| `BalanEN` (BOOL) | `0x78` | `0x103C` | **RESTORED** |
| `CapBatCell` (`pack_capacity_setting`) | `0x7C` | `0x103E` | `0x103E` | **MATCH** |
| `SCPDelay` (μs) | `0x80` | `0x1040` | `0x1040` | **MATCH** |
| `VolStartBalan` | `0x84` | `0x1042` | `0x1042` | **MATCH** |
| `CellConWireRes[0..31]` (μΩ writable) | `0x88..0x104` | `0x1044..0x1082` | — | **NOT EXPOSED** — 32 entities of low value to typical HA users; skip until requested |
| `DevAddr` | `0x108` | `0x1084` | — | **NOT EXPOSED** — matches the user's `bms_ids` config; redundant |
| `TIMProdischarge` | `0x10C` | `0x1086` | — | **NOT EXPOSED** — niche; skip until requested |
| packed-bit register | `0x114` | `0x108A` | was `0x1114` | **FIXED** to spec address |
| `TIMSmartSleep` (UINT8 hours) | `0x118` | `0x108C` | — | **NOT EXPOSED** — niche |

## Packed-bit register (UINT16 at `0x108A`)

V1.1 bit positions (V1.0 only had bits 0–6):

| Bit | Mask | Spec name | Impl entity | Status |
|---|---|---|---|---|
| 0 | `0x0001` | `HeatEN` | — | not exposed (heating not yet validated end-to-end) |
| 1 | `0x0002` | Disable temp-sensor | — | not exposed |
| 2 | `0x0004` | GPS Heartbeat | — | not exposed |
| 3 | `0x0008` | Port Switch (RS485 / CAN) | — | not exposed |
| 4 | `0x0010` | LCD Always On | — | not exposed |
| 5 | `0x0020` | Special Charger | — | not exposed |
| 6 | `0x0040` | `SmartSleep` | `smart_sleep_switch` | **MATCH** |
| 7 | `0x0080` | `DisablePCLModule` | `disable_pcl_module_switch` | **MATCH** (V1.1 only) |
| 8 | `0x0100` | `TimedStoredData` | `timed_stored_data_switch` | **FIXED** — was `0x0020` (Special Charger) |
| 9 | `0x0200` | `ChargingFloatMode` | — | not exposed |

## Static info block @ `0x1400`

| Field | Spec byte | Spec reg | Impl offset | Verdict |
|---|---|---|---|---|
| `ManufacturerDeviceID` (16 ASCII) | `0x00` | `0x1400..0x1407` | `0x00` | **MATCH** |
| `HardwareVersion` (8 ASCII) | `0x10` | `0x1408..0x140B` | `0x08` | **MATCH** |
| `SoftwareVersion` (8 ASCII) | `0x18` | `0x140C..0x140F` | `0x0C` | **MATCH** |
| `ODDRunTime` (s, UINT32) | `0x20` | `0x1410..0x1411` | — | **NOT EXPOSED** — duplicates the RT-block `RunTime` |
| `PWROnTimes` (UINT32) | `0x24` | `0x1412..0x1413` | — | **NOT EXPOSED** — niche |
| serial-number-like ASCII | — | `0x1414..` | `0x28` | **FW-EXTENSION** — not in V1.1 spec; present on PB2A16S20P; kept as `serial_number` |

## What this audit closes

- **Safety-relevant**: the charge / discharge OTP labels were swapped, meaning a
  user enabling safety writes to change the discharge over-temp would have
  written to the charge over-temp register and vice versa. Hardware capture
  showed identical values (`70/60 °C` on both sides) so the bug was invisible
  to value cross-checks; only the spec disambiguates them.
- **Functional regression from PR #3**: `BatChargeEN` / `BatDisChargeEN` /
  `BalanEN` are documented in both V1.0 and V1.1 at `0x1038`/`0x103A`/`0x103C`
  with `1=open, 0=close` boolean semantics. PR #3 removed them on the
  incorrect assumption that they had no Modbus address. Hardware capture
  confirms all three read `1` (matching app `Charge ON / Discharge ON /
  Balance ON`).
- **Packed-bit register**: location now matches V1.1 spec; the `timed_stored_data_switch`
  mask now points at the correct bit (BIT8, not BIT5/Special-Charger).
- **Speculative real-time fields removed**: `charge_status` / `charge_status_time` /
  `heating_active` / `heating_current` had ad-hoc offsets with no spec
  backing. `heating_active` / `heating_current` are re-mapped to the spec'd
  locations (still hidden behind `debug_unverified_fields` because we have
  no hardware sample with the heater running). `charge_status*` removed
  entirely (BLE-only field).
