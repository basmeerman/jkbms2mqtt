# Field audit against real hardware

Source: `scripts/captures/BMS_1.txt` (JK_PB2A16S20P, fw 15.41) +
BMS app screenshots in repo-root `/docs/*.jpeg`. All hex offsets below are word
offsets from the indicated block base; raw bytes are quoted verbatim from the
dump.

Verdict legend:

- **VERIFIED** — raw bytes at our offset decode to the same value as the BMS app.
- **WRONG** — our offset reads a different value than the app; the field needs a different address.
- **SUSPECT** — our offset reads a value compatible with the app, but the data is all zeros, so we can't actually prove the offset.

---

## Real-time block (`0x1200`)

| Field | Offset | Raw | Decoded | App | Verdict |
|---|---|---|---|---|---|
| `cell_voltages_v[1..16]` | `0x00..0x0F` | 16× `0c6f` | 3.183 V × 16 | 3.183 V × 16 | **VERIFIED** |
| `cell_resistances_ohm[1..16]` | `0x80..0x8F` (ours) | mostly zero + mirror noise | wrong values | 0.062 / 0.066 / 0.076 … | **WRONG** — actual offset is `0x25..0x34` (raw at `0x1225..0x1234` matches the app cell-by-cell) |
| Cell-present bitmap | `0x20..0x21` | `0000 ffff` | 16 cells present | 16 cells | **VERIFIED** |
| `cell_voltage_avg_v` | `0x22` | `0c6f` | 3.183 V | 3.183 V | **VERIFIED** |
| MOS temp | `0x45` | `00e7` | 23.1 °C | 23.1 °C | **VERIFIED** |
| Total voltage | `0x48..0x49` | `0000 c6eb` | 50.923 V | 50.92 V | **VERIFIED** |
| Total current | `0x4C..0x4D` | `0000 0000` | 0.000 A | 0.00 A | **VERIFIED** (zero-data) |
| Probe 1 | `0x4E` | `00d8` | 21.6 °C | T1 21.6 °C | **VERIFIED** |
| Probe 2 | `0x4F` | `00d8` | 21.6 °C | T2 21.5 °C | **VERIFIED** (display lag) |
| Probe 3 | `0x7C` (ours) | `0000` (zero/mirror) | 0.0 °C | (not shown in app) | **WRONG** — actual offset `0xF4` (raw `00e7` = 23.1 °C in block C) |
| Probe 4 | `0x7D` (ours) | `0000` | 0.0 °C | T4 21.6 °C | **WRONG** — actual offset `0xF5` (raw `00d8` = 21.6 °C) |
| Probe 5 | `0x7E` (ours) | `0000` | 0.0 °C | T5 21.8 °C | **WRONG** — actual offset `0xF6` (raw `00da` = 21.8 °C) |
| Alarm bits | `0x50..0x51` | `0000 0000` | 0 | no alarms | **VERIFIED** (zero-data) |
| Balance state \| SoC | `0x53` | `003b` | balance off, SoC 59 % | 59 % | **VERIFIED** |
| Remaining capacity | `0x54..0x55` | `0002 cee9` | 184.04 Ah | 184.0 Ah | **VERIFIED** |
| Nominal capacity | `0x56..0x57` | `0004 ca90` | 314.0 Ah | 314.0 Ah | **VERIFIED** |
| Cycle count | `0x58..0x59` | `0000 0000` | 0 | 0 | **VERIFIED** (zero-data) |
| `total_cycle_capacity_ah` | `0x5A..0x5B` | `0000 6138` | 24.89 Ah | 24.9 Ah | **VERIFIED** ⭐ |
| SoH \| precharge | `0x5C` | `6400` | 100 % | 100 % | **VERIFIED** |
| Runtime | `0x5E..0x5F` | `00ba a7b0` | 12 232 624 s | 141d13h59m27s = 12 232 767 s | **VERIFIED** (143 s drift between capture & screenshot) |
| Charge \| discharge enabled | `0x60` | `0101` | both ON | both ON | **VERIFIED** |
| `heating_current_a` | `0x64` | `0000` | 0.000 A | 0.000 A | **SUSPECT** (only verifiable when heater runs) |
| `heating_active` | `0x65` | `0000` | OFF | OFF | **SUSPECT** |
| `charge_status_id` | `0x6C` | `0000` | id 0 → `standby` | **Bulk** | **WRONG** — actual location not known from this single capture (registers near 0x1268..0x126B show non-zero data that may carry it) |
| `charge_status_time_s` | `0x6D` | `0000` | 0 s | 0 s | **SUSPECT** |

## Static-info block (`0x1400`)

| Field | Offset | Decoded | App | Verdict |
|---|---|---|---|---|
| `model` | `0x00..0x07` | `JK_PB2A16S20P` | JK_PB2A16S20P | **VERIFIED** |
| `hw_version` | `0x08..0x0B` | `15A` | 15A | **VERIFIED** |
| `sw_version` | `0x0C..0x0F` | `15.41` | 15.41 | **VERIFIED** |
| `serial_number` | `0x28..0x2F` | `50314490295000` | (matches `5031449029500` in info hex) | **VERIFIED** |

The ASCII at offset `0x18..0x1B` is `295.` — part of an extended serial string,
**not** a cell-chemistry code. The previous `cell_type` entity (already removed
in PR #2) was decoding this as a u16 `12857` = ASCII `"29"`.

## Settings block (`0x1000`) — the big finding

**All writable register addresses in our table are off.** The actual JK settings
layout is one 4-byte (u32) parameter every 2 register words (`+0x02`); our
table assumes `+0x04` spacing. Combined with the `U32_DECI` encoding we used
for currents (when the BMS actually uses `U32_MILLI`), we are reading and would
write the **wrong register** for almost every entry.

Worked example for the safety-tier `max_charge_current`:

- Our table: address `0x102C`, encoding `U32_DECI`, decoded = `0x0000_0258 / 10 = 60.0 A`.
- App: `Continued Charge Curr = 40.0 A`.
- Real location: `0x1016`, encoding `U32_MILLI`, raw `0x0000_9C40 / 1000 = 40.0 A`. ✓
- Address `0x102C` is actually `Charge OTPR` (charge over-temperature recovery).

A user with `enable_safety_writes: true` who sets `max_charge_current = 100` via
HA would, today, write 1000 to `0x102C` → set Charge OTPR to 100 °C. The pack
would then not recover from charge-over-temperature until cells reach 100 °C.
**This is the most serious finding of the audit.**

Reconstructed register table (every line below matches the BMS app):

| Address | Setting | Encoding | App label | Tier |
|---|---|---|---|---|
| `0x1000` | `smart_sleep_voltage` | `U32_MILLI` | Vol. Smart Sleep | basic |
| `0x1002` | `cell_voltage_undervoltage_protection` | `U32_MILLI` | Cell UVP | safety |
| `0x1004` | `cell_voltage_undervoltage_recovery` | `U32_MILLI` | Cell UVPR | safety |
| `0x1006` | `cell_voltage_overvoltage_protection` | `U32_MILLI` | Cell OVP | safety |
| `0x1008` | `cell_voltage_overvoltage_recovery` | `U32_MILLI` | Cell OVPR | safety |
| `0x100A` | `balance_trigger_voltage` | `U32_MILLI` | Balance Trig. Volt. (= 0.010 V) | basic |
| `0x100C` | `cell_soc100_voltage` | `U32_MILLI` | SOC-100% Volt. | basic |
| `0x100E` | `cell_soc0_voltage` | `U32_MILLI` | SOC-0% Volt. | basic |
| `0x1010` | `cell_request_charge_voltage` | `U32_MILLI` | Vol. Cell RCV | basic |
| `0x1012` | `cell_request_float_voltage` | `U32_MILLI` | Vol. Cell RFV | basic |
| `0x1014` | `power_off_voltage` | `U32_MILLI` | Power Off Vol. | safety |
| `0x1016` | `max_charge_current` | `U32_MILLI` | Continued Charge Curr | safety |
| `0x1018` | `charge_overcurrent_protection_delay` | `U32_RAW` | Charge OCP Delay (s) | safety |
| `0x101A` | `charge_overcurrent_protection_recovery_time` | `U32_RAW` | Charge OCPR Time (s) | safety |
| `0x101C` | `max_discharge_current` | `U32_MILLI` | Continued Discharge Curr | safety |
| `0x101E` | `discharge_overcurrent_protection_delay` | `U32_RAW` | Discharge OCP Delay (s) | safety ⭐ NEW |
| `0x1020` | `discharge_overcurrent_protection_recovery_time` | `U32_RAW` | Discharge OCPR Time (s) | safety ⭐ NEW |
| `0x1022` | `short_circuit_protection_recovery_time` | `U32_RAW` | SCPR Time (s) | safety ⭐ NEW |
| `0x1024` | `max_balance_current` | `U32_MILLI` | Max Balance Cur. | basic |
| `0x1026` | `discharge_overtemperature_protection` | `I32_DECI` | Discharge OTP (°C) | safety |
| `0x1028` | `discharge_overtemperature_protection_recovery` | `I32_DECI` | Discharge OTPR | safety |
| `0x102A` | `charge_overtemperature_protection` | `I32_DECI` | Charge OTP | safety |
| `0x102C` | `charge_overtemperature_protection_recovery` | `I32_DECI` | Charge OTPR | safety |
| `0x102E` | `charge_undertemperature_protection` | `I32_DECI` | Charge UTP | safety |
| `0x1030` | `charge_undertemperature_protection_recovery` | `I32_DECI` | Charge UTPR | safety |
| `0x1032` | `power_tube_overtemperature_protection` | `I32_DECI` | MOS OTP | safety |
| `0x1034` | `power_tube_overtemperature_protection_recovery` | `I32_DECI` | MOS OTPR | safety |
| `0x1036` | `cell_count` | `U32_RAW` | Cell Count | safety |
| `0x103E` | `nominal_capacity_ah` | `U32_MILLI` | Battery Capacity (314.0 Ah → 314000 mAh) | safety ⭐ NEW (was never writable) |
| `0x1040` | `short_circuit_protection_delay_us` | `U32_RAW` | SCP Delay (µs) | safety ⭐ NEW |
| `0x1042` | `balance_starting_voltage` | `U32_MILLI` | Start Balance Volt. | basic |

The four-byte gap before `0x103E` (`0x1038..0x103D` reading `0x0001` three
times) is probably the three single-byte mode flags (Li-ion / LiFePO₄ / LTO
chemistry select), but we can't confidently name them from one capture and
they should not be writable until verified.

## Packed-bit register `0x1114` = `0x3200`

App toggles that are ON: `Charge`, `Discharge`, `Balance`, `Charging Float Mode`,
`Discharge OCP 2`, `Discharge OCP 3`. The current value `0x3200` = `0011 0010 0000 0000` —
bits 9, 12, 13 set.

Our table reads bits 5, 6, 7 (`mask 0x0020 / 0x0040 / 0x0080`) and reports all
three as OFF. The app's `Timed Stored Data` toggle (which our `0x0020` bit was
supposed to be) is genuinely OFF, so we can't disprove our mapping from this
capture — but we can't prove it either. **SUSPECT — disable until verified.**

---

## Summary of action items

1. **Cell resistance offset wrong** → move from `0x80` to `0x25` (block A).
   Verified to the byte with all 16 cells.
2. **Probes 3/4/5 offset wrong** → move from `0x7C/D/E` to `0xF4/F5/F6`
   (block C). Pre-existing bug since the protocol pivot; matches probes T4/T5
   in the app to within display precision.
3. **Every writable setting address wrong** — re-derived above, every line
   matches the app one-to-one. **Until this is fixed, writes must be blocked
   entirely** (or at least the safety tier) — currently they would write to
   the wrong register, with worst-case safety impact.
4. **Current encoding wrong** — `max_charge_current`, `max_discharge_current`,
   `max_balance_current` are `U32_MILLI` (mA) not `U32_DECI` (deci-A).
5. **`charge_status` offset wrong** → unknown actual location. Mark
   speculative and hide from HA by default.
6. **Heating fields** → values match (both zero) but can't prove offset.
   Mark speculative.
7. **Packed-bit positions** → can't prove from this capture. Mark speculative.
8. **New entities to add (verified by app data)**:
   - `discharge_overcurrent_protection_delay` @ `0x101E`
   - `discharge_overcurrent_protection_recovery_time` @ `0x1020`
   - `short_circuit_protection_delay_us` @ `0x1040`
   - `short_circuit_protection_recovery_time` @ `0x1022`
   - `nominal_capacity_ah` writable @ `0x103E`
9. **`cell_resistances_ohm` semantic** — the BMS internally calls these
   *"Cells Wire Resistance"*, which is a connection resistance, **not** an
   internal cell resistance. The current entity name `Cell_<n>_ohm` is OK as
   a unit but the description should clarify.
