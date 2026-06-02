# Migration guide

This document describes the MQTT topic naming convention used by `jkbms2mqtt`
and the write-tier safety policy. It is also the reference for anyone porting
dashboards or automations from another JK-BMS-to-MQTT solution.

## MQTT topic naming convention

Every published topic is anchored at `<bms_name>/<suffix>`. `<bms_name>` is
`<bms_name_prefix>_<slave_id>` — the prefix defaults to `BMS` and the
`slave_id` is the Modbus address (DIP-switch setting) of the BMS. So with
`bms_ids: [1, 2, 3, 4, 5, 6]` and the default prefix you get topics under
`BMS_1/…`, `BMS_2/…`, … `BMS_6/…`. The HA device id for the same BMS is
`BMS_<slave_id>_device`.

Topic suffixes follow the convention widely used in other JK-BMS Home
Assistant integrations.

### Live data — read-only sensors

| Topic suffix | Unit | device_class | Notes |
|---|---|---|---|
| `Total_Voltage_V` | V | voltage | Total pack voltage |
| `Total_Current_A` | A | current | Negative = discharge |
| `Total_Power_W` | W | power | Signed (V × I) |
| `SOC_percentage` | % | battery | |
| `SOH_percentage` | % | — | |
| `Remaining_Capacity_Ah` | Ah | — | |
| `Battery_Capacity_Ah` | Ah | — | Nominal pack capacity |
| `Cycle_Count` | — | — | |
| `Balance_current` | A | current | |
| `Mos_temp` | °C | temperature | MOSFET temperature |
| `Probe_1_temp` … `Probe_5_temp` | °C | temperature | Up to five probes |
| `cell_voltage_average` | V | voltage | Over populated cells only |
| `cell_voltage_delta` | V | voltage | `max − min` |
| `cell_voltage_max_value` / `cell_voltage_min_value` | V | voltage | |
| `cell_voltage_max_number` / `cell_voltage_min_number` | — | — | 1-indexed cell |
| `present_cell_count` | — | — | Cells the BMS reports as present |
| `Cell_N_volt` (N=1..16) | V | voltage | Per-cell voltage |
| `Total_runtime` | s | duration | Total runtime since power-on |
| `Switch_Charge` / `Switch_Discharge` / `Switch_Balance` | — | binary_sensor | Reported MOSFET states |

### Static / device info — published once at startup

| Topic suffix | Notes |
|---|---|
| `bms` | BMS model string |
| `fw` | Hardware version |
| `sw` | Software / firmware version |
| `serialnb` | Serial number |

### Writable settings (only when their tier toggle is on)

Each writable parameter publishes:

- State topic:   `<bms_name>/control/<param>`
- Command topic: `<bms_name>/control/<param>/set`

The discovery payload carries the appropriate `min` / `max` / `step` bounds.
Writable entities are only registered in HA Discovery when the corresponding
tier toggle (`enable_basic_writes` / `enable_safety_writes`) is `true`.

## Write tiers

Writing settings back to the BMS is gated by two add-on options, both **off**
by default. Turning either on is an explicit acknowledgement of responsibility
for the cell pack's electrical and thermal safety:

- **`enable_basic_writes`** — routine, low-risk: charge / discharge / balance
  switches, balance trigger voltage, SoC display mapping, request-charge /
  request-float voltages, smart-sleep tuning.
- **`enable_safety_writes`** — protection thresholds: cell OVP / UVP, max
  charge / discharge current, OCP delays, charge / discharge under-temperature
  / over-temperature thresholds and recoveries, power-tube OTP, cell count.

A rejected write (out-of-range value, BMS Modbus exception, transport failure)
publishes a JSON message on `<bms_name>/error`:

```json
{"param": "max_charge_current", "reason": "value 700.0 outside [0, 600]"}
```

## Behavioural guarantees

- **Standard Modbus framing.** Wire-level framing, CRC, and transaction
  serialisation are handled by `pymodbus`. The reply length is mathematically
  determined by the request, so the byte-stream desync failure mode that
  plagued frame-based JK protocols cannot occur here.
- **Cell-count aware.** Statistics (average, min, max, delta) are computed
  over the cells the BMS reports as present via its cell-present bitmap at
  register `0x1220`.
- **Cell delta is `max − min`**, computed from the populated cells.
- **Hardcoded baud rate.** Serial baud is fixed at 115200, not exposed as a
  user-tunable option, to prevent misconfiguration.
- **Graceful degradation.** Real-time data is read in three Modbus blocks
  (`0x1200/120`, `0x1278/50`, `0x12F0/16`). Failures of the second or third
  block are non-fatal — the bridge fills zeros for the missing fields and
  publishes whatever it has.

## Migrating from the Jean-Luc-style add-on

The topic suffixes (`Total_Voltage_V`, `Cell_N_volt`, `Mos_temp`, …) match
the v4.0.0 English convention used by that add-on. Existing dashboards and
automations keyed on `BMS_<n>/…` keep working as long as:

- You set `bms_name_prefix: BMS` (the default) in this add-on's config.
- The slave IDs in `bms_ids` match the DIP-switch addresses you were already
  using.

Things to set on each BMS via its display or BLE app before this add-on can
talk to it:

- **UART1 protocol** = `001 — JK BMS RS485 Modbus V1.0` (the standard
  protocol). This add-on does not implement the proprietary master-mode
  protocol some older add-ons use.
- **DIP-switch ID** unique per BMS, in `1..15`. Address `0` is master-mode
  and is not used here.

If you have retained MQTT messages under legacy French topic names
(`Tension_Totale_volt`, `Sonde_X_temp`, etc.) and want to preserve HA's history
continuity, the bridge itself does not rewrite retained messages — you can do
it manually with `mosquitto_pub`:

```bash
BMS=BMS_1
declare -A RENAMES=(
  [Tension_Totale_volt]=Total_Voltage_V
  [Courant_total]=Total_Current_A
  [Puissance_Totale]=Total_Power_W
  # …extend as needed…
)
for old in "${!RENAMES[@]}"; do
  new=${RENAMES[$old]}
  value=$(mosquitto_sub -h "$MQTT_HOST" -t "$BMS/$old" -C 1 -W 1 || echo "")
  if [[ -n "$value" ]]; then
    mosquitto_pub -h "$MQTT_HOST" -r -t "$BMS/$new" -m "$value"
  fi
done
```
