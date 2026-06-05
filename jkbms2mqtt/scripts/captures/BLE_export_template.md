# BLE settings export — fill in from JK BMS app

> **How to use**: open the JK app over BLE for the BMS you also dumped from
> the gateway. For each row, paste the value the app shows next to the
> matching field. Leave a row blank if the app doesn't show that field.
> Save the file as `scripts/captures/BMS_<id>_BLE.md` and send it back along
> with the gateway dump.

## Header

| Key | Value |
|---|---|
| BMS ID (DIP-switch / slave addr) |   |
| Captured at (date/time) |   |
| Firmware version (Status > sw) |   |
| Hardware version (Status > fw) |   |
| Serial / Manufacturer string |   |
| Cell type shown in app (LFP / NMC / LTO) |   |

## Status tab (read-only anchors)

Used to cross-check the real-time block. Note signs (`+` for charge,
`−` for discharge on current).

| Field | App value | Notes |
|---|---|---|
| Total voltage | | V |
| Pack current | | A — sign is **important** |
| Battery Power | | W |
| Average cell voltage | | V |
| Cell voltage diff (Δ) | | V |
| Remain capacity | | Ah |
| Battery capacity | | Ah |
| Remain Battery (SoC) | | % |
| SoH | | % (Settings → 数据 if not on Status) |
| MOS temp | | °C |
| Battery T1 | | °C |
| Battery T2 | | °C |
| Battery T3 | | °C (blank if not shown) |
| Battery T4 | | °C |
| Battery T5 | | °C |
| Cycle Count | | |
| Cycle Capacity | | Ah |
| Runtime / TIME header at top | | e.g. `141D13H59M27S` |
| Detail Logs Count | | |
| Time Enter Sleep | | s |
| Time Emerg. | | s |
| Heat Current | | A |
| Heating Status | | ON / OFF |
| Par-Limiter (Disable PCL) | | ON / OFF |
| Charge Status | | Stand-by / Bulk / Absorption / Float |
| Charge Status Time | | s |
| LCD Buzzer Alarm | | ON / OFF |
| DRY1 Alarm | | ON / OFF |
| DRY2 Alarm | | ON / OFF |

### Per-cell voltages and resistances

| Cell # | Voltage (V) | Wire resistance (Ω) |
|---|---|---|
| 1 | | |
| 2 | | |
| 3 | | |
| 4 | | |
| 5 | | |
| 6 | | |
| 7 | | |
| 8 | | |
| 9 | | |
| 10 | | |
| 11 | | |
| 12 | | |
| 13 | | |
| 14 | | |
| 15 | | |
| 16 | | |
| 17 | | |
| 18 | | |
| 19 | | |
| 20 | | |
| 21 | | |
| 22 | | |
| 23 | | |
| 24 | | |
| 25 | | |
| 26 | | |
| 27 | | |
| 28 | | |
| 29 | | |
| 30 | | |
| 31 | | |
| 32 | | |

## Settings tab — basic settings

| Field | App value | Notes |
|---|---|---|
| Cell Count | | |
| Battery Capacity | | Ah |
| Balance Trig. Volt. | | V (cell delta) |
| Calibrating Volt. | | V (pack total) |
| Calibrating Curr. | | A |
| Start Balance Volt. | | V |
| Max Balance Cur. | | A |

## Settings tab — voltages / SoC

| Field | App value | Notes |
|---|---|---|
| Cell OVP | | V (over-voltage protection) |
| Vol. Cell RCV | | V (request charge) |
| SOC-100% Volt. | | V |
| Cell OVPR | | V (over-voltage recovery) |
| Cell UVPR | | V (under-voltage recovery) |
| SOC-0% Volt. | | V |
| Cell UVP | | V (under-voltage protection) |
| Power Off Vol. | | V |
| Vol. Cell RFV | | V (request float) |
| Vol. Smart Sleep | | V |
| Time Smart Sleep | | h |

## Settings tab — currents and timing

| Field | App value | Notes |
|---|---|---|
| Continued Charge Curr. | | A |
| Charge OCP Delay | | s |
| Charge OCPR Time | | s |
| Continued Discharge Curr. | | A |
| Discharge OCP Delay | | s |
| Discharge OCPR Time | | s |
| Discharge OTP | | °C |
| Discharge OTPR | | °C |
| Charge OTP | | °C |
| Charge OTPR | | °C |
| Charge UTPR | | °C |
| Charge UTP | | °C |
| TMP Stop Heating | | °C |
| TMP Start Heating | | °C |
| MOS OTP | | °C |
| MOS OTPR | | °C |
| SCP Delay | | µs |
| SCPR Time | | s |
| Device Addr. | | |
| Data Stored Period | | s |
| RCV Time | | h |
| RFV Time | | h |
| Emerg. Time | | min |

## Settings tab — text fields

| Field | App value |
|---|---|
| User Private Data |  |
| User Data 2 |  |
| UART1 Protocol No. |  |
| UART2 Protocol No. |  |
| CAN Protocol No. |  |
| LCD Buzzer Trigger |  |
| LCD Buzzer Trigger Val |  |
| LCD Buzzer Release Val |  |
| DRY 1 Trigger |  |
| DRY 1 Trigger Val |  |
| DRY 1 Release Val |  |
| DRY 2 Trigger |  |
| DRY 2 Trigger Val |  |
| DRY 2 Release Val |  |

## Control tab (toggles)

| Toggle | State (ON / OFF) |
|---|---|
| Charge | |
| Discharge | |
| Balance | |
| Emergency | |
| Heating | |
| Disable Temp. Sensor | |
| Display Always On | |
| Smart Sleep On | |
| Disable Par-Limiter | |
| Timed Stored Data | |
| Charging Float Mode | |
| DRY ARM Intermittent | |
| Discharge OCP 2 | |
| Discharge OCP 3 | |

## Cells Wire Resistance — *settings* (not the measured ones above)

The app has a `Con. Wire Res. Settings` section under Settings → Advance.
Fill these only if non-zero.

| Cell # | Configured Ω (mΩ shown) |
|---|---|
| 1 | |
| 2 | |
| 3 | |
| 4 | |
| 5 | |
| 6 | |
| 7 | |
| 8 | |
| 9 | |
| 10 | |
| 11 | |
| 12 | |
| 13 | |
| 14 | |
| 15 | |
| 16 | |

(Repeat 17..32 only if your pack uses more than 16 cells.)
