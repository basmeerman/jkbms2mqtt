# Home Assistant dashboards for jkbms2mqtt

Generated, static Lovelace dashboards for the packs published by this add-on.
One **Overview** of every pack plus a per-pack **detail subview** (Live, Cells,
Diagnostics, Controls, History).

These target **this bridge's** real entity ids (`<domain>.bms_<n>_<slug>`,
e.g. `sensor.bms_1_total_pack_voltage`), verified against a live install. They
are *not* compatible with other JK-BMS add-ons that name entities differently.
See [`PLAN.md`](PLAN.md) for the design and the verified naming map.

## 1. Generate the YAML

The dashboard is generated from your actual `bms_ids` so non-contiguous ids and
mixed pack sizes are handled.

```sh
# all packs 16s
python dashboards/generate.py --bms-ids 1,2,3,4,5,6 --cells 16

# non-contiguous ids, different cell counts per pack
python dashboards/generate.py --bms-ids 1,3,7 --cells 1=16,3=8,7=24
```

Requires Python 3 + PyYAML (`pip install pyyaml`) — a generator-time tool only,
nothing extra runs in the add-on. Outputs:

- `out/jkbms2mqtt-dashboard.yaml` — paste into the dashboard Raw config editor.
- `packages/jkbms_aggregates.yaml` — an HA *package* (see step 4).
- `out/verify-entities.jinja` — paste into **Developer Tools → Template** to
  confirm the bridge created the exact entity ids this dashboard expects,
  **before** importing. See [`TESTPLAN.md`](TESTPLAN.md) for the full
  verification procedure (start here).

## 2. Install the HACS frontend cards

The dashboard uses these custom cards — install each via [HACS](https://hacs.xyz/)
→ Frontend, then **restart Home Assistant**:

| Card | Used for |
|---|---|
| **Mushroom** | Charge / discharge / balance state tiles, cell stats, alarm chip |
| **bar-card** | SoC bar on the Overview tiles |
| **entity-progress-card** | SoC progress bar on the detail Live section |
| **stack-in-card** | Per-cell voltage / resistance tables |

History uses the built-in `history-graph` card (no HACS needed). Gauges,
`entities`, `grid`, `heading`, `markdown`, `sections` are all core.

## 3. Import the dashboard

1. **Settings → Dashboards → + Add Dashboard → New dashboard from scratch**, save.
2. Open it, top-right ⋮ → **Edit dashboard** → ⋮ → **Raw configuration editor**.
3. Replace the contents with `out/jkbms2mqtt-dashboard.yaml`. Save.

Overview tiles only render for packs that are actually publishing
(`has_value(sensor.bms_<n>_total_pack_voltage)`), so missing ids stay hidden.
Tap a pack's heading to open its detail subview.

## 4. Install the aggregates package (optional but recommended)

The bridge publishes **per-pack only** — there is no bank-wide alarm or total.
`packages/jkbms_aggregates.yaml` adds them as HA template entities:

- `binary_sensor.jkbms_any_alarm` — ON if any pack has active alarms.
- `sensor.jkbms_bank_total_power`, `…_bank_min_soc`, `…_bank_max_temp`.

Enable packages once in `configuration.yaml`:

```yaml
homeassistant:
  packages: !include_dir_named packages
```

Copy `jkbms_aggregates.yaml` into `<config>/packages/` and restart HA.

## Notes & caveats

- **Controls (writes) are tier-gated.** The Controls section references
  `number.*` / `switch.*` entities that the bridge only publishes when
  `enable_basic_writes` / `enable_safety_writes` are on. With a tier off, those
  rows show *Unavailable* — the current value is still readable as a sensor on
  the device page. ⚠️ Safety thresholds can damage cells if set wrong.
- **Re-generate after changing `bms_ids` or cell counts** and re-paste.
- **Five temperature probes** are shown (`probe_1..5`); unused probes show
  Unavailable. Unverified fields (`heating`, packed-bit toggles) are excluded.
