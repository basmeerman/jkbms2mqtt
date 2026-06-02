# Migration guide

This document tracks the MQTT topic naming convention used by `jkbms2mqtt` and
the explicit write-safety policy. It is also the reference for anyone porting
dashboards or automations from another JK-BMS-to-MQTT solution.

## MQTT topic naming convention

Every published topic is anchored at `<bms_name>/<suffix>`. `<bms_name>` defaults
to `JK_BMS_<n>` where `n` is the BMS's bus address (or its reported unit number
in broadcast mode).

Topic suffixes follow the same English snake / Pascal mix that is widely used
in JK-BMS Home Assistant integrations.

### Live data — read-only sensors

| Topic suffix             | Unit | device_class | Notes |
|---|---|---|---|
| `Total_Voltage_V`        | V    | voltage      | Total pack voltage |
| `Total_Current_A`        | A    | current      | Negative = discharge |
| `Total_Power_W`          | W    | power        | |
| `SOC_percentage`         | %    | battery      | |
| `SOH_percentage`         | %    | —            | |
| `Remaining_Capacity_Ah`  | Ah   | —            | |
| `Battery_Capacity_Ah`    | Ah   | —            | |
| `Cycle_Count`            | —    | —            | |
| `Cycle_Capacity_Ah`      | Ah   | —            | |
| `Balance_current`        | A    | current      | |
| `Balance_Action`         | —    | —            | 0 / 1 |
| `Mos_temp`               | °C   | temperature  | MOSFET temperature |
| `Probe_1_temp` … `Probe_4_temp` | °C | temperature | |
| `cell_voltage_average`   | V    | voltage      | Over populated cells only |
| `cell_voltage_delta`     | V    | voltage      | `max − min` |
| `cell_voltage_max_value` | V    | voltage      | |
| `cell_voltage_min_value` | V    | voltage      | |
| `cell_voltage_max_number` | —   | —            | 1-indexed cell index |
| `cell_voltage_min_number` | —   | —            | |
| `Cell_N_volt` (N=1..16)  | V    | voltage      | Per-cell voltage |
| `Cell_N_ohm` (N=1..16)   | Ω    | —            | Per-cell internal resistance |
| `Total_runtime`          | s    | duration     | Total runtime |
| `charge_status`          | —    | —            | 0 = Bulk, 1 = Float, 2 = Other |
| `charge_status_time`     | s    | duration     | |
| `Heating_Current`        | A    | current      | |
| `Switch_Charge`          | —    | binary_sensor | Reported MOSFET state |
| `Switch_Discharge`       | —    | binary_sensor | |
| `Switch_Balance`         | —    | binary_sensor | |
| `Heating`                | —    | binary_sensor | |

### Static / device info — published once at startup

| Topic suffix          | Notes |
|---|---|
| `bms`                 | BMS model |
| `fw` / `sw`           | Firmware / software version |
| `uptime`              | seconds |
| `power_count`         | Power-on counter |
| `serialnb`            | Serial number |
| `brand`               | Manufacturer brand |
| `manufacturing_date`  | YYMMDD |
| `uart1_protocol_number` / `can_protocol_number` | |
| `lcd_buzzer_trigger` / `_trigger_value` / `_release_value` | |
| `request_charge_voltage_time` / `request_float_voltage_time` | hours |

### Writable settings

Each writable parameter publishes:

- State topic: `<bms_name>/control/<param>/state`
- Command topic: `<bms_name>/control/<param>/set`

The discovery payload carries the appropriate `min` / `max` / `step` bounds.

Writable entities are only registered in HA Discovery when:

1. The active transport / topology pair supports writes (TCP gateway or USB
   serial, in master-poll topology); **and**
2. The corresponding tier toggle is enabled in the add-on options.

## Write tiers

Writing settings back to the BMS is gated by two add-on options, both **off**
by default. Turning them on is an explicit acknowledgement of responsibility
for the cell pack's electrical and thermal safety:

- **`enable_basic_writes`** — routine, low-risk: charge / discharge / balance
  switches, balance trigger voltage, SoC display mapping, request-charge /
  request-float voltages, smart-sleep tuning, LCD / buzzer.
- **`enable_safety_writes`** — protection thresholds: cell OVP / UVP, max
  charge / discharge current, OCP delays, charge / discharge / under-temperature
  / over-temperature recoveries, power-tube OTP, cell count, total battery
  capacity.

In modes where writes are architecturally impossible — broadcast / listen on
RS485, or CAN bus — *no* writable entity is published to HA Discovery, no
matter what the toggles say. Posting to a `/set` topic in those modes
produces a structured error on `<bms_name>/error` and a `WARNING` in the
add-on log; the bytes never touch the bus.

## Behavioural guarantees

- **Frame validation is total.** A malformed reply never crashes the bridge —
  it is logged at DEBUG level and the loop continues.
- **Bus arbitration.** All transactions on a single bus go through a single
  `asyncio.Lock` with a configurable inter-frame gap (default 50 ms). Polls
  and writes cannot interleave on the wire.
- **Cell-count aware.** Statistics (average, min, max, delta) are computed
  over the BMS-reported cell count only — no implicit divide-by-16.
- **Cell delta is `max − min`**, computed from the populated cells.
- **Hardcoded baud rate.** Serial baud is fixed at 115200, not exposed as a
  user-tunable option, to prevent misconfiguration.
- **Read-only modes never advertise writes.** Broadcast and CAN topologies
  omit every `number` / `switch` / `select` HA Discovery payload.

## Manual topic rewrite

If you have retained MQTT messages under a different topic naming convention
(legacy French names like `Tension_Totale_volt`, `Sonde_X_temp`, etc.) and you
want to migrate them so Home Assistant's history stays continuous, the
straightforward approach is a one-shot `mosquitto_pub` rewrite. The bridge
itself does not rewrite retained messages on the broker — it simply starts
publishing under the new names.

```bash
BMS=JK_BMS_1
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
