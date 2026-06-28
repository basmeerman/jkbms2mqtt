# Local test plan — verify the dashboards on Home Assistant

For a 6-pack setup (`bms_ids: 1,2,3,4,5,6`) using the standard
`bms_name_prefix: BMS`, i.e. devices `BMS_1`..`BMS_6` and entities
`<domain>.bms_<n>_device_<object_id>`.

Work top to bottom. **Phase 1 is the gate** — do not import the 2,500-line
dashboard until the entity names verify, or you'll spend the night chasing
`Unavailable` tiles. Fill in the results table at the bottom as you go.

Generate the artifacts first:

```sh
python dashboards/generate.py --bms-ids 1,2,3,4,5,6 --cells 16
```

(adjust `--cells` if any pack isn't 16S, e.g. `--cells 1=16,2=16,3=8,4=8,5=16,6=16`)

Produces in `dashboards/out/`: `jkbms2mqtt-dashboard.yaml`,
`verify-entities.jinja`, and in `dashboards/packages/`: `jkbms_aggregates.yaml`.

---

## Phase 0 — Preconditions

| # | Check | Pass criteria |
|---|---|---|
| 0.1 | Add-on running | jkbms2mqtt add-on log shows polling, no connection errors |
| 0.2 | All 6 packs polled | Log shows reads for slave ids 1–6 (or note which are offline) |
| 0.3 | MQTT discovery done | **Settings → Devices** lists `BMS_1`…`BMS_6` |
| 0.4 | Note write tiers | Record whether `enable_basic_writes` / `enable_safety_writes` are on — it changes what Controls should show |

## Phase 1 — Entity-name verification (the gate)

This confirms HA created the *exact* ids the dashboard expects, before importing.

1. **Developer Tools → Template**.
2. Clear the editor, paste the entire contents of `out/verify-entities.jinja`.
3. Read the rendered report on the right.

**Pass criteria & interpretation:**

| Line | Expected | If not… |
|---|---|---|
| `BMS n read-only: 60/60 present` | 60/60 for every online pack (16S) | A whole pack at `0/60` → that pack is offline or named differently. A **few** missing across *all* packs → an object_id mismatch; report it to me with the `not resolving:` list. |
| `BMS n controls …: 0/34` | `0/34` if write tiers are **off**; `34/34` if both **on** | Matches your Phase 0.4 note. Partial (e.g. `11/34`) means only `enable_basic_writes` is on — expected. |
| `Bank aggregates: 0/4` | `0/4` now (package not installed yet) | Becomes `4/4` after Phase 3. |

> Cell count: a 16S pack contributes 16 `cell_*_volt` + 16 `cell_*_ohm`. For an
> 8S pack regenerate with the right `--cells` or expect `cell_9..16` to miss.
> Temperature probes (`probe_1..5_temp`) are intentionally **not** in this
> check — unwired probes show Unavailable on the dashboard, which is normal.

**🚩 STOP if any pack's read-only count is not full.** Paste the `not resolving:`
list back to me — that's a naming bug to fix in the generator before import.

## Phase 2 — Install HACS frontend cards

**HACS → Frontend**, install each, then **restart HA**:

| Card | Needed by |
|---|---|
| Mushroom | charge/discharge/balance tiles, cell chips, alarm chip |
| bar-card | Overview SoC bar |
| entity-progress-card | detail SoC bar |
| stack-in-card | per-cell voltage / resistance tables |

(history-graph and all other cards are core — nothing to install.)

| # | Check | Pass criteria |
|---|---|---|
| 2.1 | Cards installed | All four show as installed in HACS |
| 2.2 | HA restarted | Frontend reloaded after install |

## Phase 3 — Install the aggregates package

1. In `configuration.yaml` (once): `homeassistant: { packages: !include_dir_named packages }`.
2. Copy `packages/jkbms_aggregates.yaml` to `<config>/packages/`.
3. **Developer Tools → YAML → Check configuration**, then **Restart**.
4. Re-run the Phase 1 template.

| # | Check | Pass criteria |
|---|---|---|
| 3.1 | Config valid | Check configuration = valid |
| 3.2 | Aggregates resolve | Phase-1 report now shows `Bank aggregates: 4/4 present` |
| 3.3 | Values sane | `sensor.jkbms_bank_total_power` ≈ sum of packs; `…_min_soc` = lowest pack SoC; `…_max_temp` = hottest MOS |

## Phase 4 — Import & Overview

1. **Settings → Dashboards → + Add Dashboard → New dashboard from scratch**, save.
2. Open it → ⋮ **Edit** → ⋮ **Raw configuration editor** → paste
   `out/jkbms2mqtt-dashboard.yaml` → **Save**.

| # | Check | Pass criteria |
|---|---|---|
| 4.1 | No YAML error | Editor saves without "Unable to parse" |
| 4.2 | 6 tiles render | Overview shows one tile per online pack |
| 4.3 | SoC bar | bar-card shows colour by level (red<20, orange<50, green) |
| 4.4 | Gauges | Voltage/Power/Current gauges show needles, sane ranges |
| 4.5 | Alarm chip | green "None" when no alarm; cell stats populated |
| 4.6 | Offline pack hidden | If a pack is offline its tile does **not** render (visibility gate) |

## Phase 5 — Per-pack detail subview

Tap a pack heading → its `BMS n` subview opens.

| # | Section | Pass criteria |
|---|---|---|
| 5.1 | Live | SoC progress bar; 3 gauges; **charge/discharge/balance tiles show On/Off in the right colour** (green=on/red=off — this is the binary_sensor fix, verify it's not stuck "Unknown/grey"); balance-current arrow direction matches sign |
| 5.2 | Live temps | MOS + wired probes show °C; unwired probes Unavailable (ok) |
| 5.3 | Cells | voltage table renders; **highest cell blue, lowest red, rest green**; resistance table populated; row count matches cell count |
| 5.4 | Diagnostics | SoH, cycles, runtime, capacities, nameplate (model/hw/sw/serial) all populated |
| 5.5 | Controls | If tiers off: rows Unavailable + the warning note shows. If on: numbers/switches editable |
| 5.6 | History | 3 history-graphs draw lines after a few minutes of data |
| 5.7 | Nav | Each `BMS n` subview is reachable; back returns to Overview; tab bar shows only "Overview" (subviews hidden) |

## Phase 6 — Writes (only if you choose to enable a tier)

⚠️ Optional and operational. Skip unless you want to exercise the controls.

| # | Check | Pass criteria |
|---|---|---|
| 6.1 | Enable basic writes | Set `enable_basic_writes: true`, restart add-on |
| 6.2 | Control appears | A basic `number`/`switch` becomes editable in Controls |
| 6.3 | Round-trip | Nudge a safe value (e.g. balance trigger), confirm it sticks and reads back; revert it |
| 6.4 | Gating | A safety `number` stays Unavailable while `enable_safety_writes` is off |

## Results

| Phase | Result (✅/🚩) | Notes |
|---|---|---|
| 0 Preconditions | | |
| 1 Entity names | | paste `not resolving:` lists here |
| 2 HACS cards | | |
| 3 Aggregates | | |
| 4 Overview | | |
| 5 Detail | | |
| 6 Writes (opt) | | |

Anything 🚩 in phase 1, 4, or 5 → send me the exact text/screenshot and I'll
fix the generator and re-emit.
