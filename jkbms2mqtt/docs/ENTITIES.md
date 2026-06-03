# Entity reference

Every MQTT entity the add-on publishes for each BMS, with its topic, unit,
display precision, and access state.

## Access states

| State | Meaning |
|---|---|
| **Read-only** | Published as an HA sensor / binary sensor. Cannot be changed from HA. |
| **Read / write (basic)** | Always visible as a status sensor. Becomes a writable `number` / `switch` entity when `enable_basic_writes: true`. |
| **Read / write (safety)** | Always visible as a status sensor. Becomes a writable `number` / `switch` entity when `enable_safety_writes: true`. |

A write to a parameter whose tier is disabled is rejected with a structured
error message on `<bms_name>/error`, e.g.:

```json
{"param": "max_charge_current", "reason": "write rejected: enable_safety_writes is off; enable it in the add-on configuration to modify this parameter"}
```

This way the BMS settings are always observable from HA, but mutation is gated
behind an explicit configuration choice — protecting the pack from accidental
or unauthorised changes.

All topics below are relative to `<bms_name>/` (e.g. `BMS_1/Total_Voltage_V`).
For writable entities the same topic carries the current setting value;
commanding a new value posts to `<topic>/set`.

## Read-only telemetry

### Pack-level (live)

| Topic suffix | Object id | Unit | Decimals | Description |
|---|---|---|---|---|
| `Total_Voltage_V` | `total_voltage` | V | 3 | Total pack voltage |
| `Total_Current_A` | `total_current` | A | 3 | Pack current (signed; − = discharge) |
| `Total_Power_W` | `total_power` | W | 1 | Pack power (signed) |
| `SOC_percentage` | `soc_percentage` | % | 0 | State of charge |
| `SOH_percentage` | `soh_percentage` | % | 0 | State of health |
| `Remaining_Capacity_Ah` | `remaining_capacity_ah` | Ah | 2 | Remaining capacity |
| `Battery_Capacity_Ah` | `nominal_capacity_ah` | Ah | 2 | Nominal capacity |
| `Cycle_Count` | `cycle_count` | — | 0 | Charge-cycle count |
| `Cycle_Capacity_Ah` | `total_cycle_capacity_ah` | Ah | 2 | Lifetime accumulated charge throughput |
| `Total_runtime` | `total_runtime` | s | 0 | Time since BMS power-on |
| `Balance_current` | `balance_current` | A | 3 | Cell-balance current |
| `Mos_temp` | `mos_temp` | °C | 1 | MOSFET temperature |
| `Probe_1_temp` … `Probe_5_temp` | `probe_1_temp` … `probe_5_temp` | °C | 1 | External probe temperatures |
| `Heating_Current` | `heating_current` | A | 3 | Heating-element current (PB-series only) |
| `charge_status` | `charge_status` | — | — | Charge FSM state (`standby` / `bulk` / `absorption` / `float`) |
| `charge_status_time` | `charge_status_time` | s | 0 | Seconds spent in current charge FSM state |
| `alarm_bits` | `alarm_bits` | — | 0 | Raw 32-bit alarm bitmap |
| `alarms` | `alarms` | — | — | Active alarms, comma-separated |

### Pack-level (binary)

| Topic suffix | Object id | Description |
|---|---|---|
| `Switch_Charge` | `switch_charge` | Charge MOSFET reported state |
| `Switch_Discharge` | `switch_discharge` | Discharge MOSFET reported state |
| `Switch_Balance` | `switch_balance` | Balance reported state |
| `Heating` | `heating` | Heating-element on/off (PB-series only) |

### Cell statistics

| Topic suffix | Object id | Unit | Decimals | Description |
|---|---|---|---|---|
| `cell_voltage_average` | `cell_voltage_average` | V | 3 | Average cell voltage (populated cells) |
| `cell_voltage_delta` | `cell_voltage_delta` | V | 3 | Max − min cell voltage |
| `cell_voltage_max_value` | `cell_voltage_max_value` | V | 3 | Highest cell voltage |
| `cell_voltage_min_value` | `cell_voltage_min_value` | V | 3 | Lowest cell voltage |
| `cell_voltage_max_number` | `cell_voltage_max_number` | — | 0 | 1-indexed cell with highest voltage |
| `cell_voltage_min_number` | `cell_voltage_min_number` | — | 0 | 1-indexed cell with lowest voltage |
| `present_cell_count` | `present_cell_count` | — | 0 | Number of cells the BMS reports as present |

### Per cell (1..N, where N = `present_cell_count`)

| Topic suffix | Object id | Unit | Decimals | Description |
|---|---|---|---|---|
| `Cell_<n>_volt` | `cell_<n>_volt` | V | 3 | Cell `n` voltage |
| `Cell_<n>_ohm` | `cell_<n>_ohm` | Ω | 3 | Cell `n` internal resistance |

### Static / nameplate

| Topic suffix | Object id | Description |
|---|---|---|
| `bms` | `bms_model` | BMS model identifier |
| `fw` | `hw_version` | Hardware version |
| `sw` | `sw_version` | Software / firmware version |
| `serialnb` | `serial_number` | Serial number |
| `Cell_Type` | `cell_type` | Cell chemistry (`LFP` / `NMC` / `LTO` / `id-N`) |

## Read / write — basic tier

Visible as `number` / `switch` when `enable_basic_writes: true`, otherwise as
`sensor` / `binary_sensor` showing the current BMS value.

| Topic suffix | Object id | Unit | Description |
|---|---|---|---|
| `control/charging_switch` | `charging_switch` | — | Enable / disable the charge MOSFET |
| `control/discharging_switch` | `discharging_switch` | — | Enable / disable the discharge MOSFET |
| `control/balance_switch` | `balance_switch` | — | Enable / disable active cell balancing |
| `control/balance_trigger_voltage` | `balance_trigger_voltage` | V | Cell-delta voltage that triggers balancing |
| `control/balance_starting_voltage` | `balance_starting_voltage` | V | Minimum cell voltage before balancing engages |
| `control/max_balance_current` | `max_balance_current` | A | Maximum balance current (hardware-capped at 10 A) |
| `control/cell_soc100_voltage` | `cell_soc100_voltage` | V | Cell voltage representing 100 % SoC (display only) |
| `control/cell_soc0_voltage` | `cell_soc0_voltage` | V | Cell voltage representing 0 % SoC (display only) |
| `control/cell_request_charge_voltage` | `cell_request_charge_voltage` | V | Cell voltage the BMS requests from the charger |
| `control/cell_request_float_voltage` | `cell_request_float_voltage` | V | Cell float voltage the BMS requests from the charger |
| `control/smart_sleep_voltage` | `smart_sleep_voltage` | V | Cell voltage below which the BMS enters smart sleep |
| `control/disable_pcl_module_switch` | `disable_pcl_module_switch` | — | Disable the pre-charge limit module |
| `control/smart_sleep_switch` | `smart_sleep_switch` | — | Enable smart-sleep behaviour |
| `control/timed_stored_data_switch` | `timed_stored_data_switch` | — | Enable periodic data storage in BMS RAM |

## Read / write — safety tier

Visible as `number` / `switch` when `enable_safety_writes: true`, otherwise as
`sensor` / `binary_sensor` showing the current BMS value.

> **Warning** — wrong values here can damage cells, cause overcurrent, or pose
> a fire risk. Do not enable this tier unless you understand the consequences.

| Topic suffix | Object id | Unit | Description |
|---|---|---|---|
| `control/cell_voltage_undervoltage_protection` | `cell_voltage_undervoltage_protection` | V | Under-voltage protection threshold |
| `control/cell_voltage_undervoltage_recovery` | `cell_voltage_undervoltage_recovery` | V | UVP recovery threshold |
| `control/cell_voltage_overvoltage_protection` | `cell_voltage_overvoltage_protection` | V | Over-voltage protection threshold |
| `control/cell_voltage_overvoltage_recovery` | `cell_voltage_overvoltage_recovery` | V | OVP recovery threshold |
| `control/power_off_voltage` | `power_off_voltage` | V | Cell voltage at which the BMS powers off |
| `control/max_charge_current` | `max_charge_current` | A | Maximum charge current |
| `control/charge_overcurrent_protection_delay` | `charge_overcurrent_protection_delay` | s | Charge OCP trip delay |
| `control/charge_overcurrent_protection_recovery_time` | `charge_overcurrent_protection_recovery_time` | s | Charge OCP recovery time |
| `control/max_discharge_current` | `max_discharge_current` | A | Maximum discharge current |
| `control/charge_overtemperature_protection` | `charge_overtemperature_protection` | °C | Charge OTP threshold |
| `control/charge_overtemperature_protection_recovery` | `charge_overtemperature_protection_recovery` | °C | Charge OTP recovery |
| `control/discharge_overtemperature_protection` | `discharge_overtemperature_protection` | °C | Discharge OTP threshold |
| `control/discharge_overtemperature_protection_recovery` | `discharge_overtemperature_protection_recovery` | °C | Discharge OTP recovery |
| `control/charge_undertemperature_protection` | `charge_undertemperature_protection` | °C | Charge UTP threshold (lithium-plating risk) |
| `control/charge_undertemperature_protection_recovery` | `charge_undertemperature_protection_recovery` | °C | Charge UTP recovery |
| `control/power_tube_overtemperature_protection` | `power_tube_overtemperature_protection` | °C | MOSFET OTP threshold |
| `control/power_tube_overtemperature_protection_recovery` | `power_tube_overtemperature_protection_recovery` | °C | MOSFET OTP recovery |
| `control/cell_count` | `cell_count` | — | Number of cells in the pack |

## Service topics

| Topic | Direction | Description |
|---|---|---|
| `<bms_name>/error` | Bridge → MQTT | Structured JSON error for rejected writes |
| `jkbms2mqtt/availability` | Bridge → MQTT | LWT `online` / `offline` for the bridge process |

## Fields surveyed but **not** exposed

These fields appear in some other JK-BMS bridges but cannot be read through
the standard JK RS485 Modbus V1.0 / V1.1 register map. They are only available
on the BLE / UART-TTL protocol variants and so cannot be added to this bridge:

- `emergency_time_countdown` — BLE-only state machine
- `smart_sleep_countdown` — BLE-only
- `balancer_status_bitmask` — fine-grained bitmask only on BLE (a single
  `Switch_Balance` boolean + `Balance_current` are provided)
- `can_protocols_enabled_bitmask` — BLE-only readback of an inverter-protocol
  configuration

`online_status` is implicit: the bridge does not publish state messages while
the BMS is unreachable, and HA shows the entity as unavailable until a fresh
update arrives.
