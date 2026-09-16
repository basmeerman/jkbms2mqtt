#!/usr/bin/env python3
"""Generate a static Home Assistant Lovelace dashboard for the jkbms2mqtt bridge.

Produces a multi-pack Lovelace dashboard + a bank-aggregates package as a pure
function of ``bms_ids`` + each pack's cell count. The whole dashboard is static
YAML (no ``decluttering-card``), so it is fully inspectable.

The add-on imports this module to auto-install the dashboard on startup
(``install_dashboard`` option); the repo-root ``dashboards/generate.py`` is a
thin CLI wrapper for manual / legacy use.

## Entity-id naming

The bridge suggests no entity id. MQTT entities carry ``has_entity_name``,
so Home Assistant derives ``<domain>.<device name>_<entity name>`` by
slugifying the device name (``BMS_<n>``) and the discovery ``name``. Every
id this generator emits is therefore ``<domain>.bms_<n>_<name slug>``,
computed from the entity table — the single source of truth.

An install that registered its entities under an older build keeps those
ids (HA never renames an existing entity); ``scripts/rename_entities.py``
migrates such an install to this scheme.
"""

from __future__ import annotations

import argparse
import re
import unicodedata
from pathlib import Path
from typing import Final

import yaml

from jkbms2mqtt.entities import (
    BRIDGE_SENSORS,
    CELL_STATS_SENSORS,
    FIXED_SENSORS,
    LIVE_BINARY_SENSORS,
    LIVE_SENSORS,
    PACKED_BIT_ENTITIES,
    WRITABLE_ENTITIES,
    expand_cell_entities,
)
from jkbms2mqtt.mqtt import writable_component
from jkbms2mqtt.protocol.jk_modbus import MAX_CELLS
from jkbms2mqtt.protocol.jk_settings import Encoding, WriteTier

# --------------------------------------------------------------------------- #
# Entity-id naming — derived from the entity table.
#
# HA slugifies "<device name> <entity name>" into the entity id, so the id
# of every entity is ``<domain>.bms_<n>_<slug of that entity's name>``.
# --------------------------------------------------------------------------- #


def _ha_slugify(text: str) -> str:
    """Slugify a name the way Home Assistant builds an entity id from it."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", ascii_text.lower()).strip("_")


def _slug_table() -> dict[str, str]:
    """``object_id`` -> entity-id slug, for every entity the bridge publishes."""
    read_only = (
        *LIVE_SENSORS, *LIVE_BINARY_SENSORS, *CELL_STATS_SENSORS,
        *FIXED_SENSORS, *BRIDGE_SENSORS, *expand_cell_entities(MAX_CELLS),
    )
    table = {e.object_id: _ha_slugify(e.description) for e in read_only}
    table.update({w.object_id: _ha_slugify(w.description) for w in WRITABLE_ENTITIES})
    table.update({p.object_id: _ha_slugify(p.bit.description) for p in PACKED_BIT_ENTITIES})
    return table


SLUG: Final[dict[str, str]] = _slug_table()


def ent(domain: str, n: int, key: str) -> str:
    return f"{domain}.bms_{n}_{SLUG[key]}"


def sensor(n: int, key: str) -> str:
    return ent("sensor", n, key)


def binsensor(n: int, key: str) -> str:
    return ent("binary_sensor", n, key)


_WRITABLES = {w.object_id: w for w in WRITABLE_ENTITIES}


def tier_enabled(object_id: str, *, basic_writes: bool, safety_writes: bool) -> bool:
    """Whether the write tier of this setting is on (register table is the truth)."""
    tier = _WRITABLES[object_id].register.tier
    return basic_writes if tier is WriteTier.BASIC else safety_writes


def setting(n: int, object_id: str, *, writable: bool) -> str:
    """Entity id of a writable setting, whose domain follows its write tier.

    Tier on: ``number`` / ``switch``. Tier off: the bridge publishes the same
    object_id read-only as ``sensor`` / ``binary_sensor``.

    The entity name is the same either way, so only the domain changes.
    """
    w = _WRITABLES[object_id]
    domain = writable_component(
        is_bool=w.register.encoding is Encoding.BOOL32, writable=writable
    ).value
    return ent(domain, n, object_id)


# Writable params, in dashboard order. Each renders through ``setting()``, so
# the row is a control when its tier is on and a read-only sensor otherwise.
BASIC_NUMBERS = (
    ("smart_sleep_voltage", "Smart-sleep voltage"),
    ("balance_trigger_voltage", "Balance trigger voltage"),
    ("balance_starting_voltage", "Balance starting voltage"),
    ("max_balance_current", "Max balance current"),
    ("cell_soc100_voltage", "Cell voltage @ 100% SoC"),
    ("cell_soc0_voltage", "Cell voltage @ 0% SoC"),
    ("cell_request_charge_voltage", "Cell request-charge voltage"),
    ("cell_request_float_voltage", "Cell request-float voltage"),
)
BASIC_SWITCHES = (
    ("charging_switch", "Charging switch"),
    ("discharging_switch", "Discharging switch"),
    ("balance_switch", "Balance switch"),
)
SAFETY_NUMBERS = (
    ("cell_voltage_undervoltage_protection", "Cell UVP"),
    ("cell_voltage_undervoltage_recovery", "Cell UVP recovery"),
    ("cell_voltage_overvoltage_protection", "Cell OVP"),
    ("cell_voltage_overvoltage_recovery", "Cell OVP recovery"),
    ("power_off_voltage", "Power-off voltage"),
    ("max_charge_current", "Max charge current"),
    ("charge_overcurrent_protection_delay", "Charge OCP delay"),
    ("charge_overcurrent_protection_recovery_time", "Charge OCP recovery time"),
    ("max_discharge_current", "Max discharge current"),
    ("discharge_overcurrent_protection_delay", "Discharge OCP delay"),
    ("discharge_overcurrent_protection_recovery_time", "Discharge OCP recovery time"),
    ("short_circuit_protection_delay_us", "Short-circuit protection delay (µs)"),
    ("short_circuit_protection_recovery_time", "Short-circuit protection recovery time"),
    ("discharge_overtemperature_protection", "Discharge OTP"),
    ("discharge_overtemperature_protection_recovery", "Discharge OTP recovery"),
    ("charge_overtemperature_protection", "Charge OTP"),
    ("charge_overtemperature_protection_recovery", "Charge OTP recovery"),
    ("charge_undertemperature_protection", "Charge UTP"),
    ("charge_undertemperature_protection_recovery", "Charge UTP recovery"),
    ("power_tube_overtemperature_protection", "MOSFET OTP"),
    ("power_tube_overtemperature_protection_recovery", "MOSFET OTP recovery"),
    ("cell_count", "Cell count"),
    ("pack_capacity_setting", "Pack capacity setting"),
)


# --------------------------------------------------------------------------- #
# YAML emission — clean static output with block scalars for multiline Jinja.
# --------------------------------------------------------------------------- #


class _Block(str):
    """A string that should be emitted as a YAML literal block scalar (``|``)."""


def _block_representer(dumper: yaml.Dumper, data: _Block) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style="|")


yaml.add_representer(_Block, _block_representer)


def dump_yaml(data: object) -> str:
    return yaml.dump(data, sort_keys=False, allow_unicode=True, width=100)


# --------------------------------------------------------------------------- #
# Card builders.
# --------------------------------------------------------------------------- #


def _switch_state_card(n: int, object_id: str, label: str) -> dict:
    """Mushroom tile for a reported binary_sensor MOSFET/balance state.

    Note the corrected model vs the jean-luc dashboards: this is a
    ``binary_sensor`` with ``on``/``off`` states, not a ``sensor`` with
    ``'0'``/``'1'``.
    """
    e = binsensor(n, object_id)
    return {
        "type": "custom:mushroom-template-card",
        "primary": label,
        "secondary": (
            f"{{% if is_state('{e}','on') %}}On"
            f"{{% elif is_state('{e}','off') %}}Off{{% else %}}Unknown{{% endif %}}"
        ),
        "icon": (
            f"{{% if is_state('{e}','on') %}}mdi:toggle-switch"
            f"{{% elif is_state('{e}','off') %}}mdi:toggle-switch-off"
            f"{{% else %}}mdi:help-circle{{% endif %}}"
        ),
        "icon_color": (
            f"{{% if is_state('{e}','on') %}}green"
            f"{{% elif is_state('{e}','off') %}}red{{% else %}}grey{{% endif %}}"
        ),
        "entity": e,
        "tap_action": {"action": "more-info"},
    }


def _balance_current_card(n: int) -> dict:
    e = sensor(n, "balance_current")
    return {
        "type": "custom:mushroom-template-card",
        "primary": "Balance current",
        "secondary": f"{{{{ states('{e}') }}}} A",
        "icon": (
            f"{{% set c = states('{e}') | float(0) %}}"
            f"{{% if c > 0 %}}mdi:arrow-up-bold"
            f"{{% elif c < 0 %}}mdi:arrow-down-bold{{% else %}}mdi:arrow-right-bold{{% endif %}}"
        ),
        "icon_color": (
            f"{{% set c = states('{e}') | float(0) %}}"
            f"{{% if c > 0 %}}green{{% elif c < 0 %}}red{{% else %}}blue{{% endif %}}"
        ),
        "entity": e,
        "tap_action": {"action": "more-info"},
    }


def _alarm_chip(n: int) -> dict:
    """Mushroom tile that goes red with the alarm text when alarms are active."""
    e = sensor(n, "alarms")
    empties = "['', 'unknown', 'unavailable', 'none', 'None']"
    cond = f"states('{e}') in {empties}"
    return {
        "type": "custom:mushroom-template-card",
        "primary": "Alarms",
        "secondary": f"{{% if {cond} %}}None{{% else %}}{{{{ states('{e}') }}}}{{% endif %}}",
        "icon": f"{{% if {cond} %}}mdi:shield-check{{% else %}}mdi:alert{{% endif %}}",
        "icon_color": f"{{% if {cond} %}}green{{% else %}}red{{% endif %}}",
        "entity": e,
        "tap_action": {"action": "more-info"},
    }


# Freshness fade: green while data is fresh, linearly to grey at STALE_AFTER_S.
STALE_AFTER_S = 300
_FRESH_RGB = (0x41, 0xCD, 0x52)  # same green as the SoC bar / cell table
_STALE_RGB = (0x9E, 0x9E, 0x9E)


def _last_seen_card(n: int) -> dict:
    """Mushroom tile: 'Last seen · 12 seconds ago', icon fading green → grey.

    The template references the ``last_seen`` entity, so it re-renders on every
    poll while data flows; ``now()`` re-renders it once a minute after that, so
    a silent pack keeps greying out. mushroom's ``icon_color`` accepts hex.
    """
    e = sensor(n, "last_seen")
    missing = f"states('{e}') in ['', 'unknown', 'unavailable']"
    frac = (
        f"{{% set f = ((now() - as_datetime(states('{e}'))).total_seconds() "
        f"/ {STALE_AFTER_S}) %}}{{% set f = [[f, 0] | max, 1] | min %}}"
    )
    channels = ", ".join(
        f"({a} + ({b} - {a}) * f) | round | int" for a, b in zip(_FRESH_RGB, _STALE_RGB, strict=True)
    )
    grey = "#" + "".join(f"{c:02x}" for c in _STALE_RGB)
    return {
        "type": "custom:mushroom-template-card",
        "primary": "Last seen",
        "secondary": (
            f"{{% if {missing} %}}Never"
            f"{{% else %}}{{{{ relative_time(as_datetime(states('{e}'))) }}}} ago{{% endif %}}"
        ),
        "icon": "mdi:access-point-network",
        "icon_color": (
            f"{{% if {missing} %}}{grey}"
            f"{{% else %}}{frac}{{{{ '#%02x%02x%02x' | format({channels}) }}}}{{% endif %}}"
        ),
        "entity": e,
        "tap_action": {"action": "more-info"},
    }


def _gauge(entity: str, name: str, mn: float, mx: float, severity: dict | None = None) -> dict:
    g = {"type": "gauge", "entity": entity, "name": name, "min": mn, "max": mx, "needle": True}
    if severity:
        g["severity"] = severity
    return g


def _mushroom_entity(entity: str, name: str, icon: str | None = None,
                     color: str | None = None) -> dict:
    card = {"type": "custom:mushroom-entity-card", "entity": entity, "name": name,
            "tap_action": {"action": "more-info"}}
    if icon:
        card["icon"] = icon
    if color:
        card["icon_color"] = color
    return card


def _heading(title: str, icon: str | None = None) -> dict:
    h = {"type": "heading", "heading": title, "heading_style": "title"}
    if icon:
        h["icon"] = icon
    return h


# --- Overview tile --------------------------------------------------------- #


def _summary_tile(primary: str, secondary: str, icon: str, color: str, entity: str) -> dict:
    """A plain sum tile (power / current): static secondary label, coloured icon."""
    return {
        "type": "custom:mushroom-template-card",
        "primary": primary,
        "secondary": secondary,
        "icon": icon,
        "icon_color": color,
        "entity": entity,
        "tap_action": {"action": "more-info"},
    }


def _summary_attr_tile(
    primary: str, label: str, attr_entity: str, *, icon: str, color: str, entity: str
) -> dict:
    """A tile whose secondary shows ``<label> (<reporting BMS>)`` from the ``bms`` attr.

    Uses ``icon_color`` (the real mushroom-template-card property) so the icon is
    actually tinted — ``color`` is silently ignored by mushroom.
    """
    return {
        "type": "custom:mushroom-template-card",
        "primary": primary,
        "secondary": f"{label} ({{{{ state_attr('{attr_entity}', 'bms') }}}})",
        "icon": icon,
        "icon_color": color,
        "entity": entity,
        "tap_action": {"action": "more-info"},
    }


def bank_summary_section() -> dict:
    """Full-width top row: bank power, current, hottest pack, lowest-SoC pack, alarm.

    References the aggregate entities from the package (jkbms_bank_*). Temp / SoC /
    alarm show the reporting pack on the secondary line via the entity's ``bms``
    attribute.
    """
    power = "sensor.jkbms_bank_total_power"
    current = "sensor.jkbms_bank_total_current"
    maxtemp = "sensor.jkbms_bank_max_temp"
    minsoc = "sensor.jkbms_bank_min_soc"
    alarm = "binary_sensor.jkbms_any_alarm"
    row = {
        "type": "grid",
        "columns": 5,
        "square": False,
        "cards": [
            _summary_tile(
                f"{{{{ states('{power}') }}}} W", "Total power",
                "mdi:flash", "amber", power,
            ),
            _summary_tile(
                f"{{{{ states('{current}') }}}} A", "Total current",
                "mdi:current-dc", "light-blue", current,
            ),
            _summary_attr_tile(
                f"{{{{ states('{maxtemp}') }}}} °C", "Max temp", maxtemp,
                icon="mdi:thermometer", color="deep-orange", entity=maxtemp,
            ),
            _summary_attr_tile(
                f"{{{{ states('{minsoc}') }}}} %", "Min SoC", minsoc,
                icon="mdi:battery-low", color="green", entity=minsoc,
            ),
            _summary_attr_tile(
                f"{{% if is_state('{alarm}', 'on') %}}Alarm{{% else %}}OK{{% endif %}}",
                "Alarms", alarm,
                icon=(
                    f"{{% if is_state('{alarm}', 'on') %}}mdi:alert"
                    f"{{% else %}}mdi:shield-check{{% endif %}}"
                ),
                color=f"{{% if is_state('{alarm}', 'on') %}}red{{% else %}}green{{% endif %}}",
                entity=alarm,
            ),
        ],
    }
    return {
        "type": "grid",
        "column_span": 3,
        "cards": [{"type": "heading", "heading": "Bank summary", "heading_style": "title"}, row],
    }


def overview_section(n: int) -> dict:
    """One Overview section: a per-pack tile, gated on the pack being present."""
    heading = {
        "type": "heading",
        "heading": f"BMS {n}",
        "heading_style": "title",
        "icon": "mdi:battery",
        "tap_action": {"action": "navigate", "navigation_path": f"bms-{n}"},
    }
    # Core `gauge`, not the HACS bar-card: that repository was removed from
    # HACS as unmaintained (issue #21), and the dashboard must render on a
    # stock Home Assistant. With `needle: true` HA turns the legacy `severity`
    # map into coloured bands, so the reading keeps its red / yellow / green
    # meaning; the map is sorted by value, so "low is bad" is expressed by
    # giving red the lowest threshold.
    soc_gauge = _gauge(
        sensor(n, "soc_percentage"), "SoC", 0, 100,
        {"red": 0, "yellow": 20, "green": 50},
    )
    gauges = {
        "type": "grid",
        "columns": 3,
        "square": False,
        "cards": [
            _gauge(sensor(n, "total_voltage"), "Voltage", 40, 60),
            _gauge(sensor(n, "total_power"), "Power", -11000, 11000),
            _gauge(sensor(n, "total_current"), "Current", -200, 200),
        ],
    }
    stats = {
        "type": "grid",
        "columns": 2,
        "square": False,
        "cards": [
            _mushroom_entity(sensor(n, "cell_voltage_average"), "Avg cell", color="light-green"),
            _mushroom_entity(sensor(n, "cell_voltage_delta"), "Delta", color="light-blue"),
            _mushroom_entity(sensor(n, "cell_voltage_min_value"), "Min cell"),
            _mushroom_entity(sensor(n, "mos_temp"), "MOS", icon="mdi:thermometer"),
            _mushroom_entity(sensor(n, "cycle_count"), "Cycles", icon="mdi:battery-heart-outline"),
            _alarm_chip(n),
        ],
    }
    tile = {
        "type": "vertical-stack",
        "cards": [heading, _last_seen_card(n), soc_gauge, gauges, stats],
    }
    # Hide the tile only for a pack that has never reported. HA's visibility
    # engine does NOT support a `template` condition (only state/numeric_state/
    # screen/user/location/time/and/or/not) — an unsupported condition is
    # treated as unmet and the section vanishes in view mode while still
    # showing in edit mode. So express "has reported" as the supported state
    # form on the retained last_seen sensor, which ignores bridge availability:
    # a pack that went silent stays on screen, greyed out, instead of vanishing.
    # Multiple visibility conditions are AND-ed.
    present = sensor(n, "last_seen")
    return {
        "type": "grid",
        "cards": [tile],
        "column_span": 1,
        "visibility": [
            {"condition": "state", "entity": present, "state_not": "unavailable"},
            {"condition": "state", "entity": present, "state_not": "unknown"},
        ],
    }


# --- Per-pack detail subview ----------------------------------------------- #


def _live_section(n: int) -> dict:
    cards = [
        _heading("Live", icon="mdi:flash"),
        _last_seen_card(n),
        {
            "type": "entities",
            "entities": [
                {"entity": sensor(n, "last_seen"), "name": "Last successful poll",
                 "format": "datetime"},
            ],
        },
        {
            "type": "custom:entity-progress-card",
            "entity": sensor(n, "soc_percentage"),
            "name": "State of charge",
            "max_value": 100,
        },
        {
            "type": "grid",
            "columns": 3,
            "square": False,
            "cards": [
                _gauge(sensor(n, "total_voltage"), "Voltage", 40, 60),
                _gauge(sensor(n, "total_power"), "Power", -11000, 11000),
                _gauge(sensor(n, "total_current"), "Current", -200, 200),
            ],
        },
        {
            "type": "grid",
            "columns": 2,
            "square": False,
            "cards": [
                _switch_state_card(n, "switch_charge", "Charge"),
                _switch_state_card(n, "switch_discharge", "Discharge"),
                _switch_state_card(n, "switch_balance", "Balance"),
                _balance_current_card(n),
            ],
        },
        {
            "type": "grid",
            "columns": 3,
            "square": False,
            "cards": [
                _mushroom_entity(sensor(n, "mos_temp"), "MOS", icon="mdi:thermometer"),
                _mushroom_entity(sensor(n, "probe_1_temp"), "Probe 1", icon="mdi:thermometer"),
                _mushroom_entity(sensor(n, "probe_2_temp"), "Probe 2", icon="mdi:thermometer"),
                _mushroom_entity(sensor(n, "probe_3_temp"), "Probe 3", icon="mdi:thermometer"),
                _mushroom_entity(sensor(n, "probe_4_temp"), "Probe 4", icon="mdi:thermometer"),
                _mushroom_entity(sensor(n, "probe_5_temp"), "Probe 5", icon="mdi:thermometer"),
            ],
        },
    ]
    return {"type": "grid", "cards": cards}


def _cell_voltage_markdown(n: int, cells: int) -> _Block:
    """Per-cell voltage table; max cell blue, min cell red, others green."""
    maxnum = sensor(n, "cell_voltage_max_number")
    minnum = sensor(n, "cell_voltage_min_number")
    lines = ["| Cell | Voltage | Cell | Voltage |", "|---|---|---|---|"]
    # two cells per row
    for row_start in range(1, cells + 1, 2):
        cellsrow = []
        for k in (row_start, row_start + 1):
            if k > cells:
                cellsrow.extend(["", ""])
                continue
            volt = sensor(n, f"cell_{k}_volt")
            colored = (
                f"{{% if states('{maxnum}') == '{k}' %}}"
                f'<font color="#3090C7">{{{{ states("{volt}") }}}} V</font>'
                f"{{% elif states('{minnum}') == '{k}' %}}"
                f'<font color="#fa4b54">{{{{ states("{volt}") }}}} V</font>'
                f"{{% else %}}"
                f'<font color="#41cd52">{{{{ states("{volt}") }}}} V</font>'
                f"{{% endif %}}"
            )
            cellsrow.extend([f"#{k}", colored])
        lines.append("| " + " | ".join(cellsrow) + " |")
    return _Block("\n".join(lines) + "\n")


def _cell_resistance_markdown(n: int, cells: int) -> _Block:
    lines = ["| Cell | Resistance | Cell | Resistance |", "|---|---|---|---|"]
    for row_start in range(1, cells + 1, 2):
        cellsrow = []
        for k in (row_start, row_start + 1):
            if k > cells:
                cellsrow.extend(["", ""])
                continue
            ohm = sensor(n, f"cell_{k}_ohm")
            cellsrow.extend([f"#{k}", f'{{{{ states("{ohm}") }}}} Ω'])
        lines.append("| " + " | ".join(cellsrow) + " |")
    return _Block("\n".join(lines) + "\n")


def _cells_section(n: int, cells: int) -> dict:
    cards = [
        _heading("Cells", icon="mdi:battery-high"),
        {
            "type": "grid",
            "columns": 3,
            "square": False,
            "cards": [
                _mushroom_entity(sensor(n, "cell_voltage_max_value"), "Max cell"),
                _mushroom_entity(sensor(n, "cell_voltage_min_value"), "Min cell"),
                _mushroom_entity(sensor(n, "cell_voltage_delta"), "Delta"),
            ],
        },
        {
            "type": "custom:stack-in-card",
            "cards": [
                {"type": "markdown", "content": _cell_voltage_markdown(n, cells)},
            ],
        },
        {
            "type": "custom:stack-in-card",
            "cards": [
                {"type": "markdown", "content": _cell_resistance_markdown(n, cells)},
            ],
        },
    ]
    return {"type": "grid", "cards": cards}


def _diagnostics_section(n: int) -> dict:
    cards = [
        _heading("Diagnostics", icon="mdi:stethoscope"),
        {
            "type": "entities",
            "entities": [
                {"entity": sensor(n, "soh_percentage"), "name": "State of health"},
                {"entity": sensor(n, "cycle_count"), "name": "Cycle count"},
                {"entity": sensor(n, "total_cycle_capacity_ah"), "name": "Lifetime throughput"},
                {"entity": sensor(n, "remaining_capacity_ah"), "name": "Remaining capacity"},
                {"entity": sensor(n, "nominal_capacity_ah"), "name": "Nominal capacity"},
                {"entity": sensor(n, "total_runtime"), "name": "Runtime"},
                {"entity": sensor(n, "present_cell_count"), "name": "Cells present"},
                {"entity": sensor(n, "alarm_bits"), "name": "Alarm bitmap (raw)"},
            ],
        },
        {
            "type": "entities",
            "title": "Nameplate",
            "entities": [
                {"entity": sensor(n, "bms_model"), "name": "Model"},
                {"entity": sensor(n, "hw_version"), "name": "Hardware version"},
                {"entity": sensor(n, "sw_version"), "name": "Software version"},
                {"entity": sensor(n, "serial_number"), "name": "Serial number"},
            ],
        },
    ]
    return {"type": "grid", "cards": cards}


def _controls_section(n: int, *, basic_writes: bool, safety_writes: bool) -> dict:
    def rows(items: tuple[tuple[str, str], ...]) -> list[dict]:
        return [
            {
                "entity": setting(
                    n, oid,
                    writable=tier_enabled(
                        oid, basic_writes=basic_writes, safety_writes=safety_writes
                    ),
                ),
                "name": name,
            }
            for oid, name in items
        ]

    def mode(enabled: bool, option: str) -> str:
        return "editable" if enabled else f"read-only (set `{option}: true` to edit)"

    note = _Block(
        "**Settings always show the BMS's current value.** The write tiers decide\n"
        "whether they can be changed from Home Assistant:\n\n"
        f"- Basic settings: {mode(basic_writes, 'enable_basic_writes')}\n"
        f"- Safety thresholds: {mode(safety_writes, 'enable_safety_writes')}\n\n"
        "The add-on rebuilds this dashboard when it restarts; a manually installed\n"
        "copy must be regenerated after changing a tier.\n\n"
        "⚠️ Safety thresholds can damage cells or cause a fire if set wrong.\n"
    )
    cards = [
        _heading("Controls", icon="mdi:tune"),
        {"type": "markdown", "content": note},
        {"type": "entities", "title": "Basic settings",
         "entities": rows(BASIC_SWITCHES + BASIC_NUMBERS)},
        {"type": "entities", "title": "Safety thresholds", "entities": rows(SAFETY_NUMBERS)},
    ]
    return {"type": "grid", "cards": cards}


def _history_section(n: int) -> dict:
    cards = [
        _heading("History", icon="mdi:chart-line"),
        {"type": "history-graph", "hours_to_show": 24, "title": "State of charge",
         "entities": [{"entity": sensor(n, "soc_percentage")}]},
        {"type": "history-graph", "hours_to_show": 24, "title": "Voltage & power",
         "entities": [{"entity": sensor(n, "total_voltage")},
                      {"entity": sensor(n, "total_power")}]},
        {"type": "history-graph", "hours_to_show": 24, "title": "Temperatures",
         "entities": [{"entity": sensor(n, "mos_temp")},
                      {"entity": sensor(n, "probe_1_temp")},
                      {"entity": sensor(n, "probe_2_temp")}]},
    ]
    return {"type": "grid", "cards": cards}


def detail_view(n: int, cells: int, *, basic_writes: bool, safety_writes: bool) -> dict:
    return {
        "title": f"BMS {n}",
        "path": f"bms-{n}",
        "subview": True,
        "type": "sections",
        "max_columns": 3,
        "sections": [
            _live_section(n),
            _cells_section(n, cells),
            _diagnostics_section(n),
            _controls_section(n, basic_writes=basic_writes, safety_writes=safety_writes),
            _history_section(n),
        ],
    }


# --------------------------------------------------------------------------- #
# Aggregates package (synthesises what the bridge does not publish).
# --------------------------------------------------------------------------- #


EMPTIES = "['', 'unknown', 'unavailable', 'none', 'None']"


def _entity_list(ids: list[int], object_id: str) -> str:
    return "[" + ", ".join(f"'{sensor(n, object_id)}'" for n in ids) + "]"


def _numeric_agg(ids: list[int], object_id: str, reducer: str) -> _Block:
    """A bank rollup: collect valid pack states, then min/max/sum over them.

    Uses an explicit accumulation loop rather than a
    ``map('states') | reject('in', …) | map('float')`` filter chain: the
    ``reject('in', …)`` test raises inside HA's Jinja sandbox, which silently
    renders the whole template sensor ``unknown``. The loop form is the same
    one the any-alarm binary_sensor uses, and it works.
    """
    ents = _entity_list(ids, object_id)
    return _Block(
        "{% set ns = namespace(vals=[]) %}\n"
        f"{{% for e in {ents} %}}\n"
        "  {% if states(e) not in ['', 'unknown', 'unavailable'] %}"
        "{% set ns.vals = ns.vals + [states(e) | float(0)] %}{% endif %}\n"
        "{% endfor %}\n"
        f"{{{{ (ns.vals | {reducer}) if ns.vals | length > 0 else 'unknown' }}}}\n"
    )


def _pairs(ids: list[int], object_id: str) -> str:
    """Jinja list of ``[bms_id, entity_id]`` pairs, for argmin/argmax loops."""
    return "[" + ", ".join(f"[{n}, '{sensor(n, object_id)}']" for n in ids) + "]"


def _argext_bms(ids: list[int], object_id: str, mode: str) -> _Block:
    """Which pack holds the extreme value, as ``BMS <n>`` (for a sensor attribute)."""
    cmp = ">" if mode == "max" else "<"
    pairs = _pairs(ids, object_id)
    return _Block(
        "{% set ns = namespace(best=None, bms='unknown') %}\n"
        f"{{% for p in {pairs} %}}\n"
        "  {% set v = states(p[1]) %}\n"
        "  {% if v not in ['', 'unknown', 'unavailable'] %}\n"
        "    {% set f = v | float(0) %}\n"
        f"    {{% if ns.best is none or f {cmp} ns.best %}}"
        "{% set ns.best = f %}{% set ns.bms = 'BMS ' ~ p[0] %}{% endif %}\n"
        "  {% endif %}\n"
        "{% endfor %}\n"
        "{{ ns.bms }}\n"
    )


def _alarming_bms(ids: list[int]) -> _Block:
    """Comma-joined list of packs with active alarms, or ``None`` (attribute)."""
    pairs = _pairs(ids, "alarms")
    return _Block(
        "{% set ns = namespace(items=[]) %}\n"
        f"{{% for p in {pairs} %}}\n"
        f"  {{% if states(p[1]) not in {EMPTIES} %}}"
        "{% set ns.items = ns.items + ['BMS ' ~ p[0]] %}{% endif %}\n"
        "{% endfor %}\n"
        "{{ ns.items | join(', ') if ns.items | length > 0 else 'None' }}\n"
    )


def aggregates_package(ids: list[int]) -> dict:
    alarm_list = _entity_list(ids, "alarms")
    return {
        "template": [
            {
                "binary_sensor": [
                    {
                        "name": "JKBMS Any Alarm",
                        "unique_id": "jkbms_any_alarm",
                        "device_class": "problem",
                        "state": _Block(
                            "{% set ns = namespace(active=false) %}\n"
                            f"{{% for e in {alarm_list} %}}\n"
                            f"  {{% if states(e) not in {EMPTIES} %}}"
                            "{% set ns.active = true %}{% endif %}\n"
                            "{% endfor %}\n"
                            "{{ ns.active }}\n"
                        ),
                        "attributes": {"bms": _alarming_bms(ids)},
                    }
                ],
                "sensor": [
                    {
                        "name": "JKBMS Bank Total Power",
                        "unique_id": "jkbms_bank_total_power_w",
                        "unit_of_measurement": "W",
                        "device_class": "power",
                        "state": _numeric_agg(ids, "total_power", "sum | round(1)"),
                    },
                    {
                        "name": "JKBMS Bank Total Current",
                        "unique_id": "jkbms_bank_total_current_a",
                        "unit_of_measurement": "A",
                        "device_class": "current",
                        "state": _numeric_agg(ids, "total_current", "sum | round(2)"),
                    },
                    {
                        "name": "JKBMS Bank Min SoC",
                        "unique_id": "jkbms_bank_min_soc",
                        "unit_of_measurement": "%",
                        "device_class": "battery",
                        "state": _numeric_agg(ids, "soc_percentage", "min"),
                        "attributes": {"bms": _argext_bms(ids, "soc_percentage", "min")},
                    },
                    {
                        "name": "JKBMS Bank Max Temp",
                        "unique_id": "jkbms_bank_max_temp",
                        "unit_of_measurement": "°C",
                        "device_class": "temperature",
                        "state": _numeric_agg(ids, "mos_temp", "max"),
                        "attributes": {"bms": _argext_bms(ids, "mos_temp", "max")},
                    },
                ],
            }
        ]
    }


# --------------------------------------------------------------------------- #
# Entity-name probe — paste into Developer Tools → Template to confirm the
# bridge created the exact entity ids the dashboard references, BEFORE import.
# --------------------------------------------------------------------------- #

# Read-only sensors that should have a value whenever a pack is online.
CORE_SENSORS = (
    "total_voltage", "total_current", "total_power", "soc_percentage",
    "soh_percentage", "remaining_capacity_ah", "nominal_capacity_ah",
    "cycle_count", "total_cycle_capacity_ah", "total_runtime", "balance_current",
    "mos_temp", "cell_voltage_average", "cell_voltage_delta",
    "cell_voltage_max_value", "cell_voltage_min_value", "cell_voltage_max_number",
    "cell_voltage_min_number", "present_cell_count", "alarms", "alarm_bits",
    "bms_model", "hw_version", "sw_version", "serial_number", "last_seen",
)
CORE_BINARY = ("switch_charge", "switch_discharge", "switch_balance")


def _expected_entities(
    ids: list[int], cells: dict[int, int], *, basic_writes: bool, safety_writes: bool
) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for n in ids:
        core = [sensor(n, o) for o in CORE_SENSORS]
        core += [binsensor(n, o) for o in CORE_BINARY]
        core += [sensor(n, f"cell_{k}_volt") for k in range(1, cells[n] + 1)]
        core += [sensor(n, f"cell_{k}_ohm") for k in range(1, cells[n] + 1)]
        groups[f"BMS {n} read-only"] = core
        groups[f"BMS {n} settings"] = [
            setting(
                n, o,
                writable=tier_enabled(o, basic_writes=basic_writes, safety_writes=safety_writes),
            )
            for o, _ in BASIC_SWITCHES + BASIC_NUMBERS + SAFETY_NUMBERS
        ]
    groups["Bank aggregates (need package)"] = [
        "binary_sensor.jkbms_any_alarm",
        "sensor.jkbms_bank_total_power",
        "sensor.jkbms_bank_min_soc",
        "sensor.jkbms_bank_max_temp",
    ]
    return groups


def verify_template(
    ids: list[int], cells: dict[int, int], *, basic_writes: bool = False, safety_writes: bool = False
) -> str:
    """A self-contained Jinja report for Developer Tools → Template."""
    groups = _expected_entities(
        ids, cells, basic_writes=basic_writes, safety_writes=safety_writes
    )
    lines = ["{%- set groups = {"]
    for name, ents in groups.items():
        joined = ", ".join(f"'{e}'" for e in ents)
        lines.append(f"  '{name}': [{joined}],")
    lines.append("} -%}")
    lines.append("JKBMS dashboard entity check")
    lines.append("============================")
    lines.append("{%- for group, ents in groups.items() %}")
    lines.append("{%- set ns = namespace(missing=[]) %}")
    lines.append("{%- for e in ents %}")
    lines.append("{%-   if states(e) in ['unknown', 'unavailable'] %}")
    lines.append("{%-     set ns.missing = ns.missing + [e] %}")
    lines.append("{%-   endif %}")
    lines.append("{%- endfor %}")
    lines.append("")
    lines.append("{{ group }}: {{ ents | length - ns.missing | length }}/{{ ents | length }} present")
    lines.append("{%- if ns.missing %}")
    lines.append("  not resolving: {{ ns.missing | join(', ') }}")
    lines.append("{%- endif %}")
    lines.append("{%- endfor %}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# CLI.
# --------------------------------------------------------------------------- #


def parse_ids(raw: str) -> list[int]:
    ids = [int(p.strip()) for p in raw.split(",") if p.strip()]
    for i in ids:
        if not 1 <= i <= 15:
            raise SystemExit(f"bms id {i} out of range (1..15)")
    return ids


def parse_cells(raw: str, ids: list[int]) -> dict[int, int]:
    """``--cells 16`` (all) or ``--cells 1=16,3=8`` (per-id, default 16)."""
    if "=" not in raw:
        return {n: int(raw) for n in ids}
    out = {n: 16 for n in ids}
    for part in raw.split(","):
        k, v = part.split("=")
        out[int(k.strip())] = int(v.strip())
    return out


def build_dashboard(
    ids: list[int], cells: dict[int, int], *, basic_writes: bool = False, safety_writes: bool = False
) -> dict:
    """Build the dashboard; the write tiers must match the add-on's options."""
    overview = {
        "title": "Overview",
        "path": "overview",
        "type": "sections",
        "max_columns": 3,
        "sections": [bank_summary_section()] + [overview_section(n) for n in ids],
    }
    views = [overview] + [
        detail_view(n, cells[n], basic_writes=basic_writes, safety_writes=safety_writes)
        for n in ids
    ]
    return {"title": "JK-BMS", "views": views}


def _header(
    ids: list[int], cells: dict[int, int], *, basic_writes: bool, safety_writes: bool
) -> str:
    def onoff(enabled: bool) -> str:
        return "on" if enabled else "off"

    return (
        "# Generated by jkbms2mqtt (dashboard.py) — do not edit by hand.\n"
        f"# bms-ids: {','.join(map(str, ids))}  "
        f"cells: {','.join(f'{n}={cells[n]}' for n in ids)}\n"
        f"# writes: basic={onoff(basic_writes)}  safety={onoff(safety_writes)}\n"
    )


def write_files(
    *,
    ids: list[int],
    cells: dict[int, int],
    dashboard_path: Path,
    package_path: Path,
    verify_path: Path | None = None,
    basic_writes: bool = False,
    safety_writes: bool = False,
) -> None:
    """Render the dashboard + package (+ optional probe) and write them to disk."""
    tiers = {"basic_writes": basic_writes, "safety_writes": safety_writes}
    header = _header(ids, cells, **tiers)
    dashboard_path.parent.mkdir(parents=True, exist_ok=True)
    dashboard_path.write_text(header + dump_yaml(build_dashboard(ids, cells, **tiers)))
    package_path.parent.mkdir(parents=True, exist_ok=True)
    package_path.write_text(header + dump_yaml(aggregates_package(ids)))
    if verify_path is not None:
        verify_path.parent.mkdir(parents=True, exist_ok=True)
        verify_path.write_text(verify_template(ids, cells, **tiers))


def install(
    config_dir: Path,
    ids: list[int],
    cells: dict[int, int],
    *,
    basic_writes: bool,
    safety_writes: bool,
) -> tuple[Path, Path]:
    """Write the auto-install dashboard + package into the HA config dir.

    The write tiers come from the add-on options: tier changes only
    take effect on an add-on restart, which also rewrites this dashboard, so
    its controls / read-only rows always match what the bridge publishes.
    Files land under ``<config>/jkbms2mqtt/`` so the one-time
    ``configuration.yaml`` block can include them. Returns the two paths.
    """
    base = Path(config_dir) / "jkbms2mqtt"
    dashboard_path = base / "dashboard.yaml"
    package_path = base / "packages" / "jkbms_aggregates.yaml"
    write_files(
        ids=ids, cells=cells,
        dashboard_path=dashboard_path, package_path=package_path,
        basic_writes=basic_writes, safety_writes=safety_writes,
    )
    return dashboard_path, package_path


def main(default_dir: Path | None = None) -> int:
    here = default_dir if default_dir is not None else Path.cwd()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bms-ids", default="1,2,3,4,5,6",
                    help="comma-separated slave ids (1..15), e.g. 1,3,7")
    ap.add_argument("--cells", default="16",
                    help="cells per pack: '16' or per-id '1=16,3=8,7=24'")
    ap.add_argument("--basic-writes", action="store_true",
                    help="enable_basic_writes is on: basic settings become controls")
    ap.add_argument("--safety-writes", action="store_true",
                    help="enable_safety_writes is on: safety thresholds become controls")
    ap.add_argument("--out", default=str(here / "out" / "jkbms2mqtt-dashboard.yaml"))
    ap.add_argument("--package-out", default=str(here / "packages" / "jkbms_aggregates.yaml"))
    ap.add_argument("--verify-out", default=str(here / "out" / "verify-entities.jinja"))
    args = ap.parse_args()

    ids = parse_ids(args.bms_ids)
    cells = parse_cells(args.cells, ids)
    write_files(
        ids=ids, cells=cells,
        dashboard_path=Path(args.out), package_path=Path(args.package_out),
        verify_path=Path(args.verify_out),
        basic_writes=args.basic_writes, safety_writes=args.safety_writes,
    )
    print(
        f"wrote {args.out} ({len(ids)} packs, "
        f"basic_writes={args.basic_writes}, safety_writes={args.safety_writes})"
    )
    print(f"wrote {args.package_out}")
    print(f"wrote {args.verify_out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
