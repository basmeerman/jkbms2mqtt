# Home Assistant dashboards for jkbms2mqtt — plan

Status: in progress. This document is the design of record for the dashboards
generated under `dashboards/`. It is grounded in the bridge's **real, deployed**
entity ids — verified verbatim against a 6-pack install's Developer-Tools
entity dump (BMS_1), cross-checked so every entity the generator references
exists on the running instance.

## Entity-id naming — derived, not hand-maintained

Since 2.2.0 the bridge suggests no entity id. MQTT entities carry
`has_entity_name`, so Home Assistant slugifies the device name plus each
entity's `name`:

```
<domain>.bms_<n>_<slug of the entity name>
```

`sensor.bms_1_total_voltage`, `sensor.bms_1_state_of_charge`,
`sensor.bms_1_cell_1_resistance`, `sensor.bms_1_maximum_charge_current`
(plus `number.bms_1_maximum_charge_current_control` once its write tier is on).

The generator computes the same slugs from the entity table (`SLUG` in
`dashboard.py`), so generator and bridge cannot drift: renaming an entity moves
both. `check_entities.py` reconciles the two at the `(domain, object_id)`
level in every write-tier combination.

Earlier builds *did* override the id — `object_id` until HA 2026.4 removed it,
then `default_entity_id` — so installs made before 2.2.0 carry a mix of
`…_total_pack_voltage` and `…_device_total_voltage` ids. HA never renames an
existing entity, so `scripts/rename_entities.py` migrates them; it matches on
the bridge's `unique_id`, which has not changed.

## Background: why a fresh build, not a port

A widely-used set of JK-BMS dashboards exists at
[`basmeerman/jkbms-rs485-addon-dashboards`](https://github.com/basmeerman/jkbms-rs485-addon-dashboards),
but it targets **jean-luc1203's Node-RED add-on**, whose Home Assistant
entity-id model differs fundamentally from this bridge. Porting it 1:1 fails
because of five independent mismatches:

1. **Entity-id model.** jean-luc yields unit suffixes
   (`sensor.bms_1_total_voltage_v`) and doubled prefixes
   (`sensor.bms_1_bms_1_alarm_list`). jkbms2mqtt's running build yields
   HA-derived names (`sensor.bms_1_total_voltage`) — see the naming section
   above. Different on both sides; a 1:1 port matches neither.
2. **Missing entities.** `*_total_runtime_formatted`, `*_charge_status_text`,
   `*_charge_status_time_formatted`, `*_balance_action`, `*_bms_alarm_list`,
   `*_visual_status`, and most nameplate fields (`brand`, `manufacturing_date`,
   `password1/2`, `uart1_protocol_number`, `uptime`, `lcd_buzzer_*`) are **not
   published** by this bridge.
3. **Domain flip on switch states.** charge/discharge/balance reported states
   are `sensor` with `'0'`/`'1'` in jean-luc; here they are **`binary_sensor`**
   with `'on'`/`'off'` (`entities.py:354-387`). Both domain and the Jinja
   comparison values change.
4. **No master/broadcast topology.** `JKBMS-Dashboard-Master.yaml` and the
   `bms_master_*` family only exist in jean-luc's `bms_broadcasting: true`
   mode. This bridge has one topology, `master_poll` (`config.yaml:33`), and
   never publishes a `bms_master` device. That dashboard is dropped entirely.
5. **No global aggregates.** The bridge publishes **per-pack only**. The old
   `binary_sensor.bms_global_bms_global_alarm` (alarm beacon gate) and any
   bank totals must be synthesised with HA template entities.

What is reused from the old construct: the per-pack `${n}` templating idea, the
Overview-tiles → per-pack-`subview` navigation model (full-V2's pattern, which
keeps the tab bar clean), the max/min/normal cell-voltage colouring table, and
the card set (mushroom, entity-progress, stack-in, button, history-graph, and
the core gauge).

## Tier gating & exclusions

**Tier gating:** writable params are published as `number`/`switch` only when
`enable_basic_writes` / `enable_safety_writes` are on; otherwise as read-only
`sensor`/`binary_sensor`. The generator takes the tiers (`--basic-writes` /
`--safety-writes`; the add-on passes its options) and renders each Controls row
pointing at the `…_control` entity when a tier is on and at the permanent
read-only twin otherwise, so a setting is always visible either way.
On legacy installs the read-only variants use the `device_` id form, like
`last_seen` (verified against a legacy install's BMS_1 entity dump, add-on
2.1.2).

**Unverified/hidden by default:** `heating`, `heating_current`, and the packed
bits `disable_pcl_module_switch`/`smart_sleep_switch`/`timed_stored_data_switch`
only exist with `debug_unverified_fields: true`. Excluded from the dashboard.

## Deliverable: a static-YAML generator (approach B)

```
dashboards/
  PLAN.md                         # this file
  README.md                       # HACS prereqs, beacon image, generator + import
  generate.py                     # programmatic builder → static Lovelace YAML
  packages/
    jkbms_aggregates.yaml         # generated: any-alarm + bank totals (HA package)
  out/
    jkbms2mqtt-dashboard.yaml      # generated: paste into Raw config editor
```

`generate.py` builds Lovelace cards as Python dicts and dumps clean static YAML
(no `decluttering-card` in the output — every pack is materialised). It reads
the real `bms_ids` and a per-pack (or global) cell count, so non-contiguous ids
(`1,3,7`) and mixed pack sizes (8s/16s/24s) are handled. Run:

```sh
python dashboards/generate.py --bms-ids 1,2,3,4,5,6 --cells 16
```

### Output structure

- **Overview view** (`type: sections`): one tile per pack, each in its own
  section gated by `has_value('sensor.bms_<n>_last_seen')`. Tile:
  SoC bar, V/A/W gauges, avg/delta/min cell, MOS temp, cycle count, and an
  alarm chip that turns red when `…_comma_separated_list_of_active_alarms` is non-empty. The tile
  heading taps through to that pack's detail subview.
- **Per-pack detail subview** (`subview: true`, one per id), sections:
  - **Live** — SoC (entity-progress), V/A/W gauges, charge/discharge/balance
    state (mushroom-template, corrected `binary_sensor` + `on/off` Jinja),
    balance current with direction arrow, temps.
  - **Cells** — max/min/normal-coloured voltage table (Jinja) + resistance
    table, sized to the pack's cell count.
  - **Diagnostics** — SoH, cycle count, cycle capacity, runtime,
    present_cell_count, alarm_bits, nameplate.
  - **Controls** — basic + safety settings with a warning note; `number`/`switch`
    rows for a tier that is on, read-only `sensor`/`binary_sensor` rows otherwise.
  - **History** — core `history-graph` for SoC, voltage, power, temps.

### Aggregates package (`packages/jkbms_aggregates.yaml`, generated)

Synthesises what the bridge does not publish:
- `binary_sensor.jkbms_any_alarm` — ON if any pack's `…_comma_separated_list_of_active_alarms` is
  non-empty. Drives the optional alarm beacon (replaces `bms_global_*`).
- `sensor.jkbms_bank_total_power_w`, `…_min_soc`, `…_max_temp` — bank rollups.

Installed under `config/packages/` (separate from the dashboard import).

## Build sequence

1. [x] Plan doc.
2. [x] `generate.py`: entity helpers + Overview + per-pack detail (Live, Cells,
   Diagnostics) + Controls + History; aggregates package emitter.
3. [x] Generate sample output for `1..6`, validate YAML parses.
4. [x] README: HACS cards, generator usage, raw-editor paste, tier-gating caveat.
5. [ ] (Optional follow-up) ApexCharts cell-voltage bar; verify on a live HA.

## Risks / gotchas handled

- `_v`/`_a`/`_w` suffix drop; the three object_id renames
  (`nominal_capacity_ah`, `total_cycle_capacity_ah`, `pack_capacity_setting`).
- binary_sensor domain + `on/off` Jinja for reported switch states.
- Tier-gating domain flip (sensor↔number/switch) in Controls.
- Variable cell count per pack; non-contiguous `bms_ids`.
- Slave ids are 1..15 (Modbus), not 1..16.
- Standalone dashboard: `navigation_path` is view-relative, not an add-on
  ingress URL.
