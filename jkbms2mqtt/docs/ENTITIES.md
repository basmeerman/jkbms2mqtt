# Entity reference

Every MQTT entity the add-on publishes for each BMS, with its topic, unit,
display precision, access state, and the **HA device-page section** it lands
in. Every register offset and encoding is calibrated against
[`specifications/BMS.RS485.Modbus.V1.1.pdf`](specifications/BMS.RS485.Modbus.V1.1.pdf)
(see [`specifications/README.md`](specifications/README.md)); deviations are
documented in [`FIELD_AUDIT.md`](FIELD_AUDIT.md).

## HA device-page sections

Each entity is published with an `entity_category` so the Home Assistant
device page sorts it into one of four sections:

| Section | `entity_category` | Used for |
|---|---|---|
| **Sensors** | (none) | Primary read-only telemetry — the values you check on the device page every day |
| **Controls** | (none) | A writable entity with no category. Currently empty: the charging / discharging / balance switches are spec-defined configuration parameters (they tune device behaviour rather than being the pack's main power switch), so they land under **Configuration** instead. |
| **Configuration** | `config` | Every writable setting + every packed-bit mode toggle; settable thresholds, current limits, OTP / UTP thresholds, etc. When the tier is off the entity is published read-only — but still categorised as Configuration. |
| **Diagnostics** | `diagnostic` | Read-only debug / lifetime / static info: model, hw, sw, serial number, cycle count, cycle capacity, runtime, SoH, raw alarm bitmap, present cell count, per-cell internal resistances |

See HA's own definition at
[developers.home-assistant.io/docs/core/entity/#categorizing-entities](https://developers.home-assistant.io/docs/core/entity/#categorizing-entities).

## Access states

| State | Meaning |
|---|---|
| **Read-only** | Published as an HA sensor / binary sensor. Cannot be changed from HA. |
| **Read / write (basic)** | Always visible as a status sensor. Becomes a writable `number` / `switch` entity when `enable_basic_writes: true`. |
| **Read / write (safety)** | Always visible as a status sensor. Becomes a writable `number` / `switch` entity when `enable_safety_writes: true`. |
| **Unverified** | Modbus register offset has not been confirmed against real hardware. Hidden by default; set `debug_unverified_fields: true` to surface them for experimentation. Marked ⚠ below. |

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

| Topic suffix | Object id | Unit | Decimals | HA section | Description |
|---|---|---|---|---|---|
| `Total_Voltage_V` | `total_voltage` | V | 3 | Sensors | Total pack voltage |
| `Total_Current_A` | `total_current` | A | 3 | Sensors | Pack current (signed; − = discharge) |
| `Total_Power_W` | `total_power` | W | 1 | Sensors | Pack power (signed) |
| `SOC_percentage` | `soc_percentage` | % | 0 | Sensors | State of charge |
| `SOH_percentage` | `soh_percentage` | % | 0 | **Diagnostics** | State of health |
| `Remaining_Capacity_Ah` | `remaining_capacity_ah` | Ah | 2 | Sensors | Remaining capacity |
| `Battery_Capacity_Ah` | `nominal_capacity_ah` | Ah | 2 | Sensors | Nominal capacity |
| `Cycle_Count` | `cycle_count` | — | 0 | **Diagnostics** | Charge-cycle count |
| `Cycle_Capacity_Ah` | `total_cycle_capacity_ah` | Ah | 2 | **Diagnostics** | Lifetime accumulated charge throughput |
| `Total_runtime` | `total_runtime` | s | 0 | **Diagnostics** | Time since BMS power-on |
| `Balance_current` | `balance_current` | A | 3 | Sensors | Cell-balance current |
| `Mos_temp` | `mos_temp` | °C | 1 | Sensors | MOSFET temperature |
| `Probe_1_temp` … `Probe_5_temp` | `probe_1_temp` … `probe_5_temp` | °C | 1 | Sensors | External probe temperatures |
| `Heating_Current` ⚠ | `heating_current` | A | 3 | **Diagnostics** | Heating-element current — unverified |
| `alarm_bits` | `alarm_bits` | — | 0 | **Diagnostics** | Raw 32-bit alarm bitmap |
| `alarms` | `alarms` | — | — | Sensors | Active alarms, comma-separated |

### Pack-level (binary)

| Topic suffix | Object id | HA section | Description |
|---|---|---|---|
| `Switch_Charge` | `switch_charge` | Sensors | Charge MOSFET reported state |
| `Switch_Discharge` | `switch_discharge` | Sensors | Discharge MOSFET reported state |
| `Switch_Balance` | `switch_balance` | Sensors | Balance reported state |
| `Heating` ⚠ | `heating` | Sensors | Heating-element on/off (PB-series only) — unverified |

### Cell statistics

| Topic suffix | Object id | Unit | Decimals | HA section | Description |
|---|---|---|---|---|---|
| `cell_voltage_average` | `cell_voltage_average` | V | 3 | Sensors | Average cell voltage (populated cells) |
| `cell_voltage_delta` | `cell_voltage_delta` | V | 3 | Sensors | Max − min cell voltage |
| `cell_voltage_max_value` | `cell_voltage_max_value` | V | 3 | Sensors | Highest cell voltage |
| `cell_voltage_min_value` | `cell_voltage_min_value` | V | 3 | Sensors | Lowest cell voltage |
| `cell_voltage_max_number` | `cell_voltage_max_number` | — | 0 | Sensors | 1-indexed cell with highest voltage |
| `cell_voltage_min_number` | `cell_voltage_min_number` | — | 0 | Sensors | 1-indexed cell with lowest voltage |
| `present_cell_count` | `present_cell_count` | — | 0 | **Diagnostics** | Number of cells the BMS reports as present |

### Per cell (1..N, where N = `present_cell_count`)

| Topic suffix | Object id | Unit | Decimals | HA section | Description |
|---|---|---|---|---|---|
| `Cell_<n>_volt` | `cell_<n>_volt` | V | 3 | Sensors | Cell `n` voltage |
| `Cell_<n>_ohm` | `cell_<n>_ohm` | Ω | 3 | **Diagnostics** | Cell `n` internal resistance |

### Static / nameplate

| Topic suffix | Object id | HA section | Description |
|---|---|---|---|
| `bms` | `bms_model` | **Diagnostics** | BMS model identifier |
| `fw` | `hw_version` | **Diagnostics** | Hardware version |
| `sw` | `sw_version` | **Diagnostics** | Software / firmware version |
| `serialnb` | `serial_number` | **Diagnostics** | Serial number |

## Read / write — basic tier

All entries land in HA's **Configuration** section (`entity_category: config`).
All addresses calibrated against spec V1.1 and verified against
`scripts/captures/BMS_1.txt`. Visible as `number` / `switch` when
`enable_basic_writes: true`, as `sensor` / `binary_sensor` otherwise (current
value still shown). The three primary on/off controls
(`charging_switch` / `discharging_switch` / `balance_switch`) are also
configuration-tier per the spec — they tune device behaviour rather than
being the device's main on/off switch.

| Topic suffix | Object id | Unit | Description |
|---|---|---|---|
| `control/smart_sleep_voltage` | `smart_sleep_voltage` | V | Cell voltage below which the BMS enters smart sleep |
| `control/balance_trigger_voltage` | `balance_trigger_voltage` | V | Cell-delta voltage that triggers balancing |
| `control/cell_soc100_voltage` | `cell_soc100_voltage` | V | Cell voltage representing 100 % SoC |
| `control/cell_soc0_voltage` | `cell_soc0_voltage` | V | Cell voltage representing 0 % SoC |
| `control/cell_request_charge_voltage` | `cell_request_charge_voltage` | V | Cell voltage requested from charger |
| `control/cell_request_float_voltage` | `cell_request_float_voltage` | V | Cell float voltage requested |
| `control/max_balance_current` | `max_balance_current` | A | Maximum balance current (hardware-capped at 10 A) |
| `control/charging_switch` | `charging_switch` | — | Enable / disable the charge MOSFET (spec byte 0x70 → reg 0x1038) |
| `control/discharging_switch` | `discharging_switch` | — | Enable / disable the discharge MOSFET (spec byte 0x74 → reg 0x103A) |
| `control/balance_switch` | `balance_switch` | — | Enable / disable active cell balancing (spec byte 0x78 → reg 0x103C) |
| `control/balance_starting_voltage` | `balance_starting_voltage` | V | Minimum cell voltage before balancing engages |

### Unverified packed-bit toggles (basic, hidden by default)

All in HA's **Configuration** section (`entity_category: config`). The
packed-bit register at `0x1114` holds several boolean flags but the bit
positions are not yet confirmed. Marked `verified=False`; visible only when
`debug_unverified_fields: true`.

| Topic suffix | Object id | Description |
|---|---|---|
| `control/disable_pcl_module_switch` ⚠ | `disable_pcl_module_switch` | Disable pre-charge limit module — unverified |
| `control/smart_sleep_switch` ⚠ | `smart_sleep_switch` | Enable smart-sleep behaviour — unverified |
| `control/timed_stored_data_switch` ⚠ | `timed_stored_data_switch` | Enable periodic data storage in BMS RAM — unverified |

## Read / write — safety tier

All entries land in HA's **Configuration** section (`entity_category: config`).
All addresses and encodings verified against `scripts/captures/BMS_1.txt` and
the BMS app screenshots. Visible as `number` when `enable_safety_writes: true`,
as `sensor` otherwise.

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
| `control/discharge_overcurrent_protection_delay` | `discharge_overcurrent_protection_delay` | s | Discharge OCP trip delay |
| `control/discharge_overcurrent_protection_recovery_time` | `discharge_overcurrent_protection_recovery_time` | s | Discharge OCP recovery time |
| `control/short_circuit_protection_recovery_time` | `short_circuit_protection_recovery_time` | s | SCP recovery time |
| `control/discharge_overtemperature_protection` | `discharge_overtemperature_protection` | °C | Discharge OTP threshold |
| `control/discharge_overtemperature_protection_recovery` | `discharge_overtemperature_protection_recovery` | °C | Discharge OTP recovery |
| `control/charge_overtemperature_protection` | `charge_overtemperature_protection` | °C | Charge OTP threshold |
| `control/charge_overtemperature_protection_recovery` | `charge_overtemperature_protection_recovery` | °C | Charge OTP recovery |
| `control/charge_undertemperature_protection` | `charge_undertemperature_protection` | °C | Charge UTP threshold (lithium-plating risk) |
| `control/charge_undertemperature_protection_recovery` | `charge_undertemperature_protection_recovery` | °C | Charge UTP recovery |
| `control/power_tube_overtemperature_protection` | `power_tube_overtemperature_protection` | °C | MOSFET OTP threshold |
| `control/power_tube_overtemperature_protection_recovery` | `power_tube_overtemperature_protection_recovery` | °C | MOSFET OTP recovery |
| `control/cell_count` | `cell_count` | — | Number of cells in the pack |
| `control/pack_capacity_setting` | `pack_capacity_setting` | Ah | Configured pack capacity (drives SoC scaling) |
| `control/short_circuit_protection_delay_us` | `short_circuit_protection_delay_us` | µs | Short-circuit protection trip delay |

## Service topics

| Topic | Direction | Description |
|---|---|---|
| `<bms_name>/error` | Bridge → MQTT | Structured JSON error for rejected writes |
| `jkbms2mqtt/availability` | Bridge → MQTT | LWT `online` / `offline` for the bridge process |

## Fields surveyed but **not** exposed

These fields appear in some other JK-BMS bridges but cannot be read through
the standard JK RS485 Modbus V1.0 / V1.1 register map (verified against
[`specifications/`](specifications/)). They are only available on the BLE /
UART-TTL protocol variants and so cannot be added to this bridge:

- `charge_status` / `charge_status_time` — the "Bulk / Float" FSM state the
  BMS app shows; not in V1.1 Modbus register map at any address.

- `emergency_time_countdown` — BLE-only state machine
- `smart_sleep_countdown` — BLE-only
- `balancer_status_bitmask` — fine-grained bitmask only on BLE (a single
  `Switch_Balance` boolean + `Balance_current` are provided)
- `can_protocols_enabled_bitmask` — BLE-only readback of an inverter-protocol
  configuration

`cell_type` (cell chemistry — LFP / NMC / LTO) is reported via the BLE
protocol but does not appear to have a stable Modbus register address across
firmware variants. Pending a verified register location it is not exposed.

`online_status` is implicit: the bridge does not publish state messages while
the BMS is unreachable, and HA shows the entity as unavailable until a fresh
update arrives.
